"""ProviderC API-key pool: parsing, rotate-only failover, and task->key binding.

Mirrors the ProviderB pool tests (test_provider_b_music_provider.py) for the numbered
PROVIDER_C_API_KEY_N scheme, plus the rotate-only behaviors that are ProviderC-specific:
no per-key lease, cooldown + failover on 429, and account-scoped follow-ups
pinned to the creating key (in-process binding or try-each-key cross-process).
"""
import asyncio
import logging
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from EdennCode.exceptions import EdennConfigurationError
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen import provider_c as provider_c_mod
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_c import (
    GenerateParams,
    ProviderCApi,
    _KEY_COOLDOWNS,
    has_provider_c_api_key_configured,
    provider_c_api_key_candidates_from_env,
)
from EdennCode.Deployment.music_callback_api_deployment.provider_c_callback_service import (
    ProviderCCallbackService,
)


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    _KEY_COOLDOWNS._state.clear()
    yield
    _KEY_COOLDOWNS._state.clear()


@pytest.fixture(autouse=True)
def _deterministic_key_order(monkeypatch):
    monkeypatch.setattr(provider_c_mod.random, "shuffle", lambda seq: None)


def _bearer(request: httpx.Request) -> str:
    return request.headers.get("Authorization", "").removeprefix("Bearer ")


def test_env_key_pool_loads_numbered_keys_before_legacy_key() -> None:
    with patch.dict(
        os.environ,
        {
            "PROVIDER_C_API_KEY": "legacy-key",
            "PROVIDER_C_API_KEY_1": "key-1",
            "PROVIDER_C_API_KEY_3": "key-3",
        },
        clear=True,
    ):
        assert provider_c_api_key_candidates_from_env() == [
            ("PROVIDER_C_API_KEY_1", "key-1"),
            ("PROVIDER_C_API_KEY_3", "key-3"),
            ("PROVIDER_C_API_KEY", "legacy-key"),
        ]
        assert has_provider_c_api_key_configured()
        api = ProviderCApi(base_url="https://provider-c.test/api/v1")
    assert [(k.label, k.value) for k in api._key_pool.keys] == [
        ("PROVIDER_C_API_KEY_1", "key-1"),
        ("PROVIDER_C_API_KEY_3", "key-3"),
        ("PROVIDER_C_API_KEY", "legacy-key"),
    ]
    assert api.api_key == "key-1"


def test_constructor_key_short_circuits_pool() -> None:
    with patch.dict(os.environ, {"PROVIDER_C_API_KEY_1": "pool-key"}, clear=True):
        api = ProviderCApi(api_key="explicit-key", base_url="https://provider-c.test/api/v1")
    assert [(k.label, k.value) for k in api._key_pool.keys] == [
        ("constructor_api_key", "explicit-key"),
    ]


def test_missing_keys_raise_configuration_error() -> None:
    with patch.dict(os.environ, {}, clear=True):
        assert not has_provider_c_api_key_configured()
        with pytest.raises(EdennConfigurationError):
            ProviderCApi(base_url="https://provider-c.test/api/v1")


def _pool_api(transport: httpx.MockTransport) -> ProviderCApi:
    return ProviderCApi(
        base_url="https://provider-c.test/api/v1",
        client=httpx.AsyncClient(transport=transport),
        max_retries=0,
    )


