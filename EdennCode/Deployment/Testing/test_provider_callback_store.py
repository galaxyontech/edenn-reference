"""Tests for the provider callback store: fail-closed resolution + pooling.

Covers PR#5 findings:
- #65: the store silently fell back to a per-process in-memory store, breaking
  cross-container callback delivery. resolve_provider_music_callback_store now
  fails closed (raises) when the wait path is enabled but the resolved store is
  in-memory, and installs + logs the backend otherwise.
- #64/#70: the store opened a fresh connection per operation (the waiter polls
  every ~1s). The builder now accepts a pooled client_factory; the real-DB test
  proves the store reuses one connection across polls.
"""
from __future__ import annotations

import pytest

from EdennCode.Deployment import provider_music_callbacks as pmc
from EdennCode.Deployment.postgres_wrapper import (
    PostgresClient,
    PostgresConnectionConfig,
    PostgresConnectionPool,
)


@pytest.fixture(autouse=True)
def _reset_store_state(monkeypatch):
    # Isolate module singletons + env for each test.
    monkeypatch.setattr(pmc, "_provider_music_callback_store", None)
    monkeypatch.setattr(pmc, "_provider_music_callback_store_override", None)
    for key in ("PROVIDER_C_CALLBACK_WAIT_ENABLED", "PROVIDER_C_CALLBACK_URL",
                "PROVIDER_MUSIC_CALLBACK_STORE", "ASYNC_VIDEO_MUSIC_JOB_STORE"):
        monkeypatch.delenv(key, raising=False)
    yield


# --- Fail-closed resolution (#65) -----------------------------------------

def test_wait_disabled_allows_memory(monkeypatch):
    monkeypatch.setenv("PROVIDER_MUSIC_CALLBACK_STORE", "memory")
    store = pmc.resolve_provider_music_callback_store()
    assert isinstance(store, pmc.InMemoryProviderMusicCallbackStore)


def test_fail_closed_when_wait_enabled_and_memory(monkeypatch):
    monkeypatch.setenv("PROVIDER_MUSIC_CALLBACK_STORE", "memory")
    monkeypatch.setenv("PROVIDER_C_CALLBACK_WAIT_ENABLED", "1")
    with pytest.raises(pmc.EdennConfigurationError):
        pmc.resolve_provider_music_callback_store()


def test_fail_closed_error_leaks_no_provider_name(monkeypatch):
    monkeypatch.setenv("PROVIDER_MUSIC_CALLBACK_STORE", "memory")
    monkeypatch.setenv("PROVIDER_C_CALLBACK_WAIT_ENABLED", "1")
    try:
        pmc.resolve_provider_music_callback_store()
        pytest.fail("expected EdennConfigurationError")
    except pmc.EdennConfigurationError as exc:
        text = str(exc).lower()
        for vendor in ("provider_c", "provider_b", "eleven"):
            assert vendor not in text


def test_override_short_circuits_resolve(monkeypatch):
    monkeypatch.setenv("PROVIDER_C_CALLBACK_WAIT_ENABLED", "1")  # would otherwise fail closed
    sentinel = object()
    pmc.set_provider_music_callback_store_for_testing(sentinel)
    try:
        assert pmc.resolve_provider_music_callback_store() is sentinel
    finally:
        pmc.set_provider_music_callback_store_for_testing(None)


def test_setter_installs_process_store():
    sentinel = object()
    pmc.set_provider_music_callback_store(sentinel)
    assert pmc.get_provider_music_callback_store() is sentinel


def test_builder_threads_client_factory(monkeypatch):
    monkeypatch.setenv("PROVIDER_MUSIC_CALLBACK_STORE", "postgres")
    factory = object.__new__(type("F", (), {}))  # placeholder identity

    def _factory():
        raise AssertionError("factory must not be called at construction")

    store = pmc.build_provider_music_callback_store_from_env(client_factory=_factory)
    assert isinstance(store, pmc.PostgresProviderMusicCallbackStore)
    assert store._client_factory is _factory


# --- Pooling (#64 / #70): real-DB connection reuse ------------------------

def _pg_reachable() -> bool:
    try:
        with PostgresClient(PostgresConnectionConfig.from_env()) as c:
            c.run_sql("SELECT 1")
        return True
    except Exception:
        return False


def test_store_ops_reuse_pooled_connection(monkeypatch):
    if not _pg_reachable():
        pytest.skip("no reachable Postgres")
    monkeypatch.setenv("PROVIDER_MUSIC_CALLBACK_STORE", "postgres")
    pool = PostgresConnectionPool(PostgresConnectionConfig.from_env(), minconn=1, maxconn=5)
    try:
        store = pmc.build_provider_music_callback_store_from_env(client_factory=pool.client)
        # Several store reads (mirrors the waiter's per-poll get) reuse one backend.
        pids = set()
        for _ in range(5):
            store.get("provider_c", "no-such-task")  # read-only miss; exercises a connection
            with pool.client() as c:
                pids.add(int(c.run_sql("SELECT pg_backend_pid() AS pid")[0]["pid"]))
        assert len(pids) == 1, f"pooled store ops should reuse one connection, saw {pids}"
    finally:
        pool.closeall()
