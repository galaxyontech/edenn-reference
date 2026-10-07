from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_FILE_NAME = "sfx_project.json"
PROJECT_SCHEMA_VERSION = 3

# Where a sound's *reason to exist* comes from. Video-native engines can only
# serve the first two; the rest are intent-driven and text-routed by necessity.
EVENT_ORIGINS = ("visual", "ambience", "stylistic", "offscreen", "narrative", "user")
# How an event's start time is decided (and therefore how it may be evaluated).
TIMING_AUTHORITIES = ("motion_snap", "cut_snap", "design", "user")
# Synthesis routes an event/bed variant can come from.
ROUTE_TEXT = "text"
ROUTE_VIDEO_NATIVE = "video_native"


@dataclass
class SfxVariant:
    path: str
    route: str = ROUTE_TEXT
    prompt: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Any) -> "SfxVariant":
        if isinstance(payload, str):  # v2 files stored bare paths
            return cls(path=payload)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in known})


@dataclass
class SoundFXEvent:
    event_id: str
    start_time: float
    end_time: float
    event_description: str
    sound_event_local_path: str
    confidence: float = 0.0
    # v2 fields — every field below must keep a default so v1 call sites and
    # persisted v1 payloads stay loadable.
    event_type: str = ""
    sound_prompt: str = ""
    gain_db: float = 0.0
    muted: bool = False
    variants: List[SfxVariant] = field(default_factory=list)
    selected_variant: int = -1
    source: str = "detected"  # "detected" | "user"
    refined_start_time: Optional[float] = None
    # v3 fields
    origin: str = "visual"
    timing_authority: str = "motion_snap"
    lane: str = "sfx"
    route: str = ROUTE_TEXT  # preferred synthesis route for regeneration

    @property
    def variant_paths(self) -> List[str]:
        # Back-compat alias for v2 call sites.
        return [v.path for v in self.variants]

    @property
    def effective_start(self) -> float:
        return self.refined_start_time if self.refined_start_time is not None else self.start_time

    @property
    def duration(self) -> float:
        return max(0.0, self.end_time - self.start_time)

    @property
    def generation_prompt(self) -> str:
        return self.sound_prompt.strip() or self.event_description

    @property
    def active_audio_path(self) -> str:
        if 0 <= self.selected_variant < len(self.variants):
            return self.variants[self.selected_variant].path
        return self.sound_event_local_path

    def select_variant(self, index: int) -> None:
        if not 0 <= index < len(self.variants):
            raise IndexError(
                f"variant {index} out of range for event {self.event_id} "
                f"({len(self.variants)} variants)"
            )
        self.selected_variant = index
        self.sound_event_local_path = self.variants[index].path

    def add_variant(self, path: str, *, select: bool = True, route: str = ROUTE_TEXT, prompt: str = "") -> int:
        self.variants.append(SfxVariant(path=str(path), route=route, prompt=prompt))
        index = len(self.variants) - 1
        if select:
            self.select_variant(index)
        return index

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "SoundFXEvent":
        payload = dict(payload)
        raw_variants = payload.pop("variants", None)
        legacy_paths = payload.pop("variant_paths", None)
        known = {f for f in cls.__dataclass_fields__}
        event = cls(**{k: v for k, v in payload.items() if k in known and k != "variants"})
        if raw_variants:
            event.variants = [SfxVariant.from_dict(v) for v in raw_variants]
        elif legacy_paths:
            event.variants = [SfxVariant(path=p) for p in legacy_paths]
        return event


@dataclass
class SfxSuggestion:
    """A proposed-but-uncommitted sound: shown as a ghost event with rationale."""

    suggestion_id: str
    origin: str  # stylistic | offscreen | narrative
    start_time: float
    end_time: float
    description: str
    sound_prompt: str
    rationale: str = ""
    style: str = ""
    event_type: str = ""
    timing_authority: str = "design"
    status: str = "pending"  # pending | accepted | rejected

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "SfxSuggestion":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in known})


@dataclass
class AmbienceBed:
    prompt: str
    audio_path: str = ""
    gain_db: float = -8.0
    enabled: bool = True
    loop: bool = True
    variant_paths: List[str] = field(default_factory=list)
    selected_variant: int = -1
    route: str = ROUTE_TEXT
    # Duck the bed under event windows so generated beds don't double-hit
    # against our discrete events.
    duck_db: float = -6.0

    @property
    def active_audio_path(self) -> str:
        if 0 <= self.selected_variant < len(self.variant_paths):
            return self.variant_paths[self.selected_variant]
        return self.audio_path

    def add_variant(self, path: str, *, select: bool = True) -> int:
        self.variant_paths.append(path)
        index = len(self.variant_paths) - 1
        if select:
            self.selected_variant = index
            self.audio_path = path
        return index

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "AmbienceBed":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in known})