def test_generation_rotates_to_next_key_after_rate_limit(monkeypatch) -> None:
    async def _instant_sleep(_s):
        return None

    monkeypatch.setattr(provider_c_mod.asyncio, "sleep", _instant_sleep)
    poll_bearers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generate"):
            if _bearer(request) == "key-1":
                return httpx.Response(429, json={"code": 429, "msg": "rate limited"})
            return httpx.Response(200, json={"code": 200, "data": {"taskId": "task-9"}})
        if request.url.path.endswith("/generate/record-info"):
            poll_bearers.append(_bearer(request))
            return httpx.Response(200, json={
                "code": 200,
                "data": {
                    "status": "SUCCESS",
                    "response": {"provider_cData": [
                        {"id": "audio-9", "audioUrl": "https://cdn.test/a.mp3"},
                    ]},
                },
            })
        raise AssertionError(f"unexpected path {request.url.path}")

    with patch.dict(
        os.environ,
        {"PROVIDER_C_API_KEY_1": "key-1", "PROVIDER_C_API_KEY_2": "key-2"},
        clear=True,
    ):
        api = _pool_api(httpx.MockTransport(handler))
        task_id, result, _base = asyncio.run(
            api.generate_and_poll_tracks(GenerateParams(prompt="lofi"), timeout_s=5.0)
        )

    assert task_id == "task-9"
    assert result.tracks[0].audio_id == "audio-9"
    # Polling reused the key that created the task, never the cooled key-1.
    assert poll_bearers and set(poll_bearers) == {"key-2"}
    # The rate-limited key is cooling; the successful key is clean.
    assert _KEY_COOLDOWNS.remaining_s("PROVIDER_C_API_KEY_1") > 0
    assert _KEY_COOLDOWNS.remaining_s("PROVIDER_C_API_KEY_2") == 0
    # Both task and track ids are bound to the creating key for follow-ups.
    assert api._key_for_id("task-9").value == "key-2"
    assert api._key_for_id("audio-9").value == "key-2"
    assert api._headers_for("task-9")["Authorization"] == "Bearer key-2"


def test_attempt_order_puts_cooling_keys_last() -> None:
    with patch.dict(
        os.environ,
        {"PROVIDER_C_API_KEY_1": "key-1", "PROVIDER_C_API_KEY_2": "key-2"},
        clear=True,
    ):
        api = ProviderCApi(base_url="https://provider-c.test/api/v1")
    _KEY_COOLDOWNS.mark_rate_limited("PROVIDER_C_API_KEY_1")
    assert [k.label for k in api._attempt_keys()] == ["PROVIDER_C_API_KEY_2", "PROVIDER_C_API_KEY_1"]
    _KEY_COOLDOWNS.mark_success("PROVIDER_C_API_KEY_1")
    assert [k.label for k in api._attempt_keys()] == ["PROVIDER_C_API_KEY_1", "PROVIDER_C_API_KEY_2"]


def test_cooldown_backoff_is_exponential_and_capped(monkeypatch) -> None:
    monkeypatch.setenv("PROVIDER_C_KEY_COOLDOWN_BASE_S", "10")
    monkeypatch.setenv("PROVIDER_C_KEY_COOLDOWN_MAX_S", "25")
    assert _KEY_COOLDOWNS.mark_rate_limited("K") == 10
    assert _KEY_COOLDOWNS.mark_rate_limited("K") == 20
    assert _KEY_COOLDOWNS.mark_rate_limited("K") == 25  # capped
    _KEY_COOLDOWNS.mark_success("K")
    assert _KEY_COOLDOWNS.remaining_s("K") == 0


def test_timestamped_lyrics_tries_each_key_when_unbound() -> None:
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(_bearer(request))
        if _bearer(request) == "key-1":
            # HTTP 200 but the body says this key does not own the task.
            return httpx.Response(200, json={"code": 404, "msg": "task not found"})
        return httpx.Response(200, json={
            "code": 200,
            "data": {"alignedWords": [
                {"word": "hi", "startS": 0.0, "endS": 0.4, "success": True, "palign": 0},
            ]},
        })

    with patch.dict(
        os.environ,
        {"PROVIDER_C_API_KEY_1": "key-1", "PROVIDER_C_API_KEY_2": "key-2"},
        clear=True,
    ):
        api = _pool_api(httpx.MockTransport(handler))
        words = asyncio.run(api.get_timestamped_lyrics("foreign-task", "foreign-audio"))

    assert tried == ["key-1", "key-2"]
    assert [w.text for w in words] == ["hi"]
    # The discovered owner is remembered for subsequent calls.
    assert api._key_for_id("foreign-task").value == "key-2"


