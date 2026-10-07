import asyncio
import os
import time
from unittest.mock import patch

import pytest

from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen import provider_b_music
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_music import (
    ProviderBMusicProvider,
    ProviderBTask,
)


def test_env_key_pool_loads_numbered_keys_before_legacy_key() -> None:
    with patch.dict(
        os.environ,
        {
            "PROVIDER_B_API_KEY": "default-key",
            "PROVIDER_B_API_KEY_1": "key-1",
            "PROVIDER_B_API_KEY_2": "key-2",
            "PROVIDER_B_API_KEY_4": "key-4",
        },
        clear=True,
    ):
        provider = ProviderBMusicProvider()

    keys = provider._key_pool.all_keys()
    assert [(key.label, key.value) for key in keys] == [
        ("PROVIDER_B_API_KEY_1", "key-1"),
        ("PROVIDER_B_API_KEY_2", "key-2"),
        ("PROVIDER_B_API_KEY_4", "key-4"),
        ("PROVIDER_B_API_KEY", "default-key"),
    ]


def test_clone_vocal_uses_vocal_upload(monkeypatch, tmp_path) -> None:
    audio_path = tmp_path / "sample.m4a"
    audio_path.write_bytes(b"test")
    provider = ProviderBMusicProvider(api_key="test-key")
    calls: list[tuple[str, str]] = []

    async def _fake_upload_audio_file(path, *, purpose: str = "audio") -> str:
        calls.append((str(path), purpose))
        return "vocal_123"

    monkeypatch.setattr(provider, "upload_audio_file", _fake_upload_audio_file)

    result = asyncio.run(provider.clone_vocal(audio_path))

    assert result == "vocal_123"
    assert calls == [(str(audio_path), "vocal")]


def test_request_json_uses_constructor_key_for_get(monkeypatch) -> None:
    provider = ProviderBMusicProvider(
        api_key="test-key",
        base_url="https://example.test",
        max_retries=0,
    )
    provider._credit_cache["constructor_api_key"] = (10000, time.time())
    calls: list[dict] = []

    class _FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"id": "task_123", "status": "succeeded"}

    class _FakeAsyncClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def request(self, method, url, *, headers, **kwargs):
            calls.append(
                {
                    "method": method,
                    "url": url,
                    "headers": headers,
                    "kwargs": kwargs,
                }
            )
            return _FakeResponse()

    monkeypatch.setattr(provider_b_music.httpx, "AsyncClient", _FakeAsyncClient)

    async def _run():
        async with provider._cycle():
            return await provider._request_json("GET", "/v1/song/query/task_123")

    result = asyncio.run(_run())

    assert result == {"id": "task_123", "status": "succeeded"}
    assert calls == [
        {
            "method": "GET",
            "url": "https://example.test/v1/song/query/task_123",
            "headers": {"Authorization": "Bearer test-key"},
            "kwargs": {},
        }
    ]


def test_upload_audio_file_uses_constructor_key_without_json_content_type(monkeypatch, tmp_path) -> None:
    audio_path = tmp_path / "sample.m4a"
    audio_path.write_bytes(b"test")
    provider = ProviderBMusicProvider(api_key="test-key", base_url="https://example.test")
    provider._credit_cache["constructor_api_key"] = (10000, time.time())
    calls: list[dict] = []

    class _FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"id": "file_123"}

    class _FakeAsyncClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, url, *, headers, files):
            calls.append(
                {
                    "url": url,
                    "headers": headers,
                    "purpose": files["purpose"],
                    "filename": files["file"][0],
                }
            )
            return _FakeResponse()

    monkeypatch.setattr(provider_b_music.httpx, "AsyncClient", _FakeAsyncClient)

    result = asyncio.run(provider.upload_audio_file(audio_path, purpose="melody"))

    assert result == "file_123"
    assert calls == [
        {
            "url": "https://example.test/v1/files/upload",
            "headers": {"Authorization": "Bearer test-key"},
            "purpose": (None, "melody"),
            "filename": "sample.m4a",
        }
    ]
    assert provider._file_key("file_123").label == "constructor_api_key"


