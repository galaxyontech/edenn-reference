from __future__ import annotations

import asyncio
import os
import socket
import time
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from uuid import uuid4

import httpx

import sentry_sdk
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, model_validator

from EdennCode.Deployment.api_common import (
    ApiContext,
    ErrorDetail,
    JobStatus,
    LyricsTimestampModel,
    TokenUsageBreakdownResponse,
    TokenUsageCountsResponse,
    cleanup_temp_dir,
    download_public_file_to_disk,
    edenn_error_to_http_exception,
    filename_from_url,
    guess_audio_content_type,
    guess_image_content_type,
    prepare_audio_for_provider_b_vocal_clone,
    probe_audio_metrics,
    resolve_optional_media_source_to_disk,
    sanitize_filename,
    service_version,
    write_upload_to_disk,
    normalize_token_usage_breakdown,
    normalize_token_usage_counts,
)
from EdennCode.Deployment.async_video_job_store import (
    AsyncVideoJobState as _AsyncJobState,
    InMemoryAsyncVideoJobStore,
    build_async_video_job_store_from_env,
)
from EdennCode.Deployment.auth.key_store import Principal
from EdennCode.Deployment.auth.middleware import get_principal, resolve_user_id
from EdennCode.Deployment.error_codes import resolve as _resolve_error_code
from EdennCode.Deployment.output_naming import contains_provider_token
from EdennCode.Deployment.response_guardrail import guard_response_model
from EdennCode.Deployment.workflows import VideoGenerationResult, VideoPreGenerationResult
from EdennCode.Deployment.recommendation_persistence import (
    AlignmentRecord,
    CreativeFeatureSnapshotRecord,
    CreativeRecord,
    GenerationJobRecord,
    MusicAssetRecord,
    RecommendationAssetIds,
    VideoAssetRecord,
    VideoGenerationRecommendationPayload,
)
from EdennCode.Util.MediaUtils import compress_video_to_max_height, get_video_duration
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoPreprocessStage.media_preprocess_stage import (
    validate_input_video_duration,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata.video_metadata import (
    VideoMetadata,
)
from EdennCode.exceptions import EdennApiError, EdennError

VALID_MUSIC_MODEL_SPECS = {"edenn_basic", "edenn_enhanced", "edenn_studio"}
# Inbound legacy client aliases for modelspec, kept ONLY for backward
# compatibility with requests already in the wild. This is the single shared
# map for every endpoint (v1/v2, video and multi-image) — do not fork it, and
# do not add new vendor-named aliases: new clients must use the edenn_* names.
LEGACY_MODEL_MAP = {
    "provider_a": "edenn_basic",
    "provider_a": "edenn_basic",
    "provider_a": "edenn_basic",
    "provider_b": "edenn_enhanced",
    "provider_c": "edenn_studio",
}
_PUBLIC_PROVIDER_FAILURE_MESSAGE = (
    "The request could not be completed. Please try again later."
)
CREATION_API_CALL_UNIT_COST_USD = 0.065
COST_MODEL_SPEC_NAME = "edenn-perceptron-1.1"
BASELINE_INPUT_TOKEN_COST_PER_1M_USD = 5.00
BASELINE_OUTPUT_TOKEN_COST_PER_1M_USD = 30.00
BASELINE_TOKEN_COST_MULTIPLIER = 1.2
CREATION_COST_DECIMAL_PLACES = 6
TOKEN_COST_DECIMAL_PLACES = 6
# Last-resort track name when the understanding summary carries neither a
# music_title nor a video_title.
MUSIC_TITLE_FALLBACK = "Untitled Track"


@dataclass(frozen=True)
class VideoGenerationInputSource:
    path: Path
    content_type: Optional[str] = None
    source_url: Optional[str] = None


_async_job_store: dict[str, _AsyncJobState] = {}
_memory_async_video_job_store = InMemoryAsyncVideoJobStore(_async_job_store)
_async_video_job_store: Any | None = None
_async_video_job_store_override: Any | None = None
_async_video_job_semaphore: asyncio.Semaphore | None = None
_async_video_job_semaphore_limit: int | None = None
_ASYNC_JOB_TTL_SECONDS_DEFAULT = 6 * 60 * 60


def _validate_staged_input_video_duration(video_path: Path) -> None:
    duration_s = get_video_duration(video_path)
    validate_input_video_duration(duration_s, source_path=video_path)


class SceneModel(BaseModel):
    scene_index: int
    start_timestamp: float
    end_timestamp: float
    visual_summary: str
    key_actions: str
    mood: str


class VideoGenerationCostResponse(BaseModel):
    # Client-facing fields, declared first so the key-order restoration on read
    # surfaces them at the top of the block.
    total_cost: float = 0.0
    # Length in seconds of the generated creative (the delivered video).
    creative_duration: Optional[float] = None
    # Internal cost breakdown. Retained in the stored result_json because the
    # per-key usage/billing recorder (auth.usage_recorder) reads these off it;
    # the v2 status read path strips them so a client only sees total_cost +
    # creative_duration.
    model_spec_name: str = COST_MODEL_SPEC_NAME
    creation_cost: float = 0.0
    creation_times: int = 0
    token_num: Optional[int] = None
    token_cost: Optional[float] = None


# ----------------------------------------------------------------------------
# Job response blocks.
#
# The job response is an envelope (job_id / status / version) plus five metadata
# blocks. Only URLs are exposed — storage blob names stay server-side. There is
# exactly one generated track, so no field carries a primary/secondary prefix.
#
# MultiImageJobResponse subclasses these blocks (see api_multi_image_generation),
# which is what makes "multi-image is a superset of video" structurally true
# rather than hand-maintained.
# ----------------------------------------------------------------------------


class RequestMetadata(BaseModel):
    """What the caller asked for, as the pipeline resolved it."""

    modelspec: str = Field(default="edenn_basic")
    include_vocals: bool = Field(default=False)
    vocal_gender: str = Field(default="female")
    user_requested_language: str = Field(default="")


class ResponseMetadata(BaseModel):
    """Job lifecycle facts."""

    job_received_timestamp: Optional[int] = None
    job_finished_timestamp: Optional[int] = None
    # Whether the source media was compressed before processing (image-music sets
    # this; video leaves it False). Shared so both responses have one shape.
    compression_applied: bool = Field(default=False)


class VideoGeometry(BaseModel):
    """Probed geometry of the delivered video.

    Only the client-facing dimensions are exposed. ``extra="ignore"`` drops the
    richer internal probe keys (codec, bitrate, channels, sample rate, audio
    activity, source path, ...) so they never reach a response — the pipeline
    reads those off its own internal metadata model, not this one.
    The two pipelines probe the length under different names, so ``duration_s`` is
    accepted on input and mirrored into ``duration`` (never null on either path),
    but it is ``exclude``d from the response — only ``duration`` is surfaced.
    """

    model_config = ConfigDict(extra="ignore")

    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[float] = None
    # Input-only: mirrored into ``duration`` by the validator, excluded from output.
    duration_s: Optional[float] = Field(default=None, exclude=True)
    fps: Optional[float] = None

    @model_validator(mode="after")
    def _mirror_duration(self) -> "VideoGeometry":
        if self.duration is None and self.duration_s is not None:
            self.duration = self.duration_s
        elif self.duration_s is None and self.duration is not None:
            self.duration_s = self.duration
        return self


class VideoMetadataBlock(BaseModel):
    video_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    # Size of the delivered video file, mirroring audio_metadata.audio_size_bytes.
    video_size_bytes: Optional[int] = None
    geometry: VideoGeometry = Field(default_factory=VideoGeometry)
    video_summary: Dict[str, Any] | str = Field(default_factory=dict)


class AudioMetadataBlock(BaseModel):
    """The generated music.

    ``audio_url`` is the clip muxed into the video; ``complete_audio_url`` is the
    full-length track it was cut from.
    """

    audio_url: Optional[str] = None
    audio_duration_s: Optional[float] = None
    audio_size_bytes: Optional[int] = None
    complete_audio_url: Optional[str] = None
    complete_audio_duration_s: Optional[float] = None
    complete_audio_size_bytes: Optional[int] = None
    # Song-style name for the track, distinct from any descriptive video title.
    music_title: str = ""
    # The music brief the generator was given, as a plain string.
    music_description: Optional[str] = None
    # Lyrics for the complete track.
    full_lyrics: Optional[str] = None
    full_lyrics_timestamps: List[LyricsTimestampModel] = Field(default_factory=list)
    full_word_level_lyrics_timestamps: List[LyricsTimestampModel] = Field(
        default_factory=list)
    # Lyrics aligned to the delivered video.
    lyrics_timestamps: List[LyricsTimestampModel] = Field(default_factory=list)
    word_level_lyrics_timestamps: List[LyricsTimestampModel] = Field(
        default_factory=list)


class VideoJobResponse(BaseModel):
    job_id: str
    status: str = Field(default=JobStatus.COMPLETED)
    version: str = Field(default_factory=service_version)
    modelspec: str = Field(default="edenn_basic")
    response_metadata: ResponseMetadata = Field(default_factory=ResponseMetadata)
    cost_metadata: VideoGenerationCostResponse = Field(
        default_factory=VideoGenerationCostResponse)
    video_metadata: VideoMetadataBlock = Field(default_factory=VideoMetadataBlock)
    audio_metadata: AudioMetadataBlock = Field(default_factory=AudioMetadataBlock)


class AsyncVideoJobAcceptedResponse(BaseModel):
    """Returned immediately by POST /api/v1/jobs/async_video_music_gen."""

    job_id: str
    status: str = Field(default=JobStatus.PENDING)
    version: str = Field(default_factory=service_version)


class AsyncVideoJobStatusResponse(BaseModel):
    """Returned by GET /api/v1/jobs/async_video_music_gen/{job_id}."""

    job_id: str
    status: str
    version: str = Field(default_factory=service_version)
    result: Optional[VideoJobResponse] = None
    error: Optional[Dict[str, Any]] = None
    created_at: int


class ServerTimingResponse(BaseModel):
    hostname: str
    pid: int
    image_tag: Optional[str] = None
    total_server_s: float
    stages: Dict[str, float] = Field(default_factory=dict)


class VideoCompressionResponse(BaseModel):
    job_id: str
    status: str = Field(default=JobStatus.COMPLETED)
    version: str = Field(default_factory=service_version)
    compression_applied: bool
    output_uploaded: bool = False
    max_height: int
    source_video_metadata: Dict[str, Any]
    output_video_metadata: Dict[str, Any]
    output_blob: Optional[str] = None
    output_url: Optional[str] = None
    output_path: Optional[str] = None
    content_type: str = "video/mp4"
    server_timing: Optional[ServerTimingResponse] = None


class VideoPreGenerationPreviewResponse(BaseModel):
    job_id: str
    video_id: str
    creative_id: str
    primary_music_id: str
    selected_music_id: str
    alignment_id: str
    secondary_music_id: Optional[str] = None
    status: str = Field(default=JobStatus.COMPLETED)
    version: str = Field(default_factory=service_version)
    compression_applied: bool = False
    source_video_blob: Optional[str] = None
    source_video_url: Optional[str] = None
    source_video_path: Optional[str] = None
    thumbnail_blob: Optional[str] = None
    thumbnail_url: Optional[str] = None
    thumbnail_path: Optional[str] = None
    video_metadata: Dict[str, Any]
    scenes: List[SceneModel]
    video_summary: Dict[str, Any] | str
    video_title: str = ""
    video_description: str = ""
    music_prompt: Dict[str, Any]
    sanitized_prompt: str = ""
    sanitized_style_prompt: Optional[str] = None
    sanitized_lyrics_prompt: Optional[str] = None
    include_vocals: bool = Field(default=False)
    vocal_gender: str = Field(default="female")
    modelspec: str = Field(default="edenn_basic")
    user_requested_language: str = Field(default="")
    detected_category: str = ""
    was_transformed: bool = False
    detected_references: List[str] = Field(default_factory=list)
    token_usage: Optional[int] = None
    raw_token_usage: Optional[TokenUsageCountsResponse] = None
    token_usage_breakdown: Optional[TokenUsageBreakdownResponse] = None
    job_received_timestamp: Optional[int] = None
    job_finished_timestamp: Optional[int] = None
    server_timing: Optional[ServerTimingResponse] = None


def _scene_to_model(scene: Any) -> SceneModel:
    return SceneModel(
        scene_index=getattr(scene, "scene_index", 0),
        start_timestamp=getattr(scene, "start_timestamp", 0.0),
        end_timestamp=getattr(scene, "end_timestamp", 0.0),
        visual_summary=getattr(scene, "visual_summary", ""),
        key_actions=getattr(scene, "key_actions", ""),
        mood=getattr(scene, "mood", ""),
    )


def _music_prompt_text(raw_value: Any) -> Optional[str]:
    """The music brief as a plain string — this is ``music_description``.

    The basic path emits it under ``global_music_prompt``, the enhanced/studio
    path under ``style_prompt``, and the multi-image path passes the prompt text
    directly. ``lyrics_prompt`` is never surfaced.
    """
    if isinstance(raw_value, dict):
        text = raw_value.get("global_music_prompt") or raw_value.get("style_prompt")
    elif isinstance(raw_value, str):
        text = raw_value
    else:
        text = None
    text = text.strip() if isinstance(text, str) else None
    return text or None


def _safe_float(value: Any) -> Optional[float]:
    """Convert a loose provider/model value into a float when possible."""

    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> Optional[int]:
    """Convert a loose media metadata value into an int when possible."""

    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _word_timestamps_to_json(items: Any) -> list[dict[str, Any]]:
    """Serialize WordTS-like objects without depending on their concrete class."""

    rows: list[dict[str, Any]] = []
    for item in items or []:
        rows.append(
            {
                "text": getattr(item, "text", ""),
                "startS": float(getattr(item, "startS", 0.0) or 0.0),
                "endS": float(getattr(item, "endS", 0.0) or 0.0),
                "i": getattr(item, "i", None),
            }
        )
    return rows


def _scenes_to_json(scenes: List[SceneModel]) -> list[dict[str, Any]]:
    """Serialize API scene models for storage in video_asset.scene_summary_json."""

    return [
        {
            "scene_index": scene.scene_index,
            "start_timestamp": scene.start_timestamp,
            "end_timestamp": scene.end_timestamp,
            "visual_summary": scene.visual_summary,
            "key_actions": scene.key_actions,
            "mood": scene.mood,
        }
        for scene in scenes
    ]


def _dict_value(raw_value: Any) -> dict[str, Any]:
    """Return dict values as-is and wrap non-dict values for JSON persistence."""

    if isinstance(raw_value, dict):
        return dict(raw_value)
    if raw_value is None:
        return {}
    return {"raw": raw_value}


def ensure_music_title(*candidates: Any) -> str:
    """First non-empty string candidate, else the fallback constant.

    ``music_title`` must never be empty. Callers pass the song-style title first,
    then descriptive titles (e.g. the video/slideshow title) as fallbacks, so
    every path — video and multi-image alike — always surfaces a title.
    """
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return MUSIC_TITLE_FALLBACK


def _music_title_from_summary(video_summary: Any) -> str:
    """Song-style ``music_title`` from the video-understanding summary.

    ``music_title`` is part of the video_summary schema, but the model does
    occasionally omit it. Fall back to the descriptive ``video_title`` and then
    to a constant so the field is never empty.
    """
    if isinstance(video_summary, dict):
        return ensure_music_title(
            video_summary.get("music_title"), video_summary.get("video_title")
        )
    return MUSIC_TITLE_FALLBACK


def _elapsed_s(start: float) -> float:
    return round(max(0.0, time.perf_counter() - start), 3)


def _safe_timing_value(value: Any) -> float:
    try:
        return round(max(0.0, float(value)), 3)
    except (TypeError, ValueError):
        return 0.0


def _record_stage_timing(stages: Dict[str, float], stage: str, start: float) -> None:
    stages[stage] = _elapsed_s(start)


def _build_server_timing(
    *,
    request_start: float,
    stages: Dict[str, float],
) -> ServerTimingResponse:
    image_tag = (os.getenv("IMAGE_TAG") or "").strip() or None
    return ServerTimingResponse(
        hostname=socket.gethostname(),
        pid=os.getpid(),
        image_tag=image_tag,
        total_server_s=_elapsed_s(request_start),
        stages={
            stage: _safe_timing_value(duration)
            for stage, duration in stages.items()
        },
    )


def _blob_suffix(source_path: Path) -> str:
    suffix = Path(source_path).suffix.lower()
    if not suffix or len(suffix) > 16:
        return ""
    if not all(ch.isalnum() or ch == "." for ch in suffix):
        return ""
    return suffix


def _provider_neutral_blob_name(
    *,
    job_id: str,
    folder: str,
    label: str,
    source_path: Path,
) -> str:
    safe_label = sanitize_filename(label).strip(" ._/") or "asset"
    return f"jobs/{job_id}/{folder.strip('/')}/{safe_label}{_blob_suffix(source_path)}"


def _public_video_metadata(video_metadata: Any) -> dict[str, Any]:
    data = (
        video_metadata.to_dict()
        if hasattr(video_metadata, "to_dict")
        else _dict_value(video_metadata)
    )
    if data.get("path"):
        data["path"] = f"source_video{_blob_suffix(Path(str(data['path'])))}"
    return data


def _inspect_public_video_metadata(source_path: Path, *, logger: Any) -> dict[str, Any]:
    metadata = VideoMetadata.from_file(source_path)
    try:
        return _public_video_metadata(metadata)
    finally:
        temp_folder = getattr(metadata, "temp_folder", None)
        if temp_folder:
            cleanup_temp_dir(Path(str(temp_folder)), logger=logger)


def _normalize_requested_modelspec(modelspec: Optional[str]) -> str:
    requested_modelspec_raw = (
        modelspec or "edenn_basic").strip().lower() or "edenn_basic"
    requested_modelspec = LEGACY_MODEL_MAP.get(
        requested_modelspec_raw,
        requested_modelspec_raw,
    )
    if requested_modelspec not in VALID_MUSIC_MODEL_SPECS:
        allowed = ", ".join(sorted(VALID_MUSIC_MODEL_SPECS))
        raise EdennApiError(
            f"Invalid modelspec. Allowed values: {allowed}.",
            public_message=f"Invalid modelspec. Allowed values: {allowed}.",
            status_code=400,
            component="api",
            operation="validate_modelspec",
        )
    return requested_modelspec


def _validate_prompt_fields(
    *,
    requested_modelspec: str,
    verbose_instruction: bool,
    user_prompt: str,
    music_style_prompt: Optional[str],
    lyrics_prompt: Optional[str],
) -> tuple[str, str]:
    normalized_music_style_prompt = (music_style_prompt or "").strip()
    normalized_lyrics_prompt = (lyrics_prompt or "").strip()
    if not verbose_instruction and (normalized_music_style_prompt or normalized_lyrics_prompt):
        raise HTTPException(
            status_code=400,
            detail=(
                "music_style_prompt and lyrics_prompt require "
                "verbose_instruction=true."
            ),
        )
    if verbose_instruction and (user_prompt or "").strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "When verbose_instruction=True, omit user_prompt and use "
                "music_style_prompt / lyrics_prompt instead."
            ),
        )
    if verbose_instruction and not normalized_music_style_prompt:
        raise HTTPException(
            status_code=400,
            detail="music_style_prompt is required when verbose_instruction=True.",
        )
    if verbose_instruction and requested_modelspec not in {"edenn_enhanced", "edenn_studio"}:
        raise HTTPException(
            status_code=400,
            detail=(
                "verbose_instruction requires modelspec=edenn_enhanced or modelspec=edenn_studio."
            ),
        )
    return normalized_music_style_prompt, normalized_lyrics_prompt


