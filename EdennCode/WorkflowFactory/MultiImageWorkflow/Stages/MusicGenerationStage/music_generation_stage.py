from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, Optional, List

from EdennCode.MusicGenerationCore.models import (
    MusicGenerationOptions,
    MusicGenerationRequest,
    MusicGenerationResult,
    MusicModelSpec,
    SectionPlan,
    SectionTiming,
    TimestampedWord,
    normalize_modelspec,
)
from EdennCode.MusicGenerationCore.service import MusicGenerationService


@dataclass
class MultiImageMusicGenerationStageInput:
    plan_metadata: Dict[str, object]
    section_plan: SectionPlan
    total_duration: float
    output_dir: Path
    include_vocals: bool = False
    vocal_gender: str = "female"
    lyrics_language: Optional[str] = None
    modelspec: str = MusicModelSpec.EDENN_BASIC.value
    vocal_id: Optional[str] = None
    vocal_sample_path: Optional[Path] = None
    water_mark: bool = False
    audio_output_format: Optional[str] = None


@dataclass
class MultiImageMusicGenerationStageOutput:
    music_path: Path
    prompt: str
    used_modelspec: str
    lyrics_timestamps: List[TimestampedWord] = field(default_factory=list)
    line_level_lyrics_timestamps: List[TimestampedWord] = field(default_factory=list)
    full_lyrics: Optional[str] = None
    section_timeline: List[SectionTiming] = field(default_factory=list)
    generation_manifest: Dict[str, object] = field(default_factory=dict)
    full_track_paths: List[Path] = field(default_factory=list)
    vocal_id_used: Optional[str] = None
    # Full generated-track length; used to bound the slideshow audio window.
    music_duration_s: float = 0.0


# A full-length generated song carries roughly this many lyric lines; sheets at
# or above it already get near-complete provider alignment (the video pipeline's
# 25-48 line sheets measure ~95% track coverage).
FULL_SONG_TARGET_LINES = 26
_FULL_SONG_REPEAT_LABELS = ("Chorus", "Reprise", "Final Chorus", "Outro Chorus")


def expand_plan_for_full_song(plan: SectionPlan) -> SectionPlan:
    """Write the song's loops into the lyric sheet so they get timestamps.

    The slideshow plan sizes lyric_lines to the video window (seconds), but the
    provider composes a full-length song from the sheet and aligns ONLY the
    sheet — content the song repeats beyond it is delivered unstamped, so
    full_lyrics/timestamps stop mid-track (verified against the provider's raw
    task payload, 2026-07-17). The generated song loops the written content
    anyway; appending explicit repeat sections (chorus/reprise built from the
    planned lines) makes the sheet — and therefore the alignment — cover the
    whole delivered track.
    """
    sections_with_lines = [
        section
        for section in plan.sections
        if any(str(line).strip() for line in section.lyric_lines)
    ]
    if not sections_with_lines:
        return plan
    total_lines = sum(
        len([line for line in section.lyric_lines if str(line).strip()])
        for section in sections_with_lines
    )
    if total_lines >= FULL_SONG_TARGET_LINES:
        return plan

    closing = sections_with_lines[-1]
    opening = sections_with_lines[0]
    sources = (closing, opening, closing, closing)
    appended = []
    for label, source in zip(_FULL_SONG_REPEAT_LABELS, sources):
        if total_lines >= FULL_SONG_TARGET_LINES:
            break
        lines = [line for line in source.lyric_lines if str(line).strip()]
        appended.append(
            replace(
                source,
                section_id=f"{source.section_id}_repeat{len(appended) + 1}",
                label=label,
                lyric_lines=list(lines),
            )
        )
        total_lines += len(lines)
    if not appended:
        return plan
    return replace(plan, sections=[*plan.sections, *appended])


class MultiImageMusicGenerationStage:
    def __init__(self, *, music_generation_service: MusicGenerationService) -> None:
        self.music_generation_service = music_generation_service

    async def run(self, stage_input: MultiImageMusicGenerationStageInput) -> MultiImageMusicGenerationStageOutput:
        if stage_input.total_duration <= 0:
            raise ValueError("total_duration must be positive")

        # Vocal jobs send a full-song sheet so the provider's alignment (and
        # therefore full_lyrics/timestamps) covers the whole delivered track,
        # not just the slideshow window's worth of lines sung once.
        section_plan = stage_input.section_plan
        if stage_input.include_vocals:
            section_plan = expand_plan_for_full_song(section_plan)

        request = MusicGenerationRequest(
            request_id=f"multi_image_{normalize_modelspec(stage_input.modelspec).value}",
            modelspec=normalize_modelspec(stage_input.modelspec),
            section_plan=section_plan,
            options=MusicGenerationOptions(
                include_vocals=stage_input.include_vocals,
                vocal_gender=stage_input.vocal_gender,
                lyrics_language=stage_input.lyrics_language,
                vocal_id=stage_input.vocal_id,
                vocal_sample_path=stage_input.vocal_sample_path,
                water_mark=stage_input.water_mark,
                output_format=(stage_input.audio_output_format or "").strip() or None,
            ),
            output_dir=Path(stage_input.output_dir),
        )
        result = await self.music_generation_service.generate(request)
        if not result.primary.audio_path.exists():
            raise FileNotFoundError(result.primary.audio_path)

        return MultiImageMusicGenerationStageOutput(
            music_path=result.primary.audio_path,
            prompt=result.prompt_summary,
            used_modelspec=result.used_modelspec.value,
            lyrics_timestamps=result.primary.lyrics_timestamps,
            line_level_lyrics_timestamps=result.primary.line_level_lyrics_timestamps,
            full_lyrics=result.primary.full_lyrics,
            music_duration_s=result.primary.duration_s,
            section_timeline=result.primary.section_timeline,
            generation_manifest=result.prompt_manifest,
            full_track_paths=[
                result.primary.audio_path,
                *[variant.audio_path for variant in result.alternates],
            ],
            vocal_id_used=result.vocal_id_used,
        )
