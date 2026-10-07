from __future__ import annotations

import asyncio
import math
import os
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import sentry_sdk
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from EdennCode.Deployment.api_common import (
    ApiContext,
    JobStatus,
    LyricsTimestampModel,
    cleanup_temp_dir,
    edenn_error_to_http_exception,
    guess_audio_content_type,
    guess_image_content_type,
    prepare_audio_for_provider_b_vocal_clone,
    probe_audio_metrics,
    resolve_optional_media_source_to_disk,
    sanitize_filename,
    service_version,
    write_upload_to_disk,
)
# Reuse the video-music component models so the multi-image response is a
# superset-compatible shape of VideoJobResponse (video is the canonical schema).
# api_video_generation does not import this module, so there is no import cycle.
from EdennCode.Deployment.api_video_generation import (
    AudioMetadataBlock,
    LEGACY_MODEL_MAP,
    ResponseMetadata,
    VideoGenerationCostResponse,
    VideoGeometry,
    VideoJobResponse,
    VideoMetadataBlock,
    _music_prompt_text,
    _video_generation_cost_response,
    ensure_music_title,
    local_file_size_bytes,
)
from EdennCode.Deployment.async_video_job_store import (
    AsyncVideoJobState as _AsyncJobState,
    InMemoryAsyncVideoJobStore,
    build_async_multi_image_job_store_from_env,
)
from EdennCode.Deployment.auth.key_store import Principal
from EdennCode.Deployment.auth.middleware import get_principal, resolve_user_id
from EdennCode.Deployment.multi_image_workflows import MultiImageGenerationResult
from EdennCode.Deployment.response_guardrail import guard_response_model
from EdennCode.Util.MediaUtils import (
    SUPPORTED_TRANSITIONS,
    get_video_dimensions,
    get_video_duration,
    get_video_fps,
    parse_transition_spec,
)
# The preprocess stage silently skips staged files whose extension it does not
# recognize; the billing-floor bump must count only the files that will render.
from EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.PreprocessStage.preprocess_stage import (
    SUPPORTED_EXTENSIONS as _SLIDESHOW_IMAGE_EXTENSIONS,
)
from EdennCode.exceptions import EdennError, EdennImageDurationTooShortError

VALID_MUSIC_MODEL_SPECS = {"edenn_basic", "edenn_enhanced", "edenn_studio"}
# Single-value transition modes accepted by the API: the two pseudo-modes plus
# every concrete effect name. A comma-separated value is treated as an explicit
# per-boundary list instead (each entry validated against SUPPORTED_TRANSITIONS).
VALID_TRANSITION_MODES = {"none", "random"} | set(SUPPORTED_TRANSITIONS)
_TRANSITION_DURATION_MIN_S = 0.05
_TRANSITION_DURATION_MAX_S = 2.0


def parse_and_validate_transition(
    transition: str,
    transition_duration_s: float,
) -> tuple[str, Optional[List[str]]]:
    """Parse and validate a transition request, raising ``HTTPException`` on bad input.

    Returns ``(transition_mode, transitions)`` exactly as consumed by
    ``MultiImageGenerationOrchestrator.run()`` — a single-mode string plus an
    optional explicit per-boundary list. Shared by the v1 in-process router and
    the v2 durable submit endpoint so both surfaces enforce the identical contract.
    """
    requested_transition, requested_transitions = parse_transition_spec(
        transition if transition else "random"
    )
    if requested_transitions is not None:
        # Explicit per-boundary list: validate every effect name.
        invalid = sorted(
            {name for name in requested_transitions if name not in SUPPORTED_TRANSITIONS}
        )
        if invalid:
            allowed = ", ".join(sorted(SUPPORTED_TRANSITIONS))
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid transition name(s): {', '.join(invalid)}. "
                    f"Allowed effect names: {allowed}."
                ),
            )
    elif requested_transition not in VALID_TRANSITION_MODES:
        allowed = ", ".join(["none", "random", *sorted(SUPPORTED_TRANSITIONS)])
        raise HTTPException(
            status_code=400,
            detail=f"Invalid transition '{transition}'. Allowed values: {allowed}.",
        )
    if not (
        _TRANSITION_DURATION_MIN_S
        <= transition_duration_s
        <= _TRANSITION_DURATION_MAX_S
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"transition length must be between {_TRANSITION_DURATION_MIN_S} "
                f"and {_TRANSITION_DURATION_MAX_S} seconds."
            ),
        )
    return requested_transition, requested_transitions


# Multi-image slideshow limits, shared by the v1 in-process router and the v2
# durable submit endpoint so both surfaces enforce one identical contract.
MULTI_IMAGE_MIN_IMAGES = 3
MULTI_IMAGE_MAX_IMAGES = 10
# Cap on the total slideshow length (image_count * per_image_duration). Beyond
# this the job is rejected at submit rather than rendering an over-long video.
MULTI_IMAGE_MAX_TOTAL_DURATION_S = 150.0
# Per-image display-time bounds. Below the 3s floor the request is rejected with
# error code 10008. The Form fields keep the looser gt=0/le=60 bounds on purpose
# so a sub-3s value reaches the shared validators and surfaces as the coded 400,
# not a FastAPI 422.
MULTI_IMAGE_PER_IMAGE_MIN_S = 3.0
MULTI_IMAGE_PER_IMAGE_MAX_S = 60.0
# Billing floor on the slideshow total: multi-image is priced per delivered
# second, so a plan totalling less than this is lengthened (never rejected) by
# adding 1s to one image at a time, round-robin from the first image, until the
# total reaches the floor.
MULTI_IMAGE_MIN_TOTAL_DURATION_S = 15.0
# Tolerance for float accumulation when comparing duration sums (e.g. a
# total_duration_s split into thirds must not trigger a spurious extra +1s).
_DURATION_EPS_S = 1e-9
# Multi-image is priced by the delivered video's length: this many USD per second
# of output video (cost_metadata.total_cost = video_seconds * this).
MULTI_IMAGE_COST_PER_SECOND_USD = 0.05