def test_timestamped_lyrics_skips_silent_non_owner_key() -> None:
    """Live-verified aggregator behavior: a non-owner key answers 200/200 with
    an EMPTY record instead of an error. The fallback must keep trying."""
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(_bearer(request))
        if _bearer(request) == "key-1":
            # Silent non-owner: success shape, no words.
            return httpx.Response(200, json={"code": 200, "data": {"alignedWords": []}})
        return httpx.Response(200, json={
            "code": 200,
            "data": {"alignedWords": [
                {"word": "hi", "startS": 0.0, "endS": 0.4, "success": True, "palign": 0},
            ]},
        })

    with patch.dict(
        os.environ,
        {"PROVIDER_C_API_KEY_1": "key-1", "PROVIDER_C_API_KEY_2": "key-2"},
        clear=True,
    ):
        api = _pool_api(httpx.MockTransport(handler))
        words = asyncio.run(api.get_timestamped_lyrics("foreign-task", "foreign-audio"))

    assert tried == ["key-1", "key-2"]
    assert [w.text for w in words] == ["hi"]


def test_timestamped_lyrics_empty_from_all_keys_is_returned_as_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 200, "data": {"alignedWords": []}})

    with patch.dict(
        os.environ,
        {"PROVIDER_C_API_KEY_1": "key-1", "PROVIDER_C_API_KEY_2": "key-2"},
        clear=True,
    ):
        api = _pool_api(httpx.MockTransport(handler))
        words = asyncio.run(api.get_timestamped_lyrics("foreign-task", "foreign-audio"))

    assert words == []


def test_single_key_pool_behaves_like_legacy_client() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_bearer(request))
        return httpx.Response(200, json={
            "code": 200,
            "data": {"alignedWords": []},
        })

    with patch.dict(os.environ, {"PROVIDER_C_API_KEY": "only-key"}, clear=True):
        api = _pool_api(httpx.MockTransport(handler))
        asyncio.run(api.get_timestamped_lyrics("t", "a"))

    assert seen == ["only-key"]


def test_callback_service_candidate_keys_without_provider_c_module(tmp_path) -> None:
    service = ProviderCCallbackService(
        base_dir=tmp_path, logger=logging.getLogger("test"), provider_c_module=None
    )
    with patch.dict(
        os.environ,
        {"PROVIDER_C_API_KEY": "legacy", "PROVIDER_C_API_KEY_2": "key-2", "PROVIDER_C_API_KEY_10": "key-10"},
        clear=True,
    ):
        assert service._candidate_api_keys() == [
            ("PROVIDER_C_API_KEY_2", "key-2"),
            ("PROVIDER_C_API_KEY_10", "key-10"),
            ("PROVIDER_C_API_KEY", "legacy"),
        ]


def test_callback_service_lyrics_record_retries_across_keys(tmp_path) -> None:
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(_bearer(request))
        if _bearer(request) == "key-1":
            return httpx.Response(401, json={"msg": "wrong account"})
        return httpx.Response(200, json={"code": 200, "data": {"status": "SUCCESS"}})

    service = ProviderCCallbackService(
        base_dir=tmp_path, logger=logging.getLogger("test"), provider_c_module=None
    )

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await service._fetch_lyrics_record("task-x", client, timeout_s=5.0)

    with patch.dict(
        os.environ,
        {"PROVIDER_C_API_KEY_1": "key-1", "PROVIDER_C_API_KEY_2": "key-2"},
        clear=True,
    ):
        body = asyncio.run(_run())

    assert tried == ["key-1", "key-2"]
    assert body == {"code": 200, "data": {"status": "SUCCESS"}}


def test_callback_service_lyrics_record_skips_silent_non_owner(tmp_path) -> None:
    """Non-owner keys answer 200/200 with data=null (live-verified)."""
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(_bearer(request))
        if _bearer(request) == "key-1":
            return httpx.Response(200, json={"code": 200, "data": None})
        return httpx.Response(200, json={"code": 200, "data": {"status": "SUCCESS"}})

    service = ProviderCCallbackService(
        base_dir=tmp_path, logger=logging.getLogger("test"), provider_c_module=None
    )

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await service._fetch_lyrics_record("task-x", client, timeout_s=5.0)

    with patch.dict(
        os.environ,
        {"PROVIDER_C_API_KEY_1": "key-1", "PROVIDER_C_API_KEY_2": "key-2"},
        clear=True,
    ):
        body = asyncio.run(_run())

    assert tried == ["key-1", "key-2"]
    assert body == {"code": 200, "data": {"status": "SUCCESS"}}
