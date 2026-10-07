"""Regression tests for the 2026-08-17 prod studio-provider failures.

Job A: a slow-but-successful generation was abandoned by a hardcoded 200s
budget and dead-lettered on its only attempt (the paid task finished 8.5s
after we gave up). Job B: a single edge-gateway 522 on one lyrics status poll
aborted the whole job. These pin the fixes: 52x is transient, one bad poll
never abandons a paid task, a created task is never resubmitted on another
key/base, the creating key is durably recorded and rebindable for resume, and
the provider stage keeps a resume attempt for resumable tiers.
"""
import asyncio
import os
from unittest.mock import patch

import httpx
import pytest

from EdennCode.Deployment import provider_vocabulary
from EdennCode.exceptions import (
    EdennProviderAuthenticationError,
    EdennProviderError,
    EdennProviderRateLimitError,
    EdennProviderResponseError,
    EdennProviderTimeoutError,
)
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen import provider_c as provider_mod
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_c import (
    GenerateParams,
    ProviderCApi,
    _KEY_COOLDOWNS,
)


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    _KEY_COOLDOWNS._state.clear()
    yield
    _KEY_COOLDOWNS._state.clear()


@pytest.fixture(autouse=True)
def _deterministic_key_order(monkeypatch):
    monkeypatch.setattr(provider_mod.random, "shuffle", lambda seq: None)


@pytest.fixture(autouse=True)
def _instant_sleep(monkeypatch):
    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(provider_mod.asyncio, "sleep", _no_sleep)


@pytest.fixture
def music_triage_vocabulary(monkeypatch):
    """Declare which upstream names this deployment triages as music generation.

    The 20xxx (AI analysis) / 30xxx (music) split reads its vocabulary from
    configuration rather than from a table written into the source, so a test
    that expects a music code has to supply one — and has to drop the memoised
    copy on the way out, or it would decide the routing for every test after it.
    """
    monkeypatch.setenv("MUSIC_PROVIDER_NAMES", "provider_c")
    provider_vocabulary.reset_cache()
    try:
        yield
    finally:
        monkeypatch.undo()
        provider_vocabulary.reset_cache()


def _api(transport: httpx.MockTransport, *, max_retries: int = 0) -> ProviderCApi:
    return ProviderCApi(
        base_url="https://provider.test/api/v1",
        client=httpx.AsyncClient(transport=transport),
        max_retries=max_retries,
    )


_SUCCESS_RECORD = {
    "code": 200,
    "data": {
        "status": "SUCCESS",
        "response": {"provider_cData": [
            {"id": "audio-1", "audioUrl": "https://cdn.test/a.mp3"},
        ]},
    },
}

_PENDING_RECORD = {"code": 200, "data": {"status": "PENDING"}}