def test_pick_key_for_cycle_skips_low_balance_and_warns_on_low_served_key(monkeypatch) -> None:
    provider = ProviderBMusicProvider(api_key="test-key")
    provider._key_pool = provider_b_music._ProviderBKeyPool(
        [
            provider_b_music._ProviderBKey("low", "low-key"),
            provider_b_music._ProviderBKey("served", "served-key"),
        ]
    )
    balances = {"low": 500, "served": 1200}

    async def _fake_fetch_balance(api_key):
        return balances[api_key.label]

    monkeypatch.setattr(provider, "_fetch_balance_fen", _fake_fetch_balance)
    monkeypatch.setattr(provider_b_music.random, "shuffle", lambda _keys: None)

    token = provider.begin_warning_collection()
    try:
        selected = asyncio.run(provider._pick_key_for_cycle())
        warning = provider.finish_warning_collection(token)
    except Exception:
        provider.finish_warning_collection(token)
        raise

    assert selected.label == "served"
    assert warning is not None
    assert "served" in warning
    assert "¥12.00" in warning


def test_all_keys_dry_raises_provider_error(monkeypatch) -> None:
    provider = ProviderBMusicProvider(api_key="test-key")

    async def _fake_fetch_balance(_api_key):
        return 500

    monkeypatch.setattr(provider, "_fetch_balance_fen", _fake_fetch_balance)

    with pytest.raises(Exception, match="all provider_b keys exhausted"):
        asyncio.run(provider._pick_key_for_cycle())


def test_generate_song_task_binds_task_to_current_cycle_key(monkeypatch) -> None:
    provider = ProviderBMusicProvider(api_key="test-key")
    provider._credit_cache["constructor_api_key"] = (10000, time.time())
    seen_keys: list[str] = []

    async def _fake_request_json(method, path, *, payload=None, api_key=None):
        seen_keys.append(api_key.label)
        assert method == "POST"
        assert path == "/v1/song/generate"
        assert payload["lyrics"] == "hello"
        return {"id": "task_123", "status": "queued"}

    monkeypatch.setattr(provider, "_request_json", _fake_request_json)

    task = asyncio.run(provider.generate_song_task(lyrics="hello", prompt="pop"))

    assert isinstance(task, ProviderBTask)
    assert task.task_id == "task_123"
    assert task.api_key_label == "constructor_api_key"
    assert provider._task_key("task_123").label == "constructor_api_key"
    assert seen_keys == ["constructor_api_key"]


def test_extend_song_task_sends_tail_extend_position(monkeypatch) -> None:
    provider = ProviderBMusicProvider(api_key="test-key")
    provider._credit_cache["constructor_api_key"] = (10000, time.time())
    seen_payloads: list[dict] = []

    async def _fake_request_json(method, path, *, payload=None, api_key=None):
        seen_payloads.append(payload)
        assert api_key.label == "constructor_api_key"
        assert method == "POST"
        assert path == "/v1/song/extend"
        return {"id": "task_extend_123", "status": "queued"}

    monkeypatch.setattr(provider, "_request_json", _fake_request_json)

    task = asyncio.run(
        provider.extend_song_task(
            upload_audio_id="file_123",
            lyrics="extended lyrics",
            prompt="cinematic pop",
            model="provider_b-8",
            n=4,
            extend_type="tail",
            extend_at_ms=156_080,
        )
    )

    assert task.task_id == "task_extend_123"
    assert seen_payloads == [
        {
            "upload_audio_id": "file_123",
            "lyrics": "extended lyrics",
            "model": "provider_b-8",
            "n": 3,
            "extend_type": "tail",
            "extend_at": 156_080,
            "prompt": "cinematic pop",
        }
    ]


def test_extend_song_task_clamps_extend_position_to_provider_range(monkeypatch) -> None:
    provider = ProviderBMusicProvider(api_key="test-key")
    provider._credit_cache["constructor_api_key"] = (10000, time.time())
    seen_payloads: list[dict] = []

    async def _fake_request_json(method, path, *, payload=None, api_key=None):
        seen_payloads.append(payload)
        return {"id": "task_extend_123", "status": "queued"}

    monkeypatch.setattr(provider, "_request_json", _fake_request_json)

    asyncio.run(
        provider.extend_song_task(
            upload_audio_id="file_123",
            lyrics="extended lyrics",
            extend_at_ms=4_000,
        )
    )
    asyncio.run(
        provider.extend_song_task(
            upload_audio_id="file_456",
            lyrics="extended lyrics",
            extend_at_ms=900_000,
        )
    )

    assert [payload["extend_at"] for payload in seen_payloads] == [8_000, 420_000]