async def _prepare_vocal_clone_request_input(
    *,
    vocal_id: Optional[str],
    vocal_sample: UploadFile | None,
    vocal_sample_url: Optional[str],
    vocal_source_dir: Path,
    requested_modelspec: str,
) -> tuple[Optional[str], Optional[Path]]:
    vocal_sample_path = await resolve_optional_media_source_to_disk(
        upload=vocal_sample,
        remote_url=vocal_sample_url,
        destination_dir=vocal_source_dir,
        fallback_filename="vocal_sample.m4a",
        asset_label="vocal sample",
    )
    normalized_vocal_id = (vocal_id or "").strip() or None
    if normalized_vocal_id and vocal_sample_path is not None:
        raise EdennApiError(
            "Provide either vocal_id or vocal_sample/vocal_sample_url, not both.",
            public_message="Provide either vocal_id or vocal_sample/vocal_sample_url, not both.",
            status_code=400,
            component="api",
            operation="validate_vocal_clone_inputs",
        )
    if requested_modelspec != "edenn_enhanced" and (
        normalized_vocal_id or vocal_sample_path is not None
    ):
        raise EdennApiError(
            "Vocal clone inputs are only supported for modelspec=edenn_enhanced.",
            public_message="Vocal clone inputs are only supported for modelspec=edenn_enhanced.",
            status_code=400,
            component="api",
            operation="validate_vocal_clone_modelspec",
        )
    prepared_vocal_sample_path = (
        prepare_audio_for_provider_b_vocal_clone(
            source_audio_path=vocal_sample_path,
            destination_dir=vocal_source_dir,
        )
        if vocal_sample_path is not None
        else None
    )
    return normalized_vocal_id, prepared_vocal_sample_path


def _public_async_error_payload(error: EdennError) -> dict[str, Any]:
    entry = _resolve_error_code(error)
    message = error.public_message if entry.code // 1000 == 10 else entry.message
    if contains_provider_token(message):
        message = _PUBLIC_PROVIDER_FAILURE_MESSAGE

    return ErrorDetail(
        error_code=entry.code,
        message=message,
        retryable=entry.retryable,
    ).model_dump()


def _optional_text(value: Any) -> Optional[str]:
    """Normalize optional coarse-filter text columns."""

    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_text(*values: Any) -> Optional[str]:
    """Return the first non-empty text value from a list of candidates."""

    for value in values:
        text = _optional_text(value)
        if text:
            return text
    return None


def _async_job_ttl_seconds() -> int:
    raw_value = os.getenv(
        "ASYNC_VIDEO_MUSIC_JOB_TTL_SECONDS",
        str(_ASYNC_JOB_TTL_SECONDS_DEFAULT),
    )
    try:
        return max(0, int(raw_value))
    except ValueError:
        return _ASYNC_JOB_TTL_SECONDS_DEFAULT


def _get_async_video_job_store(context: ApiContext | None = None) -> Any:
    if context is not None and getattr(context, "async_video_job_store", None) is not None:
        return context.async_video_job_store
    if _async_video_job_store_override is not None:
        return _async_video_job_store_override

    global _async_video_job_store
    if _async_video_job_store is None:
        _async_video_job_store = build_async_video_job_store_from_env(
            memory_store=_memory_async_video_job_store,
        )
    return _async_video_job_store


def _set_async_video_job_store_for_testing(store: Any | None) -> None:
    global _async_video_job_store_override
    _async_video_job_store_override = store


def _cleanup_expired_async_jobs(
    *,
    now: Optional[int] = None,
    context: ApiContext | None = None,
) -> None:
    ttl_seconds = _async_job_ttl_seconds()
    if ttl_seconds <= 0:
        return
    _get_async_video_job_store(context).cleanup_expired(
        ttl_seconds=ttl_seconds,
        now=now,
    )


def _register_async_job(
    job_id: str,
    *,
    context: ApiContext | None = None,
) -> _AsyncJobState:
    _cleanup_expired_async_jobs(context=context)
    return _get_async_video_job_store(context).register(job_id)


def _set_async_job_processing(
    job_id: str,
    *,
    context: ApiContext | None = None,
) -> _AsyncJobState:
    return _get_async_video_job_store(context).set_processing(job_id)


def _complete_async_job(
    job_id: str,
    result: "VideoJobResponse",
    *,
    context: ApiContext | None = None,
) -> _AsyncJobState:
    store = _get_async_video_job_store(context)
    stored_result = (
        result
        if isinstance(store, InMemoryAsyncVideoJobStore)
        else result.model_dump(mode="json")
    )
    return store.complete(job_id, stored_result)


def _fail_async_job(
    job_id: str,
    error: dict[str, Any],
    *,
    context: ApiContext | None = None,
) -> _AsyncJobState:
    return _get_async_video_job_store(context).fail(job_id, error)


def _get_async_job_state(
    job_id: str,
    *,
    context: ApiContext | None = None,
) -> Optional[_AsyncJobState]:
    _cleanup_expired_async_jobs(context=context)
    return _get_async_video_job_store(context).get(job_id)


def _async_video_job_max_in_flight_per_replica() -> int:
    raw_value = os.getenv("ASYNC_VIDEO_MUSIC_MAX_IN_FLIGHT_PER_REPLICA", "1")
    try:
        return max(1, int(raw_value))
    except ValueError:
        return 1


def _get_async_video_job_semaphore() -> asyncio.Semaphore:
    global _async_video_job_semaphore, _async_video_job_semaphore_limit
    limit = _async_video_job_max_in_flight_per_replica()
    if _async_video_job_semaphore is None or _async_video_job_semaphore_limit != limit:
        _async_video_job_semaphore = asyncio.Semaphore(limit)
        _async_video_job_semaphore_limit = limit
    return _async_video_job_semaphore


def _state_result_model(state: _AsyncJobState) -> Optional["VideoJobResponse"]:
    result = state.result
    if result is None:
        return None
    if isinstance(result, VideoJobResponse):
        return result
    return VideoJobResponse.model_validate(result)


def _state_result_json(state: _AsyncJobState) -> Optional[dict[str, Any]]:
    result = state.result
    if result is None:
        return None
    if isinstance(result, VideoJobResponse):
        return result.model_dump(mode="json")
    if isinstance(result, dict):
        return result
    return VideoJobResponse.model_validate(result).model_dump(mode="json")


def _creation_api_call_count(result: Any) -> int:
    raw_value = getattr(result, "generation_api_call_count", 1)
    try:
        return max(0, int(raw_value or 0))
    except (TypeError, ValueError):
        return 1


def _token_usage_counts_for_cost(
    raw_value: Any,
) -> Optional[TokenUsageCountsResponse]:
    if raw_value is None:
        return None
    if isinstance(raw_value, TokenUsageCountsResponse):
        return raw_value
    if isinstance(raw_value, BaseModel):
        raw_value = raw_value.model_dump()
    if not isinstance(raw_value, dict):
        return None
    return normalize_token_usage_counts(raw_value)


