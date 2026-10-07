from __future__ import annotations

from copy import deepcopy
import json
import logging
import time
from pathlib import Path
from typing import Any, List, Literal, Optional, Union
from urllib.parse import urlparse

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field, ValidationError

from EdennCode.Deployment.api_common import (
    ApiContext,
    edenn_error_to_http_exception,
    filename_from_url,
    service_version,
)
from EdennCode.Deployment.shared_pg_pool import api_pg_client
from EdennCode.Deployment.error_codes import (
    INPUT_INVALID,
    public_error_payload,
    redact_error_blob,
    scrub_provider_names,
)
from EdennCode.Deployment.api_video_generation import (
    LEGACY_MODEL_MAP,
    VALID_MUSIC_MODEL_SPECS,
    _normalize_async_callback_url,
    _prepare_vocal_clone_request_input,
    _validate_async_music_volume,
)
from EdennCode.Deployment.api_multi_image_generation import (
    resolve_fixed_image_durations,
    resolve_uniform_image_timing,
    validate_multi_image_request,
)
from EdennCode.Deployment.async_pipeline_v2.artifact_service import (
    ArtifactStagingService,
    guess_video_content_type,
)
from EdennCode.Deployment.async_pipeline_v2.models import (
    AsyncV2Artifact,
    JobStatus,
    TaskEnvelope,
    new_id,
)
from EdennCode.Deployment.async_pipeline_v2.queue_names import namespaced_queue_name
from EdennCode.Deployment.async_pipeline_v2.queue.postgres_queue import PostgresTaskQueue
from EdennCode.Deployment.async_pipeline_v2.repositories import AsyncPipelineV2Repository
from EdennCode.Deployment.async_pipeline_v2.result_paths import (
    AUDIO_URL_PATH,
    COMPLETE_AUDIO_URL_PATH,
    THUMBNAIL_URL_PATH,
    VIDEO_URL_PATH,
    get_path,
    order_result_keys,
    strip_cost_metadata,
    strip_result_envelope_duplicates,
    set_path_if_present,
)
from EdennCode.Deployment.auth.key_store import Principal
from EdennCode.Deployment.auth.middleware import get_principal
from EdennCode.Deployment.recommendation_persistence import RecommendationAssetIds
from EdennCode.exceptions import EdennError, EdennLyricsUnsupportedModelspecError
from EdennCode.Deployment.async_pipeline_v2.input_guardrails import (
    MAX_SOURCE_VIDEO_BYTES,
    validate_source_video_metadata,
    validate_source_video_size,
)


logger = logging.getLogger(__name__)


# SAS re-signing on read. Both tables carry the current nested paths AND the flat
# paths of results written before the response was restructured — old rows are
# replayed verbatim, so without the flat entries every historical job would start
# serving the expired SAS baked into its stored payload. `set_path_if_present`
# only writes a key that already exists, so each row is touched by exactly one of
# the two tiers and never ends up a hybrid of both.
_RESULT_URL_PATHS_BY_ARTIFACT: dict[
    tuple[str, Optional[str]], tuple[tuple[str, ...], ...]
] = {
    ("matched_audio", "primary"): (AUDIO_URL_PATH, ("audio_url",)),
    ("complete_audio", "primary"): (COMPLETE_AUDIO_URL_PATH, ("complete_audio_url",)),
    ("remixed_video", "final"): (VIDEO_URL_PATH, ("video_url",)),
    # Multi-image renders its slideshow as `output_video`. It used to be absent
    # here, leaving its video_url to the blob fallback alone.
    ("output_video", "final"): (VIDEO_URL_PATH, ("video_url",)),
    ("thumbnail", "thumbnail"): (THUMBNAIL_URL_PATH, ("thumbnail_url",)),
    # Legacy-only: these fields no longer exist on the current response.
    ("source_video", "input"): (("upload_url",),),
    ("secondary_complete_audio", "secondary"): (("secondary_complete_audio_url",),),
}

# (url_path, blob_path, container setting) for URLs no artifact row covered. The
# current response carries no blob names, so `blob_path` resolves to nothing and
# the blob is recovered from the stale URL instead; legacy rows still hit their
# stored blob first.
_RESULT_BLOB_URL_FALLBACKS: tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...] = (
    (AUDIO_URL_PATH, ("audio_metadata", "audio_blob"), "audio_container_name"),
    (
        COMPLETE_AUDIO_URL_PATH,
        ("audio_metadata", "complete_audio_blob"),
        "audio_container_name",
    ),
    (VIDEO_URL_PATH, ("video_metadata", "video_blob"), "output_container"),
    (THUMBNAIL_URL_PATH, ("video_metadata", "thumbnail_blob"), "output_container"),
    # Legacy flat rows.
    (("upload_url",), ("upload_blob",), "upload_container"),
    (("audio_url",), ("audio_blob",), "audio_container_name"),
    (("complete_audio_url",), ("complete_audio_blob",), "audio_container_name"),
    (
        ("secondary_complete_audio_url",),
        ("secondary_complete_audio_blob",),
        "audio_container_name",
    ),
    (("video_url",), ("video_blob",), "output_container"),
    (("thumbnail_url",), ("thumbnail_blob",), "output_container"),
)

def _parse_str_list(raw: Union[str, List[str], None]) -> list[str]:
    """Parse a form field that may be a JSON array or a comma-separated list.

    Also accepts a repeated form field (the key sent once per value): FastAPI
    hands those over as a list of strings, and each entry is parsed the same
    way, so ``field=a&field=b`` and ``field=a,b`` are equivalent.
    """
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        out: list[str] = []
        for item in raw:
            parsed = _parse_str_list(item)
            if not parsed and item.strip():
                # A non-blank entry that parses to nothing (an explicit "[]")
                # must stay visible to downstream validation rather than
                # vanish — its comma-string form is a clean 400, and dropping
                # it here would bypass the per-boundary/per-image count checks.
                out.append(item.strip())
            else:
                out.extend(parsed)
        return out
    text = raw.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return [part.strip() for part in text.split(",") if part.strip()]


def _parse_float_list(
    raw: Union[str, List[str], None], *, field_name: str
) -> Optional[list[float]]:
    """Parse a JSON array, comma list, or repeated field of numbers; None if absent.

    Returns ``None`` only when the field is genuinely absent (blank). A field that
    is present but resolves to zero numbers (e.g. an explicit empty ``[]``) is a
    client mistake, not a request for default timing, so it raises
    HTTPException(400) rather than silently falling back. A malformed list (values
    that are not numbers) is likewise a clean 400 at submit.
    """
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        if all(not item.strip() for item in raw):
            return None
    elif not raw.strip():
        return None
    items = _parse_str_list(raw)
    if not items:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must contain at least one number.",
        )
    try:
        return [float(item) for item in items]
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be a list of numbers.",
        ) from exc


_TRANSITION_DURATION_MIN_S = 0.05
_TRANSITION_DURATION_MAX_S = 2.0


def _fit_per_boundary(items: list, boundary_count: int, field_name: str) -> list:
    """A single value applies to every boundary; otherwise the count must match."""
    if len(items) == 1:
        return items * boundary_count
    if len(items) != boundary_count:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{field_name} must have 1 or {boundary_count} entries "
                f"(one per image boundary), got {len(items)}."
            ),
        )
    return list(items)