def _image_duration_too_short_error(received_s: float) -> HTTPException:
    """Coded 400 (error_code 10008) for a per-image display time below the floor."""
    message = (
        f"Each image must be displayed for at least "
        f"{MULTI_IMAGE_PER_IMAGE_MIN_S:g} seconds (received {received_s:g}s)."
    )
    return edenn_error_to_http_exception(
        EdennImageDurationTooShortError(message, public_message=message)
    )


def bump_durations_to_billing_floor(durations: Sequence[float]) -> List[float]:
    """Return per-image durations whose total meets the 15s billing floor.

    A slideshow plan shorter than ``MULTI_IMAGE_MIN_TOTAL_DURATION_S`` is
    lengthened rather than rejected: 1 second is added to one image at a time,
    round-robin starting from the first image, until the total reaches the
    floor. Plans already at or above the floor come back with the same values.
    """
    out = [float(v) for v in durations]
    if not out:
        return out
    total = sum(out)
    step = 0
    while total < MULTI_IMAGE_MIN_TOTAL_DURATION_S - _DURATION_EPS_S:
        out[step % len(out)] += 1.0
        total += 1.0
        step += 1
    return out


def resolve_uniform_image_timing(
    image_count: int, per_image_duration: float
) -> tuple[float, Optional[List[float]]]:
    """Apply the billing floor to a uniform-duration plan.

    Returns ``(effective_per_image_duration, explicit_durations)``. When the
    plan already meets the floor — or the round-robin bump lands equally on
    every image — timing stays uniform (``explicit_durations`` is None) and the
    beat-aligned pipeline is preserved. Otherwise the bumped per-image list is
    returned; the caller passes it downstream as explicit durations, which
    disables beat alignment exactly like caller-supplied fixed timing.
    """
    if image_count <= 0:
        return per_image_duration, None
    bumped = bump_durations_to_billing_floor([per_image_duration] * image_count)
    if max(bumped) - min(bumped) <= _DURATION_EPS_S:
        return bumped[0], None
    return per_image_duration, bumped


def _multi_image_video_seconds(
    geometry: "VideoGeometry", audio_duration_s: Optional[float]
) -> float:
    """Length of the delivered creative (slideshow video), in seconds.

    Prefers the probed output-video duration; falls back to the muxed audio length
    (which equals the video length) if geometry carries no duration.
    """
    duration_s: Optional[float] = None
    if geometry is not None:
        duration_s = (
            geometry.duration if geometry.duration is not None else geometry.duration_s
        )
    if duration_s is None:
        duration_s = audio_duration_s
    return max(0.0, float(duration_s or 0.0))


def _multi_image_total_cost(
    geometry: "VideoGeometry", audio_duration_s: Optional[float]
) -> float:
    """Multi-image charge = delivered video seconds * MULTI_IMAGE_COST_PER_SECOND_USD."""
    return round(
        _multi_image_video_seconds(geometry, audio_duration_s)
        * MULTI_IMAGE_COST_PER_SECOND_USD,
        6,
    )


def validate_transition_length(
    *,
    transition_mode: str,
    transitions: Optional[List[str]],
    transition_length: Optional[float],
) -> None:
    """Reject a transition length that cannot apply to the requested transition.

    ``transition_length`` only has meaning when the caller has chosen a specific
    effect — a single named transition or an explicit per-boundary list. It is
    NOT accepted for ``random`` / ``none`` (the effect, and thus a sensible blend
    length, is not caller-controlled there). ``None`` means the caller did not
    send a length, which is always fine. Raises ``HTTPException``.
    """
    if transition_length is None:
        return
    explicit_effect = transitions is not None or (
        (transition_mode or "").strip().lower() in SUPPORTED_TRANSITIONS
    )
    if not explicit_effect:
        raise HTTPException(
            status_code=400,
            detail=(
                "transition_length is only valid with a specific named transition "
                "or an explicit transition list; it is not accepted for 'random' "
                "or 'none'."
            ),
        )


def resolve_fixed_image_durations(
    *,
    image_count: int,
    per_image_durations: Optional[List[float]],
    total_duration_s: Optional[float],
    image_order: str,
) -> Optional[List[float]]:
    """Validate and resolve explicit fixed-order timing to a per-image list.

    Returns ``None`` when the caller requested no explicit timing (the workflow
    then keeps its current uniform + beat-aligned behavior). When either input
    is present the images are shown for exactly these seconds and beat alignment
    is turned off downstream, so the timing is only meaningful when the image
    order is authoritative — both inputs require ``image_order='fixed'`` and are
    mutually exclusive. Every resolved per-image value must be at least
    ``MULTI_IMAGE_PER_IMAGE_MIN_S`` (coded 400, error 10008 below it), and a
    total under ``MULTI_IMAGE_MIN_TOTAL_DURATION_S`` is bumped up to the billing
    floor (see ``bump_durations_to_billing_floor``), never rejected. Raises
    ``HTTPException(400)`` on any violation.
    """
    if per_image_durations is None and total_duration_s is None:
        return None
    if image_order != "fixed":
        raise HTTPException(
            status_code=400,
            detail=(
                "per_image_durations and total_duration_s require image_order=fixed."
            ),
        )
    if per_image_durations is not None and total_duration_s is not None:
        raise HTTPException(
            status_code=400,
            detail="Provide either per_image_durations or total_duration_s, not both.",
        )

    if per_image_durations is not None:
        durations = list(per_image_durations)
        if len(durations) != image_count:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"per_image_durations must have exactly {image_count} entries "
                    f"(one per image); received {len(durations)}."
                ),
            )
    else:
        total_s = float(total_duration_s if total_duration_s is not None else 0.0)
        # NaN fails every comparison, so check finiteness explicitly.
        if not math.isfinite(total_s) or total_s <= 0:
            raise HTTPException(
                status_code=400,
                detail="total_duration_s must be a finite number greater than 0.",
            )
        if image_count < MULTI_IMAGE_MIN_IMAGES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"At least {MULTI_IMAGE_MIN_IMAGES} images are required "
                    f"(received {image_count})."
                ),
            )
        per_each = total_s / image_count
        durations = [per_each] * image_count

    for value in durations:
        # One-sided comparisons are all False for NaN, so reject non-finite
        # values explicitly — NaN must not slip past the range checks into a
        # billed job (the pre-split chained comparison used to catch it).
        if not math.isfinite(value):
            raise HTTPException(
                status_code=400,
                detail="Image durations must be finite numbers.",
            )
        if value < MULTI_IMAGE_PER_IMAGE_MIN_S:
            raise _image_duration_too_short_error(value)
        if value > MULTI_IMAGE_PER_IMAGE_MAX_S:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Each image duration must be <= "
                    f"{MULTI_IMAGE_PER_IMAGE_MAX_S:g}s (received {value:g}s)."
                ),
            )
    durations = bump_durations_to_billing_floor(durations)
    total = sum(durations)
    if total > MULTI_IMAGE_MAX_TOTAL_DURATION_S:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Total duration {total:.1f}s exceeds the "
                f"{MULTI_IMAGE_MAX_TOTAL_DURATION_S:.0f}s maximum."
            ),
        )
    return durations