def _token_cost_usd_from_baseline(raw_token_usage: Any) -> Optional[float]:
    counts = _token_usage_counts_for_cost(raw_token_usage)
    if counts is None:
        return None

    prompt_tokens = max(0, int(counts.prompt_tokens or 0))
    completion_tokens = max(0, int(counts.completion_tokens or 0))
    total_tokens = max(0, int(counts.total_tokens or 0))
    if prompt_tokens == 0 and completion_tokens == 0 and total_tokens > 0:
        prompt_tokens = total_tokens

    token_cost = (
        prompt_tokens * BASELINE_INPUT_TOKEN_COST_PER_1M_USD
        + completion_tokens * BASELINE_OUTPUT_TOKEN_COST_PER_1M_USD
    ) / 1_000_000
    return round(
        token_cost * BASELINE_TOKEN_COST_MULTIPLIER,
        TOKEN_COST_DECIMAL_PLACES,
    )


def _token_num_from_usage(raw_token_usage: Any) -> Optional[int]:
    counts = _token_usage_counts_for_cost(raw_token_usage)
    if counts is None:
        return None

    total_tokens = max(0, int(counts.total_tokens or 0))
    if total_tokens > 0:
        return total_tokens
    return max(0, int(counts.prompt_tokens or 0)) + max(
        0,
        int(counts.completion_tokens or 0),
    )


def _video_generation_cost_response(
    result: Any,
    *,
    raw_token_usage: Any = None,
    token_cost: Optional[int] = None,
    total_cost_override: Optional[float] = None,
    creative_duration: Optional[float] = None,
) -> VideoGenerationCostResponse:
    """Build the cost block.

    ``total_cost`` is the authoritative charge. By default it is the
    generation-call cost plus the planning token cost; a pipeline that prices its
    output differently (multi-image bills by output duration) passes
    ``total_cost_override`` to set it directly. The generation/token breakdown is
    always populated for internal accounting regardless.
    """
    if raw_token_usage is None and token_cost is not None:
        raw_token_usage = {
            "prompt_tokens": token_cost,
            "completion_tokens": 0,
            "total_tokens": token_cost,
        }
    creation_times = _creation_api_call_count(result)
    creation_cost = round(
        creation_times * CREATION_API_CALL_UNIT_COST_USD,
        CREATION_COST_DECIMAL_PLACES,
    )
    token_cost_usd = _token_cost_usd_from_baseline(raw_token_usage)
    total_cost = (
        round(float(total_cost_override), CREATION_COST_DECIMAL_PLACES)
        if total_cost_override is not None
        else round(creation_cost + float(token_cost_usd or 0.0), CREATION_COST_DECIMAL_PLACES)
    )
    return VideoGenerationCostResponse(
        total_cost=total_cost,
        creative_duration=(
            round(float(creative_duration), CREATION_COST_DECIMAL_PLACES)
            if creative_duration is not None
            else None
        ),
        creation_cost=creation_cost,
        creation_times=creation_times,
        token_num=_token_num_from_usage(raw_token_usage),
        token_cost=token_cost_usd,
    )


@dataclass(frozen=True)
class VideoJobAssets:
    """Uploaded media the response points at.

    URLs only: the storage blob names stay server-side (the v2 status route
    re-signs from the artifact rows, not from the response body).
    """

    audio_url: Optional[str] = None
    audio_duration_s: Optional[float] = None
    audio_size_bytes: Optional[int] = None
    complete_audio_url: Optional[str] = None
    complete_audio_duration_s: Optional[float] = None
    complete_audio_size_bytes: Optional[int] = None
    video_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    video_size_bytes: Optional[int] = None


def local_file_size_bytes(path: Any) -> Optional[int]:
    """Best-effort byte size of a local media file, None if unreadable/gone."""
    if not path:
        return None
    try:
        return Path(str(path)).stat().st_size
    except OSError:
        return None


def _lyrics_models(items: Any) -> List[LyricsTimestampModel]:
    return [
        LyricsTimestampModel(
            text=getattr(ts, "text", ""),
            startS=float(getattr(ts, "startS", 0.0)),
            endS=float(getattr(ts, "endS", 0.0)),
            i=getattr(ts, "i", None),
        )
        for ts in (items or [])
    ]


def build_video_job_response(
    *,
    job_id: str,
    result: Any,
    requested_modelspec: str,
    assets: VideoJobAssets,
) -> VideoJobResponse:
    """Assemble the video-music job response.

    The single place the 5-block shape is built: the sync endpoint, the legacy
    async runner, the v2 workers (monolith and split-finalize) and the CLI all
    route through here, so a field can only ever land in one block.
    """
    video_summary = getattr(result, "video_summary", None)
    music_title = _music_title_from_summary(video_summary)
    # music_title is surfaced once, in audio_metadata; strip the duplicate that the
    # planning model wrote into the summary dict.
    public_summary: Dict[str, Any] | str
    if isinstance(video_summary, dict):
        public_summary = {k: v for k, v in video_summary.items() if k != "music_title"}
    else:
        public_summary = video_summary if video_summary is not None else {}
    video_geometry = VideoGeometry.model_validate(
        _public_video_metadata(getattr(result, "video_metadata", None))
    )
    response = VideoJobResponse(
        job_id=job_id,
        modelspec=(
            getattr(result, "used_music_model_spec", None) or requested_modelspec
        ),
        response_metadata=ResponseMetadata(
            job_received_timestamp=getattr(result, "job_received_timestamp", None),
            job_finished_timestamp=getattr(result, "job_finished_timestamp", None),
            compression_applied=getattr(result, "compression_applied", False),
        ),
        cost_metadata=_video_generation_cost_response(
            result,
            raw_token_usage=getattr(result, "token_usage", None) or None,
            creative_duration=video_geometry.duration,
        ),
        video_metadata=VideoMetadataBlock(
            video_url=assets.video_url,
            thumbnail_url=assets.thumbnail_url,
            video_size_bytes=(
                assets.video_size_bytes
                if assets.video_size_bytes is not None
                else local_file_size_bytes(
                    getattr(result, "remixed_video_path", None)
                )
            ),
            geometry=video_geometry,
            video_summary=public_summary,
        ),
        audio_metadata=AudioMetadataBlock(
            audio_url=assets.audio_url,
            audio_duration_s=assets.audio_duration_s,
            audio_size_bytes=assets.audio_size_bytes,
            complete_audio_url=assets.complete_audio_url,
            complete_audio_duration_s=assets.complete_audio_duration_s,
            complete_audio_size_bytes=assets.complete_audio_size_bytes,
            music_title=music_title,
            music_description=_music_prompt_text(getattr(result, "music_prompt", None)),
            full_lyrics=getattr(result, "primary_full_lyrics", None),
            full_lyrics_timestamps=_lyrics_models(
                getattr(result, "primary_full_lyrics_timestamps", None)
            ),
            full_word_level_lyrics_timestamps=_lyrics_models(
                getattr(result, "primary_full_word_level_lyrics_timestamps", None)
            ),
            lyrics_timestamps=_lyrics_models(
                getattr(result, "lyrics_timestamps", None)
            ),
            word_level_lyrics_timestamps=_lyrics_models(
                getattr(result, "word_level_lyrics_timestamps", None)
            ),
        ),
    )
    # Final guardrail: no upstream brand/model token reaches the client, whatever
    # the LLM or provider wrote into the free-text fields.
    return guard_response_model(response)


def _normalize_async_vocal_gender(value: str) -> str:
    gender = (value or "").strip().lower() or "female"
    if gender not in {"female", "male"}:
        raise HTTPException(
            status_code=400,
            detail="vocal_gender must be 'female' or 'male'.",
        )
    return gender


def _validate_async_music_volume(value: float) -> None:
    if value < 0.0 or value > 1.0:
        raise HTTPException(
            status_code=400,
            detail="music_volume must be between 0.0 and 1.0.",
        )


def _normalize_async_callback_url(value: Optional[str]) -> Optional[str]:
    url = (value or "").strip()
    if not url:
        return None

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(
            status_code=400,
            detail="callback_url must be an absolute http or https URL.",
        )

    hostname = parsed.hostname or ""
    if hostname.lower() in {"localhost", "localhost.localdomain"}:
        raise HTTPException(
            status_code=400,
            detail="callback_url must not target localhost.",
        )

    try:
        host_ip = ip_address(hostname)
    except ValueError:
        return url

    if host_ip.is_private or host_ip.is_loopback or host_ip.is_link_local:
        raise HTTPException(
            status_code=400,
            detail="callback_url must not target private or local network addresses.",
        )
    return url


