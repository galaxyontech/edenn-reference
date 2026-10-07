import pytest

from EdennCode.exceptions import EdennProviderRateLimitError, EdennProviderResponseError
from EdennCode.TestSuites.helpers.integration import (
    handle_remote_http_failure,
    run_with_remote_rate_limit_retry,
)


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_handle_remote_http_failure_raises_in_strict_mode(monkeypatch) -> None:
    """Verify strict remote HTTP failures surface as retryable provider errors."""

    monkeypatch.setenv("STRICT_REMOTE_INTEGRATION", "1")

    response = _FakeResponse(
        502,
        payload={"detail": "The generation service returned an unexpected response. Please try again later."},
    )

    with pytest.raises(EdennProviderResponseError):
        handle_remote_http_failure(response)


def test_handle_remote_http_failure_raises_retryable_upstream_responses_locally(monkeypatch) -> None:
    """Verify local remote runs still classify upstream 502 responses as retryable."""

    monkeypatch.delenv("STRICT_REMOTE_INTEGRATION", raising=False)

    response = _FakeResponse(
        502,
        payload={"detail": "The generation service returned an unexpected response. Please try again later."},
    )

    with pytest.raises(EdennProviderResponseError):
        handle_remote_http_failure(response)


def test_handle_remote_http_failure_ignores_success_responses() -> None:
    """Verify successful remote HTTP responses pass through without raising."""

    response = _FakeResponse(200, payload={"ok": True})

    assert handle_remote_http_failure(response) is None


def test_handle_remote_http_failure_leaves_non_retryable_statuses_alone() -> None:
    """Verify caller assertions handle non-retryable request errors such as HTTP 400."""

    response = _FakeResponse(400, payload={"detail": "Bad request"})

    assert handle_remote_http_failure(response) is None


def test_handle_remote_http_failure_retries_structured_500_details() -> None:
    """Verify structured API 500 payloads become retryable provider response errors."""

    response = _FakeResponse(
        500,
        payload={
            "detail": {
                "status": "failed",
                "error_code": 90001,
                "message": "The upstream provider is temporarily unavailable.",
                "retryable": True,
            }
        },
    )

    with pytest.raises(EdennProviderResponseError) as exc_info:
        handle_remote_http_failure(response)

    assert exc_info.value.status_code == 500
    assert "upstream provider" in str(exc_info.value)


def test_run_with_remote_rate_limit_retry_retries_then_succeeds(monkeypatch) -> None:
    """Verify transient rate limits retry with exponential backoff before success."""

    sleep_calls = []
    attempts = {"count": 0}

    monkeypatch.setattr("EdennCode.TestSuites.helpers.integration.random.uniform", lambda _a, _b: 0.25)
    monkeypatch.setattr("EdennCode.TestSuites.helpers.integration.time.sleep", sleep_calls.append)

    def _operation() -> str:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise EdennProviderRateLimitError("rate limited", retryable=True)
        return "ok"

    result = run_with_remote_rate_limit_retry(
        _operation,
        operation_name="test-op",
    )

    assert result == "ok"
    assert attempts["count"] == 3
    assert sleep_calls == [1.25, 2.25]


def test_run_with_remote_rate_limit_retry_raises_after_exhausting_retries(monkeypatch) -> None:
    """Verify repeated rate limits are raised after the configured retry budget."""

    sleep_calls = []
    attempts = {"count": 0}

    monkeypatch.setattr("EdennCode.TestSuites.helpers.integration.random.uniform", lambda _a, _b: 0.5)
    monkeypatch.setattr("EdennCode.TestSuites.helpers.integration.time.sleep", sleep_calls.append)

    def _operation() -> None:
        attempts["count"] += 1
        raise EdennProviderRateLimitError("rate limited", retryable=True)

    with pytest.raises(EdennProviderRateLimitError):
        run_with_remote_rate_limit_retry(
            _operation,
            operation_name="test-op",
        )

    assert attempts["count"] == 5
    assert sleep_calls == [1.5, 2.5, 4.5, 8.5]


def test_run_with_remote_rate_limit_retry_does_not_retry_non_rate_limit_errors(monkeypatch) -> None:
    """Verify non-transient provider errors fail immediately without sleeping."""

    attempts = {"count": 0}
    sleep_calls = []

    monkeypatch.setattr("EdennCode.TestSuites.helpers.integration.time.sleep", sleep_calls.append)

    def _operation() -> None:
        attempts["count"] += 1
        raise EdennProviderResponseError("bad gateway", retryable=True)

    with pytest.raises(EdennProviderResponseError):
        run_with_remote_rate_limit_retry(
            _operation,
            operation_name="test-op",
        )

    assert attempts["count"] == 1
    assert sleep_calls == []


def test_run_with_remote_rate_limit_retry_retries_transient_server_errors(monkeypatch) -> None:
    """Verify generic transient HTTP 500-style exceptions use the retry path."""

    sleep_calls = []
    attempts = {"count": 0}

    class _TransientServerError(Exception):
        status_code = 500

    monkeypatch.setattr("EdennCode.TestSuites.helpers.integration.random.uniform", lambda _a, _b: 0.1)
    monkeypatch.setattr("EdennCode.TestSuites.helpers.integration.time.sleep", sleep_calls.append)

    def _operation() -> str:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise _TransientServerError("internal server error")
        return "ok"

    result = run_with_remote_rate_limit_retry(
        _operation,
        operation_name="test-op",
    )

    assert result == "ok"
    assert attempts["count"] == 3
    assert sleep_calls == [1.1, 2.1]