def validate_multi_image_request(
    image_count: int,
    per_image_duration: float,
    *,
    enforce_uniform_timing: bool = True,
) -> None:
    """Enforce the multi-image count and uniform-duration limits.

    Raises ``HTTPException(400)`` when there are fewer than
    ``MULTI_IMAGE_MIN_IMAGES`` or more than ``MULTI_IMAGE_MAX_IMAGES`` images,
    when ``per_image_duration`` is below ``MULTI_IMAGE_PER_IMAGE_MIN_S`` (coded
    400, error 10008), or when the cumulative duration
    (``image_count * per_image_duration``) exceeds
    ``MULTI_IMAGE_MAX_TOTAL_DURATION_S`` seconds.

    The count bounds always apply. Pass ``enforce_uniform_timing=False`` when
    the caller supplied explicit fixed-order timing: the uniform
    ``per_image_duration`` is ignored downstream in that case, and the explicit
    values are already bounded by ``resolve_fixed_image_durations`` — so gating
    on the uniform value would reject an otherwise-valid request.
    """
    if image_count < MULTI_IMAGE_MIN_IMAGES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"At least {MULTI_IMAGE_MIN_IMAGES} images are required "
                f"(received {image_count})."
            ),
        )
    if image_count > MULTI_IMAGE_MAX_IMAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Too many images ({image_count}); max {MULTI_IMAGE_MAX_IMAGES}.",
        )
    if not enforce_uniform_timing:
        return
    # NaN fails every comparison below, so reject non-finite values explicitly
    # (the endpoint Form bounds already 422 NaN; this covers direct callers).
    if not math.isfinite(per_image_duration):
        raise HTTPException(
            status_code=400,
            detail="per_image_duration must be a finite number.",
        )
    if per_image_duration < MULTI_IMAGE_PER_IMAGE_MIN_S:
        raise _image_duration_too_short_error(per_image_duration)
    total_duration_s = image_count * per_image_duration
    if total_duration_s > MULTI_IMAGE_MAX_TOTAL_DURATION_S:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Total duration {total_duration_s:.1f}s exceeds the "
                f"{MULTI_IMAGE_MAX_TOTAL_DURATION_S:.0f}s maximum "
                f"({image_count} images x {per_image_duration:g}s each). "
                f"Reduce the image count or per_image_duration."
            ),
        )


_ASYNC_JOB_TTL_SECONDS_DEFAULT = 6 * 60 * 60

# Module-level async job store + concurrency state. Mirrors the async video-music
# path: a context-provided store wins, otherwise a process-wide store is built
# lazily from the environment. A test override hook allows deterministic stores.
_memory_async_multi_image_job_store = InMemoryAsyncVideoJobStore()
_async_multi_image_job_store: Any | None = None
_async_multi_image_job_store_override: Any | None = None
_async_multi_image_job_semaphore: asyncio.Semaphore | None = None
_async_multi_image_job_semaphore_limit: int | None = None


# ----------------------------------------------------------------------------
# Job response.
#
# Image-music reuses the video-music response blocks verbatim, so the two
# responses are structurally identical. Image-specific values live inside the
# shared blocks: `compression_applied` on response_metadata, and the slideshow
# `video_title`/`video_description` inside `video_metadata.video_summary`.
# ----------------------------------------------------------------------------


class MultiImageJobResponse(VideoJobResponse):
    """Image-music job response — identical shape to the video-music response."""


class AsyncMultiImageJobAcceptedResponse(BaseModel):
    """Returned immediately by POST /api/v1/jobs/async_multi-image."""

    job_id: str
    status: str = Field(default=JobStatus.PENDING)
    version: str = Field(default_factory=service_version)


class AsyncMultiImageJobStatusResponse(BaseModel):
    """Returned by GET /api/v1/jobs/async_multi-image/{job_id}."""

    job_id: str
    status: str
    version: str = Field(default_factory=service_version)
    result: Optional[MultiImageJobResponse] = None
    error: Optional[Dict[str, Any]] = None
    created_at: int


@dataclass(frozen=True)
class _PreparedMultiImageJob:
    job_dir: Path
    source_dir: Path
    output_video_path: Path
    requested_modelspec: str
    requested_volume: float
    normalized_vocal_id: Optional[str]
    prepared_vocal_sample_path: Optional[Path]
    requested_transition: str = "random"
    requested_transitions: Optional[List[str]] = None
    requested_transition_duration_s: float = 0.4
    # Timing after the 15s billing floor. When the round-robin bump goes
    # non-uniform, the explicit list is set and takes precedence downstream
    # (disabling beat alignment); otherwise the uniform value carries any bump.
    effective_per_image_duration: float = 3.0
    effective_per_image_durations: Optional[List[float]] = None


# ---------------------------------------------------------------------------
# Async job store accessors (mirror the async video-music helpers).
# ---------------------------------------------------------------------------


def _async_job_ttl_seconds() -> int:
    raw_value = os.getenv(
        "ASYNC_MULTI_IMAGE_JOB_TTL_SECONDS",
        str(_ASYNC_JOB_TTL_SECONDS_DEFAULT),
    )
    try:
        return max(0, int(raw_value))
    except ValueError:
        return _ASYNC_JOB_TTL_SECONDS_DEFAULT