def test_extend_song_task_rejects_unknown_extend_type() -> None:
    provider = ProviderBMusicProvider(api_key="test-key")

    with pytest.raises(provider_b_music.EdennValidationError, match="extend_type"):
        asyncio.run(
            provider.extend_song_task(
                upload_audio_id="file_123",
                lyrics="extended lyrics",
                extend_type="middle",
            )
        )


def test_vocal_id_binding_forces_later_generation_key(monkeypatch, tmp_path) -> None:
    provider = ProviderBMusicProvider(api_key="test-key")
    other_key = provider_b_music._ProviderBKey("other", "other-key")
    provider._file_api_keys["vocal_123"] = other_key
    provider._credit_cache["other"] = (10000, time.time())
    seen_required_labels: list[str] = []

    async def _fake_generate_in_cycle(*_args, **_kwargs):
        seen_required_labels.append(provider._current_key_or_none().label)
        timestamps = provider_b_music.ProviderBTimestampedLyrics(line_level=[], word_level=[])
        return tmp_path / "song.mp3", None, timestamps, None

    monkeypatch.setattr(
        provider,
        "_generate_with_variants_detailed_in_cycle",
        _fake_generate_in_cycle,
    )

    asyncio.run(
        provider.generate_with_variants_detailed(
            "pop",
            lyrics_prompt="lyrics",
            vocal_id="vocal_123",
        )
    )

    assert seen_required_labels == ["other"]


def test_required_key_below_threshold_is_retryable(monkeypatch) -> None:
    provider = ProviderBMusicProvider(api_key="test-key")
    bound_key = provider_b_music._ProviderBKey("bound", "bound-key")

    async def _fake_fetch_balance(_api_key):
        return 400

    monkeypatch.setattr(provider, "_fetch_balance_fen", _fake_fetch_balance)

    with pytest.raises(Exception) as exc_info:
        asyncio.run(provider._pick_key_for_cycle(required_key=bound_key))

    assert "bound" in str(exc_info.value)
    assert "below credit threshold" in str(exc_info.value)
    assert getattr(exc_info.value, "retryable", False) is True


def test_credit_cache_hit_skips_billing_call_within_ttl(monkeypatch) -> None:
    provider = ProviderBMusicProvider(api_key="test-key")
    fetch_calls: list[str] = []

    async def _fake_fetch_balance(api_key):
        fetch_calls.append(api_key.label)
        return 9000

    monkeypatch.setattr(provider, "_fetch_balance_fen", _fake_fetch_balance)
    monkeypatch.setattr(provider_b_music.random, "shuffle", lambda _keys: None)

    async def _two_cycles_back_to_back():
        async with provider._cycle():
            pass
        async with provider._cycle():
            pass

    asyncio.run(_two_cycles_back_to_back())

    assert fetch_calls == ["constructor_api_key"]


def test_credit_cache_refetches_after_ttl_expires(monkeypatch) -> None:
    provider = ProviderBMusicProvider(api_key="test-key")
    provider._credit_cache_ttl_s = 0.0
    fetch_calls: list[str] = []

    async def _fake_fetch_balance(api_key):
        fetch_calls.append(api_key.label)
        return 9000

    monkeypatch.setattr(provider, "_fetch_balance_fen", _fake_fetch_balance)

    async def _two_cycles_back_to_back():
        async with provider._cycle():
            pass
        async with provider._cycle():
            pass

    asyncio.run(_two_cycles_back_to_back())

    assert fetch_calls == ["constructor_api_key", "constructor_api_key"]


