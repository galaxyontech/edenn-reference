from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class MusicModelSpec(str, Enum):
    EDENN_BASIC = "edenn_basic"
    EDENN_ENHANCED = "edenn_enhanced"
    EDENN_STUDIO = "edenn_studio"


LEGACY_MODEL_ALIASES = {
    "edenn_basic": MusicModelSpec.EDENN_BASIC,
    "provider_a": MusicModelSpec.EDENN_BASIC,
    "provider_a": MusicModelSpec.EDENN_BASIC,
    "provider_a": MusicModelSpec.EDENN_BASIC,
    "edenn_enhanced": MusicModelSpec.EDENN_ENHANCED,
    "provider_b": MusicModelSpec.EDENN_ENHANCED,
    "edenn_studio": MusicModelSpec.EDENN_STUDIO,
    "provider_c": MusicModelSpec.EDENN_STUDIO,
}


def normalize_modelspec(value: str | MusicModelSpec | None) -> MusicModelSpec:
    if isinstance(value, MusicModelSpec):
        return value
    normalized = (value or "").strip().lower()
    return LEGACY_MODEL_ALIASES.get(normalized, MusicModelSpec.EDENN_BASIC)


@dataclass(frozen=True)
class TimestampedWord:
    text: str
    startS: float
    endS: float
    i: Optional[int] = None

    @property
    def start_s(self) -> float:
        return self.startS

    @property
    def end_s(self) -> float:
        return self.endS


@dataclass(frozen=True)
class NarrativeCue:
    cue_id: str
    label: str
    role: str
    target_duration_s: float
    emotion: str = ""
    description: str = ""
    transition_hint: str = ""
    source_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MusicSection:
    section_id: str
    label: str
    target_duration_s: float
    objective: str
    energy_start: float
    energy_end: float
    image_indices: list[int] = field(default_factory=list)
    cue_ids: list[str] = field(default_factory=list)
    instrumentation_focus: list[str] = field(default_factory=list)
    lyric_lines: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SectionPlan:
    summary: str
    total_duration_s: float
    overall_mood: str
    target_bpm: Optional[float]
    primary_instruments: list[str]
    cues: list[NarrativeCue]
    sections: list[MusicSection]
    music_prompt_summary: str = ""

    def section_image_counts(self) -> list[int]:
        return [max(1, len(section.image_indices)) for section in self.sections]

    def legacy_metadata(self) -> dict[str, Any]:
        sequence_plan = []
        cue_lookup = {cue.cue_id: cue for cue in self.cues}
        for section in self.sections:
            for cue_id in section.cue_ids:
                cue = cue_lookup.get(cue_id)
                if not cue:
                    continue
                source = cue.source_refs[0] if cue.source_refs else cue_id
                sequence_plan.append(
                    f"{cue.label}: {cue.description or cue.role} | "
                    f"Role: {cue.role} | Emotion: {cue.emotion} | Transition: {cue.transition_hint}"
                )
        music_structure_notes = " | ".join(
            f"{section.label}: {section.objective}" for section in self.sections
        )
        return {
            "storyline_summary": self.summary,
            "overall_mood": self.overall_mood,
            "recommended_mood": self.overall_mood,
            "target_bpm": self.target_bpm,
            "tempo_bpm": self.target_bpm,
            "primary_instruments": list(self.primary_instruments),
            "instruments": list(self.primary_instruments),
            "music_prompt_summary": self.music_prompt_summary,
            "music_prompt": self.music_prompt_summary,
            "sequence_plan": sequence_plan,
            "music_structure_notes": music_structure_notes,
        }


@dataclass(frozen=True)
class MusicGenerationOptions:
    include_vocals: bool = False
    vocal_gender: Optional[str] = None
    lyrics_language: Optional[str] = None
    vocal_id: Optional[str] = None
    vocal_sample_path: Optional[Path] = None
    max_variants: int = 2
    require_word_timestamps: bool = True
    output_format: Optional[str] = None
    callback_url: Optional[str] = None
    water_mark: bool = False


@dataclass(frozen=True)
class MusicGenerationRequest:
    request_id: str
    modelspec: MusicModelSpec
    section_plan: SectionPlan
    options: MusicGenerationOptions
    output_dir: Path
    provider_overrides: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SectionTiming:
    section_id: str
    expected_start_s: float
    expected_end_s: float
    actual_start_s: Optional[float] = None
    actual_end_s: Optional[float] = None
    confidence: Optional[float] = None


@dataclass(frozen=True)
class ProviderJobRef:
    provider: str
    task_id: Optional[str] = None
    audio_id: Optional[str] = None
    raw_ids: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MusicVariant:
    variant_id: str
    audio_path: Path
    duration_s: float
    # Word/character-level timed lyrics (the finest granularity a provider returns).
    lyrics_timestamps: list[TimestampedWord] = field(default_factory=list)
    # Line/section-level timed lyrics; empty when a provider only exposes word level
    # (consumers fall back to ``lyrics_timestamps`` in that case).
    line_level_lyrics_timestamps: list[TimestampedWord] = field(default_factory=list)
    # Plain generated lyrics text, when available.
    full_lyrics: Optional[str] = None
    section_timeline: list[SectionTiming] = field(default_factory=list)


@dataclass(frozen=True)
class MusicGenerationResult:
    used_modelspec: MusicModelSpec
    primary: MusicVariant
    alternates: list[MusicVariant]
    job_ref: ProviderJobRef
    prompt_manifest: dict[str, Any]
    prompt_summary: str
    vocal_id_used: Optional[str] = None