def _resolve_multi_image_transitions(
    *,
    transition_mode: str,
    transition_types: Union[str, List[str], None],
    transition_duration_s: Union[str, List[str], None],
    boundary_count: int,
) -> tuple[str, Optional[list[str]], Optional[list[float]]]:
    """Resolve the transition request to (mode, per-boundary effects, per-boundary durations).

    ``transition_types`` (JSON array, comma list, or repeated field) gives
    explicit per-boundary effects — each entry a supported effect name or
    ``auto``/``random`` (that boundary is randomized here so the stored plan is
    reproducible). It overrides ``transition_mode``. Otherwise
    ``transition_mode`` selects the default: ``auto``/``random`` (random per
    boundary, resolved at render) or ``none`` (hard cuts).
    ``transition_duration_s`` (same accepted shapes) is one blend length per
    boundary, each 0.05-2.0s. A single value in either list applies to every
    boundary. Raises ``HTTPException(400)`` on any violation.
    """
    from EdennCode.Util.MediaUtils import (
        SUPPORTED_TRANSITIONS,
        pick_random_transitions,
    )

    durations: Optional[list[float]] = None
    parsed_durs = _parse_float_list(
        transition_duration_s, field_name="transition_duration_s"
    )
    if parsed_durs:
        durations = _fit_per_boundary(
            parsed_durs, boundary_count, "transition_duration_s"
        )
        for d in durations:
            if not (_TRANSITION_DURATION_MIN_S <= d <= _TRANSITION_DURATION_MAX_S):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Each transition duration must be between "
                        f"{_TRANSITION_DURATION_MIN_S} and {_TRANSITION_DURATION_MAX_S} seconds."
                    ),
                )

    parsed_types = [t.strip().lower() for t in _parse_str_list(transition_types)]
    if parsed_types:
        fitted = _fit_per_boundary(parsed_types, boundary_count, "transition_types")
        resolved: list[str] = []
        for name in fitted:
            if name in {"auto", "random"}:
                resolved.append(pick_random_transitions(1)[0])
            elif name in SUPPORTED_TRANSITIONS:
                resolved.append(name)
            else:
                allowed = ", ".join(["auto", *sorted(SUPPORTED_TRANSITIONS)])
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid transition type '{name}'. Allowed: {allowed}.",
                )
        return "custom", resolved, durations

    mode = (transition_mode or "auto").strip().lower()
    if mode in {"auto", "random"}:
        return "random", None, durations
    if mode == "none":
        return "none", None, durations
    raise HTTPException(
        status_code=400,
        detail="Invalid transition_mode. Allowed values: auto, random, none.",
    )


def _normalize_multi_image_modelspec(raw: str) -> str:
    value = (raw or "edenn_basic").strip().lower() or "edenn_basic"
    value = LEGACY_MODEL_MAP.get(value, value)
    if value not in VALID_MUSIC_MODEL_SPECS:
        allowed = ", ".join(sorted(VALID_MUSIC_MODEL_SPECS))
        raise HTTPException(
            status_code=400,
            # No echo of the rejected value: the same message on every endpoint,
            # and a legacy vendor-named alias is never repeated back to a client.
            detail=f"Invalid modelspec. Allowed values: {allowed}.",
        )
    return value


class AsyncV2VideoAssetResponse(BaseModel):
    artifact_id: str
    job_id: str
    artifact_type: str
    status: str = JobStatus.COMPLETED
    version: str = Field(default_factory=service_version)
    url: Optional[str] = None
    content_type: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    status_url: str


class CreateVideoMusicJobRequest(BaseModel):
    source_video_artifact_id: Optional[str] = None
    video_url: Optional[str] = None
    modelspec: str = Field(default="edenn_basic")
    user_prompt: str = Field(default="")
    preserve_original_audio: bool = Field(default=False)
    music_volume: float = Field(default=1.0)
    # Watermark ON by default (parity with multi-image): the delivered track ends
    # with the spoken Edenn watermark unless the caller explicitly opts out.
    water_mark: bool = Field(default=True)
    # Compression ON by default (current standard): sources taller than
    # compression_max_height are downscaled before the workflow. Opting out
    # only skips downscaling — every source, regardless of this flag, is
    # re-encoded to H.264 at original resolution unless it already is H.264,
    # so delivered videos always carry an H.264 stream.
    compression_flag: bool = Field(default=True)
    compression_max_height: int = Field(default=1280, ge=1, le=4320)
    # Routing policy: video music always runs on the split pipeline. The field
    # is kept for backward compatibility only — whatever the caller sends, the
    # job is routed split whenever cross-container storage is available. The
    # monolith path survives solely for storage-less local single-process runs.
    mode: Optional[Literal["monolith", "split"]] = Field(default=None)
    verbose_instruction: bool = Field(default=False)
    music_style_prompt: Optional[str] = None
    lyrics_prompt: Optional[str] = None
    language: Optional[str] = None
    audio_output_format: Optional[str] = None
    vocal_id: Optional[str] = None
    vocal_sample_path: Optional[str] = None
    user_id: Optional[str] = None
    callback_url: Optional[str] = None
    creator_user_id: Optional[str] = None
    session_id: Optional[str] = None
    priority: int = Field(default=0, ge=0, le=1000)
    # Default no-retry: a retry re-runs the whole monolith pipeline including the
    # paid music generation, so it must be an explicit opt-in (parity with
    # multi-image, which defaults to 1 for the same reason).
    max_attempts: int = Field(default=1, ge=1, le=10)
    video_id: Optional[str] = None
    creative_id: Optional[str] = None
    primary_music_id: Optional[str] = None
    secondary_music_id: Optional[str] = None
    selected_music_id: Optional[str] = None
    alignment_id: Optional[str] = None


class CreateVideoMusicJobResponse(BaseModel):
    job_id: str
    task_id: str
    status: str = JobStatus.QUEUED
    version: str = Field(default_factory=service_version)
    status_url: str


class AsyncV2JobEventsResponse(BaseModel):
    job_id: str
    events: list[dict[str, Any]]


class AsyncV2JobStatusResponse(BaseModel):
    job_id: str
    job_type: str
    # Upleveled out of the result block: the generation modelspec surfaced once at
    # the envelope. None until a completed result carries it.
    modelspec: Optional[str] = None
    status: str
    current_stage: Optional[str] = None
    progress_percent: float = 0.0
    priority: int = 0
    version: str = Field(default_factory=service_version)
    result: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    finished_at: Optional[str] = None


def _artifact_response(artifact: AsyncV2Artifact) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "artifact_type": artifact.artifact_type,
        "role": artifact.role,
        "container": artifact.container,
        "blob_name": artifact.blob_name,
        "url": artifact.url,
        "content_type": artifact.content_type,
        "metadata": artifact.metadata_json,
        "payload_available": artifact.payload_json is not None,
        "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
    }


def _sanitize_event_payload(payload: Any) -> Any:
    """Scrub the free-text fields of an event payload before it reaches a
    client: the ``error`` sub-object can carry a provider-named error from a
    legacy row, and ``critical_warning`` is provider ops telemetry (vendor
    name, key-pool env label, account balance) that must never leave the
    server. Other fields (task ids, media URLs) are left untouched."""
    if not isinstance(payload, dict):
        return payload
    sanitized = payload
    if payload.get("error") is not None:
        sanitized = dict(payload)
        sanitized["error"] = redact_error_blob(payload["error"])
    if payload.get("critical_warning") is not None:
        if sanitized is payload:
            sanitized = dict(payload)
        sanitized.pop("critical_warning", None)
    return sanitized


def _event_response(event: Any) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "job_id": event.job_id,
        "event_type": event.event_type,
        "stage_name": event.stage_name,
        "message": scrub_provider_names(event.message),
        "payload": _sanitize_event_payload(event.payload_json),
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


def _normalize_modelspec(raw_value: str) -> str:
    value = (raw_value or "edenn_basic").strip().lower() or "edenn_basic"
    value = LEGACY_MODEL_MAP.get(value, value)
    if value not in VALID_MUSIC_MODEL_SPECS:
        allowed = ", ".join(sorted(VALID_MUSIC_MODEL_SPECS))
        raise HTTPException(
            status_code=400,
            detail=f"Invalid modelspec. Allowed values: {allowed}.",
        )
    return value


# Only these tiers expose a dedicated lyrics channel; edenn_basic takes a
# single combined prompt, so lyric direction has nowhere to go there.
_LYRICS_CAPABLE_MODELSPECS = {"edenn_enhanced", "edenn_studio"}


