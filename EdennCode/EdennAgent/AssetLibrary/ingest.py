"""Ingestion: a file becomes an asset row plus L0/L1/L2 annotations.

Nothing here analyzes anything new — it persists what the recompose stack
already computes (BACKEND_GAPS §0), through composable ``Producer`` objects so
new layers (captions, OCR, ...) drop in without touching the orchestrator.
Analysis is injectable: pass a cached observation to skip the model spend.
"""

from __future__ import annotations

import json
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

from .models import Annotation, Asset
from .refs import AssetIdentity
from .repository import AulRepository

PRODUCER_VERSIONS = {
    "probe": "ffprobe@v1",
    "scenes": "scene_detector@v1",
    "audio_activity": "silencedetect@v1",
    "signals": "signals@v1",
    "semantic": "video_understanding@v1",
    "beat_grid": "librosa_beat@v1",
}


class TechProbe:
    """L0: container/stream facts via ffprobe."""

    version = PRODUCER_VERSIONS["probe"]

    def run(self, path: Path) -> dict[str, Any]:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height,avg_frame_rate",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60)
        info = json.loads(out.stdout or "{}")
        streams = info.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), {})
        return {
            "duration_s": float(info.get("format", {}).get("duration") or 0.0),
            "width": video.get("width"), "height": video.get("height"),
            "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        }


class Producer(ABC):
    """Turns an ingested asset into annotations for one understanding layer."""

    @abstractmethod
    async def produce(self, asset: Asset, path: Path, *,
                      observation: Optional[dict[str, Any]] = None) -> list[Annotation]: ...


class VideoUnderstandingProducer(Producer):
    """L1 shots/signals/audio-activity + L2 scenes/global via the recompose stack."""

    def __init__(self, *, with_signals: bool = True) -> None:
        self.with_signals = with_signals

    async def produce(self, asset: Asset, path: Path, *,
                      observation: Optional[dict[str, Any]] = None) -> list[Annotation]:
        from ..Recompose.domain import AssetRecord
        from ..Recompose.signals import annotate_tree_signals
        from ..Recompose.understanding import understand_asset

        record = AssetRecord(kind="video", path=str(path), duration_s=asset.duration_s)
        tree = await understand_asset(record, observation=observation)
        if self.with_signals:
            annotate_tree_signals(tree, record, max_workers=8)

        def ann(layer: str, kind: str, producer_key: str, payload: dict[str, Any],
                start: Optional[float] = None, end: Optional[float] = None) -> Annotation:
            return Annotation(asset_id=asset.asset_id, project_id=asset.project_id,
                              layer=layer, kind=kind,
                              producer=PRODUCER_VERSIONS[producer_key],
                              span_start_s=start, span_end_s=end, payload=payload)

        out: list[Annotation] = []
        for a, b in tree.audio_activity:
            out.append(ann("L1", "audio_activity", "audio_activity", {}, a, b))
        for node in tree.leaves():
            a, b = round(node.start_s, 3), round(node.end_s, 3)
            out.append(ann("L1", "shot", "scenes", {"scene_key": node.scene_key}, a, b))
            if node.summary or node.mood:
                out.append(ann("L2", "scene", "semantic",
                               {"summary": node.summary, "mood": node.mood,
                                "key_actions": node.key_actions, "entities": node.entities,
                                "scene_key": node.scene_key}, a, b))
            if self.with_signals:
                out.append(ann("L1", "signals", "signals",
                               {"motion": node.motion, "brightness": node.brightness,
                                "speech": node.speech, "flags": node.quality_flags}, a, b))
        if observation:
            out.append(ann("L2", "global", "semantic",
                           {"music_prompt": observation.get("music_prompt") or {},
                            "description": observation.get("video_description", "")}))
        return out


class AudioBeatGridProducer(Producer):
    """L1 beat grid for an audio asset."""

    async def produce(self, asset: Asset, path: Path, *,
                      observation: Optional[dict[str, Any]] = None) -> list[Annotation]:
        from ..Recompose.musicsheet import build_music_sheet

        sheet = build_music_sheet(str(path))
        return [Annotation(
            asset_id=asset.asset_id, project_id=asset.project_id,
            layer="L1", kind="beat_grid", producer=PRODUCER_VERSIONS["beat_grid"],
            payload={"tempo_bpm": sheet.tempo_bpm, "n_beats": len(sheet.beats),
                     "beats": sheet.beats[:512], "beat_energy": sheet.beat_energy[:512],
                     "phrases": [p.model_dump() for p in sheet.phrases]})]


class AssetIngestor:
    """Registers an asset and its understanding annotations from a file."""

    def __init__(self, repo: AulRepository, *, probe: Optional[TechProbe] = None) -> None:
        self.repo = repo
        self.probe = probe or TechProbe()

    async def ingest(
        self, path: str | Path, *, name: Optional[str] = None, kind: str = "video",
        project_id: str = "default", generated: bool = False,
        observation: Optional[dict[str, Any]] = None, with_signals: bool = True,
        understand: bool = True, meta: Optional[dict[str, Any]] = None,
    ) -> str:
        """Returns the content-addressed asset id."""

        path = Path(path)
        asset_id, sha = AssetIdentity.from_file(path)
        tech = (self.probe.run(path) if kind != "image"
                else {"duration_s": None, "width": None, "height": None, "has_audio": False})
        asset = Asset(
            asset_id=asset_id, project_id=project_id, kind=kind, sha256=sha,
            name=name or path.name, uri=str(path), duration_s=tech["duration_s"],
            width=tech.get("width"), height=tech.get("height"), generated=generated,
            meta={**(meta or {}), "has_audio": tech.get("has_audio", False)})
        self.repo.upsert_asset(asset)

        annotations: list[Annotation] = [Annotation(
            asset_id=asset_id, project_id=project_id, layer="L0", kind="tech",
            producer=PRODUCER_VERSIONS["probe"], payload=tech)]

        producer = self._producer_for(kind, understand, with_signals)
        if producer is not None:
            annotations += await producer.produce(asset, path, observation=observation)

        # Idempotent per content: re-running a producer replaces its rows rather
        # than duplicating them (outcome annotations, from a different producer,
        # are untouched).
        self.repo.delete_annotations(asset_id, producers={a.producer for a in annotations})
        self.repo.add_annotations(annotations)
        return asset_id

    @staticmethod
    def _producer_for(kind: str, understand: bool, with_signals: bool) -> Optional[Producer]:
        if not understand:
            return None
        if kind == "video":
            return VideoUnderstandingProducer(with_signals=with_signals)
        if kind == "audio":
            return AudioBeatGridProducer()
        return None
