from __future__ import annotations

import logging
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from EdennCode.Deployment.pipeline_telemetry import (
    PipelineRunRecorder,
    pipeline_telemetry_enabled,
    provider_for_modelspec,
)

from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata import VideoMetadata
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.datamodel import SceneUnderstanding
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow import (
    VideoMusicPreGenerationOutput,
    VideoMusicWorkflowE2E,
    VideoMusicWorkflowE2EInput,
    VideoMusicWorkflowE2EOutput,
    reset_pipeline_recorder,
    set_pipeline_recorder,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.word_ts import WordTS

VALID_MUSIC_MODEL_SPECS = {"edenn_basic", "edenn_enhanced", "edenn_studio"}
LEGACY_MODEL_MAP = {
    "provider_a": "edenn_basic",
    "provider_a": "edenn_basic",
    "provider_a": "edenn_basic",
    "provider_c": "edenn_studio",
}


@dataclass
class VideoGenerationResult:
    video_metadata: VideoMetadata
    scenes: List[SceneUnderstanding]
    video_summary: Any
    video_title: str
    video_description: str
    music_prompt: Dict[str, Any]
    music_prompt_in_chinese: Optional[Dict[str, Any]]
    generated_music_path: Path
    complete_generated_music_path: Optional[Path]
    secondary_complete_generated_music_path: Optional[Path]
    remixed_video_path: Path
    include_vocals: bool
    vocal_gender: str
    lyrics_timestamps: List[WordTS]
    word_level_lyrics_timestamps: List[WordTS]
    vocal_id_used: Optional[str] = None
    primary_full_lyrics: Optional[str] = None
    primary_full_lyrics_timestamps: List[WordTS] = field(default_factory=list)
    primary_full_word_level_lyrics_timestamps: List[WordTS] = field(default_factory=list)
    secondary_full_lyrics: Optional[str] = None
    secondary_full_lyrics_timestamps: List[WordTS] = field(default_factory=list)
    secondary_full_word_level_lyrics_timestamps: List[WordTS] = field(default_factory=list)
    matching_used_track: Optional[str] = None
    thumbnail_path: Optional[Path] = None
    token_usage: Optional[Dict[str, int]] = None
    token_usage_breakdown: Optional[Dict[str, Dict[str, int]]] = None
    used_music_model_spec: str = ""
    user_requested_language: str = ""
    job_received_timestamp: Optional[int] = None
    job_finished_timestamp: Optional[int] = None
    job_id: str = ""
    video_id: str = ""
    creative_id: str = ""
    primary_music_id: str = ""
    secondary_music_id: Optional[str] = None
    selected_music_id: str = ""
    alignment_id: str = ""
    music_start_s: float = 0.0
    alignment_score: float = 0.0
    alignment_details: Dict[str, Any] = field(default_factory=dict)
    critical_warning: Optional[str] = None
    generation_api_call_count: int = 1
    # Provider-native handles on the generated track for iterative editing
    # (extend / remix / inpaint). None for ProviderA on this path.
    provider_audio_id: Optional[str] = None
    provider_task_id: Optional[str] = None


@dataclass
class VideoPreGenerationResult:
    video_metadata: VideoMetadata
    scenes: List[SceneUnderstanding]
    video_summary: Any
    video_title: str
    video_description: str
    music_prompt: Dict[str, Any]
    include_vocals: bool
    vocal_gender: str
    token_usage: Optional[Dict[str, int]]
    token_usage_breakdown: Optional[Dict[str, Dict[str, int]]]
    used_music_model_spec: str = ""
    user_requested_language: str = ""
    sanitized_prompt: str = ""
    sanitized_style_prompt: Optional[str] = None
    sanitized_lyrics_prompt: Optional[str] = None
    detected_category: str = ""
    was_transformed: bool = False
    detected_references: List[str] = field(default_factory=list)
    thumbnail_path: Optional[Path] = None
    job_received_timestamp: Optional[int] = None
    job_finished_timestamp: Optional[int] = None
    job_id: str = ""
    video_id: str = ""
    creative_id: str = ""
    primary_music_id: str = ""
    secondary_music_id: Optional[str] = None
    selected_music_id: str = ""
    alignment_id: str = ""
    stage_timing_s: Dict[str, float] = field(default_factory=dict)


class VideoGenerationOrchestrator:
    """
    High-level orchestration of the Edenn video → music workflow that returns
    structured metadata alongside the generated assets.
    """

    def __init__(
        self,
        *,
        storage: Any = None,
        llm_image_container: Optional[str] = None,
        llm_image_sas_ttl_minutes: int = 5,
        llm_image_cleanup_delay_seconds: float = 2.0,
        annotation_dispatcher: Any = None,
    ) -> None:
        self.workflow = VideoMusicWorkflowE2E(
            storage_service=storage,
            llm_image_container=llm_image_container,
            llm_image_sas_ttl_minutes=llm_image_sas_ttl_minutes,
            llm_image_cleanup_delay_seconds=llm_image_cleanup_delay_seconds,
        )
        self.annotation_dispatcher = annotation_dispatcher

    @staticmethod
    def _normalize_modelspec(value: Optional[str]) -> str:
        normalized = (value or "").strip().lower()
        mapped = LEGACY_MODEL_MAP.get(normalized, normalized)
        if mapped in VALID_MUSIC_MODEL_SPECS:
            return mapped
        return "edenn_basic"

    async def run(
        self,
        *,
        video_path: Path,
        preserve_original_audio: bool = False,
        music_volume: float = 1.0,
        water_mark: bool = False,
        include_vocals: bool = False,
        vocal_gender: str = "female",
        user_prompt: str = "",
        verbose_instruction: bool = False,
        music_style_prompt: Optional[str] = None,
        lyrics_prompt: Optional[str] = None,
        language: Optional[str] = None,
        modelspec: str = "edenn_basic",
        audio_output_format: Optional[str] = None,
        vocal_id: Optional[str] = None,
        vocal_sample_path: Optional[Path] = None,
        job_id: Optional[str] = None,
        video_id: Optional[str] = None,
        creative_id: Optional[str] = None,
        primary_music_id: Optional[str] = None,
        secondary_music_id: Optional[str] = None,
        selected_music_id: Optional[str] = None,
        alignment_id: Optional[str] = None,
    ) -> VideoGenerationResult:
        _ = language  # Kept for API compatibility; prompt language is auto-detected downstream.
        normalized_modelspec = self._normalize_modelspec(modelspec)
        normalized_audio_format = (audio_output_format or "").strip() or None
        effective_job_id = job_id or str(uuid4())

        workflow_input = VideoMusicWorkflowE2EInput(
            video_path=str(video_path),
            include_vocals=include_vocals,
            vocal_gender=vocal_gender,
            user_prompt=user_prompt,
            verbose_instruction=verbose_instruction,
            music_style_prompt=music_style_prompt,
            lyrics_prompt=lyrics_prompt,
            preserve_original_audio=preserve_original_audio,
            music_volume=music_volume,
            water_mark=water_mark,
            music_model_spec=normalized_modelspec,
            audio_output_format=normalized_audio_format,
            vocal_id=vocal_id,
            vocal_sample_path=vocal_sample_path,
            job_id=effective_job_id,
            video_id=video_id,
            creative_id=creative_id,
            primary_music_id=primary_music_id,
            secondary_music_id=secondary_music_id,
            selected_music_id=selected_music_id,
            alignment_id=alignment_id,
            annotation_dispatcher=self.annotation_dispatcher,
        )
        selector = getattr(self.workflow.azure_client, "select_for_job", None)
        llm_context = (
            selector(job_id=effective_job_id) if callable(selector) else nullcontext()
        )

        # Best-effort per-stage telemetry into pipeline_runs / pipeline_stages. The
        # recorder is bound via a contextvar so _log_pipeline_timing persists each
        # stage; any failure is swallowed and never affects the generation result.
        recorder = None
        recorder_token = None
        _tel_enabled = pipeline_telemetry_enabled()
        logging.getLogger(__name__).info(
            "pipeline telemetry: armed=%s job=%s", _tel_enabled, effective_job_id)
        if _tel_enabled:
            recorder = PipelineRunRecorder(
                job_id=effective_job_id, modelspec=normalized_modelspec)
            recorder.start()
            recorder_token = set_pipeline_recorder(recorder)
        run_started = time.time()
        try:
            with llm_context:
                output: VideoMusicWorkflowE2EOutput = await self.workflow.generate(workflow_input)
        except Exception as exc:
            if recorder is not None:
                recorder.finish(
                    status="failed",
                    duration_s=time.time() - run_started,
                    music_provider=provider_for_modelspec(normalized_modelspec),
                    error_message=str(exc),
                )
                reset_pipeline_recorder(recorder_token)
            raise
        if recorder is not None:
            recorder.finish(
                status="completed",
                duration_s=time.time() - run_started,
                music_provider=provider_for_modelspec(normalized_modelspec),
                video_summary=(
                    str(output.video_summary)
                    if getattr(output, "video_summary", None) else None
                ),
                music_prompt=getattr(output, "music_prompt", None),
            )
            reset_pipeline_recorder(recorder_token)

        return VideoGenerationResult(
            video_metadata=output.video_metadata,
            scenes=output.scenes,
            video_summary=output.video_summary,
            video_title=output.video_title,
            video_description=output.video_description,
            music_prompt=output.music_prompt,
            music_prompt_in_chinese=getattr(
                output, "music_prompt_in_chinese", None),
            generated_music_path=output.generated_music_path,
            complete_generated_music_path=getattr(
                output, "complete_generated_music_path", None),
            secondary_complete_generated_music_path=getattr(
                output, "secondary_complete_generated_music_path", None),
            remixed_video_path=output.remixed_video_path,
            include_vocals=output.include_vocals,
            vocal_gender=output.vocal_gender,
            lyrics_timestamps=output.lyrics_timestamps,
            word_level_lyrics_timestamps=getattr(
                output,
                "word_level_lyrics_timestamps",
                [],
            ),
            vocal_id_used=getattr(output, "vocal_id_used", None),
            primary_full_lyrics=getattr(output, "primary_full_lyrics", None),
            primary_full_lyrics_timestamps=getattr(
                output,
                "primary_full_lyrics_timestamps",
                [],
            ),
            primary_full_word_level_lyrics_timestamps=getattr(
                output,
                "primary_full_word_level_lyrics_timestamps",
                [],
            ),
            secondary_full_lyrics=getattr(output, "secondary_full_lyrics", None),
            secondary_full_lyrics_timestamps=getattr(
                output,
                "secondary_full_lyrics_timestamps",
                [],
            ),
            secondary_full_word_level_lyrics_timestamps=getattr(
                output,
                "secondary_full_word_level_lyrics_timestamps",
                [],
            ),
            matching_used_track=getattr(output, "matching_used_track", None),
            thumbnail_path=output.thumbnail_path,
            token_usage=output.token_usage,
            token_usage_breakdown=getattr(output, "token_usage_breakdown", None),
            used_music_model_spec=output.UsedMusicModelSpecs,
            user_requested_language=output.user_requested_language,
            job_received_timestamp=getattr(
                output, "job_received_timestamp", None),
            job_finished_timestamp=getattr(
                output, "job_finished_timestamp", None),
            job_id=getattr(output, "job_id", ""),
            video_id=getattr(output, "video_id", ""),
            creative_id=getattr(output, "creative_id", ""),
            primary_music_id=getattr(output, "primary_music_id", ""),
            secondary_music_id=getattr(output, "secondary_music_id", None),
            selected_music_id=getattr(output, "selected_music_id", ""),
            alignment_id=getattr(output, "alignment_id", ""),
            music_start_s=getattr(output, "music_start_s", 0.0),
            alignment_score=getattr(output, "alignment_score", 0.0),
            alignment_details=getattr(output, "alignment_details", {}),
            critical_warning=getattr(output, "critical_warning", None),
            generation_api_call_count=getattr(output, "generation_api_call_count", 1),
            provider_audio_id=getattr(output, "provider_audio_id", None),
            provider_task_id=getattr(output, "provider_task_id", None),
        )

    async def preview_pre_generation(
        self,
        *,
        video_path: Path,
        include_vocals: bool = False,
        vocal_gender: str = "female",
        user_prompt: str = "",
        verbose_instruction: bool = False,
        music_style_prompt: Optional[str] = None,
        lyrics_prompt: Optional[str] = None,
        language: Optional[str] = None,
        modelspec: str = "edenn_basic",
        vocal_id: Optional[str] = None,
        vocal_sample_path: Optional[Path] = None,
        job_id: Optional[str] = None,
        video_id: Optional[str] = None,
        creative_id: Optional[str] = None,
        primary_music_id: Optional[str] = None,
        secondary_music_id: Optional[str] = None,
        selected_music_id: Optional[str] = None,
        alignment_id: Optional[str] = None,
    ) -> VideoPreGenerationResult:
        _ = language
        normalized_modelspec = self._normalize_modelspec(modelspec)
        effective_job_id = job_id or str(uuid4())

        workflow_input = VideoMusicWorkflowE2EInput(
            video_path=str(video_path),
            include_vocals=include_vocals,
            vocal_gender=vocal_gender,
            user_prompt=user_prompt,
            verbose_instruction=verbose_instruction,
            music_style_prompt=music_style_prompt,
            lyrics_prompt=lyrics_prompt,
            music_model_spec=normalized_modelspec,
            vocal_id=vocal_id,
            vocal_sample_path=vocal_sample_path,
            job_id=effective_job_id,
            video_id=video_id,
            creative_id=creative_id,
            primary_music_id=primary_music_id,
            secondary_music_id=secondary_music_id,
            selected_music_id=selected_music_id,
            alignment_id=alignment_id,
            annotation_dispatcher=self.annotation_dispatcher,
        )
        selector = getattr(self.workflow.azure_client, "select_for_job", None)
        llm_context = (
            selector(job_id=effective_job_id) if callable(selector) else nullcontext()
        )
        with llm_context:
            output: VideoMusicPreGenerationOutput = await self.workflow.prepare_before_generation(workflow_input)

        return VideoPreGenerationResult(
            video_metadata=output.video_metadata,
            scenes=output.scenes,
            video_summary=output.video_summary,
            video_title=output.video_title,
            video_description=output.video_description,
            music_prompt=output.music_prompt,
            include_vocals=output.include_vocals,
            vocal_gender=output.vocal_gender,
            token_usage=output.token_usage,
            token_usage_breakdown=output.token_usage_breakdown,
            used_music_model_spec=output.used_music_model_spec,
            user_requested_language=output.user_requested_language,
            sanitized_prompt=output.sanitized_prompt,
            sanitized_style_prompt=output.sanitized_style_prompt,
            sanitized_lyrics_prompt=output.sanitized_lyrics_prompt,
            detected_category=output.detected_category,
            was_transformed=output.was_transformed,
            detected_references=output.detected_references,
            thumbnail_path=output.thumbnail_path,
            job_received_timestamp=output.job_received_timestamp,
            job_finished_timestamp=output.job_finished_timestamp,
            job_id=output.job_id,
            video_id=output.video_id,
            creative_id=output.creative_id,
            primary_music_id=output.primary_music_id,
            secondary_music_id=output.secondary_music_id,
            selected_music_id=output.selected_music_id,
            alignment_id=output.alignment_id,
            stage_timing_s=getattr(output, "stage_timing_s", {}),
        )


__all__ = [
    "VideoGenerationOrchestrator",
    "VideoGenerationResult",
    "VideoPreGenerationResult",
]