def _resolve_v2_prompt_fields(
    *,
    modelspec: str,
    user_prompt: str,
    music_style_prompt: Optional[str],
    lyrics_prompt: Optional[str],
) -> tuple[str, str]:
    """Resolve the v2 prompt contract: user_prompt plus optional lyrics_prompt.

    music_style_prompt is a deprecated alias for user_prompt (honored only when
    user_prompt is empty) and verbose_instruction is an accepted no-op, so
    pre-collapse clients keep their behavior. A non-empty lyrics_prompt on a
    modelspec without a lyrics channel is rejected with a coded 400 rather than
    silently dropping paid-for lyric direction — including when the caller
    omitted modelspec and landed on the edenn_basic default.
    """
    resolved_user_prompt = (user_prompt or "").strip()
    legacy_style = (music_style_prompt or "").strip()
    resolved_lyrics = (lyrics_prompt or "").strip()
    if legacy_style and not resolved_user_prompt:
        resolved_user_prompt = legacy_style
    if resolved_lyrics and modelspec not in _LYRICS_CAPABLE_MODELSPECS:
        raise edenn_error_to_http_exception(
            EdennLyricsUnsupportedModelspecError(
                f"lyrics_prompt is not supported for modelspec={modelspec}."
            )
        )
    return resolved_user_prompt, resolved_lyrics


def _validate_request(
    request: CreateVideoMusicJobRequest,
    *,
    has_video_upload: bool = False,
) -> dict[str, Any]:
    source_artifact_id = (request.source_video_artifact_id or "").strip()
    video_url = (request.video_url or "").strip()
    use_video_upload = has_video_upload and not video_url
    if source_artifact_id and (video_url or use_video_upload):
        raise HTTPException(
            status_code=400,
            detail="Provide source_video_artifact_id or video/video_url, not both.",
        )
    if not source_artifact_id and not video_url and not use_video_upload:
        raise HTTPException(
            status_code=400,
            detail="Provide a video upload or video_url.",
        )

    modelspec = _normalize_modelspec(request.modelspec)
    callback_url = _normalize_async_callback_url(request.callback_url)
    _validate_async_music_volume(request.music_volume)
    resolved_user_prompt, resolved_lyrics_prompt = _resolve_v2_prompt_fields(
        modelspec=modelspec,
        user_prompt=request.user_prompt,
        music_style_prompt=request.music_style_prompt,
        lyrics_prompt=request.lyrics_prompt,
    )

    payload = request.model_dump(mode="json", exclude_none=True)
    if source_artifact_id:
        payload["source_video_artifact_id"] = source_artifact_id
        payload.pop("video_url", None)
    elif video_url:
        payload["video_url"] = video_url
        payload.pop("source_video_artifact_id", None)
    else:
        payload.pop("source_video_artifact_id", None)
        payload.pop("video_url", None)
    payload["modelspec"] = modelspec
    if callback_url:
        payload["callback_url"] = callback_url
    else:
        payload.pop("callback_url", None)
    payload["music_volume"] = float(request.music_volume)
    # Lyric-directed jobs are stored in the legacy verbose payload shape
    # (verbose flag on, the resolved user prompt in the style slot, user_prompt
    # empty). Any worker generation reads that shape when a style/user_prompt
    # accompanies the lyrics; the lyrics-only variant (empty style slot)
    # requires a post-collapse worker — pre-collapse workers reject it loudly
    # ("music_style_prompt is required"), never silently — so roll out workers
    # before the API. The public job view strips the request block, so the
    # synthetic fields never reach a client.
    legacy_style_only_request = bool(
        request.verbose_instruction
        and (request.music_style_prompt or "").strip()
        and not (request.user_prompt or "").strip()
        and not resolved_lyrics_prompt
    )
    if legacy_style_only_request and modelspec not in _LYRICS_CAPABLE_MODELSPECS:
        # The pre-collapse contract rejected verbose+style on edenn_basic at
        # submit; keep that (as the coded 400) rather than accepting a job the
        # worker's explicit-direction gate would fail after the 200.
        raise edenn_error_to_http_exception(
            EdennLyricsUnsupportedModelspecError(
                f"music_style_prompt with verbose_instruction is not supported for modelspec={modelspec}.",
                public_message=(
                    "Explicit music direction (music_style_prompt / lyrics_prompt) "
                    "is not supported by this model specification. Set "
                    "modelspec=edenn_enhanced or modelspec=edenn_studio."
                ),
            )
        )
    if resolved_lyrics_prompt:
        payload["verbose_instruction"] = True
        payload["user_prompt"] = ""
        payload["lyrics_prompt"] = resolved_lyrics_prompt
        if resolved_user_prompt:
            payload["music_style_prompt"] = resolved_user_prompt
        else:
            payload.pop("music_style_prompt", None)
    elif legacy_style_only_request:
        # Full backward compatibility for the pre-collapse client shape
        # (verbose flag + style, no lyrics, empty user_prompt): stored
        # verbatim so it keeps the explicit-direction processing it always
        # had. The style-into-user_prompt fold applies only to callers using
        # music_style_prompt WITHOUT the flag (deprecated-alias usage).
        payload["verbose_instruction"] = True
        payload["user_prompt"] = ""
        payload["music_style_prompt"] = (request.music_style_prompt or "").strip()
        payload.pop("lyrics_prompt", None)
    else:
        payload["verbose_instruction"] = False
        payload["user_prompt"] = resolved_user_prompt
        payload.pop("music_style_prompt", None)
        payload.pop("lyrics_prompt", None)
    return payload


def _workdir_for_job(context: ApiContext, job_id: str, *parts: str) -> Path:
    base = Path(getattr(context.settings, "workdir", "/tmp"))
    return base / "async_pipeline_v2" / job_id / Path(*parts)


def _link_source_artifact_to_job(
    *,
    repository: AsyncPipelineV2Repository,
    job_id: str,
    source_artifact: AsyncV2Artifact,
) -> AsyncV2Artifact:
    linked_artifact_id = f"{job_id}:source_video:input"
    existing = repository.get_artifact(linked_artifact_id)
    if existing is not None:
        return existing

    metadata = dict(source_artifact.metadata_json)
    metadata["source_artifact_id"] = source_artifact.artifact_id
    metadata["source_asset_job_id"] = source_artifact.job_id
    metadata["linked_to_video_music_job"] = True
    return repository.add_artifact(
        artifact_id=linked_artifact_id,
        job_id=job_id,
        artifact_type="source_video",
        role="input",
        container=source_artifact.container,
        blob_name=source_artifact.blob_name,
        url=source_artifact.url,
        content_type=source_artifact.content_type,
        local_path=source_artifact.local_path,
        metadata_json=metadata,
    )


def _artifact_has_durable_video_url(artifact: AsyncV2Artifact) -> bool:
    return bool(artifact.url or artifact.metadata_json.get("source_url"))


def _source_filename_from_url(video_url: str) -> str:
    try:
        return filename_from_url(video_url) or "source_video.mp4"
    except EdennError as exc:
        raise edenn_error_to_http_exception(exc) from exc


def _record_url_source_artifact(
    *,
    repository: AsyncPipelineV2Repository,
    job_id: str,
    artifact_id: str,
    video_url: str,
    source_filename: str,
    request_metadata: dict[str, Any],
) -> AsyncV2Artifact:
    return repository.add_artifact(
        artifact_id=artifact_id,
        job_id=job_id,
        artifact_type="source_video",
        role="input",
        url=video_url,
        content_type=guess_video_content_type(Path(source_filename)),
        metadata_json={
            "source_kind": "remote_url",
            "source_filename": source_filename,
            "source_url": video_url,
            "request": request_metadata,
            "uploaded": False,
        },
    )


def _blob_name_from_blob_url(url: Optional[str]) -> Optional[str]:
    """Best-effort blob path from a blob-storage URL (container segment stripped).

    Used to re-sign a nested media URL whose ``blob`` was not persisted: the SAS
    query and account host are dropped and the leading container segment removed,
    leaving the blob path that can be re-signed against the current account.
    """
    if not url or not isinstance(url, str):
        return None
    try:
        path = urlparse(url).path.lstrip("/")
    except Exception:
        return None
    if not path:
        return None
    parts = path.split("/", 1)
    return parts[1] if len(parts) == 2 and parts[1] else None


def _fresh_artifact_url(storage: Any, artifact: dict[str, Any]) -> Optional[str]:
    if storage is None or not hasattr(storage, "generate_sas_url"):
        return None
    container = artifact.get("container")
    blob_name = artifact.get("blob_name")
    if not container or not blob_name:
        return None
    try:
        return storage.generate_sas_url(container=container, blob_name=blob_name)
    except Exception as exc:
        logger.warning(
            "Failed to refresh async v2 artifact URL for %s/%s: %s",
            container,
            blob_name,
            exc,
        )
        return None


