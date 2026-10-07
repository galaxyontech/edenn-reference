"""Tests for the process-wide API Postgres connection pool.

Covers finding #67: the v2 API tier opened a fresh TLS Postgres connection for
every repository/queue call. The fix routes those calls through a lazily-built
process-wide ``PostgresConnectionPool`` so borrows reuse a warm connection.

- The unit tests run without a database and assert the pool is lazy (never built
  until the first borrow), a process singleton, and that ``api_pg_client`` is a
  valid drop-in ``client_factory``.
- ``test_pooled_borrows_reuse_one_connection`` is a real-database integration
  test (skipped when no Postgres is reachable) that proves sequential borrows
  reuse the *same* backend connection, whereas ``PostgresClient.from_env`` opens
  a new one each time.
"""
from __future__ import annotations

import pytest

from EdennCode.Deployment import shared_pg_pool
from EdennCode.Deployment.postgres_wrapper import (
    PostgresClient,
    PostgresConnectionConfig,
)
from EdennCode.Deployment.async_pipeline_v2.repositories import AsyncPipelineV2Repository
from EdennCode.Deployment.async_pipeline_v2.queue.postgres_queue import PostgresTaskQueue


@pytest.fixture(autouse=True)
def _reset_pool():
    shared_pg_pool.reset_api_pg_pool_for_tests()
    yield
    shared_pg_pool.reset_api_pg_pool_for_tests()


# --- No-DB unit tests ------------------------------------------------------

def test_pool_is_lazy_singleton(monkeypatch) -> None:
    built = {"count": 0}

    class _FakePool:
        def __init__(self, config, *, minconn, maxconn):
            built["count"] += 1
            self.minconn = minconn
            self.maxconn = maxconn
            self.borrows = 0

        def client(self):
            self.borrows += 1
            return f"client-{self.borrows}"

    # No pool is built just by importing / wiring.
    assert shared_pg_pool._pool is None
    monkeypatch.setattr(shared_pg_pool, "PostgresConnectionPool", _FakePool)
    monkeypatch.setattr(
        shared_pg_pool, "PostgresConnectionConfig",
        type("C", (), {"from_env": staticmethod(lambda: object())}),
    )

    c1 = shared_pg_pool.api_pg_client()
    c2 = shared_pg_pool.api_pg_client()

    assert built["count"] == 1, "pool must be built exactly once (lazy singleton)"
    assert (c1, c2) == ("client-1", "client-2")
    # minconn=1 (like the workers) so returned connections are reused, not
    # closed; construction is deferred (lazy) to preserve import-without-DB.
    assert shared_pg_pool._pool.minconn == 1


def test_reset_disposes_pool(monkeypatch) -> None:
    closed = {"n": 0}

    class _FakePool:
        def __init__(self, *a, **k):
            pass

        def client(self):
            return "c"

        def closeall(self):
            closed["n"] += 1

    monkeypatch.setattr(shared_pg_pool, "PostgresConnectionPool", _FakePool)
    monkeypatch.setattr(
        shared_pg_pool, "PostgresConnectionConfig",
        type("C", (), {"from_env": staticmethod(lambda: object())}),
    )
    shared_pg_pool.api_pg_client()
    assert shared_pg_pool._pool is not None
    shared_pg_pool.reset_api_pg_pool_for_tests()
    assert shared_pg_pool._pool is None
    assert closed["n"] == 1


def test_api_pg_client_is_a_valid_client_factory() -> None:
    # The repository and queue accept it as their client_factory (drop-in).
    repo = AsyncPipelineV2Repository(client_factory=shared_pg_pool.api_pg_client)
    queue = PostgresTaskQueue(client_factory=shared_pg_pool.api_pg_client)
    assert repo._client_factory is shared_pg_pool.api_pg_client
    assert queue._client_factory is shared_pg_pool.api_pg_client


def test_v1_multi_image_store_threads_pooled_factory(monkeypatch) -> None:
    from EdennCode.Deployment.async_video_job_store import (
        build_async_multi_image_job_store_from_env,
    )

    monkeypatch.setenv("ASYNC_MULTI_IMAGE_JOB_STORE", "postgres")
    store = build_async_multi_image_job_store_from_env(
        client_factory=shared_pg_pool.api_pg_client
    )
    assert store.__class__.__name__ == "PostgresAsyncVideoJobStore"
    assert store._client_factory is shared_pg_pool.api_pg_client
    # Construction alone must not build the pool.
    assert shared_pg_pool._pool is None


def test_max_conn_env_override(monkeypatch) -> None:
    monkeypatch.setenv("API_PG_POOL_MAX_CONN", "17")
    assert shared_pg_pool._max_conn() == 17
    monkeypatch.setenv("API_PG_POOL_MAX_CONN", "not-a-number")
    assert shared_pg_pool._max_conn() == shared_pg_pool._DEFAULT_MAX_CONN
    monkeypatch.delenv("API_PG_POOL_MAX_CONN", raising=False)
    assert shared_pg_pool._max_conn() == shared_pg_pool._DEFAULT_MAX_CONN


# --- Real-database integration test ---------------------------------------

def _backend_pid(client: PostgresClient) -> int:
    with client as c:
        rows = c.run_sql("SELECT pg_backend_pid() AS pid")
    return int(rows[0]["pid"])


def _pg_reachable() -> bool:
    try:
        cfg = PostgresConnectionConfig.from_env()
    except Exception:
        return False
    try:
        client = PostgresClient(cfg)
        with client as c:
            c.run_sql("SELECT 1")
        return True
    except Exception:
        return False


def test_pooled_borrows_reuse_one_connection() -> None:
    if not _pg_reachable():
        pytest.skip("no reachable Postgres (set PGHOST/PGDATABASE/PGUSER or DATABASE_URL)")

    pool = shared_pg_pool.get_api_pg_pool()

    # Sequential pooled borrows (no overlap) reuse the same backend connection.
    pooled_pids = {_backend_pid(pool.client()) for _ in range(6)}
    assert len(pooled_pids) == 1, f"pooled borrows should reuse one connection, saw {pooled_pids}"

    # Non-pooled from_env opens a distinct connection each time.
    unpooled_pids = {_backend_pid(PostgresClient.from_env()) for _ in range(3)}
    assert len(unpooled_pids) == 3, f"unpooled opens should each be new, saw {unpooled_pids}"
    assert pooled_pids.isdisjoint(unpooled_pids)