def test_concurrent_cycles_isolate_their_keys(monkeypatch) -> None:
    provider = ProviderBMusicProvider(api_key="test-key")
    provider._key_pool = provider_b_music._ProviderBKeyPool(
        [
            provider_b_music._ProviderBKey("alpha", "alpha-key"),
            provider_b_music._ProviderBKey("beta", "beta-key"),
        ]
    )

    async def _fake_fetch_balance(_api_key):
        return 9000

    monkeypatch.setattr(provider, "_fetch_balance_fen", _fake_fetch_balance)

    # Force the two concurrent picks to land on different keys.
    shuffle_orderings = iter([["alpha", "beta"], ["beta", "alpha"]])

    def _deterministic_shuffle(keys: list) -> None:
        order = next(shuffle_orderings)
        keys.sort(key=lambda key: order.index(key.label))

    monkeypatch.setattr(provider_b_music.random, "shuffle", _deterministic_shuffle)

    async def _cycle_observation():
        async with provider._cycle():
            # Yield twice so the two tasks interleave inside their cycles.
            initial_key = provider._current_key_or_none().label
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            final_key = provider._current_key_or_none().label
            return initial_key, final_key

    async def _run_pair():
        return await asyncio.gather(_cycle_observation(), _cycle_observation())

    results = asyncio.run(_run_pair())

    labels = {observation[0] for observation in results}
    assert labels == {"alpha", "beta"}
    for initial, final in results:
        assert initial == final


class _FakeKeyLock:
    """In-memory key lock used to verify _cycle interacts with the lock correctly."""

    def __init__(self) -> None:
        self._held: set[str] = set()
        self.acquired_log: list[str] = []
        self.release_log: list[str] = []

    async def acquire_one_of(self, labels, *, timeout_s=600.0, poll_interval_s=0.01):
        for label in labels:
            if label not in self._held:
                self._held.add(label)
                self.acquired_log.append(label)
                return label, _FakeLockHandle(self, label)
        raise provider_b_music.EdennProviderTimeoutError(
            "fake lock: all candidate labels busy",
            provider_name="provider_b",
            operation="acquire_provider_b_key_lock",
            retryable=True,
        )

    async def acquire(self, label, *, timeout_s=600.0, poll_interval_s=0.01):
        if label in self._held:
            raise provider_b_music.EdennProviderTimeoutError(
                "fake lock: required label busy",
                provider_name="provider_b",
                operation="acquire_provider_b_key_lock",
                retryable=True,
            )
        self._held.add(label)
        self.acquired_log.append(label)
        return _FakeLockHandle(self, label)


class _FakeLockHandle:
    def __init__(self, lock: _FakeKeyLock, label: str) -> None:
        self._lock = lock
        self._label = label
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._lock._held.discard(self._label)
        self._lock.release_log.append(self._label)


def _make_provider_with_keys_and_lock(labels: list[str], lock) -> ProviderBMusicProvider:
    provider = ProviderBMusicProvider(api_key="test-key", key_lock=lock)
    provider._key_pool = provider_b_music._ProviderBKeyPool(
        [provider_b_music._ProviderBKey(label, f"{label}-value") for label in labels]
    )
    for label in labels:
        provider._credit_cache[label] = (10_000, time.time())
    return provider


def test_cycle_acquires_and_releases_lock_on_selected_key() -> None:
    lock = _FakeKeyLock()
    provider = _make_provider_with_keys_and_lock(["alpha"], lock)

    async def _run():
        async with provider._cycle() as key:
            assert key.label == "alpha"
            assert lock._held == {"alpha"}

    asyncio.run(_run())

    assert lock.acquired_log == ["alpha"]
    assert lock.release_log == ["alpha"]
    assert lock._held == set()


def test_cycle_skips_busy_keys_and_picks_a_free_one(monkeypatch) -> None:
    lock = _FakeKeyLock()
    provider = _make_provider_with_keys_and_lock(["alpha", "beta"], lock)
    monkeypatch.setattr(provider_b_music.random, "shuffle", lambda _keys: None)
    lock._held.add("alpha")  # simulate alpha already in use

    async def _run():
        async with provider._cycle() as key:
            assert key.label == "beta"

    asyncio.run(_run())

    assert lock.acquired_log == ["beta"]
    assert lock.release_log == ["beta"]


def test_cycle_skips_rate_limited_cooldown_key(monkeypatch) -> None:
    lock = _FakeKeyLock()
    provider = _make_provider_with_keys_and_lock(["alpha", "beta"], lock)
    provider._key_health_store = provider_b_music.InMemoryProviderBKeyHealthStore(
        cooldown_base_s=60,
        cooldown_max_s=60,
    )
    monkeypatch.setattr(provider_b_music.random, "shuffle", lambda _keys: None)

    async def _run():
        await provider._key_health_store.mark_rate_limited(
            "alpha",
            reason="test 429",
        )
        async with provider._cycle() as key:
            assert key.label == "beta"

    asyncio.run(_run())

    assert lock.acquired_log == ["beta"]
    assert lock.release_log == ["beta"]