def _strip_blob_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_blob_fields(child)
            for key, child in value.items()
            if "blob" not in key.lower()
        }
    if isinstance(value, list):
        return [_strip_blob_fields(item) for item in value]
    return value


def _uplevel_modelspec(public: dict[str, Any], modelspec: Any) -> dict[str, Any]:
    """Surface the generation modelspec once, at the envelope level.

    The modelspec is upleveled out of the nested ``result`` block to the top of
    the status envelope (right after ``job_type``), so a client sees it without
    descending into the result and it is never duplicated. Sourced from the
    result, it is always the normalized ``edenn_*`` value.
    """
    if "job_type" not in public:
        return {"modelspec": modelspec, **public}
    rebuilt: dict[str, Any] = {}
    for key, value in public.items():
        rebuilt[key] = value
        if key == "job_type":
            rebuilt["modelspec"] = modelspec
    return rebuilt


def _public_status_view(status_view: dict[str, Any]) -> dict[str, Any]:
    public = _strip_blob_fields(status_view)
    public.pop("stages", None)
    public.pop("artifacts", None)
    # The stored request echoes what the caller sent, plus the server-injected
    # orchestration internals (asset ids, routing, a worker-local vocal path). None
    # of it is useful to a client on read, and the worker reads request_json from
    # the database rather than this view — so drop the whole block.
    public.pop("request", None)
    public["version"] = service_version()
    # JSONB stores object keys sorted by (length, name), so a result read back from
    # the database comes out in a different order than the identical result returned
    # inline by v1. Same keys, same values — but restore the declared order so both
    # transports serialize the same way.
    if public.get("result") is not None:
        public["result"] = order_result_keys(public["result"])
        # Uplevel modelspec: surface it once at the envelope (next to job_type)
        # rather than nested in the result block.
        result_block = public["result"]
        if isinstance(result_block, dict) and "modelspec" in result_block:
            public = _uplevel_modelspec(public, result_block.pop("modelspec"))
        if isinstance(result_block, dict):
            strip_result_envelope_duplicates(result_block)
            # No cost accounting leaves the service; the stored result keeps the
            # full breakdown for per-key billing. Shared with the callback
            # egress so both surfaces stay consistent.
            strip_cost_metadata(result_block)
    # Defense-in-depth: the job-level error is the one error blob that survives
    # into the public status. Scrub provider/model names (and drop vendor
    # identity keys) so even legacy rows persisted before sanitization can't leak.
    if public.get("error") is not None:
        public["error"] = redact_error_blob(public["error"])
    return public


def _status_view_with_fresh_urls(
    status_view: dict[str, Any],
    *,
    storage: Any,
    settings: Any = None,
) -> dict[str, Any]:
    refreshed = deepcopy(status_view)
    artifacts = refreshed.get("artifacts")
    result = refreshed.get("result")
    if not isinstance(artifacts, list):
        return _public_status_view(refreshed)

    refreshed_result_paths: set[tuple[str, ...]] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        fresh_url = _fresh_artifact_url(storage, artifact)
        if not fresh_url:
            continue

        artifact["url"] = fresh_url
        if isinstance(result, dict):
            paths = _RESULT_URL_PATHS_BY_ARTIFACT.get(
                (artifact.get("artifact_type"), artifact.get("role")),
                (),
            )
            for path in paths:
                # Only one of the nested/flat variants exists on a given row; the
                # other write is a no-op.
                if set_path_if_present(result, path, fresh_url):
                    refreshed_result_paths.add(path)

    if isinstance(result, dict):
        for url_path, blob_path, container_setting in _RESULT_BLOB_URL_FALLBACKS:
            if url_path in refreshed_result_paths:
                continue
            # No guard on the URL being present: a row can carry a blob with a null
            # URL (thumbnail written before its SAS was minted), and that case still
            # has to be signed. Paths that don't apply to this row fall out below,
            # either for want of a blob or at the set_path_if_present write.
            container = getattr(settings, container_setting, None) if settings else None
            if not container:
                continue
            # Prefer a stored blob name (legacy rows); otherwise recover it from the
            # (possibly stale, possibly foreign-account) URL. Re-signing from the blob
            # against the current account guarantees we never serve a baked URL that
            # points at a decommissioned/renamed storage account (blob-not-found),
            # regardless of which image originally wrote the row.
            blob_name = get_path(result, blob_path) or _blob_name_from_blob_url(
                get_path(result, url_path)
            )
            if not blob_name:
                continue
            fresh_url = _fresh_artifact_url(
                storage,
                {"container": container, "blob_name": blob_name},
            )
            if fresh_url:
                set_path_if_present(result, url_path, fresh_url)

        # Legacy-only: results written before the response was restructured carry a
        # nested full_tracks[], each entry with its own blob. Re-mint a fresh SAS per
        # track or those URLs 404 once the staged SAS expires. Current responses have
        # no full_tracks, so this is a no-op for them.
        audio_container = (
            getattr(settings, "audio_container_name", None) if settings else None
        )
        full_tracks = result.get("full_tracks")
        if audio_container and isinstance(full_tracks, list):
            for item in full_tracks:
                if not isinstance(item, dict):
                    continue
                blob_name = item.get("blob") or _blob_name_from_blob_url(item.get("url"))
                if not blob_name:
                    continue
                fresh_url = _fresh_artifact_url(
                    storage,
                    {"container": audio_container, "blob_name": blob_name},
                )
                if fresh_url:
                    item["url"] = fresh_url

    return _public_status_view(refreshed)