def _get_async_job_store(context: ApiContext | None = None) -> Any:
    if context is not None and getattr(context, "multi_image_async_job_store", None) is not None:
        return context.multi_image_async_job_store
    if _async_multi_image_job_store_override is not None:
        return _async_multi_image_job_store_override

    global _async_multi_image_job_store
    if _async_multi_image_job_store is None:
        _async_multi_image_job_store = build_async_multi_image_job_store_from_env(
            memory_store=_memory_async_multi_image_job_store,
        )
    return _async_multi_image_job_store


def _set_async_job_store_for_testing(store: Any | None) -> None:
    global _async_multi_image_job_store_override
    _async_multi_image_job_store_override = store


def _cleanup_expired_async_jobs(
    *,
    now: Optional[int] = None,
    context: ApiContext | None = None,
) -> None:
    ttl_seconds = _async_job_ttl_seconds()
    if ttl_seconds <= 0:
        return
    _get_async_job_store(context).cleanup_expired(ttl_seconds=ttl_seconds, now=now)


def _register_async_job(job_id: str, *, context: ApiContext | None = None) -> _AsyncJobState:
    _cleanup_expired_async_jobs(context=context)
    return _get_async_job_store(context).register(job_id)


def _complete_async_job(
    job_id: str,
    result: "MultiImageJobResponse",
    *,
    context: ApiContext | None = None,
) -> _AsyncJobState:
    store = _get_async_job_store(context)
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
    return _get_async_job_store(context).fail(job_id, error)


def _get_async_job_state(
    job_id: str,
    *,
    context: ApiContext | None = None,
) -> Optional[_AsyncJobState]:
    _cleanup_expired_async_jobs(context=context)
    return _get_async_job_store(context).get(job_id)


def _state_result_model(state: _AsyncJobState) -> Optional["MultiImageJobResponse"]:
    result = state.result
    if result is None:
        return None
    if isinstance(result, MultiImageJobResponse):
        return result
    return MultiImageJobResponse.model_validate(result)


def _state_result_json(state: _AsyncJobState) -> Optional[dict[str, Any]]:
    result = state.result
    if result is None:
        return None
    if isinstance(result, MultiImageJobResponse):
        return result.model_dump(mode="json")
    if isinstance(result, dict):
        return result
    return MultiImageJobResponse.model_validate(result).model_dump(mode="json")


def _async_job_max_in_flight_per_replica() -> int:
    raw_value = os.getenv("ASYNC_MULTI_IMAGE_MAX_IN_FLIGHT_PER_REPLICA", "1")
    try:
        return max(1, int(raw_value))
    except ValueError:
        return 1


def _get_async_job_semaphore() -> asyncio.Semaphore:
    global _async_multi_image_job_semaphore, _async_multi_image_job_semaphore_limit
    limit = _async_job_max_in_flight_per_replica()
    if (
        _async_multi_image_job_semaphore is None
        or _async_multi_image_job_semaphore_limit != limit
    ):
        _async_multi_image_job_semaphore = asyncio.Semaphore(limit)
        _async_multi_image_job_semaphore_limit = limit
    return _async_multi_image_job_semaphore


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
                    "Async multi-image job %s callback returned HTTP %s for %s",
                    job_id,
                    response.status_code,
                    url,
                )
    except Exception as exc:
        logger.warning(
            "Async multi-image job %s callback delivery failed for %s: %s",
            job_id,
            url,
            exc,
            exc_info=True,
        )


def _public_async_error_payload(error: EdennError) -> dict[str, Any]:
    # Reuse the shared error catalog mapping (and 5xx Sentry capture) used by the
    # synchronous path so async failures return identical, sanitized payloads.
    detail = edenn_error_to_http_exception(error).detail
    if isinstance(detail, dict):
        return detail
    return {"message": str(detail)}


# ---------------------------------------------------------------------------
# Shared validation + response building (used by both sync and async paths).
# ---------------------------------------------------------------------------


