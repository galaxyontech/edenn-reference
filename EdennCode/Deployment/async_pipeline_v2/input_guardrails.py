"""Source-video input guardrails for async v2 jobs.

Product standard for client-supplied source videos:

* file size must not exceed 300MB;
* within that size, duration must not exceed 150 seconds;
* duration must be strictly greater than 15 seconds.

Each violation raises a dedicated ``EdennValidationError`` subclass that the
error-code catalog maps to its own public code (10005 too large, 10006 too
long, 10007 too short), so clients receive a specific, actionable status both
on synchronous rejection (HTTP 400) and on asynchronous job failure
(``error_json`` on the status endpoint).

Enforcement happens at two layers:

* the API rejects synchronously when the numbers are already known (staged
  artifact metadata, upload staging);
* workers validate the resolved original file before compression, which is
  the authoritative check and covers ``video_url`` sources the API never
  downloads.

The size limit always applies to the video as submitted, never to a
compressed derivative — which is why workers must validate before invoking
the compression step.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from EdennCode.Util.MediaUtils import get_video_duration
from EdennCode.exceptions import (
    EdennInputVideoTooLargeError,
    EdennInputVideoTooLongError,
    EdennInputVideoTooShortError,
    EdennMediaProcessingError,
)


MAX_SOURCE_VIDEO_BYTES: int = 300 * 1024 * 1024
MAX_SOURCE_VIDEO_DURATION_S: float = 150.0
MIN_SOURCE_VIDEO_DURATION_S: float = 15.0

_COMPONENT = "input_guardrails"


def validate_source_video_size(
    size_bytes: int,
    *,
    source_path: Optional[Path] = None,
) -> None:
    if size_bytes <= MAX_SOURCE_VIDEO_BYTES:
        return
    context: dict[str, Any] = {
        "size_bytes": int(size_bytes),
        "max_size_bytes": MAX_SOURCE_VIDEO_BYTES,
    }
    if source_path is not None:
        context["source_path"] = str(source_path)
    raise EdennInputVideoTooLargeError(
        (
            f"Source video is too large: {size_bytes} bytes exceeds "
            f"{MAX_SOURCE_VIDEO_BYTES} bytes (300MB)."
        ),
        component=_COMPONENT,
        operation="validate_source_video_size",
        context=context,
    )


def validate_source_video_duration(
    duration_s: float,
    *,
    source_path: Optional[Path] = None,
) -> None:
    duration = float(duration_s or 0.0)
    context: dict[str, Any] = {
        "duration_s": duration,
        "max_duration_s": MAX_SOURCE_VIDEO_DURATION_S,
        "min_duration_s": MIN_SOURCE_VIDEO_DURATION_S,
    }
    if source_path is not None:
        context["source_path"] = str(source_path)
    if duration <= 0:
        # A zero/negative duration means the probe could not read the video —
        # report a media-processing failure, not a misleading "too short".
        raise EdennMediaProcessingError(
            "Source video duration could not be determined; the file may be corrupt or not a video.",
            component=_COMPONENT,
            operation="validate_source_video_duration",
            context=context,
        )
    if duration > MAX_SOURCE_VIDEO_DURATION_S:
        raise EdennInputVideoTooLongError(
            (
                f"Source video is too long: {duration:.2f}s exceeds "
                f"{MAX_SOURCE_VIDEO_DURATION_S:.0f}s."
            ),
            component=_COMPONENT,
            operation="validate_source_video_duration",
            context=context,
        )
    if duration <= MIN_SOURCE_VIDEO_DURATION_S:
        raise EdennInputVideoTooShortError(
            (
                f"Source video is too short: {duration:.2f}s; it must be "
                f"longer than {MIN_SOURCE_VIDEO_DURATION_S:.0f}s."
            ),
            component=_COMPONENT,
            operation="validate_source_video_duration",
            context=context,
        )


def validate_source_video_file(
    path: Path,
    *,
    duration_s: Optional[float] = None,
) -> float:
    """Validate a resolved source-video file against the input standard.

    Size is checked first (cheap stat), then duration — probed with ffprobe
    when the caller does not already know it. Returns the effective duration
    so callers can reuse it. This runs on the ORIGINAL resolved file, before
    any compression.
    """

    resolved = Path(path)
    validate_source_video_size(resolved.stat().st_size, source_path=resolved)
    duration = float(duration_s) if duration_s is not None else get_video_duration(resolved)
    validate_source_video_duration(duration, source_path=resolved)
    return duration


def validate_source_video_metadata(
    metadata_json: Optional[Mapping[str, Any]],
    *,
    source_path: Optional[Path] = None,
) -> None:
    """Validate staged-artifact metadata when the numbers are already known.

    Tolerates missing or non-numeric fields (URL-sourced artifacts carry
    neither size nor duration) — the worker-side file check remains the
    authoritative enforcement for those.
    """

    if not metadata_json:
        return
    size_bytes = metadata_json.get("size_bytes")
    if isinstance(size_bytes, (int, float)) and not isinstance(size_bytes, bool):
        validate_source_video_size(int(size_bytes), source_path=source_path)
    duration = metadata_json.get("duration")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        validate_source_video_duration(float(duration), source_path=source_path)


__all__ = [
    "MAX_SOURCE_VIDEO_BYTES",
    "MAX_SOURCE_VIDEO_DURATION_S",
    "MIN_SOURCE_VIDEO_DURATION_S",
    "validate_source_video_size",
    "validate_source_video_duration",
    "validate_source_video_file",
    "validate_source_video_metadata",
]
