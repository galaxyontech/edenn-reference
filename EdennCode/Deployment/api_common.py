from __future__ import annotations

import json
import logging
import os
import subprocess
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import unquote, urlparse

import aiofiles
import httpx
import sentry_sdk
from fastapi import HTTPException, UploadFile
from pydantic import BaseModel, Field

from EdennCode.Deployment.error_codes import resolve as _resolve_error_code
from EdennCode.exceptions import (
    EdennApiError,
    EdennError,
    EdennInputVideoTooLargeError,
    EdennMediaProcessingError,
    EdennUnsafeAssetUrlError,
)
from EdennCode.Util.MediaUtils import get_video_duration, resolve_ffmpeg_binary
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.word_ts import (
    WordTS,
)


@dataclass(frozen=True)
class ApiContext:
    settings: Any
    storage: Any
    workflow: Any
    alignment_workflow: Any
    audio_creative_edit_workflow: Any
    logger: logging.Logger
    multi_image_workflow: Any = None
    vocal_clone_workflow: Any = None
    recommendation_persistence: Any = None
    annotation_dispatcher: Any = None
    async_video_job_store: Any = None
    multi_image_async_job_store: Any = None
    auth_key_store: Any = None
    usage_recorder: Any = None


class LyricsTimestampModel(BaseModel):
    text: str
    startS: float
    endS: float
    i: Optional[int] = None


