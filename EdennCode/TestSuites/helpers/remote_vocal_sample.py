from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import pytest

from .integration import is_strict_remote_integration, require_any_remote_env
from .paths import REMOTE_VOCAL_CLONE_SAMPLE_PATH

REMOTE_VOCAL_SAMPLE_PATH_ENV = "PROVIDER_B_VOCAL_CLONE_TEST_SAMPLE_PATH"
REMOTE_VOCAL_SAMPLE_URL_ENV = "PROVIDER_B_VOCAL_CLONE_TEST_SAMPLE_URL"


def _resolved_remote_vocal_sample_path() -> Optional[Path]:
    path_value = os.getenv(REMOTE_VOCAL_SAMPLE_PATH_ENV, "").strip()
    if path_value:
        return Path(path_value).expanduser()
    if REMOTE_VOCAL_CLONE_SAMPLE_PATH.exists():
        return REMOTE_VOCAL_CLONE_SAMPLE_PATH
    return None


def require_remote_vocal_sample() -> None:
    sample_path = _resolved_remote_vocal_sample_path()
    if sample_path is None:
        require_any_remote_env(
            REMOTE_VOCAL_SAMPLE_PATH_ENV,
            REMOTE_VOCAL_SAMPLE_URL_ENV,
        )
        return
    if sample_path.exists():
        return
    message = f"Remote vocal sample path does not exist: {sample_path}"
    if is_strict_remote_integration():
        pytest.fail(message)
    pytest.skip(message)


def remote_vocal_sample_form_data(
    *,
    url_field_name: str = "vocal_sample_url",
) -> dict[str, str]:
    if _resolved_remote_vocal_sample_path() is not None:
        return {}
    url_value = os.getenv(REMOTE_VOCAL_SAMPLE_URL_ENV, "").strip()
    if not url_value:
        return {}
    return {url_field_name: url_value}


def remote_vocal_sample_upload_tuple(
    *,
    field_name: str = "vocal_sample",
) -> Optional[tuple[str, tuple[str, bytes, str]]]:
    sample_path = _resolved_remote_vocal_sample_path()
    if sample_path is None:
        return None
    content_type = "audio/mpeg"
    if sample_path.suffix.lower() == ".wav":
        content_type = "audio/wav"
    elif sample_path.suffix.lower() == ".m4a":
        content_type = "audio/mp4"
    return (
        field_name,
        (
            sample_path.name,
            sample_path.read_bytes(),
            content_type,
        ),
    )


__all__ = [
    "REMOTE_VOCAL_SAMPLE_PATH_ENV",
    "REMOTE_VOCAL_SAMPLE_URL_ENV",
    "remote_vocal_sample_form_data",
    "remote_vocal_sample_upload_tuple",
    "require_remote_vocal_sample",
]