def test_cycle_rechecks_cooldown_after_lock_acquisition(monkeypatch) -> None:
    class _HealthStore:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def cooldown_remaining_s(self, label: str) -> float:
            self.calls.append(label)
            if label == "alpha" and self.calls.count("alpha") >= 2:
                return 60.0
            return 0.0

        async def mark_rate_limited(self, label: str, *, reason: str = "") -> float:
            return 60.0

        async def mark_success(self, label: str) -> None:
            return None

    lock = _FakeKeyLock()
    provider = _make_provider_with_keys_and_lock(["alpha", "beta"], lock)
    provider._key_health_store = _HealthStore()
    monkeypatch.setattr(provider_b_music.random, "shuffle", lambda _keys: None)

    async def _run():
        async with provider._cycle() as key:
            assert key.label == "beta"

    asyncio.run(_run())

    assert lock.acquired_log == ["alpha", "beta"]
    assert lock.release_log == ["alpha", "beta"]


def test_cycle_required_key_uses_single_label_acquire() -> None:
    lock = _FakeKeyLock()
    provider = _make_provider_with_keys_and_lock(["alpha", "beta"], lock)
    required = next(
        k for k in provider._key_pool.all_keys() if k.label == "beta"
    )

    async def _run():
        async with provider._cycle(required_key=required) as key:
            assert key.label == "beta"

    asyncio.run(_run())

    assert lock.acquired_log == ["beta"]
    assert lock.release_log == ["beta"]


def test_cycle_lock_timeout_surfaces_as_retryable_provider_error() -> None:
    lock = _FakeKeyLock()
    provider = _make_provider_with_keys_and_lock(["alpha"], lock)
    lock._held.add("alpha")  # all candidates already held

    async def _run():
        async with provider._cycle():
            pass

    with pytest.raises(provider_b_music.EdennProviderTimeoutError) as exc_info:
        asyncio.run(_run())
    assert getattr(exc_info.value, "retryable", False) is True


def test_cycle_reentrance_does_not_acquire_nested_lock() -> None:
    lock = _FakeKeyLock()
    provider = _make_provider_with_keys_and_lock(["alpha"], lock)

    async def _run():
        async with provider._cycle() as outer:
            assert outer.label == "alpha"
            async with provider._cycle() as inner:
                assert inner.label == "alpha"

    asyncio.run(_run())

    # Lock acquired/released exactly once across outer+inner cycles.
    assert lock.acquired_log == ["alpha"]
    assert lock.release_log == ["alpha"]


def test_generate_with_variants_cools_rate_limited_key_and_retries(monkeypatch, tmp_path) -> None:
    class _AssertingHealthStore(provider_b_music.InMemoryProviderBKeyHealthStore):
        def __init__(self, lock: _FakeKeyLock) -> None:
            super().__init__(cooldown_base_s=60, cooldown_max_s=60)
            self._key_lock = lock
            self.marked_while_held: list[bool] = []

        async def mark_rate_limited(self, label: str, *, reason: str = "") -> float:
            self.marked_while_held.append(label in self._key_lock._held)
            return await super().mark_rate_limited(label, reason=reason)

    lock = _FakeKeyLock()
    provider = _make_provider_with_keys_and_lock(["alpha", "beta"], lock)
    health_store = _AssertingHealthStore(lock)
    provider._key_health_store = health_store
    monkeypatch.setattr(provider_b_music.random, "shuffle", lambda _keys: None)
    calls: list[str] = []

    async def _fake_generate_in_cycle(*_args, **_kwargs):
        current = provider._current_key_or_none()
        assert current is not None
        calls.append(current.label)
        if current.label == "alpha":
            raise provider_b_music.EdennProviderRateLimitError(
                "ProviderB request failed with HTTP 429",
                provider_name="provider_b",
                operation="POST /v1/lyrics/generate",
                status_code=429,
                retryable=True,
                context={"api_key_label": current.label},
            )
        timestamps = provider_b_music.ProviderBTimestampedLyrics(
            line_level=[],
            word_level=[],
        )
        return tmp_path / "beta.mp3", None, timestamps, None

    monkeypatch.setattr(
        provider,
        "_generate_with_variants_detailed_in_cycle",
        _fake_generate_in_cycle,
    )

    result = asyncio.run(
        provider.generate_with_variants_detailed(
            "pop",
            lyrics_prompt="lyrics",
            save_sidecar=False,
        )
    )

    assert result[0] == tmp_path / "beta.mp3"
    assert calls == ["alpha", "beta"]
    assert lock.acquired_log == ["alpha", "beta"]
    assert lock.release_log == ["alpha", "beta"]
    snapshot = provider._key_health_store.snapshot()
    assert snapshot["alpha"]["cooldown_remaining_s"] > 0
    assert health_store.marked_while_held == [True]


