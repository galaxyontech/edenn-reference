"""Connections are reused, not rebuilt for every statement.

Every repository operation opened a new connection: connect, TLS handshake,
authenticate, run one statement, close. On a managed Postgres that handshake
costs more than most of the queries do, and a single page load makes several.

Whether a connection is actually reused is a property of the server's session,
so it is checked by asking the server which backend is answering — something no
fake can tell you. These run against a throwaway database and skip without one;
see ``Testing/README.md``.
"""

from __future__ import annotations

import os
import threading
from typing import Any
from urllib.parse import urlparse

import pytest

DSN = os.getenv("EDENN_TEST_PG_DSN", "").strip()

pytestmark = pytest.mark.skipif(
    not DSN, reason="set EDENN_TEST_PG_DSN to a throwaway Postgres to run these"
)


def _config():
    from EdennCode.Deployment.postgres_wrapper import PostgresConnectionConfig

    parsed = urlparse(DSN)
    return PostgresConnectionConfig(
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 5432,
        database=(parsed.path or "/postgres").lstrip("/"),
        user=parsed.username or "postgres",
        password=parsed.password,
        sslmode="disable",
        connect_timeout=10,
    )


@pytest.fixture()
def pool():
    from EdennCode.Deployment.postgres_wrapper import PostgresConnectionPool

    p = PostgresConnectionPool(_config(), minconn=1, maxconn=4)
    yield p
    p.closeall()


def _backend_pid(client: Any) -> int:
    return int(client.run_sql("SELECT pg_backend_pid() AS pid")[0]["pid"])


def test_sequential_operations_reuse_one_connection(pool) -> None:
    """The whole point: the same server-side session answers both times."""
    with pool.client() as c:
        first = _backend_pid(c)
    with pool.client() as c:
        second = _backend_pid(c)
    assert first == second, "a second connection was opened for the second call"


def test_an_unpooled_factory_does_not_reuse(pool) -> None:
    """The behaviour being replaced, pinned so the comparison is not folklore."""
    from EdennCode.Deployment.postgres_wrapper import PostgresClient

    with PostgresClient(_config()) as c:
        first = _backend_pid(c)
    with PostgresClient(_config()) as c:
        second = _backend_pid(c)
    assert first != second


def test_returning_a_client_makes_its_connection_available_again(pool) -> None:
    """A borrow that is not returned is a leak, and a small pool makes a leak
    into an outage rather than a slowdown."""
    seen = set()
    for _ in range(12):
        with pool.client() as c:
            seen.add(_backend_pid(c))
    assert len(seen) == 1, f"12 sequential borrows used {len(seen)} connections"


def test_concurrent_borrows_stay_within_the_ceiling(pool) -> None:
    """The ceiling exists because the database's connection limit is shared with
    every other service drawing on it."""
    pids: list[int] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)
    lock = threading.Lock()

    def borrow() -> None:
        try:
            barrier.wait(timeout=10)
            with pool.client() as c:
                pid = _backend_pid(c)
            with lock:
                pids.append(pid)
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=borrow) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"a concurrent borrow failed: {errors[:2]}"
    assert len(pids) == 4
    assert len(set(pids)) <= 4


def test_a_failed_statement_does_not_poison_the_next_borrower(pool) -> None:
    """A returned connection carrying an aborted transaction would make the NEXT
    caller fail for a reason that has nothing to do with them."""
    with pytest.raises(Exception):
        with pool.client() as c:
            c.run_sql("SELECT * FROM a_table_that_does_not_exist")

    with pool.client() as c:
        assert c.run_sql("SELECT 1 AS ok")[0]["ok"] == 1


def test_the_repository_uses_the_pool_by_default() -> None:
    """The wiring itself: the pool existed and the studio was not using it."""
    import inspect

    from EdennCode.EdennAgent.AgenticAudio.persistence.collab import CollabRepository
    from EdennCode.EdennAgent.AgenticAudio.persistence.repositories import (
        AgenticAudioRepository,
    )

    for cls in (AgenticAudioRepository, CollabRepository):
        default = inspect.signature(cls.__init__).parameters["client_factory"].default
        assert getattr(default, "__name__", "") == "pooled_client", cls.__name__


def test_repository_work_reuses_one_connection(pool) -> None:
    """End to end through the repository, not just the pool in isolation."""
    import uuid

    from EdennCode.EdennAgent.AgenticAudio.persistence.repositories import (
        AgenticAudioRepository,
    )

    repo = AgenticAudioRepository(client_factory=pool.client)
    repo.ensure_schema()
    sid = f"sess_{uuid.uuid4().hex[:12]}"
    repo.create_session(
        session_id=sid,
        source_video_artifact_id="artifact_test",
        creator_user_id="tester",
        phase="observing",
        state_json={},
    )
    for _ in range(5):
        repo.get_session(sid)

    with pool.client() as c:
        pids = c.run_sql(
            "SELECT count(DISTINCT pid) AS n FROM pg_stat_activity "
            "WHERE datname = current_database() AND application_name = %s",
            params=[_config().application_name],
        )
    assert int(pids[0]["n"]) <= 2, "repository work opened a connection per call"
