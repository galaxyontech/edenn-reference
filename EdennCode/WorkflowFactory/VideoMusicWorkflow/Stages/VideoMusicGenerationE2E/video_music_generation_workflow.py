import asyncio
import contextvars
import json
import time
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.cloud_music_gen_util import (
    build_edenn_enhanced_music_provider,
    build_music_provider,
)
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_c import ProviderCApi
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata import VideoMetadata
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicGenerationStage.music_generation_stage import MusicGenerationStage, \
    MusicGenerationStageInput, MusicGenerationStageOutput, MusicGenertionModelEnum
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.word_ts import WordTS
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicPromptOrchestrationStage.music_prompt_orchestration_stage import \
    MusicPromptOrchestrationStage, MusicPromptOrchestrationStageInput, MusicPromptOrchestrationStageOutput
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.datamodel import SceneUnderstanding
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.scene_segmentation_stage import SceneSegmentationStage, \
    SceneSegmentationStageInput
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.UserIntentUnderstandingStage.user_prompt_preprocessor import (
    UserPromptPreprocessorAgent,
    UserPromptPreprocessingStageInput,
    _is_chinese_language,
    _merge_verbose_preprocessor_results,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoAudioRemixStage.video_audio_remix_stage import VideoAudioRemixStage, \
    VideoAudioRemixStageInput
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoPreprocessStage.media_preprocess_stage import PreprocessStage, PreprocessStageInput, \
    InputMediaAssetTyps, PreprocessStageOutput, validate_input_video_duration
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoUnderstandingStage.video_understanding_page import VideoUnderstandingStage, \
    VideoUnderstandingStageInput, VideoUnderstandingStageOutput
from EdennCode.Util.MediaUtils import get_video_duration
from EdennCode.Util.MediaUtils.pipeline_util import build_azure_client
from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Deployment.storage import create_storage_service
from EdennCode.env import load_env
from EdennCode.exceptions import EdennValidationError
from EdennCode.Annotation.core.annotation_dispatcher import AnnotationDispatcher
from EdennCode.Annotation.events.music_prompt_event import MusicPromptEvent
from EdennCode.Annotation.events.remix_completion_event import RemixCompletionEvent
import logging


# logging.disable(logging.CRITICAL)
logger = logging.getLogger("Video Music E2E Generation Task")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def _normalize_token_usage(usage: Optional[Dict[str, Any]]) -> Dict[str, int]:
    if not usage:
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
    return {
        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
        "total_tokens": int(usage.get("total_tokens", 0) or 0),
    }


def _sum_token_usage(*usages: Optional[Dict[str, Any]]) -> Dict[str, int]:
    total = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    for usage in usages:
        normalized = _normalize_token_usage(usage)
        total["prompt_tokens"] += normalized["prompt_tokens"]
        total["completion_tokens"] += normalized["completion_tokens"]
        total["total_tokens"] += normalized["total_tokens"]
    return total


# Holds the active pipeline telemetry recorder for the current run. A contextvar
# (not a module global) so concurrent in-process v1 requests stay isolated. The
# recorder is set by the orchestrator around generate(); _log_pipeline_timing then
# persists each stage in addition to logging it.
_active_pipeline_recorder: "contextvars.ContextVar[Any]" = contextvars.ContextVar(
    "_active_pipeline_recorder", default=None
)


def set_pipeline_recorder(recorder: Any) -> Any:
    """Bind a recorder for the current run; returns a token for reset_pipeline_recorder."""
    return _active_pipeline_recorder.set(recorder)


def reset_pipeline_recorder(token: Any) -> None:
    try:
        _active_pipeline_recorder.reset(token)
    except Exception:
        pass


def _log_pipeline_timing(
    stage: str,
    duration_s: float,
    job_id: str,
    profile: str | None = None,
    provider: str | None = None,
    ts_start: float | None = None,
) -> None:
    """Emit a structured JSON timing log and persist the stage for pipeline observability."""
    ts_start_utc = (
        datetime.fromtimestamp(ts_start, tz=timezone.utc).isoformat()
        if ts_start else None
    )
    ts_end_utc = (
        datetime.fromtimestamp(ts_start + duration_s,
                               tz=timezone.utc).isoformat()
        if ts_start else None
    )
    logger.info(
        json.dumps({
            "event": "pipeline_timing",
            "job_id": job_id,
            "stage": stage,
            "duration_s": round(duration_s, 3),
            "profile": profile,
            "provider": provider,
            "ts_start_utc": ts_start_utc,
            "ts_end_utc": ts_end_utc,
        })
    )
    recorder = _active_pipeline_recorder.get()
    if recorder is not None:
        try:
            recorder.record_stage(stage, duration_s, ts_start=ts_start, provider=provider)
        except Exception:
            logger.warning("pipeline telemetry record_stage error", exc_info=True)


@dataclass
class VideoMusicWorkflowE2EInput:
    """
    Input contract for a single end-to-end video-music generation run.

    Attributes
    ----------
    video_path:
        Absolute path to the input video file.
    user_prompt:
        Free-text creative direction from the caller (any language).
    verbose_instruction:
        When ``True``, the caller supplies structured prompt fields instead of
        the legacy mixed ``user_prompt``.
    music_style_prompt:
        Explicit style/music direction used in verbose mode.  It is still
        adapted to scene/video understanding before provider generation.
    lyrics_prompt:
        Optional explicit lyric-generation direction used in verbose mode.
    music_model_spec:
        Requested music generation tier: ``"edenn_basic"``, ``"edenn_enhanced"``,
        or ``"edenn_studio"``.  May be overridden by the pipeline (e.g. Chinese
        vocals force ``"edenn_enhanced"``).
    include_vocals:
        Hint for vocal generation.  Overridden by the UserPromptPreprocessor.
    vocal_gender:
        Hint for vocal gender.  Overridden by the UserPromptPreprocessor.
    preserve_original_audio:
        When ``True``, the original video audio is mixed with generated music
        using ducking.  When ``False``, it is replaced entirely.
    music_volume:
        Gain multiplier for the generated music track.  ``1.0`` = unity gain.
    water_mark:
        When ``True``, append the Edenn spoken watermark to full-track music
        outputs. The matched video-length audio remains unchanged.
    audio_output_format:
        Provider-specific audio format string (e.g. ``"pcm_44100"`` for ProviderA).
        ``None`` uses the provider default.
    vocal_id:
        Pre-existing ProviderB voice-clone ID.  Mutually exclusive with
        *vocal_sample_path*.
    vocal_sample_path:
        Path to an audio sample used to clone a voice via ProviderB.  Mutually
        exclusive with *vocal_id*.
    job_id:
        Optional request identifier supplied by the API.  When present, the
        same value is used for annotation events and recommendation persistence.
    video_id, creative_id, primary_music_id, secondary_music_id, selected_music_id, alignment_id:
        Optional durable serving identifiers.  They are carried through the
        workflow without changing creative generation logic.
    annotation_dispatcher:
        Optional :class:`~EdennCode.Annotation.core.annotation_dispatcher.AnnotationDispatcher`
        instance.  When provided, the pipeline emits a structured annotation
        event at the completion of each stage (fire-and-forget, never blocks).
        When ``None``, the pipeline runs identically with no annotation overhead.
    """

    video_path: str
    music_model_spec: str
    user_prompt: Optional[str] = None
    verbose_instruction: bool = False
    music_style_prompt: Optional[str] = None
    lyrics_prompt: Optional[str] = None
    include_vocals: Optional[bool] = None
    vocal_gender: Optional[str] = None
    preserve_original_audio: bool = False
    music_volume: float = 1.0
    water_mark: bool = False
    audio_output_format: Optional[str] = None
    vocal_id: Optional[str] = None
    vocal_sample_path: Optional[Path] = None
    job_id: Optional[str] = None
    video_id: Optional[str] = None
    creative_id: Optional[str] = None
    primary_music_id: Optional[str] = None
    secondary_music_id: Optional[str] = None
    selected_music_id: Optional[str] = None
    alignment_id: Optional[str] = None
    annotation_dispatcher: Optional[AnnotationDispatcher] = None


@dataclass
class VideoMusicWorkflowE2EOutput:
    video_metadata: VideoMetadata
    scenes: List[SceneUnderstanding]
    video_summary: Any
    music_prompt: Dict[str, Any]
    generated_music_path: Path
    complete_generated_music_path: Optional[Path]
    secondary_complete_generated_music_path: Optional[Path]
    remixed_video_path: Path
    video_title: str
    video_description: str
    include_vocals: bool
    vocal_gender: str
    lyrics_timestamps: List[WordTS]
    word_level_lyrics_timestamps: List[WordTS]
    vocal_id_used: Optional[str]
    primary_full_lyrics: Optional[str]
    primary_full_lyrics_timestamps: List[WordTS]
    primary_full_word_level_lyrics_timestamps: List[WordTS]
    secondary_full_lyrics: Optional[str]
    secondary_full_lyrics_timestamps: List[WordTS]
    secondary_full_word_level_lyrics_timestamps: List[WordTS]
    matching_used_track: Optional[str]
    thumbnail_path: Optional[Path]
    token_usage: Optional[Dict[str, int]]
    token_usage_breakdown: Optional[Dict[str, Dict[str, int]]]
    UsedMusicModelSpecs: str
    user_requested_language: str
    music_prompt_in_chinese: Dict[str, Any]
    job_received_timestamp: int
    job_finished_timestamp: int
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
    # Provider-native handles on the generated track, used by downstream
    # iterative-editing flows (extend / remix / inpaint). Populated for providers
    # that return them (ProviderC audio_id/task_id, ProviderB song task_id); ``None`` for
    # ProviderA, whose Music API returns no addressable track id on this path.
    provider_audio_id: Optional[str] = None
    provider_task_id: Optional[str] = None


@dataclass
class VideoMusicPreGenerationOutput:
    video_metadata: VideoMetadata
    scenes: List[SceneUnderstanding]
    video_summary: Any
    video_title: str
    video_description: str
    music_prompt: Dict[str, Any]
    include_vocals: bool
    vocal_gender: str
    used_music_model_spec: str
    user_requested_language: str
    sanitized_prompt: str
    sanitized_style_prompt: Optional[str]
    sanitized_lyrics_prompt: Optional[str]
    detected_category: str
    was_transformed: bool
    detected_references: List[str]
    thumbnail_path: Optional[Path]
    token_usage: Dict[str, int]
    token_usage_breakdown: Dict[str, Dict[str, int]]
    job_received_timestamp: int
    job_finished_timestamp: int
    job_id: str = ""
    video_id: str = ""
    creative_id: str = ""
    primary_music_id: str = ""
    secondary_music_id: Optional[str] = None
    selected_music_id: str = ""
    alignment_id: str = ""
    stage_timing_s: Dict[str, float] = field(default_factory=dict)


class VideoMusicWorkflowE2E:
    @staticmethod
    def _normalize_modelspec(modelspec: Optional[str]) -> str:
        normalized = (modelspec or "").strip().lower()
        if normalized in {
            MusicGenertionModelEnum.EDENN_BASIC,
            MusicGenertionModelEnum.EDENN_ENHANCED,
            MusicGenertionModelEnum.EDENN_STUDIO,
        }:
            return normalized
        return MusicGenertionModelEnum.EDENN_BASIC

    @staticmethod
    def _external_modelspec_name(modelspec: Optional[str]) -> str:
        return VideoMusicWorkflowE2E._normalize_modelspec(modelspec)

    @staticmethod
    def _should_force_provider_b_for_chinese_lyrics(
        language: Optional[str],
        include_vocals: bool,
        modelspec: Optional[str],
    ) -> bool:
        return (
            include_vocals
            and _is_chinese_language(language)
            and VideoMusicWorkflowE2E._normalize_modelspec(modelspec)
            == MusicGenertionModelEnum.EDENN_BASIC
        )

    _is_chinese_language = staticmethod(_is_chinese_language)
    _merge_verbose_preprocessor_results = staticmethod(
        _merge_verbose_preprocessor_results
    )

    def __init__(
        self,
        *,
        storage_service: Any = None,
        llm_image_container: Optional[str] = None,
        llm_image_sas_ttl_minutes: int = 5,
        llm_image_cleanup_delay_seconds: float = 2.0,
    ):
        self.azure_client = build_azure_client()
        self.video_asset_preprocess: PreprocessStage = PreprocessStage()
        self.scene_segmentation_stage: SceneSegmentationStage = SceneSegmentationStage(
            scene_detector="pyscenedetect",
            llm_model_client=self.azure_client,
            storage_service=storage_service,
            llm_image_container=llm_image_container,
            llm_image_sas_ttl_minutes=llm_image_sas_ttl_minutes,
            llm_image_cleanup_delay_seconds=llm_image_cleanup_delay_seconds,
        )
        self.video_understanding_stage: VideoUnderstandingStage = VideoUnderstandingStage(
            llm_client=self.azure_client)

        self.provider_a_music_provider = None
        self.provider_c_music_provider = None
        self.provider_b_music_provider = None

        def _build_provider_a_music_provider():
            if self.provider_a_music_provider is None:
                self.provider_a_music_provider = build_music_provider()
            return self.provider_a_music_provider

        def _build_provider_c_music_provider():
            if self.provider_c_music_provider is None:
                self.provider_c_music_provider = ProviderCApi()
            return self.provider_c_music_provider

        def _build_provider_b_music_provider():
            if self.provider_b_music_provider is None:
                self.provider_b_music_provider = build_edenn_enhanced_music_provider()
            return self.provider_b_music_provider

        self.music_generation_stage: MusicGenerationStage = MusicGenerationStage(
            provider_a_music_provider_factory=_build_provider_a_music_provider,
            provider_c_music_provider_factory=_build_provider_c_music_provider,
            provider_b_music_provider_factory=_build_provider_b_music_provider,
        )
        self.music_prompt_orchestration_stage = MusicPromptOrchestrationStage(
            llm_client=self.azure_client)

        self.post_generation_remix: VideoAudioRemixStage = VideoAudioRemixStage()
        self.user_intent_understanding_stage = UserPromptPreprocessorAgent(
            llm_client=self.azure_client)

    async def prepare_before_generation(
        self,
        stage_input: VideoMusicWorkflowE2EInput,
    ) -> VideoMusicPreGenerationOutput:
        job_received_timestamp = int(time.time())
        overall_start_time = time.time()
        stage_timing_s: Dict[str, float] = {}

        job_id: str = stage_input.job_id or str(uuid.uuid4())
        video_id: str = stage_input.video_id or str(uuid.uuid4())
        creative_id: str = stage_input.creative_id or str(uuid.uuid4())
        primary_music_id: str = stage_input.primary_music_id or str(uuid.uuid4())
        secondary_music_id: Optional[str] = stage_input.secondary_music_id
        selected_music_id: str = stage_input.selected_music_id or primary_music_id
        alignment_id: str = stage_input.alignment_id or str(uuid.uuid4())
        _dispatcher: Optional[AnnotationDispatcher] = stage_input.annotation_dispatcher

        logger.info("Pre-generation preview start (job_id=%s)", job_id)
        logger.info(f"Input Video Path: {stage_input.video_path}")
        logger.info(f"User Prompt: {stage_input.user_prompt}")

        requested_modelspec = self._normalize_modelspec(
            stage_input.music_model_spec)
        input_video_path = Path(stage_input.video_path)
        validate_input_video_duration(
            get_video_duration(input_video_path),
            source_path=input_video_path,
        )

        logger.info(
            "================= 0. User Prompt Preprocessing (TOS Compliance + Intent Extraction) ==============")
        preprocessor_start_time = time.time()
        prep = await self.user_intent_understanding_stage.run(
            UserPromptPreprocessingStageInput(
                user_prompt=stage_input.user_prompt,
                verbose_instruction=stage_input.verbose_instruction,
                music_style_prompt=stage_input.music_style_prompt,
                lyrics_prompt=stage_input.lyrics_prompt,
                music_model_spec=requested_modelspec,
                job_id=job_id,
                annotation_dispatcher=_dispatcher,
            )
        )
        sanitized_prompt = prep.sanitized_prompt
        effective_include_vocals = prep.effective_include_vocals
        effective_vocal_gender = prep.effective_vocal_gender
        effective_language = prep.effective_language
        analysis_language = prep.analysis_language

        preprocessor_latency_s = time.time() - preprocessor_start_time
        stage_timing_s["user_intent_s"] = round(preprocessor_latency_s, 3)
        _log_pipeline_timing("user_intent", preprocessor_latency_s, job_id,
                             profile=stage_input.music_model_spec, ts_start=preprocessor_start_time)
        logger.info(
            f"User Intent Understanding stage took {preprocessor_latency_s:.2f}s")
        logger.info(f"Detected Video Category = {prep.detected_category}")
        logger.info(f"Downstream Generation Language = {effective_language}")
        logger.info(f"Internal Analysis Language = {analysis_language}")
        logger.info(f"Detected Include Vocals = {effective_include_vocals}")
        logger.info(
            f"Downstream Model Generation is: {prep.effective_modelspec}")

        if (stage_input.vocal_id or stage_input.vocal_sample_path) and not effective_include_vocals:
            raise EdennValidationError(
                "Vocal clone inputs can only be used when the request resolves to a vocal generation path.",
                public_message="Vocal clone inputs can only be used for vocal edenn_enhanced generation.",
                component="video_music",
                operation="prepare_before_generation",
            )
        if effective_include_vocals:
            logger.info(
                f"Detected Vocal Generation Request, Vocal Gender is = {effective_vocal_gender} vocal language is = {effective_language}")
        else:
            logger.info(
                f"Detected Instrument Generation Request, Vocal Request is = {effective_include_vocals}")
        if prep.was_transformed:
            logger.info(f"User Prompt transformed: {sanitized_prompt}")

        logger.info(
            "================ 1. Preprocess stage input and Extract Video Metadata to be persisted into downstream stage.")
        stage_start_time = time.time()
        preprocess_stage_input = PreprocessStageInput(
            asset_path=stage_input.video_path,
            asset_type=InputMediaAssetTyps.VIDEO,
            job_id=job_id,
            annotation_dispatcher=_dispatcher,
        )
        preprocess_stage_output: PreprocessStageOutput = await self.video_asset_preprocess.run(preprocess_stage_input)
        preprocess_stage_time = time.time() - stage_start_time
        stage_timing_s["video_preprocess_s"] = round(preprocess_stage_time, 3)
        _log_pipeline_timing("video_preprocess", preprocess_stage_time, job_id,
                             profile=stage_input.music_model_spec, ts_start=stage_start_time)
        logger.info(f"Preprocess stage took {preprocess_stage_time:.2f}s")

        logger.info("================= 2. Scene Segmentation Stage")
        scene_segmentation_stage_start_time = time.time()
        scene_segmentation_stage_input = SceneSegmentationStageInput(
            video_path=preprocess_stage_output.video_metadata.path,
            duration=preprocess_stage_output.video_metadata.duration,
            fps=preprocess_stage_output.video_metadata.fps,
            preferred_language=analysis_language,
            job_id=job_id,
            annotation_dispatcher=_dispatcher,
        )
        scene_segmentation_stage_output = await self.scene_segmentation_stage.run(scene_segmentation_stage_input)
        scene_segmentation_time = time.time() - scene_segmentation_stage_start_time
        stage_timing_s["scene_segmentation_s"] = round(scene_segmentation_time, 3)
        _log_pipeline_timing("scene_segmentation", scene_segmentation_time, job_id,
                             profile=stage_input.music_model_spec, ts_start=scene_segmentation_stage_start_time)
        logger.info(
            f"Detected {len(scene_segmentation_stage_output.scene_understanding_messages)} scenes.")

        logger.info(
            "===================== 3. Video Understanding Stage and Music Generation Prompt Routing Stage ")
        video_standing_start_time = time.time()
        video_understanding_stage_input = VideoUnderstandingStageInput(
            list_of_scene=scene_segmentation_stage_output.scene_understanding_messages,
            preferred_language=effective_language,
            job_id=job_id,
            annotation_dispatcher=_dispatcher,
        )
        video_understanding_stage_output: VideoUnderstandingStageOutput = await self.video_understanding_stage.run(video_understanding_stage_input)
        video_understanding_time = time.time() - video_standing_start_time
        stage_timing_s["video_understanding_s"] = round(video_understanding_time, 3)
        _log_pipeline_timing("video_understanding", video_understanding_time, job_id,
                             profile=stage_input.music_model_spec, ts_start=video_standing_start_time)
        logger.info(
            f"Generated video description: {video_understanding_stage_output.video_descriptions}")

        logger.info(
            " ====================== 3.1 Prompt Routing Stage ===================================")
        music_prompt_routing_stage_start_time = time.time()
        routing_modelspec = prep.effective_modelspec
        music_prompt_orchestration_stage_input: MusicPromptOrchestrationStageInput = MusicPromptOrchestrationStageInput(
            list_of_scene=scene_segmentation_stage_output.scene_understanding_messages,
            include_vocals=effective_include_vocals,
            vocal_gender=effective_vocal_gender,
            user_prompt=sanitized_prompt,
            language=effective_language,
            modelspec=routing_modelspec,
            video_summary=video_understanding_stage_output.video_descriptions,
            provider_c_custom_mode=effective_include_vocals,
            verbose_instruction=stage_input.verbose_instruction,
            music_style_prompt=prep.sanitized_style_prompt,
            lyrics_prompt=prep.sanitized_lyrics_prompt,
            job_id=job_id,
            annotation_dispatcher=_dispatcher,
        )
        music_prompt_orchestration_output: MusicPromptOrchestrationStageOutput = await self.music_prompt_orchestration_stage.run(music_prompt_orchestration_stage_input)
        music_generation_prompt = music_prompt_orchestration_output.music_generation_prompt
        music_prompt_latency_s = time.time() - music_prompt_routing_stage_start_time
        stage_timing_s["music_prompt_orchestration_s"] = round(music_prompt_latency_s, 3)
        _log_pipeline_timing("music_prompt_orchestration", music_prompt_latency_s, job_id,
                             profile=stage_input.music_model_spec, ts_start=music_prompt_routing_stage_start_time)
        logger.info(f"Music Generation Prompt {music_generation_prompt}")
        if _dispatcher:
            _dispatcher.emit(MusicPromptEvent(
                job_id=job_id,
                model_spec=routing_modelspec,
                style_prompt=music_generation_prompt.get("style_prompt"),
                lyrics_prompt=music_generation_prompt.get("lyrics_prompt"),
                combined_prompt=music_generation_prompt.get("prompt"),
                include_vocals=effective_include_vocals,
                vocal_gender=effective_vocal_gender,
                generation_language=effective_language,
                stage_latency_s=music_prompt_latency_s,
                token_usage=_normalize_token_usage(
                    music_prompt_orchestration_output.token_usage),
                prompt_dict=dict(music_generation_prompt),
            ))

        token_usage_breakdown = {
            "user_prompt_preprocessor": {
                "prompt_tokens": prep.prompt_tokens,
                "completion_tokens": prep.completion_tokens,
                "total_tokens": prep.tokens_used,
            },
            "scene_understanding": _normalize_token_usage(scene_segmentation_stage_output.token_usage),
            "video_summary": _normalize_token_usage(video_understanding_stage_output.token_usage),
            "music_prompt_orchestration": _normalize_token_usage(music_prompt_orchestration_output.token_usage),
        }
        total_usage = _sum_token_usage(*token_usage_breakdown.values())
        job_finished_timestamp = int(time.time())
        stage_timing_s["pre_generation_total_s"] = round(
            max(0.0, time.time() - overall_start_time),
            3,
        )

        return VideoMusicPreGenerationOutput(
            video_metadata=preprocess_stage_output.video_metadata,
            scenes=scene_segmentation_stage_output.scene_understanding_messages,
            video_summary=video_understanding_stage_output.video_descriptions,
            video_title=video_understanding_stage_output.video_title,
            video_description=video_understanding_stage_output.video_description,
            music_prompt=music_generation_prompt,
            include_vocals=effective_include_vocals,
            vocal_gender=effective_vocal_gender,
            used_music_model_spec=routing_modelspec,
            user_requested_language=effective_language,
            sanitized_prompt=sanitized_prompt,
            sanitized_style_prompt=prep.sanitized_style_prompt,
            sanitized_lyrics_prompt=prep.sanitized_lyrics_prompt,
            detected_category=prep.detected_category,
            was_transformed=prep.was_transformed,
            detected_references=list(prep.detected_references),
            thumbnail_path=scene_segmentation_stage_output.thumbnail_path,
            token_usage=total_usage,
            token_usage_breakdown=token_usage_breakdown,
            job_received_timestamp=job_received_timestamp,
            job_finished_timestamp=job_finished_timestamp,
            job_id=job_id,
            video_id=video_id,
            creative_id=creative_id,
            primary_music_id=primary_music_id,
            secondary_music_id=secondary_music_id,
            selected_music_id=selected_music_id,
            alignment_id=alignment_id,
            stage_timing_s=stage_timing_s,
        )

    async def generate(self, stage_input: VideoMusicWorkflowE2EInput) -> VideoMusicWorkflowE2EOutput:
        job_received_timestamp = int(time.time())
        overall_start_time = time.time()

        # One stable identifier shared by every annotation event in this run.
        # The API supplies this so annotation and recommender persistence share
        # the same job namespace; direct workflow callers keep the old fallback.
        job_id: str = stage_input.job_id or str(uuid.uuid4())
        video_id: str = stage_input.video_id or str(uuid.uuid4())
        creative_id: str = stage_input.creative_id or str(uuid.uuid4())
        primary_music_id: str = stage_input.primary_music_id or str(
            uuid.uuid4())
        secondary_music_id: Optional[str] = stage_input.secondary_music_id
        selected_music_id: str = stage_input.selected_music_id or primary_music_id
        alignment_id: str = stage_input.alignment_id or str(uuid.uuid4())
        _dispatcher: Optional[AnnotationDispatcher] = stage_input.annotation_dispatcher

        logger.info("Pipeline Start (job_id=%s)", job_id)
        logger.info(f"Input Video Path: {stage_input.video_path}")
        logger.info(f"User Prompt: {stage_input.user_prompt}")

        requested_modelspec = self._normalize_modelspec(
            stage_input.music_model_spec)
        input_video_path = Path(stage_input.video_path)
        validate_input_video_duration(
            get_video_duration(input_video_path),
            source_path=input_video_path,
        )

        """================= 0. User Prompt Preprocessing (TOS Compliance + Intent Extraction) =============="""
        logger.info(
            "================= 0. User Prompt Preprocessing (TOS Compliance + Intent Extraction) ==============")
        preprocessor_start_time = time.time()

        prep = await self.user_intent_understanding_stage.run(
            UserPromptPreprocessingStageInput(
                user_prompt=stage_input.user_prompt,
                verbose_instruction=stage_input.verbose_instruction,
                music_style_prompt=stage_input.music_style_prompt,
                lyrics_prompt=stage_input.lyrics_prompt,
                music_model_spec=requested_modelspec,
                job_id=job_id,
                annotation_dispatcher=_dispatcher,
            )
        )
        sanitized_prompt = prep.sanitized_prompt
        effective_include_vocals = prep.effective_include_vocals
        effective_vocal_gender = prep.effective_vocal_gender
        effective_language = prep.effective_language
        analysis_language = prep.analysis_language

        preprocessor_latency_s = time.time() - preprocessor_start_time
        _log_pipeline_timing("user_intent", preprocessor_latency_s, job_id,
                             profile=stage_input.music_model_spec, ts_start=preprocessor_start_time)
        logger.info(
            f"User Intent Understanding stage took {preprocessor_latency_s:.2f}s")
        logger.info(f"Detected Video Category = {prep.detected_category}")
        logger.info(f"Downstream Generation Language = {effective_language}")
        logger.info(f"Internal Analysis Language = {analysis_language}")
        logger.info(f"Detected Include Vocals = {effective_include_vocals}")
        logger.info(
            f"Downstream Model Generation is: {prep.effective_modelspec}")

        if (stage_input.vocal_id or stage_input.vocal_sample_path) and not effective_include_vocals:
            raise EdennValidationError(
                "Vocal clone inputs can only be used when the request resolves to a vocal generation path.",
                public_message="Vocal clone inputs can only be used for vocal edenn_enhanced generation.",
                component="video_music",
                operation="generate",
            )
        if effective_include_vocals:
            logger.info(
                f"Detected Vocal Generation Request, Vocal Gender is = {effective_vocal_gender} vocal language is = {effective_language}")
        else:
            logger.info(
                f"Detected Instrument Generation Request, Vocal Request is = {effective_include_vocals}")
        if prep.was_transformed:
            logger.info(f"User Prompt transformed: {sanitized_prompt}")

        """ ================ 1. Preprocess stage input and Extract Video Metadata to be persisted into downstream stage."""
        logger.info(
            "================ 1. Preprocess stage input and Extract Video Metadata to be persisted into downstream stage.")
        stage_start_time = time.time()
        preprocess_stage_input = PreprocessStageInput(
            asset_path=stage_input.video_path,
            asset_type=InputMediaAssetTyps.VIDEO,
            job_id=job_id,
            annotation_dispatcher=_dispatcher,
        )
        preprocess_stage_output: PreprocessStageOutput = await self.video_asset_preprocess.run(preprocess_stage_input)
        preprocess_stage_time = time.time() - stage_start_time
        _log_pipeline_timing("video_preprocess", preprocess_stage_time, job_id,
                             profile=stage_input.music_model_spec, ts_start=stage_start_time)
        logger.info(f"Preprocess stage took {preprocess_stage_time:.2f}s")

        """================== 2. Scene Segmentation Stage"""
        logger.info("================= 2. Scene Segmentation Stage")
        scene_segmentation_stage_start_time = time.time()
        scene_segmentation_stage_input = SceneSegmentationStageInput(
            video_path=preprocess_stage_output.video_metadata.path,
            duration=preprocess_stage_output.video_metadata.duration,
            fps=preprocess_stage_output.video_metadata.fps,
            preferred_language=analysis_language,
            job_id=job_id,
            annotation_dispatcher=_dispatcher,
        )
        scene_segmentation_stage_output = await self.scene_segmentation_stage.run(scene_segmentation_stage_input)
        scene_segmentation_time = time.time() - scene_segmentation_stage_start_time
        _log_pipeline_timing("scene_segmentation", scene_segmentation_time, job_id,
                             profile=stage_input.music_model_spec, ts_start=scene_segmentation_stage_start_time)
        logger.info(
            f"Detected {len(scene_segmentation_stage_output.scene_understanding_messages)} scenes.")

        """
        ===================== 3. Video Understanding Stage and Music Generation Prompt Routing Stage 
        """
        logger.info(
            "===================== 3. Video Understanding Stage and Music Generation Prompt Routing Stage ")
        video_standing_start_time = time.time()

        video_understanding_stage_input = VideoUnderstandingStageInput(
            list_of_scene=scene_segmentation_stage_output.scene_understanding_messages,
            preferred_language=effective_language,
            job_id=job_id,
            annotation_dispatcher=_dispatcher,
        )
        video_understanding_stage_output: VideoUnderstandingStageOutput = await self.video_understanding_stage.run(video_understanding_stage_input)
        video_understanding_time = time.time() - video_standing_start_time
        _log_pipeline_timing("video_understanding", video_understanding_time, job_id,
                             profile=stage_input.music_model_spec, ts_start=video_standing_start_time)
        logger.info(
            f"Generated video description: {video_understanding_stage_output.video_descriptions}")
        """
        ====================== 3.1 Prompt Routing Stage ===================================
        """
        logger.info(
            " ====================== 3.1 Prompt Routing Stage ===================================")

        music_prompt_routing_stage_start_time = time.time()

        routing_modelspec = prep.effective_modelspec

        music_prompt_orchestration_stage_input: MusicPromptOrchestrationStageInput = MusicPromptOrchestrationStageInput(
            list_of_scene=scene_segmentation_stage_output.scene_understanding_messages,
            include_vocals=effective_include_vocals,
            vocal_gender=effective_vocal_gender,
            user_prompt=sanitized_prompt,
            language=effective_language,
            modelspec=routing_modelspec,
            video_summary=video_understanding_stage_output.video_descriptions,
            provider_c_custom_mode=effective_include_vocals,
            verbose_instruction=stage_input.verbose_instruction,
            music_style_prompt=prep.sanitized_style_prompt,
            lyrics_prompt=prep.sanitized_lyrics_prompt,
            job_id=job_id,
            annotation_dispatcher=_dispatcher,
        )
        music_prompt_orchestration_output: MusicPromptOrchestrationStageOutput = await self.music_prompt_orchestration_stage.run(music_prompt_orchestration_stage_input)
        music_generation_prompt = music_prompt_orchestration_output.music_generation_prompt
        music_prompt_routing_stage_end_time = time.time()
        music_prompt_latency_s = music_prompt_routing_stage_end_time - \
            music_prompt_routing_stage_start_time
        _log_pipeline_timing("music_prompt_orchestration", music_prompt_latency_s, job_id,
                             profile=stage_input.music_model_spec, ts_start=music_prompt_routing_stage_start_time)
        logger.info(f"Music Generation Prompt {music_generation_prompt}")
        if _dispatcher:
            _dispatcher.emit(MusicPromptEvent(
                job_id=job_id,
                model_spec=routing_modelspec,
                style_prompt=music_generation_prompt.get("style_prompt"),
                lyrics_prompt=music_generation_prompt.get("lyrics_prompt"),
                combined_prompt=music_generation_prompt.get("prompt"),
                include_vocals=effective_include_vocals,
                vocal_gender=effective_vocal_gender,
                generation_language=effective_language,
                stage_latency_s=music_prompt_latency_s,
                token_usage=_normalize_token_usage(
                    music_prompt_orchestration_output.token_usage),
                prompt_dict=dict(music_generation_prompt),
            ))

        """
        ===================== 4. Music Generation Stage  ==============================
        """
        music_generation_stage_time = time.time()
        normalized_model = routing_modelspec
        logger.info(f"Resolved music model spec: {normalized_model}")
        video_descriptions = video_understanding_stage_output.video_descriptions
        if isinstance(video_descriptions, dict):
            overall_mood = video_descriptions.get("overall_mood") or ""
        else:
            overall_mood = ""

        music_generation_stage_input = MusicGenerationStageInput(
            music_generation_prompt,
            video_metadata=preprocess_stage_output.video_metadata,
            include_vocals=effective_include_vocals,
            vocal_gender=effective_vocal_gender,
            music_generation_model=normalized_model,
            provider_c_custom_mode=effective_include_vocals,
            audio_output_format=stage_input.audio_output_format,
            lyrics_language=effective_language,
            vocal_id=stage_input.vocal_id,
            vocal_sample_path=stage_input.vocal_sample_path,
            job_id=job_id,
            annotation_dispatcher=_dispatcher,
            video_category=prep.detected_category,
            scene_count=len(
                scene_segmentation_stage_output.scene_understanding_messages),
            overall_mood=overall_mood,
            water_mark=stage_input.water_mark,
        )

        music_generation_stage_output: MusicGenerationStageOutput = await self.music_generation_stage.run(music_generation_stage_input)
        music_gen_latency_s = time.time() - music_generation_stage_time
        _log_pipeline_timing("music_generation", music_gen_latency_s, job_id,
                             profile=stage_input.music_model_spec, ts_start=music_generation_stage_time)
        logger.info(f"Music generation stage took {music_gen_latency_s:.2f}s")

        # Matching stage for Chinese vocals after ProviderC generation
        music_path_for_remix = music_generation_stage_output.music_path

        """
        Video + Audio Remix Stage
        """
        video_audio_remix_stage_input: VideoAudioRemixStageInput = VideoAudioRemixStageInput(
            preserve_original_audio=stage_input.preserve_original_audio,
            music_volume=stage_input.music_volume,
            music_path=music_path_for_remix,
            video_metadata=preprocess_stage_output.video_metadata,
            job_id=job_id,
            annotation_dispatcher=_dispatcher,
            pipeline_start_time=overall_start_time,
        )
        remix_stage_start = time.time()
        video_audio_remix_stage_output = await self.post_generation_remix.run(video_audio_remix_stage_input)
        remix_latency_s = time.time() - remix_stage_start
        total_pipeline_latency_s = time.time() - overall_start_time
        _log_pipeline_timing("video_audio_remix", remix_latency_s, job_id,
                             profile=stage_input.music_model_spec, ts_start=remix_stage_start)
        _log_pipeline_timing("total", total_pipeline_latency_s, job_id,
                             profile=stage_input.music_model_spec, ts_start=overall_start_time)
        logger.info(f"Video remix output: {video_audio_remix_stage_output}")
        if _dispatcher:
            _dispatcher.emit(RemixCompletionEvent(
                job_id=job_id,
                remixed_video_filename=video_audio_remix_stage_output.remixed_video_path,
                preserve_original_audio=stage_input.preserve_original_audio,
                music_volume=stage_input.music_volume,
                stage_latency_s=remix_latency_s,
                total_pipeline_latency_s=total_pipeline_latency_s,
            ))

        remixed_video_path = Path(preprocess_stage_output.video_metadata.temp_folder) / \
            video_audio_remix_stage_output.remixed_video_path

        token_usage_breakdown = {
            "user_prompt_preprocessor": {
                "prompt_tokens": prep.prompt_tokens,
                "completion_tokens": prep.completion_tokens,
                "total_tokens": prep.tokens_used,
            },
            "scene_understanding": _normalize_token_usage(scene_segmentation_stage_output.token_usage),
            "video_summary": _normalize_token_usage(video_understanding_stage_output.token_usage),
            "music_prompt_orchestration": _normalize_token_usage(music_prompt_orchestration_output.token_usage),
        }
        total_usage = _sum_token_usage(*token_usage_breakdown.values())

        job_finished_timestamp = int(time.time())

        return VideoMusicWorkflowE2EOutput(
            video_metadata=preprocess_stage_output.video_metadata,
            scenes=scene_segmentation_stage_output.scene_understanding_messages,
            video_summary=video_understanding_stage_output.video_descriptions,
            video_title=video_understanding_stage_output.video_title,
            video_description=video_understanding_stage_output.video_description,
            music_prompt=music_generation_prompt,
            music_prompt_in_chinese=music_generation_prompt,
            generated_music_path=music_generation_stage_output.music_path,
            complete_generated_music_path=music_generation_stage_output.complete_music_path,
            secondary_complete_generated_music_path=music_generation_stage_output.secondary_complete_music_path,
            remixed_video_path=remixed_video_path,
            include_vocals=effective_include_vocals,
            vocal_gender=effective_vocal_gender,
            lyrics_timestamps=music_generation_stage_output.lyrics_timestamps,
            word_level_lyrics_timestamps=music_generation_stage_output.word_level_lyrics_timestamps,
            vocal_id_used=music_generation_stage_output.vocal_id_used,
            primary_full_lyrics=music_generation_stage_output.primary_full_lyrics,
            primary_full_lyrics_timestamps=music_generation_stage_output.primary_full_lyrics_timestamps,
            primary_full_word_level_lyrics_timestamps=music_generation_stage_output.primary_full_word_level_lyrics_timestamps,
            secondary_full_lyrics=music_generation_stage_output.secondary_full_lyrics,
            secondary_full_lyrics_timestamps=music_generation_stage_output.secondary_full_lyrics_timestamps,
            secondary_full_word_level_lyrics_timestamps=music_generation_stage_output.secondary_full_word_level_lyrics_timestamps,
            matching_used_track=music_generation_stage_output.matching_used_track,
            thumbnail_path=scene_segmentation_stage_output.thumbnail_path,
            token_usage=total_usage,
            token_usage_breakdown=token_usage_breakdown,
            UsedMusicModelSpecs=normalized_model,
            user_requested_language=effective_language,
            job_received_timestamp=job_received_timestamp,
            job_finished_timestamp=job_finished_timestamp,
            job_id=job_id,
            video_id=video_id,
            creative_id=creative_id,
            primary_music_id=primary_music_id,
            secondary_music_id=(
                secondary_music_id
                if music_generation_stage_output.secondary_complete_music_path
                else None
            ),
            selected_music_id=selected_music_id,
            alignment_id=alignment_id,
            music_start_s=music_generation_stage_output.music_start_s,
            alignment_score=music_generation_stage_output.alignment_score,
            alignment_details=dict(
                music_generation_stage_output.alignment_details),
            critical_warning=music_generation_stage_output.critical_warning,
            generation_api_call_count=music_generation_stage_output.generation_api_call_count,
            provider_audio_id=music_generation_stage_output.audio_id,
            provider_task_id=music_generation_stage_output.task_id,
        )


if __name__ == "__main__":

    workflow_input_2 = VideoMusicWorkflowE2EInput(
        video_path="/path/to/repo/sample_clip.mp4",
        user_prompt="中文歌曲",
        audio_output_format="pcm_44100",
        music_model_spec=MusicGenertionModelEnum.EDENN_BASIC,

    )
    workflow_input_3 = VideoMusicWorkflowE2EInput(
        video_path="/path/to/repo/slideshow.mp4",
        music_style_prompt="中文男生",
        lyrics_prompt="热血沸腾,必须有“我爱中华",
        music_model_spec=MusicGenertionModelEnum.EDENN_ENHANCED,
        verbose_instruction=True

    )
    load_env()
    settings = DeploymentSettings.from_env()
    storage = create_storage_service(settings)

    workflow_input = workflow_input_3  # switch to workflow_input_2 to test the other
    video_music_workflow = VideoMusicWorkflowE2E(
        storage_service=storage,
        llm_image_container=settings.llm_image_container,
        llm_image_sas_ttl_minutes=settings.llm_image_sas_ttl_minutes,
        llm_image_cleanup_delay_seconds=settings.llm_image_cleanup_delay_seconds,
    )
    work_flow_output = asyncio.run(
        video_music_workflow.generate(workflow_input))

    logger.info(f"Workflow output: {work_flow_output}")