async def _prepare_multi_image_job(
    *,
    context: ApiContext,
    job_id: str,
    images: List[UploadFile],
    modelspec: str,
    per_image_duration: float,
    music_volume: Optional[float],
    vocal_id: Optional[str],
    vocal_sample: UploadFile | None,
    vocal_sample_url: Optional[str],
    transition: str = "random",
    transition_duration_s: float = 0.4,
) -> _PreparedMultiImageJob:
    """Validate inputs and stage uploads to disk before running the workflow.

    All validation that must surface as a 4xx happens here, synchronously, so the
    async submit endpoint can reject bad requests before accepting the job and the
    file streams are consumed before FastAPI closes them.
    """

    validate_multi_image_request(len(images), per_image_duration)
    if context.multi_image_workflow is None:
        raise HTTPException(
            status_code=500, detail="Multi-image workflow is not configured.")

    job_dir = context.settings.workdir / job_id
    source_dir = job_dir / "source" / "images"
    vocal_source_dir = job_dir / "source" / "vocal"
    output_video_path = job_dir / "output" / "multi_image_story.mp4"

    requested_modelspec_raw = (
        modelspec or "edenn_basic").strip().lower() or "edenn_basic"
    requested_modelspec = LEGACY_MODEL_MAP.get(
        requested_modelspec_raw, requested_modelspec_raw)
    if requested_modelspec not in VALID_MUSIC_MODEL_SPECS:
        allowed = ", ".join(sorted(VALID_MUSIC_MODEL_SPECS))
        raise HTTPException(
            status_code=400,
            # No echo of the rejected value: the same message on every endpoint,
            # and a legacy vendor-named alias is never repeated back to a client.
            detail=f"Invalid modelspec. Allowed values: {allowed}.",
        )

    requested_transition, requested_transitions = parse_and_validate_transition(
        transition, transition_duration_s
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
        raise HTTPException(
            status_code=400,
            detail="Provide either vocal_id or vocal_sample/vocal_sample_url, not both.",
        )
    if requested_modelspec != "edenn_enhanced" and (
        normalized_vocal_id or vocal_sample_path is not None
    ):
        raise HTTPException(
            status_code=400,
            detail="Vocal clone inputs are only supported for modelspec=edenn_enhanced.",
        )
    prepared_vocal_sample_path = (
        prepare_audio_for_provider_b_vocal_clone(
            source_audio_path=vocal_sample_path,
            destination_dir=vocal_source_dir,
        )
        if vocal_sample_path is not None
        else None
    )

    staged_paths: set[Path] = set()
    for idx, image in enumerate(images):
        filename = sanitize_filename(image.filename or f"image_{idx + 1}.png")
        await write_upload_to_disk(image, source_dir / filename)
        staged_paths.add(source_dir / filename)

    # The billing-floor bump counts only the staged files the preprocess stage
    # will keep (deduped by name, recognized extension). An upload the pipeline
    # silently skips must not inflate the count: a bumped explicit-durations
    # list longer than the rendered slides would fail the job mid-flight.
    rendered_image_count = sum(
        1 for path in staged_paths
        if path.suffix.lower() in _SLIDESHOW_IMAGE_EXTENSIONS
    )
    effective_per_image_duration, effective_per_image_durations = (
        resolve_uniform_image_timing(rendered_image_count, per_image_duration)
    )

    requested_volume = (
        music_volume if music_volume is not None else context.settings.music_volume
    )
    return _PreparedMultiImageJob(
        job_dir=job_dir,
        source_dir=source_dir,
        output_video_path=output_video_path,
        requested_modelspec=requested_modelspec,
        requested_volume=requested_volume,
        normalized_vocal_id=normalized_vocal_id,
        prepared_vocal_sample_path=prepared_vocal_sample_path,
        requested_transition=requested_transition,
        requested_transitions=requested_transitions,
        requested_transition_duration_s=transition_duration_s,
        effective_per_image_duration=effective_per_image_duration,
        effective_per_image_durations=effective_per_image_durations,
    )


def _probe_video_metadata(path: Path) -> VideoGeometry:
    """Best-effort width/height/duration for the assembled slideshow video."""
    width = height = None
    try:
        width, height = get_video_dimensions(path)
    except Exception:
        width = height = None

    duration_s: Optional[float] = None
    try:
        probed = get_video_duration(path)
        duration_s = probed if probed and probed > 0 else None
    except Exception:
        duration_s = None

    fps: Optional[float] = None
    try:
        probed_fps = get_video_fps(path)
        fps = probed_fps if probed_fps and probed_fps > 0 else None
    except Exception:
        fps = None

    return VideoGeometry(
        width=width,
        height=height,
        duration_s=duration_s,
        # Video-music `video_metadata` key alias.
        duration=duration_s,
        fps=fps,
    )


def _neutral_track_filename(source_path: Path) -> str:
    """Client-facing download name for the generated track.

    The raw local filename encodes internal, provider-specific processing steps
    (tail-trim, variant labels, watermark). Expose only a neutral, semantic name
    so nothing about the upstream generator leaks through the response.
    """
    return f"complete_audio{source_path.suffix or '.mp3'}"


# The image pipeline carries lyric timestamps in SECONDS internally (TimestampedWord),
# while the video pipeline emits MILLISECONDS in the response (it applies
# to_ms_wordts in its stage). The response field is shared, so convert here — at the
# response boundary only — to millisecond parity with video. Internal consumers
# (beat alignment) keep the second-valued source untouched.
_LYRIC_S_TO_MS = 1000.0


def _to_lyrics_models(timestamps: Any) -> List[LyricsTimestampModel]:
    return [
        LyricsTimestampModel(
            text=getattr(ts, "text", ""),
            startS=float(getattr(ts, "startS", 0.0)) * _LYRIC_S_TO_MS,
            endS=float(getattr(ts, "endS", 0.0)) * _LYRIC_S_TO_MS,
            i=getattr(ts, "i", None),
        )
        for ts in (timestamps or [])
    ]


def _window_lyrics_models(
    timestamps: Any,
    *,
    start_s: float,
    duration_s: Optional[float],
) -> List[LyricsTimestampModel]:
    """Lyrics aligned to the delivered video: the slice of the complete track that
    was muxed into the slideshow, re-based so 0 is the video's first frame.

    Words in second-valued complete-track time are filtered to the window
    ``[start_s, start_s + duration_s]``, shifted by ``-start_s``, clamped to the
    window edges, and emitted in milliseconds. Without a known window duration
    (probe failure) only the words before the window are dropped — better a loose
    tail than discarding sung lyrics.
    """
    end_s = (start_s + duration_s) if duration_s else None
    out: List[LyricsTimestampModel] = []
    for ts in timestamps or []:
        word_start = float(getattr(ts, "startS", 0.0))
        word_end = float(getattr(ts, "endS", 0.0))
        if word_end <= start_s:
            continue
        if end_s is not None and word_start >= end_s:
            continue
        clamped_end = min(word_end, end_s) if end_s is not None else word_end
        out.append(
            LyricsTimestampModel(
                text=getattr(ts, "text", ""),
                startS=max(0.0, word_start - start_s) * _LYRIC_S_TO_MS,
                endS=max(0.0, clamped_end - start_s) * _LYRIC_S_TO_MS,
                i=getattr(ts, "i", None),
            )
        )
    return out


def _assemble_multi_image_response(
    *,
    job_id: str,
    result: MultiImageGenerationResult,
    audio_url: Optional[str],
    audio_duration_s: Optional[float],
    audio_size_bytes: Optional[int],
    complete_audio_url: Optional[str] = None,
    complete_audio_duration_s: Optional[float] = None,
    complete_audio_size_bytes: Optional[int] = None,
    video_url: Optional[str],
    geometry: VideoGeometry,
    thumbnail_url: Optional[str] = None,
) -> MultiImageJobResponse:
    """Build the MultiImageJobResponse from already-resolved asset refs.

    Shared by the v1 in-process builder and the v2 monolith worker so both emit an
    identical response shape. ``audio_*`` carry the windowed clip that is muxed
    into the slideshow (the part the viewer hears); ``complete_audio_*`` carry the
    full track it was cut from. When no distinct window exists (the whole track
    fits the video) the caller passes the full-track refs for both, so they
    coincide.
    """
    # Safety net: if a caller doesn't distinguish a complete track, mirror the
    # windowed audio so the complete_* fields are never left null.
    complete_audio_url = (
        complete_audio_url if complete_audio_url is not None else audio_url
    )
    if complete_audio_duration_s is None:
        complete_audio_duration_s = audio_duration_s
    if complete_audio_size_bytes is None:
        complete_audio_size_bytes = audio_size_bytes
    # Mirror the video-gen lyric convention: ``lyrics_timestamps`` /
    # ``full_lyrics_timestamps`` carry line/section level, and the ``*word_level*``
    # fields carry word/character level. When a provider only returns one
    # granularity, line level falls back to word level so a field is never emptied.
    word_source = result.lyrics_timestamps or []
    line_source = getattr(result, "line_level_lyrics_timestamps", None) or word_source
    # full_* keep the complete-track timeline; the plain fields are aligned to the
    # delivered video (the window cut from the track), matching the video pipeline.
    word_models = _to_lyrics_models(word_source)
    line_models = _to_lyrics_models(line_source)
    window_start_s = float(getattr(result, "music_start_s", 0.0) or 0.0)
    window_word_models = _window_lyrics_models(
        word_source, start_s=window_start_s, duration_s=audio_duration_s
    )
    window_line_models = _window_lyrics_models(
        line_source, start_s=window_start_s, duration_s=audio_duration_s
    )

    _planning = (
        result.planning_metadata if isinstance(result.planning_metadata, dict) else {}
    )
    # Slideshow titles live in the summary (matching the video shape); music_title
    # is surfaced once, in audio_metadata.
    video_summary = {
        "video_title": result.video_title,
        "video_description": result.video_description,
        "summary": str(_planning.get("storyline_summary", "")),
        "overall_mood": str(_planning.get("overall_mood", "")),
    }
    response = MultiImageJobResponse(
        job_id=job_id,
        modelspec=result.used_music_model_spec,
        response_metadata=ResponseMetadata(
            job_received_timestamp=result.job_received_timestamp,
            job_finished_timestamp=result.job_finished_timestamp,
            compression_applied=result.compression_applied,
        ),
        # Multi-image is billed by delivered video length ($0.05/s); the token/
        # generation breakdown is still populated for internal accounting.
        cost_metadata=_video_generation_cost_response(
            result,
            raw_token_usage=getattr(result, "token_usage", None) or None,
            total_cost_override=_multi_image_total_cost(geometry, audio_duration_s),
            creative_duration=_multi_image_video_seconds(geometry, audio_duration_s),
        ),
        video_metadata=VideoMetadataBlock(
            video_url=video_url,
            thumbnail_url=thumbnail_url,
            video_size_bytes=local_file_size_bytes(result.final_video_path),
            geometry=geometry,
            video_summary=video_summary,
        ),
        audio_metadata=AudioMetadataBlock(
            audio_url=audio_url,
            audio_duration_s=audio_duration_s,
            audio_size_bytes=audio_size_bytes,
            complete_audio_url=complete_audio_url,
            complete_audio_duration_s=complete_audio_duration_s,
            complete_audio_size_bytes=complete_audio_size_bytes,
            music_title=ensure_music_title(result.music_title, result.video_title),
            music_description=_music_prompt_text(result.music_prompt),
            full_lyrics=getattr(result, "full_lyrics", None),
            full_lyrics_timestamps=line_models,
            full_word_level_lyrics_timestamps=word_models,
            lyrics_timestamps=window_line_models,
            word_level_lyrics_timestamps=window_word_models,
        ),
    )
    # Final guardrail: no upstream brand/model token reaches the client, whatever
    # the planning model wrote into the titles, description or music brief.
    return guard_response_model(response)


def _build_multi_image_response(
    *,
    context: ApiContext,
    job_id: str,
    result: MultiImageGenerationResult,
) -> MultiImageJobResponse:
    """Upload generated artifacts (when storage is enabled) and build the response."""

    audio_url = complete_audio_url = None
    video_url = None

    # Only the generated track is surfaced; alternate takes are not uploaded.
    complete_track_path = (
        result.full_track_paths[0] if result.full_track_paths else None
    )
    # Probe local files (before cleanup) so duration/size are carried regardless
    # of whether blob storage is enabled.
    complete_audio_duration_s, complete_audio_size_bytes = probe_audio_metrics(
        complete_track_path
    )

    # audio_* is the windowed slice muxed into the slideshow (the part the viewer
    # hears); complete_audio_* is the full track. They coincide when the whole
    # track fits the video (no distinct window).
    matched_path = result.matched_music_path
    has_window = matched_path is not None
    if has_window:
        audio_duration_s, audio_size_bytes = probe_audio_metrics(matched_path)
    else:
        audio_duration_s, audio_size_bytes = (
            complete_audio_duration_s,
            complete_audio_size_bytes,
        )

    if context.storage.enabled:
        if complete_track_path is not None:
            complete_audio_blob = context.storage.upload_path(
                container=context.settings.audio_container_name,
                path=complete_track_path,
                blob_name=(
                    f"jobs/{job_id}/audio/"
                    f"{_neutral_track_filename(complete_track_path)}"
                ),
                content_type=guess_audio_content_type(complete_track_path),
            )
            if complete_audio_blob:
                complete_audio_url = context.storage.generate_sas_url(
                    container=context.settings.audio_container_name,
                    blob_name=complete_audio_blob,
                )

        # Windowed clip (audio_*). When there is no distinct window, reuse the
        # full-track refs so audio_url == complete_audio_url.
        if has_window:
            audio_blob = context.storage.upload_path(
                container=context.settings.audio_container_name,
                path=matched_path,
                blob_name=(
                    f"jobs/{job_id}/audio/window/"
                    f"{_neutral_track_filename(matched_path)}"
                ),
                content_type=guess_audio_content_type(matched_path),
            )
            if audio_blob:
                audio_url = context.storage.generate_sas_url(
                    container=context.settings.audio_container_name,
                    blob_name=audio_blob,
                )
        else:
            audio_url = complete_audio_url

        video_blob = context.storage.upload_path(
            container=context.settings.output_container,
            path=result.final_video_path,
            blob_name=f"jobs/{job_id}/video/slideshow{result.final_video_path.suffix or '.mp4'}",
            content_type="video/mp4",
        )
        if video_blob:
            video_url = context.storage.generate_sas_url(
                container=context.settings.output_container,
                blob_name=video_blob,
            )

    return _assemble_multi_image_response(
        job_id=job_id,
        result=result,
        audio_url=audio_url,
        audio_duration_s=audio_duration_s,
        audio_size_bytes=audio_size_bytes,
        complete_audio_url=complete_audio_url,
        complete_audio_duration_s=complete_audio_duration_s,
        complete_audio_size_bytes=complete_audio_size_bytes,
        video_url=video_url,
        geometry=_probe_video_metadata(result.final_video_path),
    )


async def _run_multi_image_workflow(
    *,
    context: ApiContext,
    prepared: _PreparedMultiImageJob,
    user_prompt: str,
    align_to_beats: bool,
    per_image_duration: float,
    water_mark: bool,
) -> MultiImageGenerationResult:
    return await context.multi_image_workflow.run(
        folder_path=prepared.source_dir,
        output_path=prepared.output_video_path,
        user_prompt=user_prompt,
        align_to_beats=align_to_beats,
        modelspec=prepared.requested_modelspec,
        per_image_duration=per_image_duration,
        music_volume=prepared.requested_volume,
        vocal_id=prepared.normalized_vocal_id,
        vocal_sample_path=prepared.prepared_vocal_sample_path,
        water_mark=water_mark,
        transition_mode=prepared.requested_transition,
        transitions=prepared.requested_transitions,
        transition_duration_s=prepared.requested_transition_duration_s,
        # Set only when the 15s billing-floor bump went non-uniform; explicit
        # durations take precedence downstream and disable beat alignment.
        per_image_durations=prepared.effective_per_image_durations,
    )


async def _run_async_multi_image_job_inner(
    *,
    context: ApiContext,
    job_id: str,
    prepared: _PreparedMultiImageJob,
    user_prompt: str,
    align_to_beats: bool,
    per_image_duration: float,
    water_mark: bool,
    callback_url: Optional[str],
    user_id: Optional[str] = None,
    principal: Optional[Principal] = None,
) -> None:
    """Background coroutine for POST /api/v1/jobs/async_multi-image.

    Runs the full multi-image pipeline (workflow -> blob uploads) after the HTTP
    response has been returned, writes the terminal status into the job store, and
    optionally POSTs it to ``callback_url``. Owns ``prepared.job_dir`` cleanup.
    """

    _get_async_job_store(context).set_processing(job_id)
    try:
        result = await _run_multi_image_workflow(
            context=context,
            prepared=prepared,
            user_prompt=user_prompt,
            align_to_beats=align_to_beats,
            per_image_duration=per_image_duration,
            water_mark=water_mark,
        )
        response_payload = _build_multi_image_response(
            context=context, job_id=job_id, result=result
        )
        _complete_async_job(job_id, response_payload, context=context)
        if context.usage_recorder is not None:
            context.usage_recorder.record_job(
                job_id=job_id,
                endpoint="/api/v1/jobs/async_multi-image",
                status="completed",
                principal=principal,
                model_spec=response_payload.modelspec,
                token_usage=getattr(result, "token_usage", None),
                cost_metadata=response_payload.cost_metadata,
                video_duration_s=response_payload.video_metadata.geometry.duration,
            )

    except EdennError as exc:
        context.logger.exception(
            "Async multi-image job %s failed with Edenn error: %s",
            job_id,
            exc.to_dict(),
        )
        _fail_async_job(job_id, _public_async_error_payload(exc), context=context)
        if context.usage_recorder is not None:
            context.usage_recorder.record_job(
                job_id=job_id,
                endpoint="/api/v1/jobs/async_multi-image",
                status="failed",
                principal=principal,
            )

    except Exception as exc:
        context.logger.exception("Async multi-image job %s failed: %s", job_id, exc)
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
                endpoint="/api/v1/jobs/async_multi-image",
                status="failed",
                principal=principal,
            )

    finally:
        if callback_url:
            state = _get_async_job_state(job_id, context=context)
            if state is not None:
                await _fire_callback(
                    callback_url, job_id, state, logger=context.logger
                )
        cleanup_temp_dir(prepared.job_dir, logger=context.logger)


