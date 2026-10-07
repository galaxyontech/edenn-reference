import os
import random
import time
import logging
from typing import Callable, TypeVar
from typing import Any

import pytest

from EdennCode.exceptions import (
    EdennProviderRateLimitError,
    EdennProviderResponseError,
    EdennProviderTimeoutError,
)

logger = logging.getLogger(__name__)
_T = TypeVar("_T")
_RETRYABLE_REMOTE_STATUS_CODES = {429, 500, 502, 503, 504}
_RETRYABLE_REMOTE_EXCEPTION_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "InternalServerError",
    "RateLimitError",
}


def is_strict_remote_integration() -> bool:
    return os.getenv("STRICT_REMOTE_INTEGRATION", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def should_run_remote_integration() -> bool:
    if is_strict_remote_integration():
        return True
    return os.getenv("RUN_REMOTE_INTEGRATION_LOCAL", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def require_remote_env(*env_names: str) -> None:
    if not should_run_remote_integration():
        pytest.skip(
            "Remote integration tests are skipped locally by default. "
            "Set RUN_REMOTE_INTEGRATION_LOCAL=1 to execute them outside CI."
        )

    missing = [name for name in env_names if not os.getenv(name, "").strip()]
    if not missing:
        return

    message = (
        "Missing required remote integration env vars: "
        + ", ".join(missing)
    )
    if is_strict_remote_integration():
        pytest.fail(message)
    pytest.skip(message)


def require_any_remote_env(*env_names: str) -> None:
    if not should_run_remote_integration():
        pytest.skip(
            "Remote integration tests are skipped locally by default. "
            "Set RUN_REMOTE_INTEGRATION_LOCAL=1 to execute them outside CI."
        )

    if any(os.getenv(name, "").strip() for name in env_names):
        return

    message = (
        "Missing one of required remote integration env vars: "
        + ", ".join(env_names)
    )
    if is_strict_remote_integration():
        pytest.fail(message)
    pytest.skip(message)


def require_remote_media_storage_env() -> None:
    if not should_run_remote_integration():
        pytest.skip(
            "Remote integration tests are skipped locally by default. "
            "Set RUN_REMOTE_INTEGRATION_LOCAL=1 to execute them outside CI."
        )

    azure_ready = any(
        os.getenv(name, "").strip()
        for name in (
            "AZURE_STORAGE_CONNECTION_STRING",
            "AZURE_STORAGE_ACCOUNT_URL",
        )
    )
    cos_ready = all(
        os.getenv(name, "").strip()
        for name in (
            "COS_SECRET_ID",
            "COS_SECRET_KEY",
            "COS_REGION",
            "COS_VIDEO_BUCKET",
            "COS_AUDIO_BUCKET",
            "COS_IMAGE_BUCKET",
        )
    )
    if azure_ready or cos_ready:
        return

    message = (
        "Missing remote media storage configuration. "
        "Set Azure storage env vars or the required COS_* env vars."
    )
    if is_strict_remote_integration():
        pytest.fail(message)
    pytest.skip(message)


def _remote_response_detail(response: Any) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = None

    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        if isinstance(detail, dict):
            message = detail.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        if detail is not None:
            return str(detail)

    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()

    status_code = getattr(response, "status_code", "unknown")
    return f"Remote API returned HTTP {status_code}"


def handle_remote_http_failure(response: Any) -> None:
    status_code = getattr(response, "status_code", None)
    if not isinstance(status_code, int) or 200 <= status_code < 300:
        return

    detail = _remote_response_detail(response)
    if status_code in {429, 503}:
        handle_remote_failure(
            EdennProviderRateLimitError(detail, retryable=True, status_code=status_code)
        )
        return
    if status_code == 504:
        handle_remote_failure(
            EdennProviderTimeoutError(detail, retryable=True, status_code=status_code)
        )
        return
    if status_code in {500, 502}:
        handle_remote_failure(
            EdennProviderResponseError(detail, retryable=True, status_code=status_code)
        )


def handle_remote_failure(exc: Exception) -> None:
    raise exc


def _remote_exc_status_code(exc: Exception) -> int | None:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code

    response = getattr(exc, "response", None)
    response_status_code = getattr(response, "status_code", None)
    if isinstance(response_status_code, int):
        return response_status_code
    return None


def _is_retryable_remote_exception(exc: Exception) -> bool:
    if isinstance(exc, (EdennProviderRateLimitError, EdennProviderTimeoutError)):
        return True

    if isinstance(exc, EdennProviderResponseError):
        status_code = _remote_exc_status_code(exc)
        return status_code in _RETRYABLE_REMOTE_STATUS_CODES

    status_code = _remote_exc_status_code(exc)
    if status_code in _RETRYABLE_REMOTE_STATUS_CODES:
        return True

    return type(exc).__name__ in _RETRYABLE_REMOTE_EXCEPTION_NAMES


def run_with_remote_rate_limit_retry(
    operation: Callable[[], _T],
    *,
    operation_name: str,
    retries: int = 4,
    base_delay_s: float = 1.0,
    max_jitter_s: float = 0.5,
) -> _T:
    if retries < 0:
        raise ValueError("retries must be >= 0")
    if base_delay_s < 0:
        raise ValueError("base_delay_s must be >= 0")
    if max_jitter_s < 0:
        raise ValueError("max_jitter_s must be >= 0")

    for attempt_idx in range(retries + 1):
        try:
            return operation()
        except Exception as exc:
            if not _is_retryable_remote_exception(exc) or attempt_idx >= retries:
                raise
            backoff_s = base_delay_s * (2 ** attempt_idx)
            jitter_s = random.uniform(0.0, max_jitter_s)
            wait_s = backoff_s + jitter_s
            logger.warning(
                "Retrying remote integration operation %s after transient failure in %.2fs (%d/%d): %s",
                operation_name,
                wait_s,
                attempt_idx + 1,
                retries,
                exc,
            )
            time.sleep(wait_s)

    raise AssertionError("unreachable")
