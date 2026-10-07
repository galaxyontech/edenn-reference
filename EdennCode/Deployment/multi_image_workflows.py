from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from EdennCode.MusicGenerationCore.models import SectionTiming, normalize_modelspec
from EdennCode.WorkflowFactory.MultiImageWorkflow import (
    MultiImageGenerationE2EStage,
    MultiImageWorkflowStageInput,
    MultiImageWorkflowStageOutput,
)


@dataclass
class MultiImageGenerationResult:
    final_video_path: Path
    silent_video_path: Path
    generated_music_path: Path
    full_track_paths: List[Path]
    processed_image_paths: List[Path]
    planning_metadata: Dict[str, Any]
    music_prompt: str
    lyrics_timestamps: List[Any]
    section_timeline: List[SectionTiming]
    video_title: str
    music_title: str
    video_description: str
    include_vocals: bool
    vocal_gender: str
    lyrics_language: str
    user_requested_language: str
    used_music_model_spec: str
    compression_applied: bool
    vocal_id_used: Optional[str] = None
    job_received_timestamp: Optional[int] = None
    job_finished_timestamp: Optional[int] = None
    line_level_lyrics_timestamps: List[Any] = field(default_factory=list)
    full_lyrics: Optional[str] = None
    token_usage: Optional[Dict[str, Any]] = None
    token_usage_breakdown: Optional[Dict[str, Any]] = None
    music_start_s: float = 0.0
    # The [window_start, window_start + video_len] slice of the primary track that
    # is muxed into the slideshow — surfaced as audio_url. ``None`` when no distinct
    # window exists (the whole track fits the video), in which case audio_url falls
    # back to the full track and equals complete_audio_url.
    matched_music_path: Optional[Path] = None


class MultiImageGenerationOrchestrator:
    async def run(
        self,
        *,
        folder_path: Path,
        output_path: Path,
        user_prompt: str = "",
        include_vocals: bool = False,
        vocal_gender: Optional[str] = None,
        align_to_beats: bool = True,
        lyrics_language: Optional[str] = None,
        modelspec: str = "edenn_basic",
        per_image_duration: float = 3.0,
        music_volume: float = 1.0,
        vocal_id: Optional[str] = None,
        vocal_sample_path: Optional[Path] = None,
        water_mark: bool = False,
        audio_output_format: Optional[str] = None,
        user_lyrics_prompt: Optional[str] = None,
        plan_cache: Optional[object] = None,
        transition_mode: str = "none",
        transitions: Optional[Sequence[str]] = None,
        transition_duration_s: "float | Sequence[float]" = 0.4,
        fixed_image_order: bool = False,
        per_image_durations: Optional[Sequence[float]] = None,
    ) -> MultiImageGenerationResult:
        # Unix seconds, matching video-music response_metadata (clients parse both
        # job types with the same code).
        received_at = int(time.time())
        workflow = MultiImageGenerationE2EStage(
            per_image_duration=per_image_duration,
            music_volume=music_volume,
            align_to_beats=align_to_beats,
            plan_cache=plan_cache,
        )
        output: MultiImageWorkflowStageOutput = await workflow.run(
            MultiImageWorkflowStageInput(
                folder_path=folder_path,
                output_path=output_path,
                user_prompt=user_prompt,
                include_vocals=include_vocals,
                vocal_gender=vocal_gender,
                align_to_beats=align_to_beats,
                lyrics_language=lyrics_language,
                modelspec=normalize_modelspec(modelspec).value,
                vocal_id=vocal_id,
                vocal_sample_path=vocal_sample_path,
                water_mark=water_mark,
                audio_output_format=audio_output_format,
                user_lyrics_prompt=user_lyrics_prompt,
                transition_mode=transition_mode,
                transitions=transitions,
                transition_duration_s=transition_duration_s,
                fixed_image_order=fixed_image_order,
                per_image_durations=(
                    list(per_image_durations) if per_image_durations else None
                ),
            )
        )
        finished_at = int(time.time())
        return MultiImageGenerationResult(
            final_video_path=output.final_video_path,
            silent_video_path=output.silent_video_path,
            generated_music_path=output.music_path,
            full_track_paths=list(output.full_track_paths),
            processed_image_paths=list(output.processed_image_paths),
            planning_metadata=dict(output.planning_metadata),
            music_prompt=output.music_prompt,
            lyrics_timestamps=list(output.lyrics_timestamps),
            section_timeline=list(output.section_timeline),
            video_title=output.video_title,
            music_title=output.music_title,
            video_description=output.video_description,
            include_vocals=output.include_vocals,
            vocal_gender=output.vocal_gender,
            lyrics_language=output.lyrics_language,
            user_requested_language=output.user_requested_language,
            used_music_model_spec=output.used_modelspec,
            compression_applied=output.compression_applied,
            vocal_id_used=output.vocal_id_used,
            job_received_timestamp=received_at,
            job_finished_timestamp=finished_at,
            line_level_lyrics_timestamps=list(output.line_level_lyrics_timestamps),
            full_lyrics=output.full_lyrics,
            token_usage=output.token_usage,
            token_usage_breakdown=output.token_usage_breakdown,
            music_start_s=output.music_start_s,
            matched_music_path=(
                output.matched_music_path
                if output.matched_music_path != output.music_path
                else None
            ),
        )


__all__ = ["MultiImageGenerationOrchestrator", "MultiImageGenerationResult"]