async def _run_async_multi_image_job(**kwargs: Any) -> None:
    async with _get_async_job_semaphore():
        await _run_async_multi_image_job_inner(**kwargs)


def create_multi_image_generation_router(context: ApiContext) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/api/v1/jobs/multi-image",
        response_model=MultiImageJobResponse,
        summary="Upload multiple images and receive a slideshow video with generated music.",
    )
    async def generate_multi_image_job(
        *,
        request: Request,
        images: List[UploadFile] = File(...,
                                        description="Between 3 and 10 image files."),  # support urls
        user_prompt: str = Form(""),
        modelspec: str = Form(""),
        align_to_beats: bool = Form(True),
        per_image_duration: float = Form(
            3.0, gt=0, le=60,
            description=(
                "Seconds each image is displayed (3-60; below 3 is rejected "
                "with error code 10008). When image_count x per_image_duration "
                "is under 15s, 1s is added per image round-robin from the "
                "first image until the total reaches 15s."
            ),
        ),
        music_volume: Optional[float] = Form(None),
        water_mark: bool = Form(True),
        vocal_id: Optional[str] = Form(None),
        vocal_sample: UploadFile | None = File(None, description="Optional vocal sample for edenn_enhanced vocal cloning."),
        vocal_sample_url: Optional[str] = Form(None),
        transition: str = Form(
            "random",
            description=(
                "Visual transition(s) between images. 'random' (default) picks a "
                "weighted-random effect per boundary; 'none' keeps hard cuts; a "
                "single effect name (e.g. 'fade') applies it everywhere; a "
                "comma-separated list (e.g. 'fade,dissolve,wipeleft') is applied "
                "per boundary and cycled to fit. See docs/multi-image-transitions.md "
                "for the full list of supported effect names."
            ),
        ),
        transition_duration_s: float = Form(0.4),
        user_id: Optional[str] = Form(
            None,
            description="Optional caller-supplied user identifier (overridden by "
                        "the API key's user in enforce mode).",
        ),
    ) -> MultiImageJobResponse:
        user_id = resolve_user_id(request, user_id)
        job_id = uuid4().hex
        prepared = await _prepare_multi_image_job(
            context=context,
            job_id=job_id,
            images=images,
            modelspec=modelspec,
            per_image_duration=per_image_duration,
            music_volume=music_volume,
            vocal_id=vocal_id,
            vocal_sample=vocal_sample,
            vocal_sample_url=vocal_sample_url,
            transition=transition,
            transition_duration_s=transition_duration_s,
        )

        try:
            result = await _run_multi_image_workflow(
                context=context,
                prepared=prepared,
                user_prompt=user_prompt,
                align_to_beats=align_to_beats,
                per_image_duration=prepared.effective_per_image_duration,
                water_mark=water_mark,
            )
            response = _build_multi_image_response(
                context=context, job_id=job_id, result=result
            )
            if context.usage_recorder is not None:
                context.usage_recorder.record_job(
                    job_id=job_id,
                    endpoint="/api/v1/jobs/multi-image",
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
            # Never echo the raw exception — it can name an upstream provider/model.
            raise HTTPException(
                status_code=500,
                detail="The request could not be completed. Please try again later.",
            ) from exc
        finally:
            cleanup_temp_dir(prepared.job_dir, logger=context.logger)

    # -----------------------------------------------------------------------
    # POST /api/v1/jobs/async_multi-image
    # Same parameters as the sync endpoint plus an optional callback_url.
    # Validates and stages inputs synchronously, then runs the pipeline in the
    # background. Poll GET /api/v1/jobs/async_multi-image/{job_id} for the result.
    # -----------------------------------------------------------------------

    @router.post(
        "/api/v1/jobs/async_multi-image",
        response_model=AsyncMultiImageJobAcceptedResponse,
        status_code=202,
        summary="Submit an async multi-image music generation job.",
    )
    async def submit_async_multi_image_job(
        *,
        request: Request,
        background_tasks: BackgroundTasks,
        images: List[UploadFile] = File(..., description="Between 3 and 10 image files."),
        user_prompt: str = Form(""),
        modelspec: str = Form(""),
        align_to_beats: bool = Form(True),
        per_image_duration: float = Form(
            3.0, gt=0, le=60,
            description=(
                "Seconds each image is displayed (3-60; below 3 is rejected "
                "with error code 10008). When image_count x per_image_duration "
                "is under 15s, 1s is added per image round-robin from the "
                "first image until the total reaches 15s."
            ),
        ),
        music_volume: Optional[float] = Form(None),
        water_mark: bool = Form(True),
        vocal_id: Optional[str] = Form(None),
        vocal_sample: UploadFile | None = File(
            None, description="Optional vocal sample for edenn_enhanced vocal cloning."),
        vocal_sample_url: Optional[str] = Form(None),
        transition: str = Form(
            "random",
            description=(
                "Visual transition(s) between images. 'random' (default) picks a "
                "weighted-random effect per boundary; 'none' keeps hard cuts; a "
                "single effect name (e.g. 'fade') applies it everywhere; a "
                "comma-separated list (e.g. 'fade,dissolve,wipeleft') is applied "
                "per boundary and cycled to fit. See docs/multi-image-transitions.md "
                "for the full list of supported effect names."
            ),
        ),
        transition_duration_s: float = Form(0.4),
        user_id: Optional[str] = Form(
            None,
            description="Optional caller-supplied user identifier (overridden by "
                        "the API key's user in enforce mode).",
        ),
        callback_url: Optional[str] = Form(
            None,
            description=(
                "Optional absolute http(s) URL that receives a POST when the job "
                "completes or fails. Payload matches AsyncMultiImageJobStatusResponse."
            ),
        ),
    ) -> AsyncMultiImageJobAcceptedResponse:
        user_id = resolve_user_id(request, user_id)
        normalized_callback_url = _normalize_async_callback_url(callback_url)
        job_id = uuid4().hex

        try:
            prepared = await _prepare_multi_image_job(
                context=context,
                job_id=job_id,
                images=images,
                modelspec=modelspec,
                per_image_duration=per_image_duration,
                music_volume=music_volume,
                vocal_id=vocal_id,
                vocal_sample=vocal_sample,
                vocal_sample_url=vocal_sample_url,
                transition=transition,
                transition_duration_s=transition_duration_s,
            )
        except HTTPException:
            raise
        except EdennError as exc:
            context.logger.exception(
                "Async multi-image job %s rejected with Edenn error: %s",
                job_id,
                exc.to_dict(),
            )
            raise edenn_error_to_http_exception(exc) from exc

        # The temp directory is now owned by _run_async_multi_image_job, which
        # calls cleanup_temp_dir in its finally block.
        _register_async_job(job_id, context=context)
        background_tasks.add_task(
            _run_async_multi_image_job,
            context=context,
            job_id=job_id,
            prepared=prepared,
            user_prompt=user_prompt,
            align_to_beats=align_to_beats,
            per_image_duration=per_image_duration,
            water_mark=water_mark,
            callback_url=normalized_callback_url,
            user_id=user_id,
            principal=get_principal(request),
        )
        return AsyncMultiImageJobAcceptedResponse(job_id=job_id)

    # -----------------------------------------------------------------------
    # GET /api/v1/jobs/async_multi-image/{job_id}
    # Poll for status and retrieve the full result once complete.
    # -----------------------------------------------------------------------

    @router.get(
        "/api/v1/jobs/async_multi-image/{job_id}",
        response_model=AsyncMultiImageJobStatusResponse,
        summary="Poll the status of an async multi-image music generation job.",
    )
    async def get_async_multi_image_job_status(job_id: str) -> AsyncMultiImageJobStatusResponse:
        state = _get_async_job_state(job_id, context=context)
        if state is None:
            raise HTTPException(
                status_code=404,
                detail=f"Job '{job_id}' not found. It may have expired or never existed.",
            )
        return AsyncMultiImageJobStatusResponse(
            job_id=job_id,
            status=state.status,
            result=_state_result_model(state),
            error=state.error,
            created_at=state.created_at,
        )

    return router


__all__ = [
    "AsyncMultiImageJobAcceptedResponse",
    "AsyncMultiImageJobStatusResponse",
    "MultiImageJobResponse",
    "create_multi_image_generation_router",
]