async def _compute_recommendation_embeddings(
    *,
    user_prompt: str,
    music_prompt_json: dict[str, Any] | None,
    logger: Any,
) -> tuple[Optional[list[float]], Optional[list[float]]]:
    """Compute user_prompt + music_prompt embeddings for the recommendation index.

    Fail-open: if the model gateway is unavailable or any step errors, log a warning
    and return ``(None, None)`` so the surrounding video-generation request still
    succeeds. The recommendation backfill script can fill missing rows later.
    """

    user_text = (user_prompt or "").strip()
    music_text = ""
    if music_prompt_json:
        try:
            import json as _json
            music_text = _json.dumps(music_prompt_json, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            music_text = str(music_prompt_json)

    if not user_text and not music_text:
        return None, None

    try:
        from EdennCode.ModelFactory.LanguageModelFactory.gateway_clients import (
            make_async_gateway_client,
        )
        client = make_async_gateway_client()
        deployment = os.environ.get(
            "MODEL_GATEWAY_EMBEDDING_DEPLOYMENT", "embed-standard"
        )
        try:
            resp = await client.embeddings.create(
                input=[user_text or " ", music_text or " "],
                model=deployment,
            )
        finally:
            await client.close()
    except Exception as exc:
        logger.warning(
            "Recommendation embedding computation failed; rows will be backfilled later: %s",
            exc,
            exc_info=True,
        )
        return None, None

    user_emb = resp.data[0].embedding if user_text else None
    music_emb = resp.data[1].embedding if music_text else None
    return user_emb, music_emb


def _build_recommendation_payload(
    *,
    result: VideoGenerationResult,
    asset_ids: RecommendationAssetIds,
    job_id: str,
    user_prompt: str,
    requested_modelspec: str,
    preserve_original_audio: bool,
    requested_volume: float,
    effective_input_video_path: Path,
    upload_content_type: Optional[str],
    compression_applied: bool,
    upload_blob: Optional[str],
    upload_url: Optional[str],
    audio_blob: Optional[str],
    audio_url: Optional[str],
    complete_audio_blob: Optional[str],
    complete_audio_url: Optional[str],
    secondary_complete_audio_blob: Optional[str],
    secondary_complete_audio_url: Optional[str],
    video_blob: Optional[str],
    video_url: Optional[str],
    thumbnail_blob: Optional[str],
    thumbnail_url: Optional[str],
    scenes: List[SceneModel],
    creator_user_id: Optional[str] = None,
    user_prompt_embedding: Optional[list[float]] = None,
    music_embedding: Optional[list[float]] = None,
) -> VideoGenerationRecommendationPayload:
    """Build the canonical recommender payload from a completed API request."""

    video_id = getattr(result, "video_id", None) or asset_ids.video_id
    creative_id = getattr(result, "creative_id", None) or asset_ids.creative_id
    primary_music_id = (
        getattr(result, "primary_music_id", None) or asset_ids.primary_music_id
    )
    secondary_music_id = getattr(result, "secondary_music_id", None)
    selected_music_id = (
        getattr(result, "selected_music_id", None)
        or asset_ids.selected_music_id
        or primary_music_id
    )
    alignment_id = getattr(result, "alignment_id",
                           None) or asset_ids.alignment_id
    model_spec = getattr(result, "used_music_model_spec",
                         None) or requested_modelspec
    video_metadata = result.video_metadata
    video_metadata_json = (
        video_metadata.to_dict()
        if hasattr(video_metadata, "to_dict")
        else _dict_value(video_metadata)
    )
    video_summary = _dict_value(getattr(result, "video_summary", None))
    music_prompt = _dict_value(getattr(result, "music_prompt", None))
    scene_summary_json = _scenes_to_json(scenes)
    duration_s = _safe_float(getattr(video_metadata, "duration", None))
    alignment_score = _safe_float(
        getattr(result, "alignment_score", None)) or 0.0
    selected_clip_start_s = _safe_float(
        getattr(result, "music_start_s", None)) or 0.0
    alignment_details = _dict_value(getattr(result, "alignment_details", None))
    complete_music_path = getattr(
        result, "complete_generated_music_path", None)
    secondary_complete_music_path = getattr(
        result, "secondary_complete_generated_music_path", None
    )
    thumbnail_path = getattr(result, "thumbnail_path", None)

    visual_feature_json = {
        "video_metadata": video_metadata_json,
        "video_summary": video_summary,
        "video_title": getattr(result, "video_title", "") or "",
        "video_description": getattr(result, "video_description", "") or "",
        "scenes": scene_summary_json,
    }
    music_feature_json = {
        "music_prompt": music_prompt,
        "include_vocals": bool(getattr(result, "include_vocals", False)),
        "vocal_gender": getattr(result, "vocal_gender", "") or "",
        "model_spec": model_spec,
        "matching_used_track": getattr(result, "matching_used_track", None),
        "vocal_id_used": getattr(result, "vocal_id_used", None),
    }
    token_usage_json = {
        "total": getattr(result, "token_usage", None) or {},
        "breakdown": getattr(result, "token_usage_breakdown", None) or {},
    }

    video_asset = VideoAssetRecord(
        video_id=video_id,
        job_id=job_id,
        source_video_blob=upload_blob,
        source_video_url=upload_url,
        source_video_path=str(effective_input_video_path),
        duration_s=duration_s,
        width=_safe_int(getattr(video_metadata, "width", None)),
        height=_safe_int(getattr(video_metadata, "height", None)),
        fps=_safe_float(getattr(video_metadata, "fps", None)),
        content_type=_guess_input_video_content_type(
            source_path=effective_input_video_path,
            upload_content_type=upload_content_type,
            compression_applied=compression_applied,
        ),
        scene_summary_json=scene_summary_json,
        visual_feature_json=visual_feature_json,
    )
    primary_music_asset = MusicAssetRecord(
        music_id=primary_music_id,
        job_id=job_id,
        variant_label="primary",
        full_audio_blob=complete_audio_blob,
        full_audio_url=complete_audio_url,
        full_audio_path=str(
            complete_music_path) if complete_music_path else None,
        matched_audio_blob=audio_blob,
        matched_audio_url=audio_url,
        matched_audio_path=str(result.generated_music_path),
        lyrics_text=getattr(result, "primary_full_lyrics", None),
        lyrics_timestamp_json=_word_timestamps_to_json(
            getattr(result, "primary_full_word_level_lyrics_timestamps", None)
            or getattr(result, "primary_full_lyrics_timestamps", None)
            or getattr(result, "lyrics_timestamps", None)
        ),
        music_feature_json=music_feature_json,
    )
    secondary_music_asset = None
    if secondary_music_id and secondary_complete_music_path:
        secondary_music_asset = MusicAssetRecord(
            music_id=secondary_music_id,
            job_id=job_id,
            variant_label="secondary",
            full_audio_blob=secondary_complete_audio_blob,
            full_audio_url=secondary_complete_audio_url,
            full_audio_path=str(secondary_complete_music_path),
            lyrics_text=getattr(result, "secondary_full_lyrics", None),
            lyrics_timestamp_json=_word_timestamps_to_json(
                getattr(result, "secondary_full_word_level_lyrics_timestamps", None)
                or getattr(result, "secondary_full_lyrics_timestamps", None)
            ),
            music_feature_json=music_feature_json,
        )

    generation_job = GenerationJobRecord(
        job_id=job_id,
        video_id=video_id,
        creative_id=creative_id,
        primary_music_id=primary_music_id,
        secondary_music_id=secondary_music_id if secondary_music_asset else None,
        selected_music_id=selected_music_id,
        alignment_id=alignment_id,
        model_spec=model_spec,
        include_vocals=bool(getattr(result, "include_vocals", False)),
        vocal_gender=getattr(result, "vocal_gender", "") or "",
        user_prompt=user_prompt,
        user_requested_language=getattr(
            result, "user_requested_language", "") or "",
        preserve_original_audio=preserve_original_audio,
        music_volume=requested_volume,
        token_usage_json=token_usage_json,
        job_received_timestamp=getattr(result, "job_received_timestamp", None),
        job_finished_timestamp=getattr(result, "job_finished_timestamp", None),
        creator_user_id=creator_user_id,
        user_prompt_embedding=user_prompt_embedding,
    )
    creative = CreativeRecord(
        creative_id=creative_id,
        job_id=job_id,
        video_id=video_id,
        selected_music_id=selected_music_id,
        alignment_id=alignment_id,
        title=getattr(result, "video_title", "") or "",
        description=getattr(result, "video_description", "") or "",
        result_video_blob=video_blob,
        result_video_url=video_url,
        result_video_path=str(result.remixed_video_path),
        thumbnail_blob=thumbnail_blob,
        thumbnail_url=thumbnail_url,
        thumbnail_path=str(thumbnail_path) if thumbnail_path else None,
        creator_user_id=creator_user_id,
    )
    alignment = AlignmentRecord(
        alignment_id=alignment_id,
        job_id=job_id,
        creative_id=creative_id,
        video_id=video_id,
        music_id=selected_music_id,
        alignment_score=alignment_score,
        selected_clip_start_s=selected_clip_start_s,
        selected_clip_duration_s=duration_s,
        matching_used_track=getattr(result, "matching_used_track", None),
        alignment_reason_json=alignment_details,
    )
    feature_snapshot = CreativeFeatureSnapshotRecord(
        creative_id=creative_id,
        video_id=video_id,
        selected_music_id=selected_music_id,
        alignment_id=alignment_id,
        job_id=job_id,
        language=getattr(result, "user_requested_language", "") or "",
        music_model_spec=model_spec,
        include_vocals=bool(getattr(result, "include_vocals", False)),
        vocal_gender=getattr(result, "vocal_gender", "") or "",
        genre_level1=_optional_text(music_prompt.get("genre_level1")),
        content_type=_first_text(
            video_summary.get("content_type"),
            video_summary.get("video_category"),
            video_summary.get("category"),
        ),
        tempo_bpm=_safe_float(music_prompt.get("tempo_bpm")),
        energy_level=_optional_text(music_prompt.get("energy_level")),
        overall_mood=_first_text(
            video_summary.get("overall_mood"),
            music_prompt.get("global_mood"),
        ),
        platform_hint=_optional_text(music_prompt.get("platform_hint")),
        alignment_score=alignment_score,
        visual_feature_json=visual_feature_json,
        music_feature_json=music_feature_json,
        scene_summary_json=scene_summary_json,
        music_prompt_json=music_prompt,
        music_embedding=music_embedding,
    )
    return VideoGenerationRecommendationPayload(
        generation_job=generation_job,
        creative=creative,
        video_asset=video_asset,
        primary_music_asset=primary_music_asset,
        secondary_music_asset=secondary_music_asset,
        alignment=alignment,
        feature_snapshot=feature_snapshot,
    )


def _persist_recommendation_payload(
    *,
    context: ApiContext,
    payload: VideoGenerationRecommendationPayload,
) -> None:
    """Persist recommender records after the response path has succeeded."""

    persistence = getattr(context, "recommendation_persistence", None)
    if persistence is None:
        return

    try:
        persistence.persist_video_generation(payload)
    except Exception as exc:
        context.logger.warning(
            "Recommendation persistence failed for job %s: %s",
            payload.generation_job.job_id,
            exc,
            exc_info=True,
        )


def _guess_input_video_content_type(
    *,
    source_path: Path,
    upload_content_type: Optional[str],
    compression_applied: bool,
) -> str:
    if compression_applied:
        return "video/mp4"
    if upload_content_type:
        return upload_content_type

    lowered = str(source_path).lower()
    if lowered.endswith(".mp4"):
        return "video/mp4"
    if lowered.endswith(".mov"):
        return "video/quicktime"
    if lowered.endswith(".webm"):
        return "video/webm"
    if lowered.endswith(".mkv"):
        return "video/x-matroska"
    if lowered.endswith(".avi"):
        return "video/x-msvideo"
    return "application/octet-stream"


def _reusable_input_source_url(
    input_source: VideoGenerationInputSource,
    *,
    compression_applied: bool,
) -> Optional[str]:
    if compression_applied:
        return None
    return input_source.source_url


async def _resolve_video_generation_input_source(
    *,
    video: UploadFile | None,
    video_url: Optional[str],
    destination_dir: Path,
    job_id: str,
    logger: Any,
) -> VideoGenerationInputSource:
    url_value = (video_url or "").strip()
    if url_value:
        try:
            filename = filename_from_url(url_value) or "input_video.mp4"
            local_video_path = destination_dir / filename
            logger.info(
                "Job %s: downloading input video from %s to %s",
                job_id,
                url_value,
                local_video_path,
            )
            resolved_path = await download_public_file_to_disk(
                url=url_value,
                destination=local_video_path,
                asset_label="video",
            )
            return VideoGenerationInputSource(
                path=resolved_path,
                content_type=None,
                source_url=url_value,
            )
        finally:
            if video is not None:
                await video.close()

    if video is None:
        raise EdennApiError(
            "Provide a video upload or video_url.",
            public_message="Provide a video upload or video_url.",
            status_code=400,
            component="api",
            operation="resolve_video_generation_input_source",
        )

    filename = sanitize_filename(video.filename or "upload.mp4")
    local_video_path = destination_dir / filename
    logger.info("Job %s: saving upload to %s", job_id, local_video_path)
    resolved_path = await write_upload_to_disk(video, local_video_path)
    return VideoGenerationInputSource(
        path=resolved_path,
        content_type=video.content_type,
        source_url=None,
    )


async def _fire_callback(
    url: str,
    job_id: str,
    state: _AsyncJobState,
    *,
    logger: Any,
) -> None:
    """Best-effort POST of job status to the caller-supplied callback URL."""
    payload: dict[str, Any] = {
        "job_id": job_id,
        "status": state.status,
        "result": _state_result_json(state),
        "error": state.error,
        "created_at": state.created_at,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=payload)
            if response.status_code >= 400:
                logger.warning(
                    "Async job %s callback returned HTTP %s for %s",
                    job_id,
                    response.status_code,
                    url,
                )
    except Exception as exc:
        logger.warning(
            "Async job %s callback delivery failed for %s: %s",
            job_id,
            url,
            exc,
            exc_info=True,
        )


async def _run_async_video_job_inner(
    *,
    context: ApiContext,
    job_id: str,
    job_dir: Path,
    asset_ids: RecommendationAssetIds,
    effective_input_video_path: Path,
    upload_content_type: Optional[str],
    compression_applied: bool,
    preserve_original_audio: bool,
    requested_volume: float,
    water_mark: bool = False,
    include_vocals: bool,
    vocal_gender: str,
    user_prompt: str,
    verbose_instruction: bool,
    music_style_prompt: Optional[str],
    lyrics_prompt: Optional[str],
    requested_modelspec: str,
    audio_output_format: Optional[str],
    vocal_id: Optional[str],
    vocal_sample_path: Optional[Path],
    user_id: Optional[str],
    principal: Optional[Principal] = None,
    callback_url: Optional[str],
    input_source_url: Optional[str] = None,
) -> None:
    """
    Background coroutine for POST /api/v1/jobs/async_video_music_gen.

    Runs the full video-music generation pipeline (workflow → blob uploads →
    recommendation persistence) after the HTTP response has already been
    returned to the caller. Writes final status + result into _async_job_store
    and optionally POSTs them to callback_url.
    """
    _set_async_job_processing(job_id, context=context)
    try:
        result: VideoGenerationResult = await context.workflow.run(
            video_path=effective_input_video_path,
            preserve_original_audio=preserve_original_audio,
            music_volume=requested_volume,
            water_mark=water_mark,
            include_vocals=include_vocals,
            vocal_gender=vocal_gender,
            user_prompt=user_prompt,
            verbose_instruction=verbose_instruction,
            music_style_prompt=music_style_prompt or None,
            lyrics_prompt=lyrics_prompt or None,
            modelspec=requested_modelspec,
            audio_output_format=audio_output_format,
            vocal_id=vocal_id,
            vocal_sample_path=vocal_sample_path,
            job_id=job_id,
            video_id=asset_ids.video_id,
            creative_id=asset_ids.creative_id,
            primary_music_id=asset_ids.primary_music_id,
            secondary_music_id=asset_ids.secondary_music_id,
            selected_music_id=asset_ids.selected_music_id,
            alignment_id=asset_ids.alignment_id,
        )

        # ------------------------------------------------------------------
        # Blob uploads
        # ------------------------------------------------------------------
        upload_blob = upload_url = None
        audio_blob = audio_url = None
        complete_audio_blob = complete_audio_url = None
        secondary_complete_audio_blob = secondary_complete_audio_url = None
        video_blob = video_url_value = None
        thumbnail_blob = thumbnail_url = None

        thumbnail_path_value = getattr(result, "thumbnail_path", None)
        complete_music_path = getattr(result, "complete_generated_music_path", None)
        secondary_complete_music_path = getattr(
            result, "secondary_complete_generated_music_path", None
        )
        # Probe the tracks from local files (before cleanup).
        audio_duration_s, audio_size_bytes = probe_audio_metrics(
            result.generated_music_path
        )
        complete_audio_duration_s, complete_audio_size_bytes = probe_audio_metrics(
            complete_music_path
        )
        secondary_complete_audio_duration_s, secondary_complete_audio_size_bytes = (
            probe_audio_metrics(
                secondary_complete_music_path
                if secondary_complete_music_path
                and secondary_complete_music_path.exists()
                else None
            )
        )
        audio_content_type = guess_audio_content_type(result.generated_music_path)
        complete_audio_content_type = (
            guess_audio_content_type(complete_music_path)
            if complete_music_path
            else "audio/mpeg"
        )
        secondary_complete_audio_content_type = (
            guess_audio_content_type(secondary_complete_music_path)
            if secondary_complete_music_path
            else "audio/mpeg"
        )

        if context.storage.enabled:
            if input_source_url:
                upload_url = input_source_url
            else:
                staged_upload_content_type = _guess_input_video_content_type(
                    source_path=effective_input_video_path,
                    upload_content_type=upload_content_type,
                    compression_applied=compression_applied,
                )
                upload_blob = context.storage.upload_path(
                    container=context.settings.upload_container,
                    path=effective_input_video_path,
                    blob_name=_provider_neutral_blob_name(
                        job_id=job_id,
                        folder="input",
                        label="source_video",
                        source_path=effective_input_video_path,
                    ),
                    content_type=staged_upload_content_type,
                )
            audio_blob = context.storage.upload_path(
                container=context.settings.audio_container_name,
                path=result.generated_music_path,
                blob_name=_provider_neutral_blob_name(
                    job_id=job_id,
                    folder="audio",
                    label="matched_audio",
                    source_path=result.generated_music_path,
                ),
                content_type=audio_content_type,
            )
            if complete_music_path and complete_music_path.exists():
                complete_audio_blob = context.storage.upload_path(
                    container=context.settings.audio_container_name,
                    path=complete_music_path,
                    blob_name=_provider_neutral_blob_name(
                        job_id=job_id,
                        folder="audio/complete",
                        label="complete_audio",
                        source_path=complete_music_path,
                    ),
                    content_type=complete_audio_content_type,
                )
            if secondary_complete_music_path and secondary_complete_music_path.exists():
                secondary_complete_audio_blob = context.storage.upload_path(
                    container=context.settings.audio_container_name,
                    path=secondary_complete_music_path,
                    blob_name=_provider_neutral_blob_name(
                        job_id=job_id,
                        folder="audio/complete/secondary",
                        label="secondary_audio",
                        source_path=secondary_complete_music_path,
                    ),
                    content_type=secondary_complete_audio_content_type,
                )
            video_blob = context.storage.upload_path(
                container=context.settings.output_container,
                path=result.remixed_video_path,
                blob_name=_provider_neutral_blob_name(
                    job_id=job_id,
                    folder="video",
                    label="remixed_video",
                    source_path=result.remixed_video_path,
                ),
                content_type="video/mp4",
            )
            if thumbnail_path_value and thumbnail_path_value.exists():
                try:
                    thumbnail_blob = context.storage.upload_path(
                        container=context.settings.output_container,
                        blob_name=_provider_neutral_blob_name(
                            job_id=job_id,
                            folder="thumbnail",
                            label="thumbnail",
                            source_path=thumbnail_path_value,
                        ),
                        path=thumbnail_path_value,
                        content_type=guess_image_content_type(thumbnail_path_value),
                    )
                except Exception as exc:
                    context.logger.warning(
                        "Async job %s thumbnail upload failed: %s", job_id, exc
                    )

            if upload_blob:
                upload_url = context.storage.generate_sas_url(
                    container=context.settings.upload_container,
                    blob_name=upload_blob,
                )
            if audio_blob:
                audio_url = context.storage.generate_sas_url(
                    container=context.settings.audio_container_name,
                    blob_name=audio_blob,
                )
            if complete_audio_blob:
                complete_audio_url = context.storage.generate_sas_url(
                    container=context.settings.audio_container_name,
                    blob_name=complete_audio_blob,
                )
            if secondary_complete_audio_blob:
                secondary_complete_audio_url = context.storage.generate_sas_url(
                    container=context.settings.audio_container_name,
                    blob_name=secondary_complete_audio_blob,
                )
            if video_blob:
                video_url_value = context.storage.generate_sas_url(
                    container=context.settings.output_container,
                    blob_name=video_blob,
                )
            if thumbnail_blob:
                thumbnail_url = context.storage.generate_sas_url(
                    container=context.settings.output_container,
                    blob_name=thumbnail_blob,
                )

        # ------------------------------------------------------------------
        # Build response object
        # ------------------------------------------------------------------
        scenes = [_scene_to_model(scene) for scene in result.scenes]

        response_video_id = getattr(result, "video_id", None) or asset_ids.video_id
        response_creative_id = getattr(result, "creative_id", None) or asset_ids.creative_id
        response_primary_music_id = (
            getattr(result, "primary_music_id", None) or asset_ids.primary_music_id
        )
        response_secondary_music_id = getattr(result, "secondary_music_id", None)
        response_selected_music_id = (
            getattr(result, "selected_music_id", None)
            or asset_ids.selected_music_id
            or response_primary_music_id
        )
        response_alignment_id = getattr(result, "alignment_id", None) or asset_ids.alignment_id

        # ------------------------------------------------------------------
        # Recommendation persistence
        # ------------------------------------------------------------------
        if getattr(context, "recommendation_persistence", None) is not None:
            user_prompt_emb, music_emb = await _compute_recommendation_embeddings(
                user_prompt=user_prompt,
                music_prompt_json=_dict_value(getattr(result, "music_prompt", None)),
                logger=context.logger,
            )
            persistence_payload = _build_recommendation_payload(
                result=result,
                asset_ids=RecommendationAssetIds(
                    job_id=job_id,
                    video_id=response_video_id,
                    creative_id=response_creative_id,
                    primary_music_id=response_primary_music_id,
                    secondary_music_id=response_secondary_music_id,
                    selected_music_id=response_selected_music_id,
                    alignment_id=response_alignment_id,
                ),
                job_id=job_id,
                user_prompt=user_prompt,
                requested_modelspec=requested_modelspec,
                preserve_original_audio=preserve_original_audio,
                requested_volume=requested_volume,
                effective_input_video_path=effective_input_video_path,
                upload_content_type=upload_content_type,
                compression_applied=compression_applied,
                upload_blob=upload_blob,
                upload_url=upload_url,
                audio_blob=audio_blob,
                audio_url=audio_url,
                complete_audio_blob=complete_audio_blob,
                complete_audio_url=complete_audio_url,
                secondary_complete_audio_blob=secondary_complete_audio_blob,
                secondary_complete_audio_url=secondary_complete_audio_url,
                video_blob=video_blob,
                video_url=video_url_value,
                thumbnail_blob=thumbnail_blob,
                thumbnail_url=thumbnail_url,
                scenes=scenes,
                creator_user_id=user_id,
                user_prompt_embedding=user_prompt_emb,
                music_embedding=music_emb,
            )
            _persist_recommendation_payload(context=context, payload=persistence_payload)

        response_payload = build_video_job_response(
            job_id=job_id,
            result=result,
            requested_modelspec=requested_modelspec,
            assets=VideoJobAssets(
                audio_url=audio_url,
                audio_duration_s=audio_duration_s,
                audio_size_bytes=audio_size_bytes,
                complete_audio_url=complete_audio_url,
                complete_audio_duration_s=complete_audio_duration_s,
                complete_audio_size_bytes=complete_audio_size_bytes,
                video_url=video_url_value,
                thumbnail_url=thumbnail_url,
            ),
        )
        _complete_async_job(job_id, response_payload, context=context)
        if context.usage_recorder is not None:
            context.usage_recorder.record_job(
                job_id=job_id,
                endpoint="/api/v1/jobs/async_video_music_gen",
                status="completed",
                principal=principal,
                model_spec=response_payload.modelspec,
                token_usage=getattr(result, "token_usage", None),
                cost_metadata=response_payload.cost_metadata,
                video_duration_s=response_payload.video_metadata.geometry.duration,
            )

    except EdennError as exc:
        context.logger.exception(
            "Async job %s failed with Edenn error: %s", job_id, exc.to_dict()
        )
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("job_id", job_id)
            sentry_sdk.capture_exception(exc)
        _fail_async_job(job_id, _public_async_error_payload(exc), context=context)
        if context.usage_recorder is not None:
            context.usage_recorder.record_job(
                job_id=job_id,
                endpoint="/api/v1/jobs/async_video_music_gen",
                status="failed",
                principal=principal,
            )

    except Exception as exc:
        context.logger.exception("Async job %s failed: %s", job_id, exc)
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("edenn_error_code", 90001)
            scope.set_tag("job_id", job_id)
            sentry_sdk.capture_exception(exc)
        _fail_async_job(
            job_id,
            {
                "error_code": 90001,
                "message": "An unexpected error occurred.",
                "retryable": True,
            },
            context=context,
        )
        if context.usage_recorder is not None:
            context.usage_recorder.record_job(
                job_id=job_id,
                endpoint="/api/v1/jobs/async_video_music_gen",
                status="failed",
                principal=principal,
            )

    finally:
        if callback_url:
            state = _get_async_job_state(job_id, context=context)
            if state is not None:
                await _fire_callback(
                    callback_url,
                    job_id,
                    state,
                    logger=context.logger,
                )
        cleanup_temp_dir(job_dir, logger=context.logger)


async def _run_async_video_job(**kwargs: Any) -> None:
    async with _get_async_video_job_semaphore():
        await _run_async_video_job_inner(**kwargs)


def create_video_generation_router(context: ApiContext) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/api/v1/jobs/video/compress",
        response_model=VideoCompressionResponse,
        summary="Upload a video or provide a video URL and return the compressed source video.",
    )
    async def compress_video_job(
        *,
        video: UploadFile | None = File(
            None, description="Video file (mp4, mov, etc.)."),
        video_url: Optional[str] = Form(None),
        max_height: int = Form(1280),
        crf: int = Form(23),
        preset: str = Form("veryfast"),
        force_reencode: bool = Form(False),
        return_original_on_failure: bool = Form(True),
        upload_output: bool = Form(False),
    ) -> VideoCompressionResponse:
        request_start = time.perf_counter()
        timing_stages: Dict[str, float] = {}
        job_id = uuid4().hex
        job_dir = context.settings.workdir / job_id
        source_dir = job_dir / "source"
        should_upload_output = bool(
            upload_output and getattr(context.storage, "enabled", False)
        )
        keep_local_outputs = not should_upload_output
        cleanup_completed = False

        try:
            stage_start = time.perf_counter()
            input_source = await _resolve_video_generation_input_source(
                video=video,
                video_url=video_url,
                destination_dir=source_dir,
                job_id=job_id,
                logger=context.logger,
            )
            local_video_path = input_source.path
            upload_content_type = input_source.content_type
            _record_stage_timing(timing_stages, "input_resolve_s", stage_start)

            stage_start = time.perf_counter()
            _validate_staged_input_video_duration(local_video_path)
            _record_stage_timing(timing_stages, "duration_validate_s", stage_start)

            stage_start = time.perf_counter()
            source_metadata = _inspect_public_video_metadata(
                local_video_path,
                logger=context.logger,
            )
            _record_stage_timing(timing_stages, "source_metadata_s", stage_start)

            compressed_video_path = local_video_path.with_name(
                f"{local_video_path.stem}_{max_height}h.mp4"
            )
            context.logger.info(
                "Compression job %s: compressing resolved input video to %s",
                job_id,
                compressed_video_path,
            )
            stage_start = time.perf_counter()
            # ffmpeg re-encode is CPU-heavy; off-load it so the API event loop
            # (and /healthz) stays responsive while this request compresses.
            effective_output_path = await asyncio.to_thread(
                compress_video_to_max_height,
                video_path=local_video_path,
                output_path=compressed_video_path,
                max_height=max_height,
                crf=crf,
                preset=preset,
                force_reencode=force_reencode,
                repair_decode_errors=True,
                return_original_on_failure=return_original_on_failure,
                validate_reencode=True,
            )
            _record_stage_timing(timing_stages, "ffmpeg_compress_s", stage_start)

            compression_applied = (
                effective_output_path.resolve() != local_video_path.resolve()
            )
            stage_start = time.perf_counter()
            output_metadata = _inspect_public_video_metadata(
                effective_output_path,
                logger=context.logger,
            )
            _record_stage_timing(timing_stages, "output_metadata_s", stage_start)

            content_type = _guess_input_video_content_type(
                source_path=effective_output_path,
                upload_content_type=upload_content_type,
                compression_applied=compression_applied,
            )

            output_blob = output_url = None
            output_path = None
            timing_stages["output_upload_s"] = 0.0
            if should_upload_output:
                stage_start = time.perf_counter()
                output_blob = context.storage.upload_path(
                    container=context.settings.upload_container,
                    path=effective_output_path,
                    blob_name=_provider_neutral_blob_name(
                        job_id=job_id,
                        folder="input/compressed",
                        label="compressed_video",
                        source_path=effective_output_path,
                    ),
                    content_type=content_type,
                )
                if output_blob:
                    output_url = context.storage.generate_sas_url(
                        container=context.settings.upload_container,
                        blob_name=output_blob,
                    )
                _record_stage_timing(timing_stages, "output_upload_s", stage_start)
            else:
                output_path = str(effective_output_path)

            timing_stages["cleanup_s"] = 0.0
            if not keep_local_outputs:
                stage_start = time.perf_counter()
                cleanup_temp_dir(job_dir, logger=context.logger)
                cleanup_completed = True
                _record_stage_timing(timing_stages, "cleanup_s", stage_start)

            return VideoCompressionResponse(
                job_id=job_id,
                compression_applied=compression_applied,
                output_uploaded=bool(output_blob),
                max_height=max_height,
                source_video_metadata=source_metadata,
                output_video_metadata=output_metadata,
                output_blob=output_blob,
                output_url=output_url,
                output_path=output_path,
                content_type=content_type,
                server_timing=_build_server_timing(
                    request_start=request_start,
                    stages=timing_stages,
                ),
            )
        except HTTPException:
            raise
        except EdennError as exc:
            context.logger.exception(
                "Compression job %s failed with typed Edenn error: %s",
                job_id,
                exc.to_dict(),
            )
            raise edenn_error_to_http_exception(exc) from exc
        except Exception as exc:
            context.logger.exception("Compression job %s failed: %s", job_id, exc)
            with sentry_sdk.new_scope() as scope:
                scope.set_tag("edenn_error_code", 90001)
                scope.set_tag("job_id", job_id)
                sentry_sdk.capture_exception(exc)
            raise HTTPException(
                status_code=500,
                detail=ErrorDetail(
                    error_code=90001,
                    message="An unexpected error occurred. Please try again later.",
                    retryable=True,
                ).model_dump(),
            ) from exc
        finally:
            if not keep_local_outputs and not cleanup_completed:
                cleanup_temp_dir(job_dir, logger=context.logger)

    @router.post(
        "/api/v1/jobs/video/pre-generation-preview",
        response_model=VideoPreGenerationPreviewResponse,
        summary="Run video music generation setup through prompt creation without generating music.",
    )
    async def preview_video_pre_generation(
        *,
        video: UploadFile | None = File(
            None, description="Video file (mp4, mov, etc.)."),
        video_url: Optional[str] = Form(None),
        compression_flag: bool = Form(False),
        include_vocals: bool = Form(False),
        vocal_gender: str = Form("female"),
        user_prompt: str = Form(""),
        verbose_instruction: bool = Form(False),
        music_style_prompt: Optional[str] = Form(None),
        lyrics_prompt: Optional[str] = Form(None),
        modelspec: str = Form(""),
        vocal_id: Optional[str] = Form(None),
        vocal_sample: UploadFile | None = File(
            None, description="Optional vocal sample for edenn_enhanced vocal cloning."),
        vocal_sample_url: Optional[str] = Form(None),
    ) -> VideoPreGenerationPreviewResponse:
        request_start = time.perf_counter()
        timing_stages: Dict[str, float] = {}
        job_id = uuid4().hex
        asset_ids = RecommendationAssetIds.create(job_id=job_id)
        job_dir = context.settings.workdir / job_id
        source_dir = job_dir / "source"
        vocal_source_dir = source_dir / "vocal"
        keep_local_outputs = not getattr(context.storage, "enabled", False)
        cleanup_completed = False
        preview_temp_dir: Optional[Path] = None

        try:
            stage_start = time.perf_counter()
            input_source = await _resolve_video_generation_input_source(
                video=video,
                video_url=video_url,
                destination_dir=source_dir,
                job_id=job_id,
                logger=context.logger,
            )
            local_video_path = input_source.path
            upload_content_type = input_source.content_type
            _record_stage_timing(timing_stages, "input_resolve_s", stage_start)

            effective_input_video_path = local_video_path
            stage_start = time.perf_counter()
            _validate_staged_input_video_duration(effective_input_video_path)
            _record_stage_timing(timing_stages, "duration_validate_s", stage_start)

            stage_start = time.perf_counter()
            requested_modelspec = _normalize_requested_modelspec(modelspec)
            normalized_music_style_prompt, normalized_lyrics_prompt = _validate_prompt_fields(
                requested_modelspec=requested_modelspec,
                verbose_instruction=verbose_instruction,
                user_prompt=user_prompt,
                music_style_prompt=music_style_prompt,
                lyrics_prompt=lyrics_prompt,
            )
            _record_stage_timing(timing_stages, "request_validation_s", stage_start)

            stage_start = time.perf_counter()
            normalized_vocal_id, prepared_vocal_sample_path = await _prepare_vocal_clone_request_input(
                vocal_id=vocal_id,
                vocal_sample=vocal_sample,
                vocal_sample_url=vocal_sample_url,
                vocal_source_dir=vocal_source_dir,
                requested_modelspec=requested_modelspec,
            )
            _record_stage_timing(timing_stages, "vocal_input_prepare_s", stage_start)

            compression_applied = False
            timing_stages["ffmpeg_compress_s"] = 0.0
            if compression_flag:
                compressed_video_path = local_video_path.with_name(
                    f"{local_video_path.stem}_1280h.mp4"
                )
                context.logger.info(
                    "Pre-generation job %s: compression enabled; compressing to %s",
                    job_id,
                    compressed_video_path,
                )
                stage_start = time.perf_counter()
                # ffmpeg re-encode is CPU-heavy; off-load it so the API event loop
                # (and /healthz) stays responsive while this request compresses.
                effective_input_video_path = await asyncio.to_thread(
                    compress_video_to_max_height,
                    video_path=local_video_path,
                    output_path=compressed_video_path,
                    max_height=1280,
                    repair_decode_errors=True,
                    return_original_on_failure=True,
                    validate_reencode=True,
                )
                _record_stage_timing(timing_stages, "ffmpeg_compress_s", stage_start)
                compression_applied = (
                    effective_input_video_path.resolve() != local_video_path.resolve()
                )

            gender_value = vocal_gender.strip() or "female"
            stage_start = time.perf_counter()
            result: VideoPreGenerationResult = await context.workflow.preview_pre_generation(
                video_path=effective_input_video_path,
                include_vocals=include_vocals,
                vocal_gender=gender_value,
                user_prompt=user_prompt,
                verbose_instruction=verbose_instruction,
                music_style_prompt=normalized_music_style_prompt or None,
                lyrics_prompt=normalized_lyrics_prompt or None,
                modelspec=requested_modelspec,
                vocal_id=normalized_vocal_id,
                vocal_sample_path=prepared_vocal_sample_path,
                job_id=job_id,
                video_id=asset_ids.video_id,
                creative_id=asset_ids.creative_id,
                primary_music_id=asset_ids.primary_music_id,
                secondary_music_id=asset_ids.secondary_music_id,
                selected_music_id=asset_ids.selected_music_id,
                alignment_id=asset_ids.alignment_id,
            )
            _record_stage_timing(
                timing_stages,
                "workflow_pre_generation_s",
                stage_start,
            )
            workflow_stage_timings = getattr(result, "stage_timing_s", None) or {}
            for stage_name, duration_s in workflow_stage_timings.items():
                timing_stages[f"workflow.{stage_name}"] = _safe_timing_value(
                    duration_s
                )
            llm_related_keys = {
                "user_intent_s",
                "scene_segmentation_s",
                "video_understanding_s",
                "music_prompt_orchestration_s",
            }
            llm_related_total_s = sum(
                _safe_timing_value(duration_s)
                for stage_name, duration_s in workflow_stage_timings.items()
                if stage_name in llm_related_keys
            )
            if llm_related_total_s:
                timing_stages["workflow.llm_related_total_s"] = round(
                    llm_related_total_s,
                    3,
                )

            source_video_blob = source_video_url = None
            source_video_path = None
            thumbnail_blob = thumbnail_url = None
            thumbnail_path = None
            thumbnail_path_value = getattr(result, "thumbnail_path", None)
            preview_temp_folder = getattr(
                getattr(result, "video_metadata", None),
                "temp_folder",
                None,
            )
            if preview_temp_folder:
                preview_temp_dir = Path(str(preview_temp_folder))

            source_video_url = _reusable_input_source_url(
                input_source,
                compression_applied=compression_applied,
            )
            timing_stages["source_upload_s"] = 0.0
            timing_stages["thumbnail_upload_s"] = 0.0
            if getattr(context.storage, "enabled", False):
                if source_video_url is None:
                    stage_start = time.perf_counter()
                    staged_upload_content_type = _guess_input_video_content_type(
                        source_path=effective_input_video_path,
                        upload_content_type=upload_content_type,
                        compression_applied=compression_applied,
                    )
                    source_video_blob = context.storage.upload_path(
                        container=context.settings.upload_container,
                        path=effective_input_video_path,
                        blob_name=_provider_neutral_blob_name(
                            job_id=job_id,
                            folder="input",
                            label="source_video",
                            source_path=effective_input_video_path,
                        ),
                        content_type=staged_upload_content_type,
                    )
                    if source_video_blob:
                        source_video_url = context.storage.generate_sas_url(
                            container=context.settings.upload_container,
                            blob_name=source_video_blob,
                        )
                    _record_stage_timing(timing_stages, "source_upload_s", stage_start)
                if thumbnail_path_value and thumbnail_path_value.exists():
                    try:
                        stage_start = time.perf_counter()
                        thumbnail_blob = context.storage.upload_path(
                            container=context.settings.output_container,
                            blob_name=_provider_neutral_blob_name(
                                job_id=job_id,
                                folder="thumbnail",
                                label="thumbnail",
                                source_path=thumbnail_path_value,
                            ),
                            path=thumbnail_path_value,
                            content_type=guess_image_content_type(
                                thumbnail_path_value),
                        )
                        if thumbnail_blob:
                            thumbnail_url = context.storage.generate_sas_url(
                                container=context.settings.output_container,
                                blob_name=thumbnail_blob,
                            )
                        _record_stage_timing(
                            timing_stages,
                            "thumbnail_upload_s",
                            stage_start,
                        )
                    except Exception as exc:
                        context.logger.warning(
                            "Pre-generation job %s thumbnail upload failed: %s",
                            job_id,
                            exc,
                        )
            else:
                if source_video_url is None:
                    source_video_path = str(effective_input_video_path)
                if thumbnail_path_value and thumbnail_path_value.exists():
                    thumbnail_path = str(thumbnail_path_value)

            scenes = [_scene_to_model(scene) for scene in result.scenes]
            raw_token_usage = None
            total_tokens = None
            if result.token_usage:
                raw_token_usage = normalize_token_usage_counts(
                    result.token_usage)
                total_tokens = raw_token_usage.total_tokens

            response_video_id = getattr(
                result, "video_id", None) or asset_ids.video_id
            response_creative_id = (
                getattr(result, "creative_id", None) or asset_ids.creative_id
            )
            response_primary_music_id = (
                getattr(result, "primary_music_id",
                        None) or asset_ids.primary_music_id
            )
            response_secondary_music_id = getattr(
                result, "secondary_music_id", None
            )
            response_selected_music_id = (
                getattr(result, "selected_music_id", None)
                or asset_ids.selected_music_id
                or response_primary_music_id
            )
            response_alignment_id = (
                getattr(result, "alignment_id", None) or asset_ids.alignment_id
            )

            timing_stages["cleanup_s"] = 0.0
            if not keep_local_outputs:
                stage_start = time.perf_counter()
                cleanup_temp_dir(job_dir, logger=context.logger)
                if preview_temp_dir is not None:
                    cleanup_temp_dir(preview_temp_dir, logger=context.logger)
                cleanup_completed = True
                _record_stage_timing(timing_stages, "cleanup_s", stage_start)

            # Same final net as the full job response: the preview carries
            # LLM-authored free text (summary, music prompt, references) that
            # must never name the upstream generator.
            return guard_response_model(VideoPreGenerationPreviewResponse(
                job_id=job_id,
                video_id=response_video_id,
                creative_id=response_creative_id,
                primary_music_id=response_primary_music_id,
                secondary_music_id=response_secondary_music_id,
                selected_music_id=response_selected_music_id,
                alignment_id=response_alignment_id,
                compression_applied=compression_applied,
                source_video_blob=source_video_blob,
                source_video_url=source_video_url,
                source_video_path=source_video_path,
                thumbnail_blob=thumbnail_blob,
                thumbnail_url=thumbnail_url,
                thumbnail_path=thumbnail_path,
                video_metadata=_public_video_metadata(result.video_metadata),
                scenes=scenes,
                video_summary=result.video_summary,
                video_title=getattr(result, "video_title", "") or "",
                video_description=getattr(result, "video_description", "") or "",
                music_prompt=_dict_value(getattr(result, "music_prompt", None)),
                sanitized_prompt=getattr(result, "sanitized_prompt", "") or "",
                sanitized_style_prompt=getattr(
                    result, "sanitized_style_prompt", None),
                sanitized_lyrics_prompt=getattr(
                    result, "sanitized_lyrics_prompt", None),
                include_vocals=bool(getattr(result, "include_vocals", False)),
                vocal_gender=getattr(result, "vocal_gender", "") or gender_value,
                modelspec=getattr(result, "used_music_model_spec",
                                  None) or requested_modelspec,
                user_requested_language=getattr(
                    result, "user_requested_language", "") or "",
                detected_category=getattr(result, "detected_category", "") or "",
                was_transformed=bool(getattr(result, "was_transformed", False)),
                detected_references=[
                    str(item)
                    for item in (getattr(result, "detected_references", None) or [])
                ],
                token_usage=total_tokens,
                raw_token_usage=raw_token_usage,
                token_usage_breakdown=normalize_token_usage_breakdown(
                    getattr(result, "token_usage_breakdown", None)
                ),
                cost=_video_generation_cost_response(
                    result,
                    raw_token_usage=raw_token_usage,
                ),
                job_received_timestamp=getattr(
                    result, "job_received_timestamp", None),
                job_finished_timestamp=getattr(
                    result, "job_finished_timestamp", None),
                server_timing=_build_server_timing(
                    request_start=request_start,
                    stages=timing_stages,
                ),
            ))
        except HTTPException:
            raise
        except EdennError as exc:
            context.logger.exception(
                "Pre-generation job %s failed with typed Edenn error: %s",
                job_id,
                exc.to_dict(),
            )
            raise edenn_error_to_http_exception(exc) from exc
        except Exception as exc:
            context.logger.exception("Pre-generation job %s failed: %s", job_id, exc)
            with sentry_sdk.new_scope() as scope:
                scope.set_tag("edenn_error_code", 90001)
                scope.set_tag("job_id", job_id)
                sentry_sdk.capture_exception(exc)
            raise HTTPException(
                status_code=500,
                detail=ErrorDetail(
                    error_code=90001,
                    message="An unexpected error occurred. Please try again later.",
                    retryable=True,
                ).model_dump(),
            ) from exc
        finally:
            if not keep_local_outputs and not cleanup_completed:
                cleanup_temp_dir(job_dir, logger=context.logger)
                if preview_temp_dir is not None:
                    cleanup_temp_dir(preview_temp_dir, logger=context.logger)

    @router.post(
        "/api/v1/jobs/video",
        response_model=VideoJobResponse,
        summary="Upload a video or provide a video URL and receive a remixed version with generated music.",
    )
    async def generate_video_job(
        *,
        request: Request,
        background_tasks: BackgroundTasks,
        video: UploadFile | None = File(
            None, description="Video file (mp4, mov, etc.)."),
        video_url: Optional[str] = Form(None),  # add temp key (type string)
        # purpose -> table_url
        preserve_original_audio: bool = Form(False),
        compression_flag: bool = Form(False),
        music_volume: Optional[float] = Form(None),
        water_mark: bool = Form(False),
        include_vocals: bool = Form(False),
        vocal_gender: str = Form("female"),
        user_prompt: str = Form(""),
        verbose_instruction: bool = Form(False),
        music_style_prompt: Optional[str] = Form(None),
        lyrics_prompt: Optional[str] = Form(None),
        modelspec: str = Form(""),
        audio_output_format: Optional[str] = Form(None),
        vocal_id: Optional[str] = Form(None),
        vocal_sample: UploadFile | None = File(
            None, description="Optional vocal sample for edenn_enhanced vocal cloning."),
        vocal_sample_url: Optional[str] = Form(None),
        user_id: Optional[str] = Form(
            None,
            description=(
                "Optional caller-supplied user identifier. When set, stamps "
                "creative.creator_user_id and generation_job.creator_user_id "
                "so the recommendation endpoint can return music tailored to "
                "this user's history."
            ),
        ),
    ) -> VideoJobResponse:
        user_id = resolve_user_id(request, user_id)
        job_id = uuid4().hex
        asset_ids = RecommendationAssetIds.create(job_id=job_id)
        job_dir = context.settings.workdir / job_id
        source_dir = job_dir / "source"
        vocal_source_dir = source_dir / "vocal"

        requested_volume = music_volume if music_volume is not None else context.settings.music_volume
        local_video_path = None
        upload_content_type = None
        effective_input_video_path = None
        compression_applied = False

        try:
            input_source = await _resolve_video_generation_input_source(
                video=video,
                video_url=video_url,
                destination_dir=source_dir,
                job_id=job_id,
                logger=context.logger,
            )
            local_video_path = input_source.path
            upload_content_type = input_source.content_type
            effective_input_video_path = local_video_path
            _validate_staged_input_video_duration(effective_input_video_path)
            requested_modelspec_raw = (
                modelspec or "edenn_basic").strip().lower() or "edenn_basic"
            requested_modelspec = LEGACY_MODEL_MAP.get(
                requested_modelspec_raw,
                requested_modelspec_raw,
            )
            if requested_modelspec not in VALID_MUSIC_MODEL_SPECS:
                allowed = ", ".join(sorted(VALID_MUSIC_MODEL_SPECS))
                raise EdennApiError(
                    f"Invalid modelspec. Allowed values: {allowed}.",
                    public_message=f"Invalid modelspec. Allowed values: {allowed}.",
                    status_code=400,
                    component="api",
                    operation="validate_modelspec",
                )
            normalized_music_style_prompt = (
                music_style_prompt or "").strip()
            normalized_lyrics_prompt = (lyrics_prompt or "").strip()
            if not verbose_instruction and (normalized_music_style_prompt or normalized_lyrics_prompt):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "music_style_prompt and lyrics_prompt require "
                        "verbose_instruction=true."
                    ),
                )
            if verbose_instruction and (user_prompt or "").strip():
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "When verbose_instruction=True, omit user_prompt and use "
                        "music_style_prompt / lyrics_prompt instead."
                    ),
                )
            if verbose_instruction and not normalized_music_style_prompt:
                raise HTTPException(
                    status_code=400,
                    detail="music_style_prompt is required when verbose_instruction=True.",
                )
            if verbose_instruction and requested_modelspec not in {"edenn_enhanced", "edenn_studio"}:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "verbose_instruction requires modelspec=edenn_enhanced or modelspec=edenn_studio."
                    ),
                )
            vocal_sample_path = await resolve_optional_media_source_to_disk(
                upload=vocal_sample,
                remote_url=vocal_sample_url,
                destination_dir=vocal_source_dir,
                fallback_filename="vocal_sample.m4a",
                asset_label="vocal sample",
            )
            normalized_vocal_id = (vocal_id or "").strip() or None
            if normalized_vocal_id and vocal_sample_path is not None:
                raise EdennApiError(
                    "Provide either vocal_id or vocal_sample/vocal_sample_url, not both.",
                    public_message="Provide either vocal_id or vocal_sample/vocal_sample_url, not both.",
                    status_code=400,
                    component="api",
                    operation="validate_vocal_clone_inputs",
                )
            if requested_modelspec != "edenn_enhanced" and (
                normalized_vocal_id or vocal_sample_path is not None
            ):
                raise EdennApiError(
                    "Vocal clone inputs are only supported for modelspec=edenn_enhanced.",
                    public_message="Vocal clone inputs are only supported for modelspec=edenn_enhanced.",
                    status_code=400,
                    component="api",
                    operation="validate_vocal_clone_modelspec",
                )
            prepared_vocal_sample_path = (
                prepare_audio_for_provider_b_vocal_clone(
                    source_audio_path=vocal_sample_path,
                    destination_dir=vocal_source_dir,
                )
                if vocal_sample_path is not None
                else None
            )
            if compression_flag:
                compressed_video_path = local_video_path.with_name(
                    f"{local_video_path.stem}_1280h.mp4"
                )
                context.logger.info(
                    "Job %s: compression enabled; compressing resolved input video to %s",
                    job_id,
                    compressed_video_path,
                )
                # ffmpeg re-encode is CPU-heavy; off-load it so the API event loop
                # (and /healthz) stays responsive while this request compresses.
                effective_input_video_path = await asyncio.to_thread(
                    compress_video_to_max_height,
                    video_path=local_video_path,
                    output_path=compressed_video_path,
                    max_height=1280,
                    repair_decode_errors=True,
                    return_original_on_failure=True,
                    validate_reencode=True,
                )
                compression_applied = (
                    effective_input_video_path.resolve() != local_video_path.resolve()
                )

            requested_audio_format = (
                audio_output_format or "").strip() or None
            gender_value = vocal_gender.strip() or "female"
            result: VideoGenerationResult = await context.workflow.run(
                video_path=effective_input_video_path,
                preserve_original_audio=(
                    preserve_original_audio or context.settings.preserve_original_audio
                ),
                music_volume=requested_volume,
                water_mark=water_mark,
                include_vocals=include_vocals,
                vocal_gender=gender_value,
                user_prompt=user_prompt,
                verbose_instruction=verbose_instruction,
                music_style_prompt=normalized_music_style_prompt or None,
                lyrics_prompt=normalized_lyrics_prompt or None,
                modelspec=requested_modelspec,
                audio_output_format=requested_audio_format,
                vocal_id=normalized_vocal_id,
                vocal_sample_path=prepared_vocal_sample_path,
                job_id=job_id,
                video_id=asset_ids.video_id,
                creative_id=asset_ids.creative_id,
                primary_music_id=asset_ids.primary_music_id,
                secondary_music_id=asset_ids.secondary_music_id,
                selected_music_id=asset_ids.selected_music_id,
                alignment_id=asset_ids.alignment_id,
            )
            if effective_input_video_path is None:
                raise RuntimeError(
                    "Resolved input video path missing after workflow run.")

            input_source_url = _reusable_input_source_url(
                input_source,
                compression_applied=compression_applied,
            )
            upload_blob = upload_url = None
            audio_blob = audio_url = None
            complete_audio_blob = complete_audio_url = None
            secondary_complete_audio_blob = secondary_complete_audio_url = None
            video_blob = video_url = None
            thumbnail_blob = thumbnail_url = None
            thumbnail_path_value = getattr(result, "thumbnail_path", None)
            complete_music_path = getattr(
                result, "complete_generated_music_path", None)
            secondary_complete_music_path = getattr(
                result, "secondary_complete_generated_music_path", None
            )
            # Probe the tracks from local files (before cleanup).
            audio_duration_s, audio_size_bytes = probe_audio_metrics(
                result.generated_music_path
            )
            complete_audio_duration_s, complete_audio_size_bytes = probe_audio_metrics(
                complete_music_path
            )
            secondary_complete_audio_duration_s, secondary_complete_audio_size_bytes = (
                probe_audio_metrics(
                    secondary_complete_music_path
                    if secondary_complete_music_path
                    and secondary_complete_music_path.exists()
                    else None
                )
            )
            audio_content_type = guess_audio_content_type(
                result.generated_music_path)
            complete_audio_content_type = (
                guess_audio_content_type(complete_music_path)
                if complete_music_path
                else "audio/mpeg"
            )
            secondary_complete_audio_content_type = (
                guess_audio_content_type(secondary_complete_music_path)
                if secondary_complete_music_path
                else "audio/mpeg"
            )

            if context.storage.enabled:
                if input_source_url:
                    upload_url = input_source_url
                else:
                    staged_upload_content_type = _guess_input_video_content_type(
                        source_path=effective_input_video_path,
                        upload_content_type=upload_content_type,
                        compression_applied=compression_applied,
                    )
                    upload_blob = context.storage.upload_path(
                        container=context.settings.upload_container,
                        path=effective_input_video_path,
                        blob_name=_provider_neutral_blob_name(
                            job_id=job_id,
                            folder="input",
                            label="source_video",
                            source_path=effective_input_video_path,
                        ),
                        content_type=staged_upload_content_type,
                    )
                audio_blob = context.storage.upload_path(
                    container=context.settings.audio_container_name,
                    path=result.generated_music_path,
                    blob_name=_provider_neutral_blob_name(
                        job_id=job_id,
                        folder="audio",
                        label="matched_audio",
                        source_path=result.generated_music_path,
                    ),
                    content_type=audio_content_type,
                )
                if complete_music_path and complete_music_path.exists():
                    complete_audio_blob = context.storage.upload_path(
                        container=context.settings.audio_container_name,
                        path=complete_music_path,
                        blob_name=_provider_neutral_blob_name(
                            job_id=job_id,
                            folder="audio/complete",
                            label="complete_audio",
                            source_path=complete_music_path,
                        ),
                        content_type=complete_audio_content_type,
                    )
                if secondary_complete_music_path and secondary_complete_music_path.exists():
                    secondary_complete_audio_blob = context.storage.upload_path(
                        container=context.settings.audio_container_name,
                        path=secondary_complete_music_path,
                        blob_name=_provider_neutral_blob_name(
                            job_id=job_id,
                            folder="audio/complete/secondary",
                            label="secondary_audio",
                            source_path=secondary_complete_music_path,
                        ),
                        content_type=secondary_complete_audio_content_type,
                    )
                video_blob = context.storage.upload_path(
                    container=context.settings.output_container,
                    path=result.remixed_video_path,
                    blob_name=_provider_neutral_blob_name(
                        job_id=job_id,
                        folder="video",
                        label="remixed_video",
                        source_path=result.remixed_video_path,
                    ),
                    content_type="video/mp4",
                )
                if thumbnail_path_value and thumbnail_path_value.exists():
                    try:
                        thumbnail_blob = context.storage.upload_path(
                            container=context.settings.output_container,
                            blob_name=_provider_neutral_blob_name(
                                job_id=job_id,
                                folder="thumbnail",
                                label="thumbnail",
                                source_path=thumbnail_path_value,
                            ),
                            path=thumbnail_path_value,
                            content_type=guess_image_content_type(
                                thumbnail_path_value),
                        )
                    except Exception as exc:
                        context.logger.warning(
                            "Job %s thumbnail upload failed: %s", job_id, exc)

                if upload_blob:
                    upload_url = context.storage.generate_sas_url(
                        container=context.settings.upload_container,
                        blob_name=upload_blob,
                    )
                if audio_blob:
                    audio_url = context.storage.generate_sas_url(
                        container=context.settings.audio_container_name,
                        blob_name=audio_blob,
                    )
                if complete_audio_blob:
                    complete_audio_url = context.storage.generate_sas_url(
                        container=context.settings.audio_container_name,
                        blob_name=complete_audio_blob,
                    )
                if secondary_complete_audio_blob:
                    secondary_complete_audio_url = context.storage.generate_sas_url(
                        container=context.settings.audio_container_name,
                        blob_name=secondary_complete_audio_blob,
                    )
                if video_blob:
                    video_url = context.storage.generate_sas_url(
                        container=context.settings.output_container,
                        blob_name=video_blob,
                    )
                if thumbnail_blob:
                    thumbnail_url = context.storage.generate_sas_url(
                        container=context.settings.output_container,
                        blob_name=thumbnail_blob,
                    )

            scenes = [_scene_to_model(scene) for scene in result.scenes]

            response_video_id = getattr(
                result, "video_id", None) or asset_ids.video_id
            response_creative_id = (
                getattr(result, "creative_id", None) or asset_ids.creative_id
            )
            response_primary_music_id = (
                getattr(result, "primary_music_id",
                        None) or asset_ids.primary_music_id
            )
            response_secondary_music_id = getattr(
                result, "secondary_music_id", None
            )
            response_selected_music_id = (
                getattr(result, "selected_music_id", None)
                or asset_ids.selected_music_id
                or response_primary_music_id
            )
            response_alignment_id = (
                getattr(result, "alignment_id", None) or asset_ids.alignment_id
            )

            if getattr(context, "recommendation_persistence", None) is not None:
                user_prompt_emb, music_emb = await _compute_recommendation_embeddings(
                    user_prompt=user_prompt,
                    music_prompt_json=_dict_value(getattr(result, "music_prompt", None)),
                    logger=context.logger,
                )
                persistence_payload = _build_recommendation_payload(
                    result=result,
                    asset_ids=RecommendationAssetIds(
                        job_id=job_id,
                        video_id=response_video_id,
                        creative_id=response_creative_id,
                        primary_music_id=response_primary_music_id,
                        secondary_music_id=response_secondary_music_id,
                        selected_music_id=response_selected_music_id,
                        alignment_id=response_alignment_id,
                    ),
                    job_id=job_id,
                    user_prompt=user_prompt,
                    requested_modelspec=requested_modelspec,
                    preserve_original_audio=(
                        preserve_original_audio or context.settings.preserve_original_audio
                    ),
                    requested_volume=requested_volume,
                    effective_input_video_path=effective_input_video_path,
                    upload_content_type=upload_content_type,
                    compression_applied=compression_applied,
                    upload_blob=upload_blob,
                    upload_url=upload_url,
                    audio_blob=audio_blob,
                    audio_url=audio_url,
                    complete_audio_blob=complete_audio_blob,
                    complete_audio_url=complete_audio_url,
                    secondary_complete_audio_blob=secondary_complete_audio_blob,
                    secondary_complete_audio_url=secondary_complete_audio_url,
                    video_blob=video_blob,
                    video_url=video_url,
                    thumbnail_blob=thumbnail_blob,
                    thumbnail_url=thumbnail_url,
                    scenes=scenes,
                    creator_user_id=user_id,
                    user_prompt_embedding=user_prompt_emb,
                    music_embedding=music_emb,
                )
                background_tasks.add_task(
                    _persist_recommendation_payload,
                    context=context,
                    payload=persistence_payload,
                )

            response = build_video_job_response(
                job_id=job_id,
                result=result,
                requested_modelspec=requested_modelspec,
                assets=VideoJobAssets(
                    audio_url=audio_url,
                    audio_duration_s=audio_duration_s,
                    audio_size_bytes=audio_size_bytes,
                    complete_audio_url=complete_audio_url,
                    complete_audio_duration_s=complete_audio_duration_s,
                    complete_audio_size_bytes=complete_audio_size_bytes,
                    video_url=video_url,
                    thumbnail_url=thumbnail_url,
                ),
            )
            if context.usage_recorder is not None:
                context.usage_recorder.record_job(
                    job_id=job_id,
                    endpoint="/api/v1/jobs/video",
                    status="completed",
                    principal=get_principal(request),
                    model_spec=response.modelspec,
                    token_usage=getattr(result, "token_usage", None),
                    cost_metadata=response.cost_metadata,
                    video_duration_s=response.video_metadata.geometry.duration,
                )
            return response
        except HTTPException:
            raise
        except EdennError as exc:
            context.logger.exception(
                "Job %s failed with typed Edenn error: %s",
                job_id,
                exc.to_dict(),
            )
            raise edenn_error_to_http_exception(exc) from exc
        except Exception as exc:
            context.logger.exception("Job %s failed: %s", job_id, exc)
            with sentry_sdk.new_scope() as scope:
                scope.set_tag("edenn_error_code", 90001)
                scope.set_tag("job_id", job_id)
                sentry_sdk.capture_exception(exc)
            raise HTTPException(
                status_code=500,
                detail=ErrorDetail(
                    error_code=90001,
                    message="An unexpected error occurred. Please try again later.",
                    retryable=True,
                ).model_dump(),
            ) from exc
        finally:
            cleanup_temp_dir(job_dir, logger=context.logger)

    # -----------------------------------------------------------------------
    # POST /api/v1/jobs/async_video_music_gen
    # Same parameters as the sync endpoint plus an optional callback_url.
    # Returns immediately with job_id and status="pending"; the pipeline runs
    # in the background. Poll GET /api/v1/jobs/async_video_music_gen/{job_id}
    # or wait for the callback to receive the full result.
    # -----------------------------------------------------------------------

    @router.post(
        "/api/v1/jobs/async_video_music_gen",
        response_model=AsyncVideoJobAcceptedResponse,
        summary=(
            "Submit a video for async music generation. "
            "Returns a job_id after preflight; pipeline runs in the background."
        ),
    )
    async def submit_async_video_music_gen(
        *,
        request: Request,
        background_tasks: BackgroundTasks,
        video: UploadFile | None = File(
            None, description="Video file (mp4, mov, etc.)."),
        video_url: Optional[str] = Form(None),
        preserve_original_audio: bool = Form(False),
        compression_flag: bool = Form(False),
        music_volume: Optional[float] = Form(None),
        water_mark: bool = Form(False),
        include_vocals: bool = Form(False),
        vocal_gender: str = Form("female"),
        user_prompt: str = Form(""),
        verbose_instruction: bool = Form(False),
        music_style_prompt: Optional[str] = Form(None),
        lyrics_prompt: Optional[str] = Form(None),
        modelspec: str = Form(""),
        audio_output_format: Optional[str] = Form(None),
        vocal_id: Optional[str] = Form(None),
        vocal_sample: UploadFile | None = File(
            None, description="Optional vocal sample for edenn_enhanced vocal cloning."),
        vocal_sample_url: Optional[str] = Form(None),
        user_id: Optional[str] = Form(None),
        callback_url: Optional[str] = Form(
            None,
            description=(
                "Optional URL to POST the full job result to when the pipeline "
                "completes or fails. Payload matches AsyncVideoJobStatusResponse."
            ),
        ),
    ) -> AsyncVideoJobAcceptedResponse:
        user_id = resolve_user_id(request, user_id)
        job_id = uuid4().hex
        asset_ids = RecommendationAssetIds.create(job_id=job_id)
        job_dir = context.settings.workdir / job_id
        source_dir = job_dir / "source"
        vocal_source_dir = source_dir / "vocal"

        requested_volume = music_volume if music_volume is not None else context.settings.music_volume
        local_video_path = None
        upload_content_type = None
        effective_input_video_path = None
        compression_applied = False

        # ------------------------------------------------------------------
        # Validation and file ingestion happen synchronously before we return,
        # so invalid requests are rejected with a proper HTTP error immediately
        # and the UploadFile stream is consumed before FastAPI closes it.
        # ------------------------------------------------------------------
        try:
            _validate_async_music_volume(requested_volume)
            gender_value = _normalize_async_vocal_gender(vocal_gender)
            normalized_callback_url = _normalize_async_callback_url(callback_url)

            input_source = await _resolve_video_generation_input_source(
                video=video,
                video_url=video_url,
                destination_dir=source_dir,
                job_id=job_id,
                logger=context.logger,
            )
            local_video_path = input_source.path
            upload_content_type = input_source.content_type
            effective_input_video_path = local_video_path
            _validate_staged_input_video_duration(effective_input_video_path)

            requested_modelspec_raw = (modelspec or "edenn_basic").strip().lower() or "edenn_basic"
            requested_modelspec = LEGACY_MODEL_MAP.get(
                requested_modelspec_raw, requested_modelspec_raw
            )
            if requested_modelspec not in VALID_MUSIC_MODEL_SPECS:
                allowed = ", ".join(sorted(VALID_MUSIC_MODEL_SPECS))
                raise EdennApiError(
                    f"Invalid modelspec. Allowed values: {allowed}.",
                    public_message=f"Invalid modelspec. Allowed values: {allowed}.",
                    status_code=400,
                    component="api",
                    operation="validate_modelspec",
                )

            normalized_music_style_prompt = (music_style_prompt or "").strip()
            normalized_lyrics_prompt = (lyrics_prompt or "").strip()
            if not verbose_instruction and (normalized_music_style_prompt or normalized_lyrics_prompt):
                raise HTTPException(
                    status_code=400,
                    detail="music_style_prompt and lyrics_prompt require verbose_instruction=true.",
                )
            if verbose_instruction and (user_prompt or "").strip():
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "When verbose_instruction=True, omit user_prompt and use "
                        "music_style_prompt / lyrics_prompt instead."
                    ),
                )
            if verbose_instruction and not normalized_music_style_prompt:
                raise HTTPException(
                    status_code=400,
                    detail="music_style_prompt is required when verbose_instruction=True.",
                )
            if verbose_instruction and requested_modelspec not in {"edenn_enhanced", "edenn_studio"}:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "verbose_instruction requires modelspec=edenn_enhanced or modelspec=edenn_studio."
                    ),
                )

            vocal_sample_path = await resolve_optional_media_source_to_disk(
                upload=vocal_sample,
                remote_url=vocal_sample_url,
                destination_dir=vocal_source_dir,
                fallback_filename="vocal_sample.m4a",
                asset_label="vocal sample",
            )
            normalized_vocal_id = (vocal_id or "").strip() or None
            if normalized_vocal_id and vocal_sample_path is not None:
                raise EdennApiError(
                    "Provide either vocal_id or vocal_sample/vocal_sample_url, not both.",
                    public_message="Provide either vocal_id or vocal_sample/vocal_sample_url, not both.",
                    status_code=400,
                    component="api",
                    operation="validate_vocal_clone_inputs",
                )
            if requested_modelspec != "edenn_enhanced" and (
                normalized_vocal_id or vocal_sample_path is not None
            ):
                raise EdennApiError(
                    "Vocal clone inputs are only supported for modelspec=edenn_enhanced.",
                    public_message="Vocal clone inputs are only supported for modelspec=edenn_enhanced.",
                    status_code=400,
                    component="api",
                    operation="validate_vocal_clone_modelspec",
                )
            prepared_vocal_sample_path = (
                prepare_audio_for_provider_b_vocal_clone(
                    source_audio_path=vocal_sample_path,
                    destination_dir=vocal_source_dir,
                )
                if vocal_sample_path is not None
                else None
            )

            if compression_flag:
                compressed_video_path = local_video_path.with_name(
                    f"{local_video_path.stem}_1280h.mp4"
                )
                context.logger.info(
                    "Async job %s: compression enabled; compressing to %s",
                    job_id,
                    compressed_video_path,
                )
                # ffmpeg re-encode is CPU-heavy; off-load it so the API event loop
                # (and /healthz) stays responsive while this request compresses.
                effective_input_video_path = await asyncio.to_thread(
                    compress_video_to_max_height,
                    video_path=local_video_path,
                    output_path=compressed_video_path,
                    max_height=1280,
                    repair_decode_errors=True,
                    return_original_on_failure=True,
                    validate_reencode=True,
                )
                compression_applied = (
                    effective_input_video_path.resolve() != local_video_path.resolve()
                )

            requested_audio_format = (audio_output_format or "").strip() or None
            input_source_url = _reusable_input_source_url(
                input_source,
                compression_applied=compression_applied,
            )

        except HTTPException:
            cleanup_temp_dir(job_dir, logger=context.logger)
            raise
        except EdennError as exc:
            cleanup_temp_dir(job_dir, logger=context.logger)
            raise edenn_error_to_http_exception(exc) from exc
        except Exception as exc:
            cleanup_temp_dir(job_dir, logger=context.logger)
            context.logger.exception("Async job %s pre-flight failed: %s", job_id, exc)
            raise HTTPException(
                status_code=500,
                detail=ErrorDetail(
                    error_code=90001,
                    message="An unexpected error occurred. Please try again later.",
                    retryable=True,
                ).model_dump(),
            ) from exc

        # ------------------------------------------------------------------
        # Register the job and hand off to the background runner.
        # The temp directory is now owned by _run_async_video_job, which will
        # call cleanup_temp_dir in its finally block.
        # ------------------------------------------------------------------
        _register_async_job(job_id, context=context)
        background_tasks.add_task(
            _run_async_video_job,
            context=context,
            job_id=job_id,
            job_dir=job_dir,
            asset_ids=asset_ids,
            effective_input_video_path=effective_input_video_path,
            upload_content_type=upload_content_type,
            input_source_url=input_source_url,
            compression_applied=compression_applied,
            preserve_original_audio=(
                preserve_original_audio or context.settings.preserve_original_audio
            ),
            requested_volume=requested_volume,
            water_mark=water_mark,
            include_vocals=include_vocals,
            vocal_gender=gender_value,
            user_prompt=user_prompt,
            verbose_instruction=verbose_instruction,
            music_style_prompt=normalized_music_style_prompt or None,
            lyrics_prompt=normalized_lyrics_prompt or None,
            requested_modelspec=requested_modelspec,
            audio_output_format=requested_audio_format,
            vocal_id=normalized_vocal_id,
            vocal_sample_path=prepared_vocal_sample_path,
            user_id=user_id,
            principal=get_principal(request),
            callback_url=normalized_callback_url,
        )

        return AsyncVideoJobAcceptedResponse(job_id=job_id)

    # -----------------------------------------------------------------------
    # GET /api/v1/jobs/async_video_music_gen/{job_id}
    # Poll for status and retrieve the full result once complete.
    # -----------------------------------------------------------------------

    @router.get(
        "/api/v1/jobs/async_video_music_gen/{job_id}",
        response_model=AsyncVideoJobStatusResponse,
        summary="Poll the status of an async video music generation job.",
    )
    async def get_async_video_music_gen_status(job_id: str) -> AsyncVideoJobStatusResponse:
        state = _get_async_job_state(job_id, context=context)
        if state is None:
            raise HTTPException(
                status_code=404,
                detail=f"Job '{job_id}' not found. It may have expired or never existed.",
            )
        return AsyncVideoJobStatusResponse(
            job_id=job_id,
            status=state.status,
            result=_state_result_model(state),
            error=state.error,
            created_at=state.created_at,
        )

    return router


__all__ = [
    "AsyncVideoJobAcceptedResponse",
    "AsyncVideoJobStatusResponse",
    "AudioMetadataBlock",
    "RequestMetadata",
    "ResponseMetadata",
    "SceneModel",
    "VideoGenerationCostResponse",
    "VideoGeometry",
    "VideoJobAssets",
    "VideoJobResponse",
    "VideoMetadataBlock",
    "build_video_job_response",
    "create_video_generation_router",
]