@dataclass
class MixSettings:
    preserve_original_audio: bool = True
    sfx_master_gain_db: float = 0.0
    sample_rate: int = 44_100
    channels: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "MixSettings":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in known})


@dataclass
class SfxProject:
    """
    Serializable, editable state of one video→SFX session.

    The project file is the contract between the one-shot workflow and the
    iteration loop: the workflow produces it, `SfxProjectEditor` mutates it and
    re-renders, and the eval harness reads it to score alignment.
    """

    project_dir: str
    video_path: str
    video_duration: float
    video_url: str = ""
    user_prompt: str = ""
    scene_summary: str = ""
    events: List[SoundFXEvent] = field(default_factory=list)
    ambience: Optional[AmbienceBed] = None
    suggestions: List[SfxSuggestion] = field(default_factory=list)
    mix: MixSettings = field(default_factory=MixSettings)
    revision: int = 0
    mixed_audio_path: str = ""
    final_video_path: str = ""
    sfx_only_video_path: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    schema_version: int = PROJECT_SCHEMA_VERSION

    def event_by_id(self, event_id: str) -> SoundFXEvent:
        for event in self.events:
            if event.event_id == event_id:
                return event
        raise KeyError(f"no event with id {event_id!r} in project")

    def next_event_id(self) -> str:
        existing = {event.event_id for event in self.events}
        idx = len(self.events) + 1
        while f"event_{idx:03d}" in existing:
            idx += 1
        return f"event_{idx:03d}"

    def next_suggestion_id(self) -> str:
        existing = {s.suggestion_id for s in self.suggestions}
        idx = len(self.suggestions) + 1
        while f"suggestion_{idx:03d}" in existing:
            idx += 1
        return f"suggestion_{idx:03d}"

    def suggestion_by_id(self, suggestion_id: str) -> SfxSuggestion:
        for suggestion in self.suggestions:
            if suggestion.suggestion_id == suggestion_id:
                return suggestion
        raise KeyError(f"no suggestion with id {suggestion_id!r} in project")

    @property
    def project_file(self) -> Path:
        return Path(self.project_dir) / PROJECT_FILE_NAME

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_dir": self.project_dir,
            "video_path": self.video_path,
            "video_url": self.video_url,
            "video_duration": self.video_duration,
            "user_prompt": self.user_prompt,
            "scene_summary": self.scene_summary,
            "events": [event.to_dict() for event in self.events],
            "ambience": self.ambience.to_dict() if self.ambience else None,
            "suggestions": [s.to_dict() for s in self.suggestions],
            "mix": self.mix.to_dict(),
            "revision": self.revision,
            "mixed_audio_path": self.mixed_audio_path,
            "final_video_path": self.final_video_path,
            "sfx_only_video_path": self.sfx_only_video_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def save(self, path: Optional[Path] = None) -> Path:
        target = Path(path) if path else self.project_file
        target.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = time.time()
        target.write_text(json.dumps(self.to_dict(), indent=2))
        return target

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "SfxProject":
        ambience_payload = payload.get("ambience")
        return cls(
            project_dir=payload["project_dir"],
            video_path=payload["video_path"],
            video_url=payload.get("video_url", ""),
            video_duration=float(payload["video_duration"]),
            user_prompt=payload.get("user_prompt", ""),
            scene_summary=payload.get("scene_summary", ""),
            events=[SoundFXEvent.from_dict(e) for e in payload.get("events", [])],
            ambience=AmbienceBed.from_dict(ambience_payload) if ambience_payload else None,
            suggestions=[SfxSuggestion.from_dict(s) for s in payload.get("suggestions", [])],
            mix=MixSettings.from_dict(payload.get("mix", {})),
            revision=int(payload.get("revision", 0)),
            mixed_audio_path=payload.get("mixed_audio_path", ""),
            final_video_path=payload.get("final_video_path", ""),
            sfx_only_video_path=payload.get("sfx_only_video_path", ""),
            created_at=float(payload.get("created_at", time.time())),
            updated_at=float(payload.get("updated_at", time.time())),
            schema_version=int(payload.get("schema_version", PROJECT_SCHEMA_VERSION)),
        )

    @classmethod
    def load(cls, path: Path) -> "SfxProject":
        path = Path(path)
        if path.is_dir():
            path = path / PROJECT_FILE_NAME
        return cls.from_dict(json.loads(path.read_text()))