def test_all_cooled_keys_are_exhausted(monkeypatch) -> None:
    lock = _FakeKeyLock()
    provider = _make_provider_with_keys_and_lock(["alpha", "beta"], lock)
    provider._key_health_store = provider_b_music.InMemoryProviderBKeyHealthStore(
        cooldown_base_s=60,
        cooldown_max_s=60,
    )
    monkeypatch.setattr(provider_b_music.random, "shuffle", lambda _keys: None)

    async def _run():
        await provider._key_health_store.mark_rate_limited("alpha", reason="test")
        await provider._key_health_store.mark_rate_limited("beta", reason="test")
        async with provider._cycle():
            pass

    with pytest.raises(Exception, match="all provider_b keys exhausted") as exc_info:
        asyncio.run(_run())

    skipped = getattr(exc_info.value, "context", {}).get("skipped", [])
    assert [item["reason"] for item in skipped] == [
        "rate_limited_cooldown",
        "rate_limited_cooldown",
    ]
    assert lock.acquired_log == []


def test_cycle_releases_lock_even_when_body_raises() -> None:
    lock = _FakeKeyLock()
    provider = _make_provider_with_keys_and_lock(["alpha"], lock)

    class _BodyBoom(Exception):
        pass

    async def _run():
        async with provider._cycle():
            raise _BodyBoom("intentional")

    with pytest.raises(_BodyBoom):
        asyncio.run(_run())

    assert lock.release_log == ["alpha"]
    assert lock._held == set()


def test_concurrent_cycles_acquire_distinct_keys(monkeypatch) -> None:
    lock = _FakeKeyLock()
    provider = _make_provider_with_keys_and_lock(["alpha", "beta"], lock)
    monkeypatch.setattr(provider_b_music.random, "shuffle", lambda _keys: None)

    async def _cycle_observation():
        async with provider._cycle() as key:
            # Allow the other task to enter its own cycle.
            await asyncio.sleep(0)
            return key.label

    async def _run_pair():
        return await asyncio.gather(_cycle_observation(), _cycle_observation())

    results = asyncio.run(_run_pair())

    assert set(results) == {"alpha", "beta"}
    assert sorted(lock.acquired_log) == ["alpha", "beta"]
    assert sorted(lock.release_log) == ["alpha", "beta"]


def test_default_null_key_lock_preserves_existing_cycle_behavior() -> None:
    """Regression guard: existing tests construct ProviderBMusicProvider without a
    lock, so the default NullKeyLock must keep them working unchanged.
    """
    provider = ProviderBMusicProvider(api_key="test-key")
    provider._credit_cache["constructor_api_key"] = (10_000, time.time())

    async def _run():
        async with provider._cycle() as key:
            return key.label

    assert asyncio.run(_run()) == "constructor_api_key"


def test_query_song_task_reuses_bound_task_key(monkeypatch) -> None:
    provider = ProviderBMusicProvider(api_key="test-key")
    other_key = provider_b_music._ProviderBKey("other", "other-key")
    provider._task_api_keys["task_123"] = other_key
    provider._credit_cache["other"] = (10000, time.time())
    seen_keys: list[str] = []

    async def _fake_request_json(method, path, *, payload=None, api_key=None):
        seen_keys.append(api_key.label)
        assert method == "GET"
        assert path == "/v1/song/query/task_123"
        return {"id": "task_123", "status": "succeeded"}

    monkeypatch.setattr(provider, "_request_json", _fake_request_json)

    task = asyncio.run(provider.query_song_task("task_123"))

    assert task.task_id == "task_123"
    assert task.api_key_label == "other"
    assert seen_keys == ["other"]