class TokenUsageCountsResponse(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class TokenUsageBreakdownResponse(BaseModel):
    user_prompt_preprocessor: TokenUsageCountsResponse = Field(default_factory=TokenUsageCountsResponse)
    scene_understanding: TokenUsageCountsResponse = Field(default_factory=TokenUsageCountsResponse)
    video_summary: TokenUsageCountsResponse = Field(default_factory=TokenUsageCountsResponse)
    music_prompt_orchestration: TokenUsageCountsResponse = Field(default_factory=TokenUsageCountsResponse)
    # Multi-image planning call (stays empty for the video flow).
    image_sequence_planning: TokenUsageCountsResponse = Field(default_factory=TokenUsageCountsResponse)


def service_version() -> str:
    """Return the API version field, formatted ``dev-<IMAGE_TAG>``.

    ``IMAGE_TAG`` is set at deploy time to the Docker tag (typically the short
    git SHA). Falls back to ``dev-local`` when unset so local runs and tests
    have a stable, recognizable value.
    """

    return f"dev-{os.environ.get('IMAGE_TAG', 'local')}"


class HealthResponse(BaseModel):
    status: str
    version: str = Field(default_factory=service_version)


async def sanitized_request_validation_handler(request, exc):
    """App-level 422 handler keeping the documented ``{"detail": "..."}`` shape.

    FastAPI's default validation body replays pydantic internals — the
    offending input value, ctx objects, doc URLs — which both breaks the
    documented contract and echoes request content back to the client. Only
    field paths and reasons are returned.
    """
    from fastapi.responses import JSONResponse

    problems = []
    for error in exc.errors():
        loc = ".".join(
            str(part) for part in error.get("loc", ()) if part not in ("body",)
        )
        message = str(error.get("msg", "invalid value"))
        problems.append(f"{loc}: {message}" if loc else message)
    return JSONResponse(
        status_code=422,
        content={"detail": "; ".join(problems) or "Invalid request."},
    )


class JobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ErrorDetail(BaseModel):
    status: str = JobStatus.FAILED
    error_code: int
    message: str
    retryable: bool = False


def sanitize_filename(filename: str) -> str:
    stem = Path(filename or "upload.bin").name
    safe = "".join(ch for ch in stem if ch.isalnum() or ch in {"-", "_", ".", " "})
    return safe or "upload.bin"


async def write_upload_to_disk(upload: UploadFile, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(destination, "wb") as out_file:
        while True:
            chunk = await upload.read(1 * 1024 * 1024)
            if not chunk:
                break
            await out_file.write(chunk)
    await upload.close()
    return destination


def filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise EdennApiError(
            f"Unsupported URL scheme for media download: {url}",
            public_message="The provided media URL must use http or https.",
            status_code=400,
            component="api",
            operation="resolve_media_url",
        )
    return sanitize_filename(Path(unquote(parsed.path)).name)


# ---------------------------------------------------------------------------#
# SSRF guard for caller-supplied URLs                                         #
# ---------------------------------------------------------------------------#

# Hostnames that never belong to a public asset. `metadata.google.internal` and
# the link-local 169.254.169.254 are the cloud instance-metadata endpoints —
# the classic SSRF prize, because they hand out credentials to anything that
# can make an HTTP request from inside the network.
_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "instance-data",
}


def assert_public_asset_url(url: str, *, asset_label: str = "asset") -> str:
    """Refuse a URL that would make us fetch from our own network.

    Every caller of :func:`download_public_file_to_disk` passes a URL that came
    from a request body. Without this, "download the user's video" is also
    "make an authenticated GET from inside the cluster to any address the caller
    names" — including the instance-metadata service.

    Enforced here rather than at each call site because the guarantee belongs to
    the word *public* in this function's name: three call sites already existed,
    none of them checked, and the next one would not have either.
    """

    from ipaddress import ip_address
    from urllib.parse import urlparse

    value = (url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise EdennUnsafeAssetUrlError(
            f"The {asset_label} URL must be an absolute http or https URL."
        )

    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname or hostname in _BLOCKED_HOSTNAMES:
        raise EdennUnsafeAssetUrlError(
            f"The {asset_label} URL must not point at this machine."
        )

    try:
        host_ip = ip_address(hostname)
    except ValueError:
        # A name, not a literal. We deliberately do NOT resolve it here: a
        # resolve-then-fetch is a TOCTOU window, and the HTTP client would
        # resolve again anyway. Literals are the reachable half of the attack.
        return value

    if (
        host_ip.is_private
        or host_ip.is_loopback
        or host_ip.is_link_local
        or host_ip.is_reserved
        or host_ip.is_multicast
        or host_ip.is_unspecified
    ):
        raise EdennUnsafeAssetUrlError(
            f"The {asset_label} URL must not target a private or local network address."
        )
    return value


async def download_public_file_to_disk(
    *,
    url: str,
    destination: Path,
    asset_label: str,
    max_bytes: Optional[int] = None,
) -> Path:
    """Stream a public URL to disk, optionally aborting past a byte budget.

    ``max_bytes`` bounds ingestion for guardrailed sources: the download stops
    as soon as the budget is exceeded (Content-Length is checked first when the
    server sends one), the partial file is removed, and the size violation is
    raised instead of a transport error so callers surface the specific
    too-large code rather than retrying the download.
    """
    # The URL came from a request body; enforce "public" before fetching it.
    url = assert_public_asset_url(url, asset_label=asset_label)
    destination.parent.mkdir(parents=True, exist_ok=True)

    def _too_large(observed: int) -> EdennInputVideoTooLargeError:
        return EdennInputVideoTooLargeError(
            (
                f"Download of {asset_label} from URL exceeds the "
                f"{max_bytes} byte limit (observed {observed} bytes)."
            ),
            component="api",
            operation=f"download_{asset_label}_url",
            context={"max_bytes": max_bytes, "observed_bytes": observed},
        )

    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                if max_bytes is not None:
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise _too_large(int(declared))
                downloaded = 0
                try:
                    async with aiofiles.open(destination, "wb") as out_file:
                        async for chunk in response.aiter_bytes():
                            if chunk:
                                downloaded += len(chunk)
                                if max_bytes is not None and downloaded > max_bytes:
                                    raise _too_large(downloaded)
                                await out_file.write(chunk)
                except EdennInputVideoTooLargeError:
                    destination.unlink(missing_ok=True)
                    raise
    except httpx.HTTPStatusError as exc:
        raise EdennApiError(
            f"Failed to download {asset_label} from URL {url}: HTTP {exc.response.status_code}",
            public_message=f"The provided {asset_label} URL could not be downloaded.",
            status_code=400,
            component="api",
            operation=f"download_{asset_label}_url",
            cause=exc,
        ) from exc
    except httpx.RequestError as exc:
        raise EdennApiError(
            f"Failed to download {asset_label} from URL {url}: {exc}",
            public_message=f"The provided {asset_label} URL could not be downloaded.",
            status_code=400,
            component="api",
            operation=f"download_{asset_label}_url",
            cause=exc,
        ) from exc
    return destination


async def resolve_media_source_to_disk(
    *,
    upload: UploadFile | None,
    remote_url: Optional[str],
    destination_dir: Path,
    fallback_filename: str,
    asset_label: str,
) -> Path:
    operation_label = asset_label.replace(" ", "_")
    upload_provided = upload is not None
    url_value = (remote_url or "").strip()
    url_provided = bool(url_value)

    if upload_provided == url_provided:
        raise EdennApiError(
            f"Provide exactly one of {asset_label} upload or {asset_label}_url.",
            public_message=f"Provide exactly one {asset_label} source.",
            status_code=400,
            component="api",
            operation=f"resolve_{operation_label}_source",
        )

    if upload_provided and upload is not None:
        filename = sanitize_filename(upload.filename or fallback_filename)
        return await write_upload_to_disk(upload, destination_dir / filename)

    remote_filename = filename_from_url(url_value) or fallback_filename
    return await download_public_file_to_disk(
        url=url_value,
        destination=destination_dir / remote_filename,
        asset_label=asset_label,
    )


async def resolve_optional_media_source_to_disk(
    *,
    upload: UploadFile | None,
    remote_url: Optional[str],
    destination_dir: Path,
    fallback_filename: str,
    asset_label: str,
) -> Optional[Path]:
    operation_label = asset_label.replace(" ", "_")
    upload_provided = upload is not None
    url_provided = bool((remote_url or "").strip())
    if upload_provided and url_provided:
        raise EdennApiError(
            f"Provide either {asset_label} upload or {asset_label}_url, not both.",
            public_message=f"Provide {asset_label} upload or {asset_label} URL, not both.",
            status_code=400,
            component="api",
            operation=f"resolve_optional_{operation_label}_source",
        )
    if not upload_provided and not url_provided:
        return None
    return await resolve_media_source_to_disk(
        upload=upload,
        remote_url=remote_url,
        destination_dir=destination_dir,
        fallback_filename=fallback_filename,
        asset_label=asset_label,
    )


def prepare_audio_for_provider_b_vocal_clone(
    *,
    source_audio_path: Path,
    destination_dir: Path,
) -> Path:
    output_path = destination_dir / f"{source_audio_path.stem}_vocal_clone_input.m4a"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg_bin = resolve_ffmpeg_binary()
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i",
        str(source_audio_path),
        "-vn",
        "-t",
        "30",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        str(output_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise EdennMediaProcessingError(
            "Failed to prepare the vocal sample for ProviderB vocal cloning.",
            public_message="The vocal sample could not be prepared for vocal cloning.",
            component="api",
            operation="prepare_audio_for_provider_b_vocal_clone",
            context={"source_audio_path": str(source_audio_path)},
            cause=exc,
        ) from exc
    return output_path


def parse_lyrics_timestamps_json(raw_value: Optional[str]) -> List[WordTS]:
    text = (raw_value or "").strip()
    if not text:
        return []

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EdennApiError(
            "lyrics_timestamps_json must be valid JSON.",
            public_message="lyrics_timestamps_json must be valid JSON.",
            status_code=400,
            component="api",
            operation="parse_lyrics_timestamps_json",
            cause=exc,
        ) from exc

    if not isinstance(payload, list):
        raise EdennApiError(
            "lyrics_timestamps_json must be a JSON array.",
            public_message="lyrics_timestamps_json must be a JSON array.",
            status_code=400,
            component="api",
            operation="parse_lyrics_timestamps_json",
        )

    parsed_words: List[WordTS] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise EdennApiError(
                f"lyrics_timestamps_json item at index {index} must be an object.",
                public_message="Each lyrics timestamp must be an object.",
                status_code=400,
                component="api",
                operation="parse_lyrics_timestamps_json",
            )
        try:
            lyric = LyricsTimestampModel(**item)
        except Exception as exc:
            raise EdennApiError(
                f"Invalid lyrics timestamp at index {index}: {exc}",
                public_message="One or more lyrics timestamps are invalid.",
                status_code=400,
                component="api",
                operation="parse_lyrics_timestamps_json",
                cause=exc,
            ) from exc
        parsed_words.append(
            WordTS(
                text=lyric.text,
                startS=float(lyric.startS),
                endS=float(lyric.endS),
                i=lyric.i,
            )
        )
    return parsed_words


def parse_url_list_json(raw_value: Optional[str], *, field_name: str) -> List[str]:
    text = (raw_value or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EdennApiError(
            f"{field_name} must be valid JSON.",
            public_message=f"{field_name} must be valid JSON.",
            status_code=400,
            component="api",
            operation=f"parse_{field_name}",
            cause=exc,
        ) from exc
    if not isinstance(payload, list):
        raise EdennApiError(
            f"{field_name} must be a JSON array.",
            public_message=f"{field_name} must be a JSON array.",
            status_code=400,
            component="api",
            operation=f"parse_{field_name}",
        )

    normalized_urls: List[str] = []
    for idx, item in enumerate(payload):
        if not isinstance(item, str) or not item.strip():
            raise EdennApiError(
                f"{field_name} item at index {idx} must be a non-empty string.",
                public_message=f"Each {field_name} entry must be a non-empty string.",
                status_code=400,
                component="api",
                operation=f"parse_{field_name}",
            )
        filename_from_url(item.strip())
        normalized_urls.append(item.strip())
    return normalized_urls


def probe_audio_metrics(path: Optional[Path]) -> tuple[Optional[float], Optional[int]]:
    """Best-effort ``(duration_s, size_bytes)`` for a local audio file.

    Metadata is advisory, so a missing path or any probe/stat failure degrades to
    ``None`` rather than failing the surrounding job. Call before temp cleanup.
    """
    if path is None:
        return None, None

    size_bytes: Optional[int] = None
    try:
        size_bytes = path.stat().st_size
    except OSError:
        size_bytes = None

    duration_s: Optional[float] = None
    try:
        probed = get_video_duration(path)
        duration_s = probed if probed and probed > 0 else None
    except Exception:
        duration_s = None

    return duration_s, size_bytes


def guess_audio_content_type(path_value: Any) -> str:
    try:
        if str(path_value).lower().endswith(".wav"):
            return "audio/wav"
    except Exception:
        pass
    return "audio/mpeg"


def guess_image_content_type(path_value: Any) -> str:
    try:
        lowered = str(path_value).lower()
    except Exception:
        return "image/jpeg"

    if lowered.endswith(".webp"):
        return "image/webp"
    if lowered.endswith(".png"):
        return "image/png"
    if lowered.endswith(".bmp"):
        return "image/bmp"
    return "image/jpeg"


def normalize_token_usage_counts(raw_value: Any) -> TokenUsageCountsResponse:
    if not isinstance(raw_value, dict):
        return TokenUsageCountsResponse()
    return TokenUsageCountsResponse(
        prompt_tokens=int(raw_value.get("prompt_tokens", 0) or 0),
        completion_tokens=int(raw_value.get("completion_tokens", 0) or 0),
        total_tokens=int(raw_value.get("total_tokens", 0) or 0),
    )


def normalize_token_usage_breakdown(raw_value: Any) -> TokenUsageBreakdownResponse:
    if not isinstance(raw_value, dict):
        return TokenUsageBreakdownResponse()
    return TokenUsageBreakdownResponse(
        user_prompt_preprocessor=normalize_token_usage_counts(
            raw_value.get("user_prompt_preprocessor")
        ),
        scene_understanding=normalize_token_usage_counts(
            raw_value.get("scene_understanding")
        ),
        video_summary=normalize_token_usage_counts(
            raw_value.get("video_summary")
        ),
        music_prompt_orchestration=normalize_token_usage_counts(
            raw_value.get("music_prompt_orchestration")
        ),
        image_sequence_planning=normalize_token_usage_counts(
            raw_value.get("image_sequence_planning")
        ),
    )


def cleanup_temp_dir(path: Path, *, logger: logging.Logger) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.debug("Could not remove %s: %s", path, exc)


def edenn_error_to_http_exception(error: EdennError) -> HTTPException:
    entry = _resolve_error_code(error)
    # 5xx-class errors (provider outages, storage, config, internal) must reach
    # Sentry even though they get converted to HTTPException below — FastAPI's
    # Sentry integration ignores HTTPException, so we capture explicitly here.
    if entry.http_status >= 500:
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("edenn_error_code", entry.code)
            scope.set_tag("retryable", entry.retryable)
            scope.set_context("edenn_error", error.to_dict())
            sentry_sdk.capture_exception(error)
    # 10xxx input errors can keep request-specific public text. Provider and
    # internal errors use catalog text so upstream exception messages never
    # become part of the public API response.
    message = error.public_message if entry.code // 1000 == 10 else entry.message
    return HTTPException(
        status_code=entry.http_status,
        detail=ErrorDetail(
            error_code=entry.code,
            message=message,
            retryable=entry.retryable,
        ).model_dump(),
    )


__all__ = [
    "ApiContext",
    "ErrorDetail",
    "HealthResponse",
    "JobStatus",
    "LyricsTimestampModel",
    "TokenUsageCountsResponse",
    "TokenUsageBreakdownResponse",
    "cleanup_temp_dir",
    "download_public_file_to_disk",
    "edenn_error_to_http_exception",
    "filename_from_url",
    "guess_audio_content_type",
    "guess_image_content_type",
    "normalize_token_usage_breakdown",
    "normalize_token_usage_counts",
    "parse_lyrics_timestamps_json",
    "parse_url_list_json",
    "prepare_audio_for_provider_b_vocal_clone",
    "resolve_media_source_to_disk",
    "resolve_optional_media_source_to_disk",
    "sanitize_filename",
    "write_upload_to_disk",
]
