"""CP2 tests: CreationService resolve → plan → preview → render, hermetic.

Uses the in-memory repository, the synthetic multi-scene fixture, and the
synthesized beat track — deterministic planner (no model client), real ffmpeg
render at the end. Pins the show-before-spend contract: render without a prior
plan is refused; a previewed plan renders exactly and lands in lineage.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from EdennCode.EdennAgent.AssetLibrary import AssetIngestor, InMemoryAulRepository
from EdennCode.EdennAgent.Creation import (
    BundleItem,
    CreationService,
    MusicSource,
    RequestBundle,
    ShortSpec,
    SourceRole,
    TreatmentKind,
    TreatmentSpec,
)
from EdennCode.EdennAgent.Recompose.Testing.test_recompose_m4_m5 import (
    MULTI_SCENE_TEXTS,
    _render_multi_scene_video,
)


def _beat_track(dest: Path, seconds: int = 40) -> Path:
    """Synthesize a percussive 120bpm track librosa can beat-track."""

    expr = ("0.9*sin(2*PI*82*t)*exp(-28*mod(t,0.5))"
            "+0.25*sin(2*PI*3200*t)*exp(-60*mod(t+0.25,0.5))")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"aevalsrc='{expr}':d={seconds}:s=44100",
         "-ar", "44100", "-ac", "2", str(dest)],
        check=True, capture_output=True, timeout=120)
    return dest


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """A tiny ingested world: one video, one track, a ready service."""

    root = tmp_path_factory.mktemp("creation")
    video = _render_multi_scene_video(root / "reel.mp4")
    track = _beat_track(root / "beat.wav")
    scenes = [
        {"scene_index": i, "start_timestamp": i * 4.5, "end_timestamp": (i + 1) * 4.5,
         "visual_summary": s, "key_actions": a,
         "mood": "energetic" if i % 2 == 0 else "calm"}
        for i, (s, a) in enumerate(MULTI_SCENE_TEXTS)]

    repo = InMemoryAulRepository()
    ingestor = AssetIngestor(repo)
    video_id = asyncio.run(ingestor.ingest(
        video, name="reel.mp4", observation={"scenes": scenes}, with_signals=True))
    track_id = asyncio.run(ingestor.ingest(track, kind="audio", name="beat.wav"))
    service = CreationService(repo, workdir=root / "work")
    return {"repo": repo, "service": service,
            "video_id": video_id, "track_id": track_id}


def _request(world, *, duration: float = 12.0) -> "CreationRequestArgs":
    bundle = RequestBundle(
        intent="a tight teaser",
        items=[BundleItem(ref=world["video_id"]),
               BundleItem(ref=world["track_id"])])
    short = ShortSpec(
        hypothesis="teaser: fast open, calm close", duration_s=duration,
        treatment=TreatmentSpec(kind=TreatmentKind.MUSIC_ONLY,
                                music=MusicSource.PROVIDED,
                                music_ref=world["track_id"]))
    return bundle, short


def test_resolve_via_service_looks_up_kinds_from_the_store(world) -> None:
    bundle, _ = _request(world)
    res = world["service"].resolve(bundle)
    assert res.status == "resolved"
    roles = {s.ref: s.role for s in res.bundle.sources}
    assert roles[world["video_id"]] is SourceRole.SPINE
    assert roles[world["track_id"]] is SourceRole.MUSIC


def test_unknown_ref_raises_keyerror(world) -> None:
    with pytest.raises(KeyError):
        world["service"].resolve(RequestBundle(
            items=[BundleItem(ref="asset_missing")]))


def test_plan_short_builds_a_playable_preview(world) -> None:
    bundle, short = _request(world)
    resolved = world["service"].resolve(bundle).bundle
    preview, state = asyncio.run(world["service"].plan_short(resolved, short))

    assert preview.slots, "a plan must yield slots"
    assert abs(preview.duration_s - sum(s.dur_s for s in preview.slots)) < 0.01
    assert preview.beats_out and preview.beats_out[0] >= 0.0
    assert preview.music_url and world["track_id"] in preview.music_url
    kinds = {s.asset_id: s.kind for s in preview.sources}
    assert kinds[world["video_id"]] == "video"
    assert kinds[world["track_id"]] == "audio"
    # slots reference only footage sources and stay seekable
    for slot in preview.slots:
        assert slot.asset_id == world["video_id"]
        assert 0 <= slot.seg_in_s < 36.0
    # every slot start sits on a beat of the sheet (the strip's promise)
    beats = set(round(b, 2) for b in preview.beats_out)
    starts = [round(s.t_out, 2) for s in preview.slots[1:]]
    assert all(any(abs(t - b) < 0.05 for b in beats) for t in starts)


def test_render_requires_a_previewed_plan(world) -> None:
    with pytest.raises(KeyError, match="plan .* before rendering|no previewed"):
        asyncio.run(world["service"].render_short("plan_never_seen"))


def test_lock_renders_the_previewed_plan_and_records_lineage(world) -> None:
    bundle, short = _request(world, duration=10.0)
    resolved = world["service"].resolve(bundle).bundle
    preview, _ = asyncio.run(world["service"].plan_short(resolved, short))

    out_id = asyncio.run(world["service"].render_short(preview.plan_id))
    repo = world["repo"]
    out = repo.get_asset(out_id)
    assert out is not None and out.generated
    assert out.duration_s == pytest.approx(preview.duration_s, abs=0.35)
    cuts = [e for e in repo.edges_to(out_id) if e.operation == "slot_cut"]
    assert len(cuts) == len(preview.slots)
    music = [e for e in repo.edges_to(out_id) if e.operation == "music_for"]
    assert [e.src_asset_id for e in music] == [world["track_id"]]