def create_async_pipeline_v2_router(
    context: ApiContext,
    *,
    repository: AsyncPipelineV2Repository | None = None,
    queue: PostgresTaskQueue | None = None,
    artifact_staging: ArtifactStagingService | None = None,
) -> APIRouter:
    # Client-facing path: reuse a process-wide pooled connection per DB op
    # instead of opening a fresh TLS connection each call. The pool is built
    # lazily on first use, so injected (test) repositories/queues never touch it.
    repo = repository or AsyncPipelineV2Repository(client_factory=api_pg_client)
    task_queue = queue or PostgresTaskQueue(client_factory=api_pg_client)
    staging = artifact_staging or ArtifactStagingService(
        repository=repo,
        storage=context.storage,
        upload_container=getattr(context.settings, "upload_container", "user-uploads"),
    )
    router = APIRouter(prefix="/api/v2", tags=["async-pipeline-v2"])

    @router.post("/assets/video", response_model=AsyncV2VideoAssetResponse)
    async def stage_video_asset(
        video: UploadFile | None = File(default=None),
        video_url: Optional[str] = Form(default=None),
        creator_user_id: Optional[str] = Form(default=None),
        session_id: Optional[str] = Form(default=None),
    ) -> AsyncV2VideoAssetResponse:
        upload_provided = video is not None
        url_value = (video_url or "").strip()
        url_provided = bool(url_value)
        if upload_provided == url_provided:
            raise HTTPException(
                status_code=400,
                detail="Provide exactly one of video upload or video_url.",
            )

        asset_job_id = new_id("asset_job")
        repo.create_job(
            job_id=asset_job_id,
            job_type="asset_staging",
            request_json={
                "video_url": url_value or None,
                "upload_filename": video.filename if video is not None else None,
            },
            session_id=session_id,
            creator_user_id=creator_user_id,
            status=JobStatus.PROCESSING,
        )

        try:
            destination_dir = _workdir_for_job(context, asset_job_id, "input")
            if video is not None:
                data = await video.read()
                await video.close()
                # Size guardrail BEFORE staging: an oversize upload must not be
                # probed, hashed, or pushed to blob storage first.
                validate_source_video_size(len(data))
                staged = staging.stage_video_bytes(
                    job_id=asset_job_id,
                    data=data,
                    filename=video.filename or "source_video.mp4",
                    destination_dir=destination_dir,
                    upload_content_type=video.content_type,
                    request_metadata={
                        "creator_user_id": creator_user_id,
                        "session_id": session_id,
                        "source": "upload",
                    },
                    # The strict guardrail below replaces the legacy staging
                    # duration backstop (which reports the outdated 300s limit).
                    validate_duration=False,
                )
            else:
                staged = await staging.stage_video_url(
                    job_id=asset_job_id,
                    video_url=url_value,
                    destination_dir=destination_dir,
                    request_metadata={
                        "creator_user_id": creator_user_id,
                        "session_id": session_id,
                        "source": "url",
                    },
                    validate_duration=False,
                    max_bytes=MAX_SOURCE_VIDEO_BYTES,
                )
            # Input guardrails at pre-stage time, so violations surface the
            # specific 10005/10006/10007 codes here instead of at job create.
            validate_source_video_metadata(
                staged.artifact.metadata_json,
                source_path=staged.local_path,
            )

            repo.update_job_status(
                asset_job_id,
                status=JobStatus.COMPLETED,
                current_stage="artifact-staging",
                progress_percent=100,
                result_json={"artifact": _artifact_response(staged.artifact)},
                finished=True,
            )
            return AsyncV2VideoAssetResponse(
                artifact_id=staged.artifact.artifact_id,
                job_id=asset_job_id,
                artifact_type=staged.artifact.artifact_type,
                url=staged.artifact.url,
                content_type=staged.artifact.content_type,
                metadata=staged.artifact.metadata_json,
                status_url=f"/api/v2/jobs/{asset_job_id}",
            )
        except Exception as exc:
            logger.exception("Async v2 video asset staging failed for %s: %s", asset_job_id, exc)
            error_json = public_error_payload(exc)
            repo.update_job_status(
                asset_job_id,
                status=JobStatus.FAILED,
                current_stage="artifact-staging",
                error_json=error_json,
                finished=True,
            )
            repo.add_event(
                job_id=asset_job_id,
                event_type="job.failed",
                stage_name="artifact-staging",
                message="Video asset staging failed.",
                payload_json={"error": error_json},
            )
            if isinstance(exc, EdennError):
                # Structured {error_code, message, retryable} detail so
                # guardrail rejections surface their specific code.
                raise edenn_error_to_http_exception(exc) from exc
            raise HTTPException(status_code=400, detail=error_json["message"]) from exc

    # ------------------------------------------------------------------
    # Multi-image music generation (v2). Mirrors the video-music durable path:
    # stage N source images (+ optional vocal) as artifacts, then enqueue a
    # `multi_image_monolith` task. job_type = "multi_image".
    # ------------------------------------------------------------------
    @router.post("/assets/image", response_model=AsyncV2VideoAssetResponse)
    async def stage_image_asset(
        image: UploadFile | None = File(default=None),
        image_url: Optional[str] = Form(default=None),
        index: int = Form(default=0),
        creator_user_id: Optional[str] = Form(default=None),
        session_id: Optional[str] = Form(default=None),
    ) -> AsyncV2VideoAssetResponse:
        upload_provided = image is not None
        url_value = (image_url or "").strip()
        if upload_provided == bool(url_value):
            raise HTTPException(
                status_code=400,
                detail="Provide exactly one of image upload or image_url.",
            )
        asset_job_id = new_id("asset_job")
        repo.create_job(
            job_id=asset_job_id,
            job_type="asset_staging",
            request_json={
                "image_url": url_value or None,
                "upload_filename": image.filename if image is not None else None,
            },
            session_id=session_id,
            creator_user_id=creator_user_id,
            status=JobStatus.PROCESSING,
        )
        try:
            destination_dir = _workdir_for_job(context, asset_job_id, "input")
            if image is not None:
                data = await image.read()
                await image.close()
                staged = staging.stage_image_bytes(
                    job_id=asset_job_id,
                    data=data,
                    filename=image.filename or f"image_{index + 1}.png",
                    destination_dir=destination_dir,
                    index=index,
                    upload_content_type=image.content_type,
                    request_metadata={"source": "upload", "session_id": session_id},
                )
            else:
                staged = await staging.stage_image_url(
                    job_id=asset_job_id,
                    image_url=url_value,
                    destination_dir=destination_dir,
                    index=index,
                    request_metadata={"source": "url", "session_id": session_id},
                )
            repo.update_job_status(
                asset_job_id,
                status=JobStatus.COMPLETED,
                current_stage="artifact-staging",
                progress_percent=100,
                result_json={"artifact": _artifact_response(staged.artifact)},
                finished=True,
            )
            return AsyncV2VideoAssetResponse(
                artifact_id=staged.artifact.artifact_id,
                job_id=asset_job_id,
                artifact_type=staged.artifact.artifact_type,
                url=staged.artifact.url,
                content_type=staged.artifact.content_type,
                metadata=staged.artifact.metadata_json,
                status_url=f"/api/v2/jobs/{asset_job_id}",
            )
        except Exception as exc:
            logger.exception("Async v2 image asset staging failed for %s: %s", asset_job_id, exc)
            repo.update_job_status(
                asset_job_id,
                status=JobStatus.FAILED,
                current_stage="artifact-staging",
                error_json=public_error_payload(exc),
                finished=True,
            )
            raise HTTPException(
                status_code=400, detail=public_error_payload(exc)["message"]
            ) from exc

    @router.post("/jobs/multi-image-music", response_model=CreateVideoMusicJobResponse)
    async def create_multi_image_music_job(
        request: Request,
        images: List[UploadFile] = File(default=[], description="Image files."),
        image_urls: Optional[List[str]] = Form(
            None,
            description=(
                "Image URLs — a JSON array, a comma-separated string, or the "
                "field repeated once per URL."
            ),
        ),
        # Artifact ids are not a client-facing input (they are server-issued by
        # the assets endpoint and used by internal tooling), so this keeps the
        # single-string contract instead of the repeated-field list support the
        # client-facing list params get.
        image_artifact_ids: Optional[str] = Form(None, description="JSON array or comma-separated pre-staged source_image artifact ids."),
        user_prompt: str = Form(""),
        modelspec: str = Form(""),
        align_to_beats: bool = Form(True),
        per_image_duration: float = Form(
            5.0, gt=0, le=60,
            description=(
                "Seconds each image is displayed (3-60; below 3 is rejected "
                "with error code 10008). When image_count x per_image_duration "
                "is under 15s, 1s is added per image round-robin from the "
                "first image until the total reaches 15s."
            ),
        ),
        image_order: str = Form(
            "auto",
            description=(
                "'auto' (default) lets planning pick the narrative order of the "
                "images; 'fixed' keeps the exact order the images were provided "
                "in — planning composes the story and music for that order."
            ),
        ),
        per_image_durations: Optional[List[str]] = Form(
            None,
            description=(
                "Requires image_order=fixed. Seconds per image, one per image "
                "(must match the image count) — sent as a JSON array, a "
                "comma-separated string, or the field repeated once per value. "
                "Each image is shown exactly that long and beat alignment is "
                "disabled. Each value must be at least 3s (error code 10008 "
                "below); a total under 15s is bumped by +1s per image "
                "round-robin from the first image until it reaches 15s. "
                "Mutually exclusive with total_duration_s."
            ),
        ),
        total_duration_s: Optional[float] = Form(
            None,
            description=(
                "Requires image_order=fixed. Total slideshow length in seconds, "
                "split evenly across the images (beat alignment disabled). The "
                "even per-image share must be at least 3s (error code 10008 "
                "below); a total under 15s is bumped by +1s per image "
                "round-robin from the first image until it reaches 15s. "
                "Mutually exclusive with per_image_durations."
            ),
        ),
        transition_mode: str = Form(
            "auto",
            description=(
                "Default transition behavior when transition_types is omitted: "
                "'auto' (default) / 'random' pick a random effect per image "
                "boundary; 'none' hard-cuts. Ignored when transition_types is given."
            ),
        ),
        transition_types: Optional[List[str]] = Form(
            None,
            description=(
                "Explicit per-boundary transitions — one entry per image boundary "
                "(image count - 1), sent as a JSON array, a comma-separated "
                "string, or the field repeated once per value. Each entry is an "
                "effect name (see docs/multi-image-transitions.md) or 'auto' to "
                "randomize just that boundary. A single value applies to every "
                "boundary. When given, it overrides transition_mode."
            ),
        ),
        transition_duration_s: Optional[List[str]] = Form(
            None,
            description=(
                "Cross-fade length(s) in seconds — one duration per image "
                "boundary (image count - 1), each 0.05-2.0s, sent as a JSON "
                "array, a comma-separated string, or the field repeated once per "
                "value. A single value applies to every boundary. Defaults to "
                "0.4s when omitted."
            ),
        ),
        music_volume: Optional[float] = Form(None),
        # Watermark is ON by default: the delivered track ends with the spoken
        # Edenn watermark unless the caller explicitly opts out.
        water_mark: bool = Form(True),
        # Whether the track has vocals, the singer's gender, and the lyric
        # language are inferred from user_prompt by the understanding stage — they
        # are no longer request fields. A non-empty lyric direction is an
        # explicit request that also turns vocals on for this job.
        # `lyrics_prompt` is the canonical field name (shared with the
        # video-music endpoint); `user_lyrics_prompt` is its deprecated alias,
        # honored only when `lyrics_prompt` is empty. Internally the payload
        # keeps the historical `user_lyrics_prompt` key so deployed workers
        # need no change.
        lyrics_prompt: Optional[str] = Form(None, description="Optional lyric direction; when provided the track is generated with vocals following this direction."),
        user_lyrics_prompt: Optional[str] = Form(None, description="Deprecated alias for lyrics_prompt (used only when lyrics_prompt is empty)."),
        audio_output_format: Optional[str] = Form(None, description="Optional audio output format hint (e.g. mp3, wav)."),
        creator_user_id: Optional[str] = Form(None),
        session_id: Optional[str] = Form(None),
        # Four priority levels: 0 (lowest) through 3 (highest).
        priority: int = Form(0, ge=0, le=3),
        # Default no-retry: the paid music provider must not be double-billed.
        max_attempts: int = Form(1, ge=1, le=10),
    ) -> CreateVideoMusicJobResponse:
        modelspec_norm = _normalize_multi_image_modelspec(modelspec)
        image_order_norm = (image_order or "auto").strip().lower() or "auto"
        if image_order_norm not in {"auto", "fixed"}:
            raise HTTPException(
                status_code=400,
                detail="Invalid image_order. Allowed values: auto, fixed.",
            )
        # Same range rule as video-music: reject out-of-range gain at submit
        # instead of shipping it into a billed job.
        if music_volume is not None:
            _validate_async_music_volume(music_volume)
        parsed_artifact_ids = _parse_str_list(image_artifact_ids)
        parsed_urls = _parse_str_list(image_urls)
        upload_images = [img for img in (images or []) if img is not None and img.filename]
        total_images = len(parsed_artifact_ids) + len(parsed_urls) + len(upload_images)
        # Explicit fixed-order timing (per-image list or overall total). Validated
        # against the resolved image count and gated to image_order=fixed; when
        # present, resolves to an exact per-image duration list that turns off
        # beat alignment downstream (each value >= 3s, and a sub-15s total is
        # bumped up to the billing floor). None keeps the uniform behavior.
        parsed_per_image_durations = _parse_float_list(
            per_image_durations, field_name="per_image_durations"
        )
        resolved_image_durations = resolve_fixed_image_durations(
            image_count=total_images,
            per_image_durations=parsed_per_image_durations,
            total_duration_s=total_duration_s,
            image_order=image_order_norm,
        )
        # Count limits always apply (parity with v1; a small max also caps the
        # synchronous per-image download + blob upload fan-out on the API replica).
        # The uniform-duration bounds are skipped when explicit timing wins — that
        # path ignores per_image_duration and is already bounded in the resolver.
        validate_multi_image_request(
            total_images,
            per_image_duration,
            enforce_uniform_timing=resolved_image_durations is None,
        )
        if resolved_image_durations is None:
            # 15s billing floor on the uniform plan: when the bump lands equally
            # on every image the plan stays uniform (beat alignment preserved);
            # otherwise it becomes an explicit per-image list, exactly like
            # caller-supplied fixed timing.
            per_image_duration, resolved_image_durations = (
                resolve_uniform_image_timing(total_images, per_image_duration)
            )
        # Resolve transitions to a concrete per-boundary shape here so a bad effect
        # name / length is a 4xx at submit (not a failed billed job). The count
        # check above guarantees >= 3 images, so boundary_count >= 2. "auto" entries
        # in an explicit list are randomized now for a reproducible stored plan.
        boundary_count = total_images - 1
        transition_mode_resolved, transitions, transition_durations = (
            _resolve_multi_image_transitions(
                transition_mode=transition_mode,
                transition_types=transition_types,
                transition_duration_s=transition_duration_s,
                boundary_count=boundary_count,
            )
        )

        # Referenced artifacts are validated BEFORE the job row exists: a purely
        # client-side mistake must not leave a phantom FAILED job behind. 404
        # for parity with the video-music source-artifact lookup.
        for aid in parsed_artifact_ids:
            artifact = repo.get_artifact(aid)
            if artifact is None or artifact.artifact_type != "source_image":
                raise HTTPException(
                    status_code=404,
                    detail=f"Source image artifact not found: {aid}",
                )

        job_id = new_id("job")
        payload: dict[str, Any] = {
            "modelspec": modelspec_norm,
            "user_prompt": user_prompt,
            "align_to_beats": align_to_beats,
            "per_image_duration": per_image_duration,
            "image_order": image_order_norm,
            # Explicit per-image seconds (resolved from per_image_durations or
            # total_duration_s); None keeps uniform + beat-aligned timing.
            "per_image_durations": resolved_image_durations,
            "transition_mode": transition_mode_resolved,
            "transitions": transitions,
            # Per-boundary blend lengths (list) or None for the 0.4s default.
            "transition_duration_s": transition_durations,
            "music_volume": music_volume,
            "water_mark": water_mark,
            # Vocals/gender/language are inferred downstream from user_prompt; these
            # stay as fixed defaults so the worker's request contract is unchanged
            # (the E2E stage ORs include_vocals with the prompt-inferred value).
            "include_vocals": False,
            "vocal_gender": None,
            "lyrics_language": None,
            # An explicit lyric direction; the E2E stage now treats a non-empty
            # value as a vocal request in its own right, so it is never dropped.
            "user_lyrics_prompt": (
                (lyrics_prompt or "").strip()
                or (user_lyrics_prompt or "").strip()
                or None
            ),
            "audio_output_format": (audio_output_format or "").strip() or None,
            "vocal_id": None,
            "session_id": session_id,
            "creator_user_id": creator_user_id,
        }
        principal = get_principal(request)
        enforced = getattr(request.state, "auth_enforced", False)
        if principal is not None:
            payload["auth_user_id"] = principal.user_id
            payload["auth_key_prefix"] = principal.key_prefix
            if enforced:
                creator_user_id = principal.user_id
                payload["creator_user_id"] = creator_user_id
        repo.create_job(
            job_id=job_id,
            job_type="multi_image",
            request_json=payload,
            session_id=session_id,
            creator_user_id=creator_user_id,
            priority=priority,
            status=JobStatus.QUEUED,
        )
        try:
            destination_dir = _workdir_for_job(context, job_id, "input")
            staged_image_ids: list[str] = list(parsed_artifact_ids)
            idx = len(staged_image_ids)
            for url in parsed_urls:
                staged = await staging.stage_image_url(
                    job_id=job_id, image_url=url, destination_dir=destination_dir,
                    index=idx, request_metadata={"source": "url"},
                )
                staged_image_ids.append(staged.artifact.artifact_id)
                idx += 1
            for img in upload_images:
                data = await img.read()
                await img.close()
                staged = staging.stage_image_bytes(
                    job_id=job_id, data=data,
                    filename=img.filename or f"image_{idx + 1}.png",
                    destination_dir=destination_dir, index=idx,
                    upload_content_type=img.content_type,
                    request_metadata={"source": "upload"},
                )
                staged_image_ids.append(staged.artifact.artifact_id)
                idx += 1

            # Vocal-clone inputs were removed from this endpoint; the job never
            # carries a source vocal sample.
            vocal_artifact_id: Optional[str] = None
        except HTTPException as exc:
            # The job row exists, so a staging failure must leave a structured
            # error on it (never error: null) — clients poll this job.
            repo.update_job_status(
                job_id, status=JobStatus.FAILED, current_stage="artifact-staging",
                error_json={
                    "error_code": INPUT_INVALID.code,
                    "message": str(exc.detail),
                    "retryable": False,
                },
                finished=True,
            )
            raise
        except Exception as exc:
            error_json = public_error_payload(exc)
            repo.update_job_status(
                job_id, status=JobStatus.FAILED, current_stage="artifact-staging",
                error_json=error_json, finished=True,
            )
            raise HTTPException(
                status_code=400, detail=error_json["message"]
            ) from exc

        queue_name = namespaced_queue_name("multi-image-pipeline", settings=context.settings)
        task_type = "multi_image_monolith"
        task_id = new_id("task")
        task_queue.enqueue(
            TaskEnvelope(
                task_id=task_id,
                job_id=job_id,
                queue_name=queue_name,
                task_type=task_type,
                payload_json={
                    "image_artifact_ids": staged_image_ids,
                    "vocal_sample_artifact_id": vocal_artifact_id,
                },
                priority=priority,
                max_attempts=max_attempts,
                idempotency_key=f"{job_id}:multi_image_monolith:v1",
            )
        )
        repo.add_event(
            job_id=job_id,
            event_type="job.created",
            stage_name="api",
            message="Async v2 multi-image music job created.",
            payload_json={
                "task_id": task_id,
                "queue_name": queue_name,
                "task_type": task_type,
                "image_count": len(staged_image_ids),
                "has_vocal_sample": vocal_artifact_id is not None,
            },
        )
        return CreateVideoMusicJobResponse(
            job_id=job_id,
            task_id=task_id,
            status=JobStatus.QUEUED,
            status_url=f"/api/v2/jobs/{job_id}",
        )

    async def _create_video_music_job_from_request(
        request: CreateVideoMusicJobRequest,
        *,
        video_upload: UploadFile | None = None,
        vocal_sample_upload: UploadFile | None = None,
        vocal_sample_url: Optional[str] = None,
        principal: Optional[Principal] = None,
        enforced: bool = False,
    ) -> CreateVideoMusicJobResponse:
        requested_video_url = (request.video_url or "").strip() or None
        use_video_upload = video_upload is not None and requested_video_url is None
        payload = _validate_request(
            request,
            has_video_upload=use_video_upload,
        )
        if requested_video_url and video_upload is not None:
            await video_upload.close()

        requested_source_artifact_id = (
            request.source_video_artifact_id or ""
        ).strip() or None
        source_artifact = None
        source_filename = None
        if requested_source_artifact_id:
            source_artifact = repo.get_artifact(requested_source_artifact_id)
            if source_artifact is None or source_artifact.artifact_type != "source_video":
                raise HTTPException(
                    status_code=404,
                    detail=f"Source video artifact not found: {requested_source_artifact_id}",
                )
            # Input guardrails: staged artifacts carry size/duration metadata,
            # so violations reject synchronously with their specific error code.
            try:
                validate_source_video_metadata(source_artifact.metadata_json)
            except EdennError as exc:
                raise edenn_error_to_http_exception(exc) from exc
        elif requested_video_url:
            source_filename = _source_filename_from_url(requested_video_url)

        split_storage_enabled = (
            context.storage is not None
            and getattr(context.storage, "enabled", False)
        )
        # Routing policy: video music jobs are never handed to the monolith
        # worker — the split pipeline is the only supported production path.
        # The requested mode is recorded but does not steer routing; the sole
        # monolith fallback is a storage-less environment (local
        # single-process runs), where the split pipeline physically cannot
        # move artifacts between workers.
        requested_mode = request.mode
        if split_storage_enabled:
            effective_mode = "split"
        else:
            if requested_mode == "split":
                raise HTTPException(
                    status_code=400,
                    detail="Split mode requires storage so file artifacts are available across worker containers.",
                )
            effective_mode = "monolith"
        if (
            effective_mode == "split"
            and source_artifact is not None
            and not _artifact_has_durable_video_url(source_artifact)
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Split mode requires a source video artifact with a durable URL. "
                    "Stage the asset with storage enabled or provide a video_url source."
                ),
            )
        request.mode = effective_mode
        payload["mode"] = effective_mode

        job_id = new_id("job")
        try:
            normalized_vocal_id, prepared_vocal_sample_path = (
                await _prepare_vocal_clone_request_input(
                    vocal_id=request.vocal_id,
                    vocal_sample=vocal_sample_upload,
                    vocal_sample_url=vocal_sample_url,
                    vocal_source_dir=_workdir_for_job(context, job_id, "input", "vocal"),
                    requested_modelspec=str(payload["modelspec"]),
                )
            )
            if normalized_vocal_id:
                payload["vocal_id"] = normalized_vocal_id
            else:
                payload.pop("vocal_id", None)
            if prepared_vocal_sample_path is not None:
                payload["vocal_sample_path"] = str(prepared_vocal_sample_path)
        except HTTPException:
            raise
        except EdennError as exc:
            raise edenn_error_to_http_exception(exc) from exc

        asset_ids = RecommendationAssetIds.create(job_id=job_id)
        payload.setdefault("video_id", asset_ids.video_id)
        payload.setdefault("creative_id", asset_ids.creative_id)
        payload.setdefault("primary_music_id", asset_ids.primary_music_id)
        payload.setdefault("secondary_music_id", asset_ids.secondary_music_id)
        payload.setdefault("selected_music_id", asset_ids.selected_music_id)
        payload.setdefault("alignment_id", asset_ids.alignment_id)
        payload.setdefault("job_received_timestamp", int(time.time()))

        source_artifact_id = f"{job_id}:source_video:input"
        if requested_source_artifact_id:
            payload["requested_source_video_artifact_id"] = requested_source_artifact_id
        if requested_video_url:
            payload["requested_source_video_url"] = requested_video_url
        payload["source_video_artifact_id"] = source_artifact_id

        if principal is not None:
            payload["auth_user_id"] = principal.user_id
            payload["auth_key_prefix"] = principal.key_prefix
        effective_creator = request.creator_user_id
        if principal is not None and enforced:
            effective_creator = principal.user_id
        payload["creator_user_id"] = effective_creator

        repo.create_job(
            job_id=job_id,
            job_type="video_music",
            request_json=payload,
            session_id=request.session_id,
            creator_user_id=effective_creator,
            priority=request.priority,
            status=JobStatus.QUEUED,
        )
        if use_video_upload and video_upload is not None:
            try:
                upload_filename = video_upload.filename or "source_video.mp4"
                upload_content_type = video_upload.content_type
                data = await video_upload.read()
                await video_upload.close()
                # Size guardrail BEFORE staging: an oversize upload must not be
                # probed, hashed, or pushed to blob storage first.
                validate_source_video_size(len(data))
                staged_source = staging.stage_video_bytes(
                    job_id=job_id,
                    data=data,
                    filename=upload_filename,
                    destination_dir=_workdir_for_job(context, job_id, "input"),
                    artifact_id=source_artifact_id,
                    upload_content_type=upload_content_type,
                    request_metadata={
                        "creator_user_id": request.creator_user_id,
                        "session_id": request.session_id,
                        "user_id": request.user_id,
                        "source": "upload",
                    },
                    # The strict input guardrails below replace the legacy
                    # staging-time duration backstop; running both would report
                    # the wrong limit for videos over the legacy maximum.
                    validate_duration=False,
                )
                validate_source_video_metadata(
                    staged_source.artifact.metadata_json,
                    source_path=staged_source.local_path,
                )
                linked_source = staged_source.artifact
            except EdennError as exc:
                error_json = public_error_payload(exc)
                repo.update_job_status(
                    job_id,
                    status=JobStatus.FAILED,
                    current_stage="artifact-staging",
                    error_json=error_json,
                    finished=True,
                )
                repo.add_event(
                    job_id=job_id,
                    event_type="job.failed",
                    stage_name="artifact-staging",
                    message="Source video staging failed.",
                    payload_json={"error": error_json},
                )
                raise edenn_error_to_http_exception(exc) from exc
            except Exception as exc:
                error_json = public_error_payload(exc)
                repo.update_job_status(
                    job_id,
                    status=JobStatus.FAILED,
                    current_stage="artifact-staging",
                    error_json=error_json,
                    finished=True,
                )
                repo.add_event(
                    job_id=job_id,
                    event_type="job.failed",
                    stage_name="artifact-staging",
                    message="Source video staging failed.",
                    payload_json={"error": error_json},
                )
                raise HTTPException(
                    status_code=400, detail=error_json["message"]
                ) from exc
        elif source_artifact is not None:
            linked_source = _link_source_artifact_to_job(
                repository=repo,
                job_id=job_id,
                source_artifact=source_artifact,
            )
        else:
            linked_source = _record_url_source_artifact(
                repository=repo,
                job_id=job_id,
                artifact_id=source_artifact_id,
                video_url=requested_video_url or "",
                source_filename=source_filename or "source_video.mp4",
                request_metadata={
                    "creator_user_id": request.creator_user_id,
                    "session_id": request.session_id,
                    "source": "url",
                },
            )
        if request.mode == "split":
            queue_name = namespaced_queue_name("video-preprocess", settings=context.settings)
            task_type = "video_preprocess"
            task_id = f"{job_id}:video-preprocess"
            idempotency_key = f"{job_id}:video-preprocess:v1"
        else:
            queue_name = namespaced_queue_name("video-music-pipeline", settings=context.settings)
            task_type = "video_music_monolith"
            task_id = new_id("task")
            idempotency_key = f"{job_id}:video_music_monolith:v1"

        task_queue.enqueue(
            TaskEnvelope(
                task_id=task_id,
                job_id=job_id,
                queue_name=queue_name,
                task_type=task_type,
                payload_json={
                    "source_video_artifact_id": linked_source.artifact_id,
                    "mode": request.mode,
                },
                priority=request.priority,
                max_attempts=request.max_attempts,
                idempotency_key=idempotency_key,
            )
        )
        repo.add_event(
            job_id=job_id,
            event_type="job.created",
            stage_name="api",
            message="Async v2 video music job created.",
            payload_json={
                "task_id": task_id,
                "queue_name": queue_name,
                "task_type": task_type,
                "mode": effective_mode,
                "requested_mode": requested_mode,
                "source_video_artifact_id": linked_source.artifact_id,
                "requested_source_video_artifact_id": requested_source_artifact_id,
                "requested_source_video_url": requested_video_url,
            },
        )
        return CreateVideoMusicJobResponse(
            job_id=job_id,
            task_id=task_id,
            status=JobStatus.QUEUED,
            status_url=f"/api/v2/jobs/{job_id}",
        )

    @router.post("/jobs/video-music", response_model=CreateVideoMusicJobResponse)
    async def create_video_music_job(
        http_request: Request,
        video: UploadFile | None = File(
            None, description="Video file (mp4, mov, etc.)."),
        video_url: Optional[str] = Form(None),
        preserve_original_audio: bool = Form(False),
        # Compression ON by default (parity with the JSON model above).
        compression_flag: bool = Form(True),
        compression_max_height: int = Form(1280, ge=1, le=4320),
        music_volume: Optional[float] = Form(None),
        # Watermark ON by default (parity with multi-image); opt out with false.
        water_mark: bool = Form(True),
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
        callback_url: Optional[str] = Form(None),
        mode: Optional[Literal["monolith", "split"]] = Form(None),
        source_video_artifact_id: Optional[str] = Form(None),
        creator_user_id: Optional[str] = Form(None),
        session_id: Optional[str] = Form(None),
        priority: int = Form(0, ge=0, le=1000),
        # Default no-retry: the paid music provider must not be double-billed.
        max_attempts: int = Form(1, ge=1, le=10),
    ) -> CreateVideoMusicJobResponse:
        content_type = http_request.headers.get("content-type", "").lower()
        if "application/json" in content_type:
            try:
                body = await http_request.json()
            except Exception as exc:
                raise HTTPException(
                    status_code=400, detail="Request body is not valid JSON."
                ) from exc
            try:
                request_model = CreateVideoMusicJobRequest.model_validate(body)
            except ValidationError as exc:
                # A malformed field is the client's 4xx, not an opaque 500 —
                # and the pydantic error blob (input echoes, ctx, doc URLs)
                # never reaches the client, only field paths and reasons.
                problems = "; ".join(
                    f"{'.'.join(str(part) for part in error.get('loc', ()))}: "
                    f"{error.get('msg')}"
                    for error in exc.errors()
                )
                raise HTTPException(
                    status_code=422, detail=f"Invalid request: {problems}"
                ) from exc
            return await _create_video_music_job_from_request(
                request_model,
                principal=get_principal(http_request),
                enforced=getattr(http_request.state, "auth_enforced", False),
            )

        effective_creator_user_id = creator_user_id or user_id
        requested_volume = (
            music_volume
            if music_volume is not None
            else getattr(context.settings, "music_volume", 1.0)
        )
        form_request = CreateVideoMusicJobRequest(
            source_video_artifact_id=source_video_artifact_id,
            video_url=video_url,
            modelspec=modelspec,
            user_prompt=user_prompt,
            preserve_original_audio=preserve_original_audio,
            music_volume=requested_volume,
            water_mark=water_mark,
            compression_flag=compression_flag,
            compression_max_height=compression_max_height,
            mode=mode,
            verbose_instruction=verbose_instruction,
            music_style_prompt=music_style_prompt,
            lyrics_prompt=lyrics_prompt,
            audio_output_format=audio_output_format,
            vocal_id=vocal_id,
            user_id=user_id,
            callback_url=callback_url,
            creator_user_id=effective_creator_user_id,
            session_id=session_id,
            priority=priority,
            max_attempts=max_attempts,
        )
        return await _create_video_music_job_from_request(
            form_request,
            video_upload=video,
            vocal_sample_upload=vocal_sample,
            vocal_sample_url=vocal_sample_url,
            principal=get_principal(http_request),
            enforced=getattr(http_request.state, "auth_enforced", False),
        )

    @router.get("/jobs/{job_id}", response_model=AsyncV2JobStatusResponse)
    async def get_job_status(job_id: str) -> AsyncV2JobStatusResponse:
        try:
            return AsyncV2JobStatusResponse.model_validate(
                _status_view_with_fresh_urls(
                    repo.build_status_view(job_id),
                    storage=context.storage,
                    settings=context.settings,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}") from exc

    @router.get("/jobs/{job_id}/events", response_model=AsyncV2JobEventsResponse)
    async def get_job_events(job_id: str, limit: Optional[int] = None) -> AsyncV2JobEventsResponse:
        if repo.get_job(job_id) is None:
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
        safe_limit = None if limit is None else max(1, min(int(limit), 500))
        events = repo.list_events(job_id, limit=safe_limit)
        return AsyncV2JobEventsResponse(
            job_id=job_id,
            events=[_event_response(event) for event in events],
        )

    @router.post("/jobs/{job_id}/cancel", response_model=AsyncV2JobStatusResponse)
    async def cancel_job(job_id: str) -> AsyncV2JobStatusResponse:
        """Cancel a job that has not started yet.

        Generation is billed the moment work starts, so cancel succeeds only
        while the job is still queued and unleased. Once any task has started
        the job runs to completion and cancel returns 409.
        """
        job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
        if job.status not in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}:
            canceled_tasks, allowed = task_queue.cancel_job_tasks_if_unstarted(
                job_id=job_id
            )
            if not allowed:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Job has already started. Work is billed when it starts "
                        "and will run to completion; it can no longer be canceled."
                    ),
                )
            _, applied = repo.update_job_status_checked(
                job_id,
                status=JobStatus.CANCELED,
                current_stage=job.current_stage,
                progress_percent=job.progress_percent,
                finished=True,
            )
            if applied:
                repo.add_event(
                    job_id=job_id,
                    event_type="job.canceled",
                    stage_name="api",
                    message="Async v2 job canceled before start.",
                    payload_json={"canceled_tasks": canceled_tasks},
                )
        return AsyncV2JobStatusResponse.model_validate(
            _status_view_with_fresh_urls(
                repo.build_status_view(job_id),
                storage=context.storage,
                settings=context.settings,
            )
        )

    return router


__all__ = [
    "AsyncV2JobEventsResponse",
    "AsyncV2JobStatusResponse",
    "AsyncV2VideoAssetResponse",
    "CreateVideoMusicJobRequest",
    "CreateVideoMusicJobResponse",
    "create_async_pipeline_v2_router",
]