def test_522_on_submit_is_retried_within_json_request() -> None:
    calls = {"generate": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/generate")
        calls["generate"] += 1
        if calls["generate"] == 1:
            return httpx.Response(522)
        return httpx.Response(200, json={"code": 200, "data": {"taskId": "task-1"}})

    with patch.dict(os.environ, {"PROVIDER_C_API_KEY_1": "key-1"}, clear=True):
        api = _api(httpx.MockTransport(handler), max_retries=1)
        task_id = asyncio.run(api.generate(GenerateParams(prompt="lofi")))

    assert task_id == "task-1"
    assert calls["generate"] == 2


def test_ambiguous_statuses_are_not_auto_resubmitted() -> None:
    # 408/520/524: the origin may have accepted the paid submission before the
    # edge gave up — the client may retry, but _json_request must not.
    for status in (408, 520, 524):
        calls = {"generate": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["generate"] += 1
            return httpx.Response(status)

        with patch.dict(os.environ, {"PROVIDER_C_API_KEY_1": "key-1"}, clear=True):
            api = _api(httpx.MockTransport(handler), max_retries=2)
            with pytest.raises(EdennProviderResponseError) as exc_info:
                asyncio.run(api.generate(GenerateParams(prompt="lofi")))

        assert calls["generate"] == 1, f"HTTP {status} was auto-resubmitted"
        assert exc_info.value.retryable is True


def test_522_mid_poll_does_not_abandon_generation() -> None:
    calls = {"generate": 0, "poll": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generate"):
            calls["generate"] += 1
            return httpx.Response(200, json={"code": 200, "data": {"taskId": "task-1"}})
        assert request.url.path.endswith("/generate/record-info")
        calls["poll"] += 1
        if calls["poll"] == 1:
            return httpx.Response(522)
        return httpx.Response(200, json=_SUCCESS_RECORD)

    with patch.dict(os.environ, {"PROVIDER_C_API_KEY_1": "key-1"}, clear=True):
        api = _api(httpx.MockTransport(handler))
        task_id, result, _base = asyncio.run(
            api.generate_and_poll_tracks(GenerateParams(prompt="lofi"), timeout_s=30.0)
        )

    assert task_id == "task-1"
    assert result.tracks[0].audio_id == "audio-1"
    assert calls["generate"] == 1
    assert calls["poll"] == 2


def test_transport_blip_mid_poll_does_not_abandon_generation() -> None:
    calls = {"poll": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generate"):
            return httpx.Response(200, json={"code": 200, "data": {"taskId": "task-1"}})
        calls["poll"] += 1
        if calls["poll"] == 1:
            raise httpx.ConnectError("origin reset", request=request)
        return httpx.Response(200, json=_SUCCESS_RECORD)

    with patch.dict(os.environ, {"PROVIDER_C_API_KEY_1": "key-1"}, clear=True):
        api = _api(httpx.MockTransport(handler))
        _task_id, result, _base = asyncio.run(
            api.generate_and_poll_tracks(GenerateParams(prompt="lofi"), timeout_s=30.0)
        )

    assert result.tracks[0].audio_id == "audio-1"
    assert calls["poll"] == 2


def test_rate_limit_body_code_mid_poll_is_tolerated() -> None:
    calls = {"poll": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generate"):
            return httpx.Response(200, json={"code": 200, "data": {"taskId": "task-1"}})
        calls["poll"] += 1
        if calls["poll"] == 1:
            return httpx.Response(200, json={"code": 429, "msg": "rate limited"})
        return httpx.Response(200, json=_SUCCESS_RECORD)

    with patch.dict(os.environ, {"PROVIDER_C_API_KEY_1": "key-1"}, clear=True):
        api = _api(httpx.MockTransport(handler))
        _task_id, result, _base = asyncio.run(
            api.generate_and_poll_tracks(GenerateParams(prompt="lofi"), timeout_s=30.0)
        )

    assert result.tracks[0].audio_id == "audio-1"
    assert calls["poll"] == 2


def test_first_track_poll_tolerates_blip() -> None:
    calls = {"poll": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generate"):
            return httpx.Response(200, json={"code": 200, "data": {"taskId": "task-1"}})
        calls["poll"] += 1
        if calls["poll"] == 1:
            return httpx.Response(522)
        return httpx.Response(200, json=_SUCCESS_RECORD)

    with patch.dict(os.environ, {"PROVIDER_C_API_KEY_1": "key-1"}, clear=True):
        api = _api(httpx.MockTransport(handler))
        _task_id, track, _base = asyncio.run(
            api.generate_and_poll_first_track(GenerateParams(prompt="lofi"), timeout_s=30.0)
        )

    assert track.audio_id == "audio-1"
    assert calls["poll"] == 2


def test_lyrics_poll_522_blip_recovers() -> None:
    calls = {"poll": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/lyrics"):
            return httpx.Response(200, json={"code": 200, "data": {"taskId": "lyr-1"}})
        assert request.url.path.endswith("/lyrics/record-info")
        calls["poll"] += 1
        if calls["poll"] == 1:
            return httpx.Response(522)
        return httpx.Response(200, json={
            "code": 200,
            "data": {
                "status": "SUCCESS",
                "response": {"data": [{"text": "la la la"}]},
            },
        })

    with patch.dict(os.environ, {"PROVIDER_C_API_KEY_1": "key-1"}, clear=True):
        api = _api(httpx.MockTransport(handler))
        lyrics = asyncio.run(api.generate_lyrics(prompt="rain song", timeout_s=30.0))

    assert lyrics == "la la la"
    assert calls["poll"] == 2


def test_auth_error_mid_poll_raises_immediately() -> None:
    calls = {"generate": 0, "poll": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generate"):
            calls["generate"] += 1
            return httpx.Response(200, json={"code": 200, "data": {"taskId": "task-1"}})
        calls["poll"] += 1
        return httpx.Response(401, json={"msg": "bad key"})

    with patch.dict(
        os.environ,
        {"PROVIDER_C_API_KEY_1": "key-1", "PROVIDER_C_API_KEY_2": "key-2"},
        clear=True,
    ):
        api = _api(httpx.MockTransport(handler))
        with pytest.raises(EdennProviderAuthenticationError):
            asyncio.run(
                api.generate_and_poll_tracks(GenerateParams(prompt="lofi"), timeout_s=30.0)
            )

    # Non-retryable poll error surfaces at once — and the created (paid) task
    # is NOT resubmitted on the second configured key.
    assert calls["generate"] == 1
    assert calls["poll"] == 1


def test_created_task_never_resubmitted_on_poll_exhaustion() -> None:
    calls = {"generate": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generate"):
            calls["generate"] += 1
            return httpx.Response(200, json={"code": 200, "data": {"taskId": "task-1"}})
        return httpx.Response(522)

    with patch.dict(
        os.environ,
        {"PROVIDER_C_API_KEY_1": "key-1", "PROVIDER_C_API_KEY_2": "key-2"},
        clear=True,
    ):
        api = _api(httpx.MockTransport(handler))
        # At the deadline the last mapped poll error surfaces (more informative
        # than the generic timeout), and it must not trigger a second paid
        # submission on the other key/base.
        with pytest.raises(EdennProviderResponseError):
            asyncio.run(
                api.generate_and_poll_tracks(
                    GenerateParams(prompt="lofi"),
                    timeout_s=0.3,
                    poll_s=0.1,
                )
            )

    assert calls["generate"] == 1


def test_created_extend_task_never_resubmitted() -> None:
    calls = {"extend": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generate/extend"):
            calls["extend"] += 1
            return httpx.Response(200, json={"code": 200, "data": {"taskId": "ext-1"}})
        return httpx.Response(401, json={"msg": "bad key"})

    with patch.dict(
        os.environ,
        {"PROVIDER_C_API_KEY_1": "key-1", "PROVIDER_C_API_KEY_2": "key-2"},
        clear=True,
    ):
        api = _api(httpx.MockTransport(handler))
        with pytest.raises(EdennProviderAuthenticationError):
            asyncio.run(api.extend_and_poll_track("audio-0", timeout_s=30.0))

    assert calls["extend"] == 1


def test_pure_pending_still_times_out_with_timeout_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generate"):
            return httpx.Response(200, json={"code": 200, "data": {"taskId": "task-1"}})
        return httpx.Response(200, json=_PENDING_RECORD)

    with patch.dict(os.environ, {"PROVIDER_C_API_KEY_1": "key-1"}, clear=True):
        api = _api(httpx.MockTransport(handler))
        with pytest.raises(EdennProviderTimeoutError):
            asyncio.run(
                api.generate_and_poll_tracks(
                    GenerateParams(prompt="lofi"),
                    timeout_s=0.3,
                    poll_s=0.1,
                )
            )


def test_task_created_callback_carries_key_label_and_rebind_restores_it() -> None:
    seen: list[tuple[str, str, str]] = []

    async def _recorder(task_id: str, base: str, key_label: str) -> None:
        seen.append((task_id, base, key_label))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generate"):
            return httpx.Response(200, json={"code": 200, "data": {"taskId": "task-1"}})
        return httpx.Response(200, json=_SUCCESS_RECORD)

    env = {"PROVIDER_C_API_KEY_1": "key-1", "PROVIDER_C_API_KEY_2": "key-2"}
    with patch.dict(os.environ, env, clear=True):
        api = _api(httpx.MockTransport(handler))
        asyncio.run(
            api.generate_and_poll_tracks(
                GenerateParams(prompt="lofi"),
                timeout_s=30.0,
                on_task_created=_recorder,
            )
        )
        assert seen and seen[0][0] == "task-1"
        recorded_label = seen[0][2]
        assert recorded_label in env

        # A fresh client (fresh process) has no in-memory binding: rebinding
        # from the recorded label must restore the owning key for the resume.
        fresh = _api(httpx.MockTransport(handler))
        assert fresh._key_for_id("task-1") is None
        assert fresh.rebind_task_key("task-1", recorded_label) is True
        assert fresh._key_for_id("task-1").label == recorded_label
        # An unknown (rotated-away) label degrades gracefully.
        assert fresh.rebind_task_key("task-2", "PROVIDER_C_API_KEY_99") is False


def test_provider_stage_max_attempts_gives_resumable_tiers_a_resume_attempt() -> None:
    from EdennCode.Deployment.async_pipeline_v2.workers.split_stage_workers import (
        provider_stage_max_attempts,
    )

    # Resumable tiers get a second (poll-only, spend-free) attempt even when
    # the request pins max_attempts=1.
    assert provider_stage_max_attempts(1, "edenn_studio") == 2
    assert provider_stage_max_attempts(1, "edenn_enhanced") == 2
    assert provider_stage_max_attempts(3, "edenn_studio") == 3
    # The basic tier cannot resume; a retry would re-submit and pay again.
    assert provider_stage_max_attempts(1, "edenn_basic") == 1
    assert provider_stage_max_attempts(1, "") == 1


def test_public_error_payload_definite_provider_4xx_is_not_retryable(
    music_triage_vocabulary,
) -> None:
    from EdennCode.Deployment.error_codes import public_error_payload

    quota = public_error_payload(
        EdennProviderResponseError(
            "provider request failed with HTTP 402",
            provider_name="provider_c",
            status_code=402,
        )
    )
    assert quota["error_code"] == 30500
    assert quota["retryable"] is False

    edge_522 = public_error_payload(
        EdennProviderResponseError(
            "provider request failed with HTTP 522",
            provider_name="provider_c",
            status_code=522,
        )
    )
    assert edge_522["error_code"] == 30500
    assert edge_522["retryable"] is True

    # A provider BODY code that merely looks like an HTTP 4xx must not flip
    # the flag — body codes are app-level, marked via context.response_code.
    body_code = public_error_payload(
        EdennProviderResponseError(
            "provider API error",
            provider_name="provider_c",
            status_code=455,
            context={"response_code": 455},
        )
    )
    assert body_code["retryable"] is True

    # Auth problems are ops-owned, not deterministic client rejections.
    auth = public_error_payload(
        EdennProviderAuthenticationError(
            "provider request failed with HTTP 401",
            provider_name="provider_c",
            status_code=401,
        )
    )
    assert auth["error_code"] == 30300
    assert auth["retryable"] is True

    rate_limit = public_error_payload(
        EdennProviderRateLimitError(
            "provider request failed with HTTP 429",
            provider_name="provider_c",
            status_code=429,
            retryable=True,
        )
    )
    assert rate_limit["error_code"] == 30100
    assert rate_limit["retryable"] is True

    timeout = public_error_payload(
        EdennProviderTimeoutError(
            "timed out waiting for provider task t",
            provider_name="provider_c",
            retryable=True,
        )
    )
    assert timeout["error_code"] == 30200
    assert timeout["retryable"] is True
