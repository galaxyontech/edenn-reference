"""Unit tests for the async v2 source-video input guardrails.

Standard: size ≤ 300MB; within that, duration ≤ 150s and strictly > 15s.
Each violation maps to its own public error code: 10005 (too large),
10006 (too long), 10007 (too short) — all non-retryable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from EdennCode.Deployment.async_pipeline_v2.input_guardrails import (
    MAX_SOURCE_VIDEO_BYTES,
    MAX_SOURCE_VIDEO_DURATION_S,
    MIN_SOURCE_VIDEO_DURATION_S,
    validate_source_video_duration,
    validate_source_video_file,
    validate_source_video_metadata,
    validate_source_video_size,
)
from EdennCode.Deployment.error_codes import public_error_payload, resolve
from EdennCode.exceptions import (
    EdennInputVideoTooLargeError,
    EdennInputVideoTooLongError,
    EdennInputVideoTooShortError,
    EdennMediaProcessingError,
    EdennValidationError,
)


SMOKE_SHORT_VIDEO = Path(
    "EdennCode/TestSuites/assets/smoke/videos/"
    "sample_clip.mp4"
)  # 5.2s — below the 15s minimum
SMOKE_VALID_VIDEO = Path(
    "EdennCode/TestSuites/assets/smoke/videos/Videos2026-04-08_104426_347.mp4"
)  # 20.6s — within the 15–150s window


def test_limits_match_product_standard() -> None:
    assert MAX_SOURCE_VIDEO_BYTES == 300 * 1024 * 1024
    assert MAX_SOURCE_VIDEO_DURATION_S == 150.0
    assert MIN_SOURCE_VIDEO_DURATION_S == 15.0


def test_size_at_limit_passes() -> None:
    validate_source_video_size(MAX_SOURCE_VIDEO_BYTES)


def test_size_over_limit_raises_too_large() -> None:
    with pytest.raises(EdennInputVideoTooLargeError) as exc_info:
        validate_source_video_size(MAX_SOURCE_VIDEO_BYTES + 1)
    assert isinstance(exc_info.value, EdennValidationError)


def test_duration_at_max_passes() -> None:
    validate_source_video_duration(MAX_SOURCE_VIDEO_DURATION_S)


def test_duration_over_max_raises_too_long() -> None:
    with pytest.raises(EdennInputVideoTooLongError):
        validate_source_video_duration(MAX_SOURCE_VIDEO_DURATION_S + 0.01)


def test_duration_just_above_min_passes() -> None:
    validate_source_video_duration(MIN_SOURCE_VIDEO_DURATION_S + 0.01)


def test_duration_at_min_raises_too_short() -> None:
    # The standard requires strictly greater than 15 seconds.
    with pytest.raises(EdennInputVideoTooShortError):
        validate_source_video_duration(MIN_SOURCE_VIDEO_DURATION_S)


def test_duration_zero_raises_media_processing_error() -> None:
    # An unreadable/zero duration means the probe failed — that must surface as
    # a media-processing failure (40001), not a misleading "too short" (10007).
    with pytest.raises(EdennMediaProcessingError):
        validate_source_video_duration(0.0)
    with pytest.raises(EdennMediaProcessingError):
        validate_source_video_duration(-1.0)


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (EdennInputVideoTooLargeError("x"), 10005),
        (EdennInputVideoTooLongError("x"), 10006),
        (EdennInputVideoTooShortError("x"), 10007),
    ],
)
def test_error_code_mapping(error: EdennValidationError, expected_code: int) -> None:
    entry = resolve(error)
    assert entry.code == expected_code
    assert entry.retryable is False
    payload = public_error_payload(error)
    assert payload["error_code"] == expected_code
    assert payload["retryable"] is False
    # 10xxx payloads surface the author-controlled public message.
    assert payload["message"] == error.public_message


def test_public_messages_state_the_limits() -> None:
    assert "300MB" in EdennInputVideoTooLargeError("x").public_message
    assert "150 seconds" in EdennInputVideoTooLongError("x").public_message
    assert "15 seconds" in EdennInputVideoTooShortError("x").public_message


def test_metadata_validator_tolerates_missing_fields() -> None:
    validate_source_video_metadata(None)
    validate_source_video_metadata({})
    validate_source_video_metadata({"size_bytes": None, "duration": None})
    validate_source_video_metadata({"size_bytes": "n/a", "duration": "n/a"})


def test_metadata_validator_rejects_by_size_then_duration() -> None:
    with pytest.raises(EdennInputVideoTooLargeError):
        validate_source_video_metadata(
            {"size_bytes": MAX_SOURCE_VIDEO_BYTES + 1, "duration": 400.0}
        )
    with pytest.raises(EdennInputVideoTooLongError):
        validate_source_video_metadata({"size_bytes": 1024, "duration": 400.0})
    with pytest.raises(EdennInputVideoTooShortError):
        validate_source_video_metadata({"size_bytes": 1024, "duration": 5.2})


def test_metadata_validator_accepts_in_range_video() -> None:
    validate_source_video_metadata({"size_bytes": 1024, "duration": 30.0})


def test_file_validator_rejects_short_smoke_video() -> None:
    assert SMOKE_SHORT_VIDEO.exists()
    with pytest.raises(EdennInputVideoTooShortError):
        validate_source_video_file(SMOKE_SHORT_VIDEO)


def test_file_validator_accepts_valid_smoke_video_and_returns_duration() -> None:
    assert SMOKE_VALID_VIDEO.exists()
    duration = validate_source_video_file(SMOKE_VALID_VIDEO)
    assert MIN_SOURCE_VIDEO_DURATION_S < duration <= MAX_SOURCE_VIDEO_DURATION_S


def test_file_validator_uses_provided_duration_without_probe(tmp_path: Path) -> None:
    stub = tmp_path / "stub.mp4"
    stub.write_bytes(b"0" * 128)
    with pytest.raises(EdennInputVideoTooLongError):
        validate_source_video_file(stub, duration_s=200.0)
    assert validate_source_video_file(stub, duration_s=30.0) == 30.0


def test_file_validator_checks_size_before_probing(tmp_path: Path) -> None:
    # A file over the size cap must be rejected as too large even when its
    # duration would also be invalid — size is the first gate in the standard.
    big = tmp_path / "big.mp4"
    with big.open("wb") as handle:
        handle.truncate(MAX_SOURCE_VIDEO_BYTES + 1)
    with pytest.raises(EdennInputVideoTooLargeError):
        validate_source_video_file(big, duration_s=5.0)
