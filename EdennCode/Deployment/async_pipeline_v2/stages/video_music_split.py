from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Optional

from EdennCode.Deployment.async_pipeline_v2.cache_service import (
    KV_UNDERSTANDING,
    analysis_inputs_digest,
    understanding_cache_key,
)
from EdennCode.Deployment.workflows import VideoGenerationResult
from EdennCode.Util.MediaUtils.ffmpeg_utils import resolve_ffmpeg_binary
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata import VideoMetadata
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicGenerationStage.music_generation_stage import (
    FullTrackLyricsData,
    MusicGenertionModelEnum,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicGenerationStage.music_generation_stage_utils import (
    align_line_level_lyrics_to_window,
    strip_section_tags,
    to_ms_wordts,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.music_matching_stage import (
    MusicMatchingStage,
    MusicMatchingStageInput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.word_ts import WordTS
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.datamodel import (
    SceneUnderstanding,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.scene_segmentation_stage import (
    SceneSegmentationStageInput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.UserIntentUnderstandingStage.user_prompt_preprocessor import (
    UserPromptPreprocessingStageInput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoAudioRemixStage.video_audio_remix_stage import (
    VideoAudioRemixStageInput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow import (
    VideoMusicWorkflowE2E,
    _normalize_token_usage,
    _sum_token_usage,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoPreprocessStage.media_preprocess_stage import (
    InputMediaAssetTyps,
    PreprocessStageInput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoUnderstandingStage.video_understanding_page import (
    VideoUnderstandingStageInput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicPromptOrchestrationStage.music_prompt_orchestration_stage import (
    MusicPromptOrchestrationStageInput,
)
from EdennCode.exceptions import EdennValidationError


logger = logging.getLogger(__name__)


def _fake_provider_enabled() -> bool:
    """Performance-test switch: replace real music generation with a canned audio."""
    return (os.getenv("ASYNC_V2_FAKE_PROVIDER") or "").strip().lower() in {
        "1", "true", "yes", "on", "y"}


def _fake_provider_delay_s() -> float:
    """Simulated generation delay (seconds) for the fake provider; default 0."""
    raw = (os.getenv("ASYNC_V2_FAKE_PROVIDER_DELAY_MS") or "").strip()
    try:
        return max(0.0, float(raw) / 1000.0)
    except ValueError:
        return 0.0


@dataclass(frozen=True)
class VideoPreprocessStageInput:
    """Input contract for the split video-preprocess stage.

    The stage receives the source path already selected by the worker. That
    path may be the original staged video or a compressed source artifact. The
    stage itself deliberately runs only the existing media preprocess component
    and does not perform prompt analysis, scene understanding, provider calls,
    or remixing.
    """

    job_id: str
    request_json: dict[str, Any]
    source_video_artifact_id: str
    prepared_source_video_artifact_id: str
    source_video_path: Path
    compression_info: dict[str, Any]


@dataclass(frozen=True)
class VideoPreprocessStageOutput:
    """Durable output from split video preprocessing.

    This output is safe to serialize between workers. It carries the artifact
    that downstream stages should use plus the exact `VideoMetadata` produced by
    the existing preprocess stage. Reusing this value avoids rerunning ffprobe,
    duration validation, and audio-activity detection in the normal-CPU analysis
    worker.
    """

    job_id: str
    source_video_artifact_id: str
    prepared_source_video_artifact_id: str
    video_metadata: VideoMetadata
    compression_info: dict[str, Any]


@dataclass(frozen=True)
class AnalysisAndPlanningStageInput:
    job_id: str
    request_json: dict[str, Any]
    source_video_artifact_id: str
    source_video_path: Path
    preprocess: Optional[VideoPreprocessStageOutput] = None
    # Optional content-addressed cache for the prompt-independent video understanding
    # (scene segmentation + video understanding). The worker injects the cache and the
    # source content identity; when absent the stage runs understanding as before.
    understanding_cache: Any = None
    cache_source_sha: Optional[str] = None
    cache_max_height: Optional[int] = None


@dataclass(frozen=True)
class AnalysisAndPlanningStageOutput:
    job_id: str
    source_video_artifact_id: str
    video_metadata: VideoMetadata
    scenes: list[SceneUnderstanding]
    video_summary: Any
    video_title: str
    video_description: str
    music_prompt: dict[str, Any]
    effective_modelspec: str
    include_vocals: bool
    vocal_gender: str
    user_requested_language: str
    detected_category: str
    overall_mood: str
    thumbnail_path: Optional[Path]
    token_usage_breakdown: dict[str, dict[str, int]]
    token_usage: dict[str, int]
    job_received_timestamp: int
    understanding_cache_state: str = "bypass"


@dataclass(frozen=True)
class ProviderCandidateGenerationStageInput:
    job_id: str
    request_json: dict[str, Any]
    # Provider generation is video-free. This path is kept for compatibility
    # with existing provider APIs that expect a VideoMetadata object, but it may
    # point to a metadata-only placeholder rather than a readable video file.
    source_video_path: Path
    analysis: AnalysisAndPlanningStageOutput
    provider_task_recorder: Optional[Callable[[dict[str, Any]], Any]] = None
    resume_provider_task_id: Optional[str] = None
    # Recorded alongside the task id: task ids are account-scoped, so a resumed
    # poll must reuse the creating key (and gateway base) or it hits a
    # non-owner account and sees a silent-empty record.
    resume_provider_key_label: Optional[str] = None
    resume_provider_base: Optional[str] = None


@dataclass(frozen=True)
class ProviderCandidateGenerationStageOutput:
    job_id: str
    model_spec: str
    candidate_audio_path: Path
    complete_audio_path: Optional[Path]
    secondary_complete_audio_path: Optional[Path]
    primary_full_lyrics: Optional[str]
    primary_full_lyrics_timestamps: list[WordTS] = field(default_factory=list)
    primary_full_word_level_lyrics_timestamps: list[WordTS] = field(default_factory=list)
    secondary_full_lyrics: Optional[str] = None
    secondary_full_lyrics_timestamps: list[WordTS] = field(default_factory=list)
    secondary_full_word_level_lyrics_timestamps: list[WordTS] = field(default_factory=list)
    provider_task_id: Optional[str] = None
    provider_audio_id: Optional[str] = None
    vocal_id_used: Optional[str] = None
    critical_warning: Optional[str] = None
    generation_api_call_count: int = 1


@dataclass(frozen=True)
class SelectionRankingRemixFinalizeStageInput:
    job_id: str
    request_json: dict[str, Any]
    source_video_path: Path
    analysis: AnalysisAndPlanningStageOutput
    candidate: ProviderCandidateGenerationStageOutput


@dataclass(frozen=True)
class SelectionRankingRemixFinalizeStageOutput:
    job_id: str
    result: VideoGenerationResult


class VideoMusicSplitStageRuntime:
    def __init__(
        self,
        *,
        storage_service: Any = None,
        llm_image_container: Optional[str] = None,
        llm_image_sas_ttl_minutes: int = 5,
        llm_image_cleanup_delay_seconds: float = 2.0,
    ) -> None:
        self.workflow = VideoMusicWorkflowE2E(
            storage_service=storage_service,
            llm_image_container=llm_image_container,
            llm_image_sas_ttl_minutes=llm_image_sas_ttl_minutes,
            llm_image_cleanup_delay_seconds=llm_image_cleanup_delay_seconds,
        )
        self.music_matching_stage = MusicMatchingStage()

    def llm_context(self, *, job_id: str):
        selector = getattr(self.workflow.azure_client, "select_for_job", None)
        return selector(job_id=job_id) if callable(selector) else nullcontext()


class VideoMusicPreprocessStage:
    """Run source-video media preprocessing for the split workflow.

    Compression and artifact selection are handled by the worker-side source
    preparation service because they are deployment I/O concerns. This stage
    preserves the core business logic by invoking the same
    `workflow.video_asset_preprocess.run()` call that monolith and the original
    split analysis stage used.
    """

    def __init__(self, runtime: VideoMusicSplitStageRuntime) -> None:
        self.runtime = runtime

    async def run(
        self,
        stage_input: VideoPreprocessStageInput,
    ) -> VideoPreprocessStageOutput:
        preprocess_output = await self.runtime.workflow.video_asset_preprocess.run(
            PreprocessStageInput(
                asset_path=stage_input.source_video_path,
                asset_type=InputMediaAssetTyps.VIDEO,
                job_id=stage_input.job_id,
            )
        )
        return VideoPreprocessStageOutput(
            job_id=stage_input.job_id,
            source_video_artifact_id=stage_input.source_video_artifact_id,
            prepared_source_video_artifact_id=stage_input.prepared_source_video_artifact_id,
            video_metadata=preprocess_output.video_metadata,
            compression_info=stage_input.compression_info,
        )


class VideoMusicAnalysisAndPlanningStage:
    """Run prompt, scene, video-understanding, and music-planning logic.

    When a `VideoPreprocessStageOutput` is supplied, the stage reuses its video
    metadata instead of rerunning the CPU-heavy media-preprocess component.
    Everything else stays in the same order as the existing workflow: prompt
    preprocessing first, scene segmentation with the resolved analysis language,
    video understanding, then music prompt orchestration.
    """

    def __init__(self, runtime: VideoMusicSplitStageRuntime) -> None:
        self.runtime = runtime

    async def run(
        self,
        stage_input: AnalysisAndPlanningStageInput,
    ) -> AnalysisAndPlanningStageOutput:
        workflow = self.runtime.workflow
        request = stage_input.request_json
        job_id = stage_input.job_id
        job_received_timestamp = int(request.get("job_received_timestamp") or time.time())
        requested_modelspec = workflow._normalize_modelspec(
            request.get("modelspec") or request.get("music_model_spec")
        )

        with self.runtime.llm_context(job_id=job_id):
            prep = await workflow.user_intent_understanding_stage.run(
                UserPromptPreprocessingStageInput(
                    user_prompt=request.get("user_prompt") or "",
                    verbose_instruction=bool(request.get("verbose_instruction", False)),
                    music_style_prompt=request.get("music_style_prompt") or None,
                    lyrics_prompt=request.get("lyrics_prompt") or None,
                    music_model_spec=requested_modelspec,
                    job_id=job_id,
                )
            )

            if (request.get("vocal_id") or request.get("vocal_sample_path")) and not prep.effective_include_vocals:
                raise EdennValidationError(
                    "Vocal clone inputs can only be used when the request resolves to a vocal generation path.",
                    public_message="Vocal clone inputs can only be used for vocal edenn_enhanced generation.",
                    component="video_music",
                    operation="analysis_and_planning",
                )

            if stage_input.preprocess is None:
                preprocess_output = await workflow.video_asset_preprocess.run(
                    PreprocessStageInput(
                        asset_path=stage_input.source_video_path,
                        asset_type=InputMediaAssetTyps.VIDEO,
                        job_id=job_id,
                    )
                )
                video_metadata = preprocess_output.video_metadata
            else:
                video_metadata = stage_input.preprocess.video_metadata

            (
                scenes,
                video_descriptions,
                video_title,
                video_description,
                scene_token_usage,
                video_understanding_token_usage,
                thumbnail_path,
                understanding_cache_state,
            ) = await self._resolve_understanding(
                workflow=workflow,
                stage_input=stage_input,
                prep=prep,
                video_metadata=video_metadata,
                job_id=job_id,
            )

            overall_mood = (
                video_descriptions.get("overall_mood") or ""
                if isinstance(video_descriptions, dict)
                else ""
            )
            music_prompt_output = await workflow.music_prompt_orchestration_stage.run(
                MusicPromptOrchestrationStageInput(
                    list_of_scene=scenes,
                    include_vocals=prep.effective_include_vocals,
                    vocal_gender=prep.effective_vocal_gender,
                    user_prompt=prep.sanitized_prompt,
                    language=prep.effective_language,
                    provider_c_custom_mode=prep.effective_include_vocals,
                    modelspec=prep.effective_modelspec,
                    video_summary=video_descriptions,
                    verbose_instruction=bool(request.get("verbose_instruction", False)),
                    music_style_prompt=prep.sanitized_style_prompt,
                    lyrics_prompt=prep.sanitized_lyrics_prompt,
                    job_id=job_id,
                )
            )

        token_usage_breakdown = {
            "user_prompt_preprocessor": {
                "prompt_tokens": int(prep.prompt_tokens or 0),
                "completion_tokens": int(prep.completion_tokens or 0),
                "total_tokens": int(prep.tokens_used or 0),
            },
            "scene_understanding": _normalize_token_usage(scene_token_usage),
            "video_summary": _normalize_token_usage(video_understanding_token_usage),
            "music_prompt_orchestration": _normalize_token_usage(music_prompt_output.token_usage),
        }

        return AnalysisAndPlanningStageOutput(
            job_id=job_id,
            source_video_artifact_id=stage_input.source_video_artifact_id,
            video_metadata=video_metadata,
            scenes=scenes,
            video_summary=video_descriptions,
            video_title=video_title,
            video_description=video_description,
            music_prompt=music_prompt_output.music_generation_prompt,
            effective_modelspec=prep.effective_modelspec,
            include_vocals=prep.effective_include_vocals,
            vocal_gender=prep.effective_vocal_gender,
            user_requested_language=prep.effective_language,
            detected_category=prep.detected_category,
            overall_mood=overall_mood,
            thumbnail_path=thumbnail_path,
            token_usage_breakdown=token_usage_breakdown,
            token_usage=_sum_token_usage(*token_usage_breakdown.values()),
            job_received_timestamp=job_received_timestamp,
            understanding_cache_state=understanding_cache_state,
        )

    async def _resolve_understanding(
        self,
        *,
        workflow: Any,
        stage_input: "AnalysisAndPlanningStageInput",
        prep: Any,
        video_metadata: VideoMetadata,
        job_id: str,
    ) -> tuple[Any, Any, str, str, Any, Any, Optional[Path], str]:
        """Produce scene + video-understanding output, reusing a content cache if able.

        Scene segmentation and video understanding depend on the video content
        (source sha + max_height) and the resolved analysis inputs (language today) —
        NOT the user prompt, which drives the separately-recomputed user-intent and
        music-prompt stages. The cache key digests the exact inputs these stages
        receive, so a new prompt still yields a new result, and the key stays correct
        even if a future change wires the prompt into understanding. Returns a tuple of
        (scenes, video_descriptions, video_title, video_description, scene_token_usage,
        understanding_token_usage, thumbnail_path, cache_state).
        """

        cache = stage_input.understanding_cache
        source_sha = stage_input.cache_source_sha
        max_height = stage_input.cache_max_height

        key: Optional[str] = None
        entry = None
        lease: Optional[str] = None
        if cache is not None and source_sha:
            digest = analysis_inputs_digest(
                {
                    "analysis_language": getattr(prep, "analysis_language", ""),
                    "effective_language": getattr(prep, "effective_language", ""),
                }
            )
            key = understanding_cache_key(
                source_sha=str(source_sha), max_height=max_height, language=digest
            )
            try:
                entry, lease = cache.get_or_lease(
                    key,
                    kind="understanding",
                    content_sha=str(source_sha),
                    key_version=KV_UNDERSTANDING,
                )
            except Exception:
                logger.warning(
                    "Understanding cache lookup failed for %s; computing.", key, exc_info=True
                )
                entry, lease = None, None

        if entry is not None:
            bundle = understanding_bundle_from_json(entry.payload_json)
            return (
                bundle["scenes"],
                bundle["video_descriptions"],
                bundle["video_title"],
                bundle["video_description"],
                {},  # no scene-understanding tokens spent on a cache hit
                {},  # no video-understanding tokens spent on a cache hit
                None,  # thumbnail intentionally not cached; finalize regenerates it
                "hit",
            )

        try:
            scene_output = await workflow.scene_segmentation_stage.run(
                SceneSegmentationStageInput(
                    video_path=video_metadata.path,
                    duration=video_metadata.duration,
                    fps=video_metadata.fps,
                    preferred_language=prep.analysis_language,
                    job_id=job_id,
                )
            )
            video_understanding_output = await workflow.video_understanding_stage.run(
                VideoUnderstandingStageInput(
                    list_of_scene=scene_output.scene_understanding_messages,
                    preferred_language=prep.effective_language,
                    job_id=job_id,
                )
            )
        except Exception:
            # Free the single-flight lease so a failure does not block caching of this
            # understanding for the lease TTL; the error still propagates as before.
            if cache is not None and lease is not None and key is not None:
                try:
                    cache.release_lease(key, lease)
                except Exception:
                    logger.debug("Failed to release understanding lease %s.", key, exc_info=True)
            raise

        state = "bypass"
        if cache is not None and source_sha and key is not None:
            state = "miss" if lease is not None else "contended"
            if lease is not None:
                try:
                    cache.complete_lease(
                        key,
                        lease,
                        kind="understanding",
                        payload_json=understanding_bundle_to_json(
                            scenes=scene_output.scene_understanding_messages,
                            video_descriptions=video_understanding_output.video_descriptions,
                            video_title=video_understanding_output.video_title,
                            video_description=video_understanding_output.video_description,
                            scene_token_usage=scene_output.token_usage,
                            vu_token_usage=video_understanding_output.token_usage,
                        ),
                        content_sha=str(source_sha),
                        key_version=KV_UNDERSTANDING,
                    )
                except Exception:
                    logger.warning(
                        "Failed to publish understanding cache for %s.", key, exc_info=True
                    )

        return (
            scene_output.scene_understanding_messages,
            video_understanding_output.video_descriptions,
            video_understanding_output.video_title,
            video_understanding_output.video_description,
            scene_output.token_usage,
            video_understanding_output.token_usage,
            scene_output.thumbnail_path,
            state,
        )


class VideoMusicProviderCandidateGenerationStage:
    """Submit or resume provider candidate generation for one model spec.

    The stage keeps using the existing provider workflow methods. In split mode
    the worker may pass a `provider_task_recorder`, which is called immediately
    after a provider accepts a task and before polling starts. If a retry already
    has a durable provider task artifact, `resume_provider_task_id` lets
    supported providers poll/download the existing task instead of submitting a
    duplicate song.
    """

    def __init__(self, runtime: VideoMusicSplitStageRuntime) -> None:
        self.runtime = runtime

    async def run(
        self,
        stage_input: ProviderCandidateGenerationStageInput,
    ) -> ProviderCandidateGenerationStageOutput:
        # Performance-test mode: skip the real (paid, high-variance) provider call
        # and return a deterministic pre-made audio after a configurable delay, so
        # the rest of the pipeline (queue, download, compress, analysis, beat-match,
        # compose) can be measured at zero generation cost. Enabled via
        # ASYNC_V2_FAKE_PROVIDER; delay simulated via ASYNC_V2_FAKE_PROVIDER_DELAY_MS.
        if _fake_provider_enabled():
            return await self._fake_candidate(stage_input)

        analysis = stage_input.analysis
        music_stage = self.runtime.workflow.music_generation_stage
        model_spec = analysis.effective_modelspec
        video_metadata = analysis.video_metadata
        provider_task_id = stage_input.resume_provider_task_id

        def _record_provider_task(payload: dict[str, Any]) -> Any:
            nonlocal provider_task_id
            task_id = payload.get("task_id")
            if task_id:
                provider_task_id = str(task_id)
            if stage_input.provider_task_recorder is None:
                return None
            payload_with_model = dict(payload)
            payload_with_model.setdefault("model_spec", model_spec)
            return stage_input.provider_task_recorder(payload_with_model)

        if model_spec == MusicGenertionModelEnum.EDENN_STUDIO:
            audio_path, _, primary_track_lyrics, _ = await music_stage.provider_c_music_generation_workflow(
                analysis.music_prompt,
                analysis.include_vocals,
                analysis.vocal_gender,
                video_metadata=video_metadata,
                provider_c_custom_mode=analysis.include_vocals,
                provider_task_recorder=_record_provider_task,
                resume_task_id=stage_input.resume_provider_task_id,
                resume_key_label=stage_input.resume_provider_key_label,
                resume_base=stage_input.resume_provider_base,
            )
            return ProviderCandidateGenerationStageOutput(
                job_id=stage_input.job_id,
                model_spec=model_spec,
                candidate_audio_path=audio_path,
                complete_audio_path=audio_path,
                secondary_complete_audio_path=None,
                primary_full_lyrics=primary_track_lyrics.full_lyrics,
                primary_full_lyrics_timestamps=list(primary_track_lyrics.lyrics_timestamps),
                primary_full_word_level_lyrics_timestamps=list(
                    primary_track_lyrics.word_level_lyrics_timestamps
                ),
                provider_task_id=provider_task_id,
                generation_api_call_count=primary_track_lyrics.generation_api_call_count,
            )

        if model_spec == MusicGenertionModelEnum.EDENN_ENHANCED:
            provider_b_provider = music_stage._require_provider_b_music_provider()
            style_prompt = (
                analysis.music_prompt.get("style_prompt")
                or analysis.music_prompt.get("prompt")
                or ""
            )
            lyrics_prompt = (
                analysis.music_prompt.get("lyrics_prompt")
                or (style_prompt if analysis.include_vocals else "")
            )
            warning_token = provider_b_provider.begin_warning_collection()
            critical_warning: Optional[str] = None
            try:
                if analysis.include_vocals:
                    (
                        audio_path,
                        _,
                        primary_track_lyrics,
                        _,
                        vocal_id_used,
                    ) = await music_stage.provider_b_music_generation_workflow(
                        style_prompt,
                        lyrics_prompt,
                        video_metadata=video_metadata,
                        vocal_id=stage_input.request_json.get("vocal_id") or None,
                        vocal_sample_path=(
                            Path(stage_input.request_json["vocal_sample_path"])
                            if stage_input.request_json.get("vocal_sample_path")
                            else None
                        ),
                        provider_task_recorder=_record_provider_task,
                        resume_task_id=stage_input.resume_provider_task_id,
                    )
                else:
                    (
                        audio_path,
                        _,
                        primary_track_lyrics,
                        _,
                        vocal_id_used,
                    ) = await music_stage.provider_b_instrumental_generation_workflow(
                        style_prompt,
                        video_metadata=video_metadata,
                        provider_task_recorder=_record_provider_task,
                        resume_task_id=stage_input.resume_provider_task_id,
                    )
            finally:
                critical_warning = provider_b_provider.finish_warning_collection(warning_token)
            return ProviderCandidateGenerationStageOutput(
                job_id=stage_input.job_id,
                model_spec=model_spec,
                candidate_audio_path=audio_path,
                complete_audio_path=audio_path,
                secondary_complete_audio_path=None,
                primary_full_lyrics=primary_track_lyrics.full_lyrics,
                primary_full_lyrics_timestamps=list(primary_track_lyrics.lyrics_timestamps),
                primary_full_word_level_lyrics_timestamps=list(
                    primary_track_lyrics.word_level_lyrics_timestamps
                ),
                provider_task_id=provider_task_id,
                vocal_id_used=vocal_id_used,
                critical_warning=critical_warning,
                generation_api_call_count=primary_track_lyrics.generation_api_call_count,
            )

        local_music_path, word_lyrics = await music_stage.provider_a_music_generation_workflow(
            analysis.music_prompt,
            analysis.include_vocals,
            analysis.vocal_gender,
            video_metadata=video_metadata,
            lyrics_language=analysis.user_requested_language,
            output_format=stage_input.request_json.get("audio_output_format") or None,
        )
        return ProviderCandidateGenerationStageOutput(
            job_id=stage_input.job_id,
            model_spec=MusicGenertionModelEnum.EDENN_BASIC,
            candidate_audio_path=local_music_path,
            complete_audio_path=None,
            secondary_complete_audio_path=None,
            primary_full_lyrics=None,
            primary_full_lyrics_timestamps=[],
            primary_full_word_level_lyrics_timestamps=list(word_lyrics),
            generation_api_call_count=1,
        )

    async def _fake_candidate(
        self,
        stage_input: ProviderCandidateGenerationStageInput,
    ) -> ProviderCandidateGenerationStageOutput:
        """Return a deterministic ~30s audio after a simulated generation delay."""
        analysis = stage_input.analysis
        delay_s = _fake_provider_delay_s()
        if delay_s > 0:
            await asyncio.sleep(delay_s)
        temp_folder = Path(analysis.video_metadata.temp_folder)
        temp_folder.mkdir(parents=True, exist_ok=True)
        audio_path = temp_folder / f"{stage_input.job_id}_fake_candidate.wav"
        if not (audio_path.exists() and audio_path.stat().st_size > 0):
            ffmpeg = resolve_ffmpeg_binary()
            cmd = [
                ffmpeg, "-y", "-f", "lavfi",
                "-i", "sine=frequency=220:duration=30",
                "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le",
                str(audio_path),
            ]
            await asyncio.to_thread(
                subprocess.run, cmd, check=True, capture_output=True)
        logger.info(
            "fake provider: returned canned audio for job %s (delay=%.1fs)",
            stage_input.job_id, delay_s,
        )
        return ProviderCandidateGenerationStageOutput(
            job_id=stage_input.job_id,
            model_spec=MusicGenertionModelEnum.EDENN_BASIC,
            candidate_audio_path=audio_path,
            complete_audio_path=None,
            secondary_complete_audio_path=None,
            primary_full_lyrics=None,
            primary_full_lyrics_timestamps=[],
            primary_full_word_level_lyrics_timestamps=[],
            generation_api_call_count=0,
        )


class VideoMusicSelectionRankingRemixFinalizeStage:
    def __init__(self, runtime: VideoMusicSplitStageRuntime) -> None:
        self.runtime = runtime

    @staticmethod
    def _candidate_cleaned_for_alignment(
        music_stage: Any,
        candidate: ProviderCandidateGenerationStageOutput,
    ) -> ProviderCandidateGenerationStageOutput:
        """Swap the candidate's track for a tag-free one before alignment.

        The provider stage hands the same file out as both the match source and
        the full track, so cleaning it once here serves both — and the full-track
        lyric timings come back inside it.
        """

        cleaned_path, cleaned_lyrics_duration_s = music_stage.clean_track_for_alignment(
            candidate.candidate_audio_path,
            model_spec=candidate.model_spec,
        )
        if cleaned_path == candidate.candidate_audio_path:
            return candidate
        return replace(
            candidate,
            candidate_audio_path=cleaned_path,
            complete_audio_path=(
                cleaned_path
                if candidate.complete_audio_path == candidate.candidate_audio_path
                else candidate.complete_audio_path
            ),
            primary_full_lyrics_timestamps=music_stage.clip_track_timestamps_s(
                candidate.primary_full_lyrics_timestamps, cleaned_lyrics_duration_s
            ),
            primary_full_word_level_lyrics_timestamps=music_stage.clip_track_timestamps_s(
                candidate.primary_full_word_level_lyrics_timestamps,
                cleaned_lyrics_duration_s,
            ),
        )

    async def run(
        self,
        stage_input: SelectionRankingRemixFinalizeStageInput,
    ) -> SelectionRankingRemixFinalizeStageOutput:
        analysis = stage_input.analysis
        candidate = stage_input.candidate
        request = stage_input.request_json
        model_spec = candidate.model_spec
        video_metadata = analysis.video_metadata
        music_stage = self.runtime.workflow.music_generation_stage
        matched_music_path = candidate.candidate_audio_path
        lyrics_timestamps: list[WordTS] = []
        word_level_lyrics_timestamps: list[WordTS] = []
        matching_used_track: Optional[str] = None
        music_start_s = 0.0
        alignment_score = 0.0
        alignment_details: dict[str, Any] = {}

        if model_spec in {
            MusicGenertionModelEnum.EDENN_ENHANCED,
            MusicGenertionModelEnum.EDENN_STUDIO,
        }:
            # Alignment picks the window the delivered video plays, and it is
            # free to land on the very end of whatever it is given — so the
            # provider's trailing tag has to be gone before it chooses. The
            # generation stage chops unconditionally now, so this is normally a
            # no-op; it stays as the boundary's own guarantee (mixed-version
            # fleets, lost filename markers).
            candidate = self._candidate_cleaned_for_alignment(music_stage, candidate)
            matching_lyrics = (
                candidate.primary_full_word_level_lyrics_timestamps
                or candidate.primary_full_lyrics_timestamps
            )
            matching_output = await self.runtime.music_matching_stage.run(
                MusicMatchingStageInput(
                    provider_c_music_provider=music_stage.provider_c_music_provider,
                    video_metadata=video_metadata,
                    local_music_path=candidate.candidate_audio_path,
                    timestamp_lyrics=matching_lyrics,
                    source_track_label="primary",
                    require_lyrics=analysis.include_vocals,
                )
            )
            matched_music_path = matching_output.reranked_music_outputs_path
            matching_used_track = matching_output.used_track or "primary"
            music_start_s = matching_output.music_start_s
            alignment_score = matching_output.alignment_score
            alignment_details = dict(matching_output.alignment_details)

            if model_spec == MusicGenertionModelEnum.EDENN_ENHANCED:
                aligned_word_level = MusicMatchingStage._offset_word_ts(
                    candidate.primary_full_word_level_lyrics_timestamps,
                    matching_output.music_start_s,
                    video_metadata.duration,
                )
                aligned_line_level = align_line_level_lyrics_to_window(
                    candidate.primary_full_lyrics_timestamps,
                    candidate.primary_full_word_level_lyrics_timestamps,
                    matching_output.music_start_s,
                    video_metadata.duration,
                )
                lyrics_timestamps = to_ms_wordts(strip_section_tags(aligned_line_level))
                word_level_lyrics_timestamps = to_ms_wordts(aligned_word_level)
            else:
                cleaned = strip_section_tags(matching_output.aligned_lyrics)
                cleaned_ms = to_ms_wordts(cleaned)
                lyrics_timestamps = cleaned_ms
                word_level_lyrics_timestamps = list(cleaned_ms)
        else:
            basic_words = (
                candidate.primary_full_word_level_lyrics_timestamps
                or candidate.primary_full_lyrics_timestamps
            )
            lyrics_timestamps = to_ms_wordts(basic_words)
            word_level_lyrics_timestamps = to_ms_wordts(basic_words)

        remix_output = await self.runtime.workflow.post_generation_remix.run(
            VideoAudioRemixStageInput(
                preserve_original_audio=bool(request.get("preserve_original_audio", False)),
                music_volume=float(request.get("music_volume", 1.0) or 1.0),
                music_path=matched_music_path,
                video_metadata=video_metadata,
                job_id=stage_input.job_id,
                pipeline_start_time=float(request.get("job_received_timestamp") or 0),
            )
        )
        remixed_video_path = Path(video_metadata.temp_folder) / remix_output.remixed_video_path
        complete_audio_path = candidate.complete_audio_path
        secondary_complete_audio_path = candidate.secondary_complete_audio_path
        water_mark = bool(request.get("water_mark", False))
        complete_audio_path, primary_full_duration_s = (
            music_stage.prepare_full_track_for_delivery(
                complete_audio_path,
                model_spec=candidate.model_spec,
                water_mark=water_mark,
            )
        )
        secondary_complete_audio_path, secondary_full_duration_s = (
            music_stage.prepare_full_track_for_delivery(
                secondary_complete_audio_path,
                model_spec=candidate.model_spec,
                water_mark=water_mark,
            )
        )
        result = VideoGenerationResult(
            video_metadata=video_metadata,
            scenes=analysis.scenes,
            video_summary=analysis.video_summary,
            video_title=analysis.video_title,
            video_description=analysis.video_description,
            music_prompt=analysis.music_prompt,
            music_prompt_in_chinese=analysis.music_prompt,
            generated_music_path=matched_music_path,
            complete_generated_music_path=complete_audio_path,
            secondary_complete_generated_music_path=secondary_complete_audio_path,
            remixed_video_path=remixed_video_path,
            include_vocals=analysis.include_vocals,
            vocal_gender=analysis.vocal_gender,
            lyrics_timestamps=lyrics_timestamps,
            word_level_lyrics_timestamps=word_level_lyrics_timestamps,
            vocal_id_used=candidate.vocal_id_used,
            primary_full_lyrics=candidate.primary_full_lyrics,
            primary_full_lyrics_timestamps=music_stage.clip_full_track_timestamps_ms(
                to_ms_wordts(candidate.primary_full_lyrics_timestamps),
                primary_full_duration_s,
            ),
            primary_full_word_level_lyrics_timestamps=(
                music_stage.clip_full_track_timestamps_ms(
                    to_ms_wordts(candidate.primary_full_word_level_lyrics_timestamps),
                    primary_full_duration_s,
                )
            ),
            secondary_full_lyrics=candidate.secondary_full_lyrics,
            secondary_full_lyrics_timestamps=music_stage.clip_full_track_timestamps_ms(
                to_ms_wordts(candidate.secondary_full_lyrics_timestamps),
                secondary_full_duration_s,
            ),
            secondary_full_word_level_lyrics_timestamps=(
                music_stage.clip_full_track_timestamps_ms(
                    to_ms_wordts(candidate.secondary_full_word_level_lyrics_timestamps),
                    secondary_full_duration_s,
                )
            ),
            matching_used_track=matching_used_track,
            thumbnail_path=analysis.thumbnail_path,
            token_usage=analysis.token_usage,
            token_usage_breakdown=analysis.token_usage_breakdown,
            used_music_model_spec=model_spec,
            user_requested_language=analysis.user_requested_language,
            job_received_timestamp=request.get("job_received_timestamp")
            or analysis.job_received_timestamp,
            job_finished_timestamp=int(time.time()),
            job_id=stage_input.job_id,
            video_id=str(request.get("video_id") or ""),
            creative_id=str(request.get("creative_id") or ""),
            primary_music_id=str(request.get("primary_music_id") or ""),
            secondary_music_id=(
                request.get("secondary_music_id")
                if secondary_complete_audio_path
                else None
            ),
            selected_music_id=str(
                request.get("selected_music_id")
                or request.get("primary_music_id")
                or ""
            ),
            alignment_id=str(request.get("alignment_id") or ""),
            music_start_s=music_start_s,
            alignment_score=alignment_score,
            alignment_details=alignment_details,
            critical_warning=candidate.critical_warning,
            generation_api_call_count=candidate.generation_api_call_count,
        )
        return SelectionRankingRemixFinalizeStageOutput(
            job_id=stage_input.job_id,
            result=result,
        )


def video_metadata_to_json(metadata: VideoMetadata) -> dict[str, Any]:
    data = metadata.to_dict()
    data["temp_folder"] = metadata.temp_folder
    data["path"] = str(metadata.path)
    return data


def video_metadata_from_json(
    data: dict[str, Any],
    *,
    source_video_path: Optional[Path],
    temp_folder: Path,
) -> VideoMetadata:
    temp_folder.mkdir(parents=True, exist_ok=True)
    metadata_path = (
        source_video_path.resolve()
        if source_video_path is not None
        else Path(data.get("path") or temp_folder / "metadata_only_source_video.unavailable")
    )
    return VideoMetadata(
        path=metadata_path,
        duration=float(data.get("duration") or 0),
        size_bytes=int(
            data.get("size_bytes")
            or (
                source_video_path.stat().st_size
                if source_video_path is not None and source_video_path.exists()
                else 0
            )
        ),
        width=data.get("width"),
        height=data.get("height"),
        fps=float(data.get("fps") or 0),
        video_codec=data.get("video_codec"),
        video_bit_rate=data.get("video_bit_rate"),
        has_audio=bool(data.get("has_audio", False)),
        audio_codec=data.get("audio_codec"),
        audio_channels=data.get("audio_channels"),
        audio_sample_rate=data.get("audio_sample_rate"),
        audio_bit_rate=data.get("audio_bit_rate"),
        audio_activity=[
            (float(start), float(end))
            for start, end in (data.get("audio_activity") or [])
        ],
        temp_folder=str(temp_folder),
    )


def preprocess_output_to_json(output: VideoPreprocessStageOutput) -> dict[str, Any]:
    return {
        "job_id": output.job_id,
        "source_video_artifact_id": output.source_video_artifact_id,
        "prepared_source_video_artifact_id": output.prepared_source_video_artifact_id,
        "video_metadata": video_metadata_to_json(output.video_metadata),
        "compression": output.compression_info,
    }


def preprocess_output_from_json(
    data: dict[str, Any],
    *,
    source_video_path: Path,
    temp_folder: Path,
) -> VideoPreprocessStageOutput:
    return VideoPreprocessStageOutput(
        job_id=str(data["job_id"]),
        source_video_artifact_id=str(data["source_video_artifact_id"]),
        prepared_source_video_artifact_id=str(
            data.get("prepared_source_video_artifact_id")
            or data.get("source_video_artifact_id")
            or ""
        ),
        video_metadata=video_metadata_from_json(
            data["video_metadata"],
            source_video_path=source_video_path,
            temp_folder=temp_folder,
        ),
        compression_info=dict(data.get("compression") or {}),
    )


def scene_to_json(scene: SceneUnderstanding) -> dict[str, Any]:
    return {
        "scene_index": scene.scene_index,
        "start_timestamp": scene.start_timestamp,
        "end_timestamp": scene.end_timestamp,
        "visual_summary": scene.visual_summary,
        "key_actions": scene.key_actions,
        "mood": scene.mood,
    }


def scene_from_json(data: dict[str, Any]) -> SceneUnderstanding:
    return SceneUnderstanding(
        scene_index=int(data.get("scene_index") or 0),
        start_timestamp=float(data.get("start_timestamp") or 0.0),
        end_timestamp=float(data.get("end_timestamp") or 0.0),
        visual_summary=str(data.get("visual_summary") or ""),
        key_actions=str(data.get("key_actions") or ""),
        mood=str(data.get("mood") or ""),
    )


def understanding_bundle_to_json(
    *,
    scenes: list[SceneUnderstanding],
    video_descriptions: Any,
    video_title: str,
    video_description: str,
    scene_token_usage: Any,
    vu_token_usage: Any,
) -> dict[str, Any]:
    """Serialize the prompt-independent video-understanding result for caching.

    Pure JSON: scenes are text-only and the music-prompt stage consumes only text, so
    no frame images are bundled. The thumbnail is intentionally excluded — finalize
    regenerates it from the video when absent.
    """

    return {
        "scenes": [scene_to_json(scene) for scene in scenes],
        "video_descriptions": video_descriptions,
        "video_title": video_title,
        "video_description": video_description,
        "scene_token_usage": scene_token_usage or {},
        "video_understanding_token_usage": vu_token_usage or {},
    }


def understanding_bundle_from_json(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "scenes": [scene_from_json(scene) for scene in (data.get("scenes") or [])],
        "video_descriptions": data.get("video_descriptions") or {},
        "video_title": str(data.get("video_title") or ""),
        "video_description": str(data.get("video_description") or ""),
        "scene_token_usage": data.get("scene_token_usage") or {},
        "video_understanding_token_usage": data.get("video_understanding_token_usage") or {},
    }


def word_ts_to_json(item: WordTS) -> dict[str, Any]:
    return {
        "text": item.text,
        "startS": item.startS,
        "endS": item.endS,
        "i": item.i,
    }


def word_ts_from_json(data: dict[str, Any]) -> WordTS:
    return WordTS(
        text=str(data.get("text") or ""),
        startS=float(data.get("startS") or 0.0),
        endS=float(data.get("endS") or 0.0),
        i=data.get("i"),
    )


def analysis_output_to_json(output: AnalysisAndPlanningStageOutput) -> dict[str, Any]:
    return {
        "job_id": output.job_id,
        "source_video_artifact_id": output.source_video_artifact_id,
        "video_metadata": video_metadata_to_json(output.video_metadata),
        "scenes": [scene_to_json(scene) for scene in output.scenes],
        "video_summary": output.video_summary,
        "video_title": output.video_title,
        "video_description": output.video_description,
        "music_prompt": output.music_prompt,
        "effective_modelspec": output.effective_modelspec,
        "include_vocals": output.include_vocals,
        "vocal_gender": output.vocal_gender,
        "user_requested_language": output.user_requested_language,
        "detected_category": output.detected_category,
        "overall_mood": output.overall_mood,
        "thumbnail_path": str(output.thumbnail_path) if output.thumbnail_path else None,
        "token_usage_breakdown": output.token_usage_breakdown,
        "token_usage": output.token_usage,
        "job_received_timestamp": output.job_received_timestamp,
    }


def analysis_output_from_json(
    data: dict[str, Any],
    *,
    source_video_path: Optional[Path],
    temp_folder: Path,
) -> AnalysisAndPlanningStageOutput:
    return AnalysisAndPlanningStageOutput(
        job_id=str(data["job_id"]),
        source_video_artifact_id=str(data["source_video_artifact_id"]),
        video_metadata=video_metadata_from_json(
            data["video_metadata"],
            source_video_path=source_video_path,
            temp_folder=temp_folder,
        ),
        scenes=[scene_from_json(row) for row in data.get("scenes", [])],
        video_summary=data.get("video_summary"),
        video_title=str(data.get("video_title") or ""),
        video_description=str(data.get("video_description") or ""),
        music_prompt=dict(data.get("music_prompt") or {}),
        effective_modelspec=str(data.get("effective_modelspec") or "edenn_basic"),
        include_vocals=bool(data.get("include_vocals", False)),
        vocal_gender=str(data.get("vocal_gender") or "female"),
        user_requested_language=str(data.get("user_requested_language") or ""),
        detected_category=str(data.get("detected_category") or ""),
        overall_mood=str(data.get("overall_mood") or ""),
        thumbnail_path=Path(data["thumbnail_path"]) if data.get("thumbnail_path") else None,
        token_usage_breakdown=dict(data.get("token_usage_breakdown") or {}),
        token_usage=dict(data.get("token_usage") or {}),
        job_received_timestamp=int(data.get("job_received_timestamp") or time.time()),
    )


def candidate_output_to_json(output: ProviderCandidateGenerationStageOutput) -> dict[str, Any]:
    return {
        "job_id": output.job_id,
        "model_spec": output.model_spec,
        "candidate_audio_path": str(output.candidate_audio_path),
        "complete_audio_path": str(output.complete_audio_path) if output.complete_audio_path else None,
        "secondary_complete_audio_path": (
            str(output.secondary_complete_audio_path)
            if output.secondary_complete_audio_path
            else None
        ),
        "primary_full_lyrics": output.primary_full_lyrics,
        "primary_full_lyrics_timestamps": [
            word_ts_to_json(item) for item in output.primary_full_lyrics_timestamps
        ],
        "primary_full_word_level_lyrics_timestamps": [
            word_ts_to_json(item)
            for item in output.primary_full_word_level_lyrics_timestamps
        ],
        "secondary_full_lyrics": output.secondary_full_lyrics,
        "secondary_full_lyrics_timestamps": [
            word_ts_to_json(item) for item in output.secondary_full_lyrics_timestamps
        ],
        "secondary_full_word_level_lyrics_timestamps": [
            word_ts_to_json(item)
            for item in output.secondary_full_word_level_lyrics_timestamps
        ],
        "provider_task_id": output.provider_task_id,
        "provider_audio_id": output.provider_audio_id,
        "vocal_id_used": output.vocal_id_used,
        "critical_warning": output.critical_warning,
        "generation_api_call_count": output.generation_api_call_count,
    }


def candidate_output_from_json(
    data: dict[str, Any],
    *,
    candidate_audio_path: Optional[Path] = None,
) -> ProviderCandidateGenerationStageOutput:
    resolved_candidate_path = candidate_audio_path or Path(data["candidate_audio_path"])
    complete_audio_path = (
        Path(data["complete_audio_path"]) if data.get("complete_audio_path") else None
    )
    if (
        complete_audio_path is not None
        and not complete_audio_path.exists()
        and str(complete_audio_path) == str(data.get("candidate_audio_path"))
    ):
        complete_audio_path = resolved_candidate_path
    return ProviderCandidateGenerationStageOutput(
        job_id=str(data["job_id"]),
        model_spec=str(data.get("model_spec") or "edenn_basic"),
        candidate_audio_path=resolved_candidate_path,
        complete_audio_path=complete_audio_path,
        secondary_complete_audio_path=(
            Path(data["secondary_complete_audio_path"])
            if data.get("secondary_complete_audio_path")
            else None
        ),
        primary_full_lyrics=data.get("primary_full_lyrics"),
        primary_full_lyrics_timestamps=[
            word_ts_from_json(row) for row in data.get("primary_full_lyrics_timestamps", [])
        ],
        primary_full_word_level_lyrics_timestamps=[
            word_ts_from_json(row)
            for row in data.get("primary_full_word_level_lyrics_timestamps", [])
        ],
        secondary_full_lyrics=data.get("secondary_full_lyrics"),
        secondary_full_lyrics_timestamps=[
            word_ts_from_json(row) for row in data.get("secondary_full_lyrics_timestamps", [])
        ],
        secondary_full_word_level_lyrics_timestamps=[
            word_ts_from_json(row)
            for row in data.get("secondary_full_word_level_lyrics_timestamps", [])
        ],
        provider_task_id=data.get("provider_task_id"),
        provider_audio_id=data.get("provider_audio_id"),
        vocal_id_used=data.get("vocal_id_used"),
        critical_warning=data.get("critical_warning"),
        generation_api_call_count=int(data.get("generation_api_call_count") or 1),
    )


__all__ = [
    "VideoPreprocessStageInput",
    "VideoPreprocessStageOutput",
    "AnalysisAndPlanningStageInput",
    "AnalysisAndPlanningStageOutput",
    "ProviderCandidateGenerationStageInput",
    "ProviderCandidateGenerationStageOutput",
    "SelectionRankingRemixFinalizeStageInput",
    "SelectionRankingRemixFinalizeStageOutput",
    "VideoMusicPreprocessStage",
    "VideoMusicAnalysisAndPlanningStage",
    "VideoMusicProviderCandidateGenerationStage",
    "VideoMusicSelectionRankingRemixFinalizeStage",
    "VideoMusicSplitStageRuntime",
    "preprocess_output_from_json",
    "preprocess_output_to_json",
    "analysis_output_from_json",
    "analysis_output_to_json",
    "candidate_output_from_json",
    "candidate_output_to_json",
]
