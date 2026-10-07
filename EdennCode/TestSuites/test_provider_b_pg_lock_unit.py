"""Unit tests for the ProviderB per-key PG advisory lock module.

All tests run without a real Postgres — psycopg2 calls are stubbed.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from EdennCode.exceptions import EdennProviderTimeoutError
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen import provider_b_pg_lock
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen import provider_b_key_health
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_pg_lock import (
    NullKeyLock,
    PgKeyLock,
    _label_to_lock_id,
)
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_key_health import (
    PgProviderBKeyHealthStore,
)


# --- _label_to_lock_id ------------------------------------------------------


def test_lock_id_is_deterministic_for_same_label() -> None:
    assert _label_to_lock_id("PROVIDER_B_API_KEY_1") == _label_to_lock_id("PROVIDER_B_API_KEY_1")


def test_lock_id_differs_for_different_labels() -> None:
    ids = {
        _label_to_lock_id(f"PROVIDER_B_API_KEY_{i}") for i in range(1, 6)
    }
    assert len(ids) == 5


def test_lock_id_fits_signed_int64() -> None:
    for label in ("PROVIDER_B_API_KEY_1", "very-long-label" * 10, ""):
        lid = _label_to_lock_id(label)
        assert -(2 ** 63) <= lid < 2 ** 63


# --- NullKeyLock ------------------------------------------------------------


def test_null_key_lock_acquire_one_of_returns_first_label() -> None:
    lock = NullKeyLock()
    label, handle = asyncio.run(lock.acquire_one_of(["a", "b", "c"]))
    assert label == "a"
    asyncio.run(handle.release())  # idempotent no-op


def test_null_key_lock_acquire_returns_immediately() -> None:
    lock = NullKeyLock()
    handle = asyncio.run(lock.acquire("only_label"))
    asyncio.run(handle.release())


def test_null_key_lock_rejects_empty_label_list() -> None:
    lock = NullKeyLock()
    with pytest.raises(ValueError):
        asyncio.run(lock.acquire_one_of([]))


# --- Fake pool plumbing ------------------------------------------------------


class _FakeConn:
    """Mimics the psycopg2 connection API surface that PgKeyLock touches."""

    def __init__(self, *, scripted_results: Dict[int, bool], raise_on: str = None):
        self._scripted = scripted_results
        self._raise_on = raise_on
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.last_lock_id: int = None  # type: ignore[assignment]

    def cursor(self) -> "_FakeCursor":
        return _FakeCursor(self)

    def commit(self) -> None:
        if self._raise_on == "commit":
            raise RuntimeError("commit boom")
        self.commits += 1

    def rollback(self) -> None:
        if self._raise_on == "rollback":
            raise RuntimeError("rollback boom")
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class _FakeCursor:
    def __init__(self, conn: _FakeConn):
        self._conn = conn
        self._row: tuple = ()

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def execute(self, sql: str, params=None) -> None:
        if self._conn._raise_on == "execute":
            raise RuntimeError("execute boom")
        if "pg_try_advisory_xact_lock" in sql:
            lock_id = params[0] if params else None
            self._conn.last_lock_id = lock_id
            scripted = self._conn._scripted.get(lock_id, False)
            self._row = (scripted,)
        else:
            # SELECT 1 startup check, etc.
            self._row = (1,)

    def fetchone(self) -> tuple:
        return self._row


class _FakePool:
    def __init__(self, conns: List[_FakeConn]):
        self._conns = list(conns)
        self.checkouts: List[_FakeConn] = []
        self.returns: List[tuple[_FakeConn, bool]] = []  # (conn, close)
        self._lock = threading.Lock()
        self._getconn_raises: Exception = None  # type: ignore[assignment]
        self.closeall_called = False

    def getconn(self) -> _FakeConn:
        with self._lock:
            if self._getconn_raises is not None:
                raise self._getconn_raises
            if not self._conns:
                raise RuntimeError("pool empty")
            conn = self._conns.pop(0)
            self.checkouts.append(conn)
            return conn

    def putconn(self, conn: _FakeConn, close: bool = False) -> None:
        with self._lock:
            self.returns.append((conn, close))
            if close:
                conn.close()
            else:
                self._conns.append(conn)

    def closeall(self) -> None:
        self.closeall_called = True


def _make_lock(pool: _FakePool, *, max_pool_size: int = 4) -> PgKeyLock:
    return PgKeyLock(pool=pool, max_pool_size=max_pool_size)  # type: ignore[arg-type]


# --- PgKeyLock acquisition --------------------------------------------------


def test_acquire_one_of_returns_first_uncontested_label() -> None:
    label_a_id = _label_to_lock_id("a")
    label_b_id = _label_to_lock_id("b")
    conn = _FakeConn(scripted_results={label_a_id: False, label_b_id: True})
    pool = _FakePool([conn] * 2)
    lock = _make_lock(pool)

    label, handle = asyncio.run(
        lock.acquire_one_of(["a", "b"], timeout_s=0.5, poll_interval_s=0.01)
    )

    assert label == "b"
    asyncio.run(handle.release())
    assert conn.commits == 1
    # The 'a' attempt should have rolled back, the 'b' attempt should commit.
    assert conn.rollbacks >= 1


def test_acquire_one_of_polls_when_all_busy_then_succeeds() -> None:
    # First two attempts return busy (None), third returns the conn.
    call_count = {"n": 0}
    pool = _FakePool([
        _FakeConn(scripted_results={_label_to_lock_id("a"): True}) for _ in range(5)
    ])

    def _stage_acquire(target_pool, label):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            return None
        return target_pool.getconn()

    lock = _make_lock(pool)

    with patch.object(
        provider_b_pg_lock, "_try_acquire_blocking", side_effect=_stage_acquire,
    ):
        label, handle = asyncio.run(
            lock.acquire_one_of(["a"], timeout_s=2.0, poll_interval_s=0.05)
        )

    assert label == "a"
    assert call_count["n"] == 3
    asyncio.run(handle.release())


def test_acquire_one_of_times_out_when_all_remain_busy() -> None:
    label_id = _label_to_lock_id("a")
    conn = _FakeConn(scripted_results={label_id: False})
    pool = _FakePool([conn] * 10)
    lock = _make_lock(pool)

    with pytest.raises(EdennProviderTimeoutError):
        asyncio.run(
            lock.acquire_one_of(
                ["a"], timeout_s=0.3, poll_interval_s=0.05,
            )
        )


def test_acquire_one_of_rejects_empty_label_list() -> None:
    pool = _FakePool([])
    lock = _make_lock(pool)
    with pytest.raises(ValueError):
        asyncio.run(lock.acquire_one_of([]))


def test_pg_error_during_try_lock_is_retried_then_times_out() -> None:
    """Each PG error contributes to the per-attempt error trail; retry until timeout."""
    pool = _FakePool([
        _FakeConn(scripted_results={}, raise_on="execute") for _ in range(20)
    ])
    lock = _make_lock(pool)

    with pytest.raises(EdennProviderTimeoutError) as exc_info:
        asyncio.run(
            lock.acquire_one_of(
                ["a", "b"], timeout_s=0.3, poll_interval_s=0.05,
            )
        )
    # The timeout message should reference the per-label errors.
    assert "execute boom" in str(exc_info.value)


def test_acquire_single_label_delegates_to_acquire_one_of() -> None:
    label_id = _label_to_lock_id("only")
    conn = _FakeConn(scripted_results={label_id: True})
    pool = _FakePool([conn])
    lock = _make_lock(pool)

    handle = asyncio.run(lock.acquire("only", timeout_s=0.5, poll_interval_s=0.01))
    asyncio.run(handle.release())

    assert conn.commits == 1


# --- Pool checkout back-pressure --------------------------------------------


def test_pool_checkout_is_backpressured_by_asyncio_semaphore() -> None:
    """When all pool slots are held, a new try-acquire awaits a permit.

    We hold ``max_pool_size`` locks, then attempt one more in parallel and
    verify it doesn't proceed until one releases.
    """
    label_id = _label_to_lock_id("k")
    pool = _FakePool([
        _FakeConn(scripted_results={label_id: True}) for _ in range(4)
    ])
    lock = _make_lock(pool, max_pool_size=2)

    async def _run():
        # Hold both permits.
        _, h1 = await lock.acquire_one_of(["k"], timeout_s=1.0, poll_interval_s=0.01)
        _, h2 = await lock.acquire_one_of(["k"], timeout_s=1.0, poll_interval_s=0.01)

        # Third attempt must block on the semaphore. Run it in the background.
        third_result: dict = {}

        async def _third():
            try:
                lbl, h3 = await asyncio.wait_for(
                    lock.acquire_one_of(["k"], timeout_s=2.0, poll_interval_s=0.01),
                    timeout=2.5,
                )
                third_result["ok"] = True
                await h3.release()
            except Exception as exc:
                third_result["error"] = exc

        third_task = asyncio.create_task(_third())
        # Give the third task a chance to start and block on the semaphore.
        await asyncio.sleep(0.2)
        assert "ok" not in third_result, (
            "third acquire should still be waiting on the semaphore"
        )

        # Release one — third should now proceed.
        await h1.release()
        await third_task
        assert third_result.get("ok") is True

        await h2.release()

    asyncio.run(_run())


# --- Release semantics ------------------------------------------------------


def test_release_commits_and_returns_connection_to_pool() -> None:
    label_id = _label_to_lock_id("a")
    conn = _FakeConn(scripted_results={label_id: True})
    pool = _FakePool([conn])
    lock = _make_lock(pool)

    label, handle = asyncio.run(
        lock.acquire_one_of(["a"], timeout_s=0.5, poll_interval_s=0.01)
    )
    asyncio.run(handle.release())

    assert conn.commits == 1
    assert conn.closed is False
    # Returned to pool (close=False).
    assert any(c is conn and not close for c, close in pool.returns)


def test_release_is_idempotent() -> None:
    label_id = _label_to_lock_id("a")
    conn = _FakeConn(scripted_results={label_id: True})
    pool = _FakePool([conn])
    lock = _make_lock(pool)

    _, handle = asyncio.run(
        lock.acquire_one_of(["a"], timeout_s=0.5, poll_interval_s=0.01)
    )
    asyncio.run(handle.release())
    asyncio.run(handle.release())  # second call must be a safe no-op

    assert conn.commits == 1


def test_release_discards_broken_connection_and_still_releases_semaphore() -> None:
    label_id = _label_to_lock_id("a")
    conn = _FakeConn(scripted_results={label_id: True}, raise_on="commit")
    pool = _FakePool([conn])
    lock = _make_lock(pool, max_pool_size=2)

    label, handle = asyncio.run(
        lock.acquire_one_of(["a"], timeout_s=0.5, poll_interval_s=0.01)
    )
    asyncio.run(handle.release())

    # Conn should be discarded (close=True) after commit failure.
    assert any(c is conn and close for c, close in pool.returns)
    # Semaphore should be released — verify by making one more acquire that
    # succeeds without contention.
    label_id2 = _label_to_lock_id("a")
    pool._conns.append(_FakeConn(scripted_results={label_id2: True}))
    _, handle2 = asyncio.run(
        lock.acquire_one_of(["a"], timeout_s=0.5, poll_interval_s=0.01)
    )
    asyncio.run(handle2.release())


# --- from_env smoke test ---------------------------------------------------


# --- Regression: timeout / cancellation / double-discard fixes -------------


def test_semaphore_exhaustion_respects_timeout() -> None:
    """Regression for Finding 1: a saturated semaphore must not pin a request
    past ``timeout_s``. With pool size 1 and an outstanding hold, a second
    acquire must raise EdennProviderTimeoutError within the budget rather
    than blocking forever.
    """
    label_id = _label_to_lock_id("k")
    pool = _FakePool([
        _FakeConn(scripted_results={label_id: True}) for _ in range(4)
    ])
    lock = _make_lock(pool, max_pool_size=1)

    async def _run():
        _, h1 = await lock.acquire_one_of(["k"], timeout_s=1.0, poll_interval_s=0.01)

        start = time.monotonic()
        with pytest.raises(EdennProviderTimeoutError):
            await lock.acquire_one_of(
                ["k"], timeout_s=0.3, poll_interval_s=0.05,
            )
        elapsed = time.monotonic() - start
        # Must time out within ~timeout_s, not block on the held semaphore.
        assert elapsed < 1.0, f"acquire bypassed timeout_s, took {elapsed:.2f}s"

        await h1.release()

    asyncio.run(_run())


def test_cancellation_during_getconn_does_not_leak_conn() -> None:
    """Regression for the reviewer's reproduction: cancellation during the
    ``getconn`` worker thread previously left the conn checked out (pool
    ``checkouts=1, returns=0``). After the fix, ``_await_to_thread_completion``
    waits for the worker to finish before propagating cancellation, so the
    bundled ``_try_acquire_blocking`` always returns the conn to the pool
    (or hands it to a handle that subsequently releases it).
    """
    label_id = _label_to_lock_id("k")
    pool = _FakePool([
        _FakeConn(scripted_results={label_id: True}) for _ in range(4)
    ])
    lock = _make_lock(pool, max_pool_size=2)

    slow_event = threading.Event()
    real_getconn = pool.getconn

    def _slow_getconn():
        slow_event.wait(timeout=1.0)
        return real_getconn()

    pool.getconn = _slow_getconn  # type: ignore[assignment]

    async def _run():
        task = asyncio.create_task(
            lock.acquire_one_of(["k"], timeout_s=2.0, poll_interval_s=0.01)
        )
        await asyncio.sleep(0.05)  # let the worker enter slow_getconn
        task.cancel()
        slow_event.set()  # release the slow getconn so the worker can finish
        try:
            await task
        except (asyncio.CancelledError, BaseException):
            pass

    asyncio.run(_run())

    # The bundled worker either returned the conn to the pool itself (miss),
    # or handed it to a handle that the cancel path released. Either way:
    # checkouts must equal returns — no leaked conn.
    assert len(pool.checkouts) == len(pool.returns), (
        f"conn leaked across cancellation: "
        f"checkouts={len(pool.checkouts)} returns={len(pool.returns)}"
    )


def test_cancellation_during_try_lock_waits_for_worker_then_cleans_up() -> None:
    """Regression for the race the reviewer flagged: cancellation during
    ``_try_lock_blocking`` used to let cleanup ``putconn`` run concurrently
    with the still-executing try-lock on the same conn. After the fix, the
    coroutine waits for the worker to fully complete via the shield-loop
    before any cleanup runs, so the worker and any putconn are strictly
    serialized on a given conn.
    """
    label_id = _label_to_lock_id("k")
    pool = _FakePool([
        _FakeConn(scripted_results={label_id: True}) for _ in range(4)
    ])
    lock = _make_lock(pool, max_pool_size=1)

    slow_event = threading.Event()
    worker_finished = threading.Event()
    real_acquire = provider_b_pg_lock._try_acquire_blocking

    def _slow_try_acquire(target_pool, label):
        slow_event.wait(timeout=1.0)
        try:
            return real_acquire(target_pool, label)
        finally:
            worker_finished.set()

    async def _run():
        with patch.object(
            provider_b_pg_lock, "_try_acquire_blocking", side_effect=_slow_try_acquire,
        ):
            task = asyncio.create_task(
                lock.acquire_one_of(["k"], timeout_s=2.0, poll_interval_s=0.01)
            )
            await asyncio.sleep(0.05)  # let worker enter the slow body
            task.cancel()
            slow_event.set()  # release worker so it can complete the conn lifecycle
            try:
                await task
            except (asyncio.CancelledError, BaseException):
                pass

        # The worker must have completed before the cancellation propagated
        # — that's the guarantee the shield-loop adds.
        assert worker_finished.is_set(), (
            "shield-loop did not wait for worker before propagating cancellation"
        )

        # No conn leak.
        assert len(pool.checkouts) == len(pool.returns), (
            f"conn leaked: checkouts={len(pool.checkouts)} "
            f"returns={len(pool.returns)}"
        )

        # And the permit must be back so a new acquire works.
        _, h = await asyncio.wait_for(
            lock.acquire_one_of(["k"], timeout_s=0.5, poll_interval_s=0.01),
            timeout=1.0,
        )
        await h.release()

    asyncio.run(_run())


def test_worker_exception_releases_semaphore_so_next_acquire_succeeds() -> None:
    """Regression for the helper's exception-suppression gap.

    Before the fix, ``_await_to_thread_completion`` let a worker exception
    propagate immediately, so ``_try_acquire``'s cleanup branch
    (``if task.exception() is not None:``) never ran and the semaphore
    permit was never released. At ``max_pool_size=1``, any worker failure
    deadlocked the pool.

    After the fix, the helper absorbs the task's exception once the task is
    ``.done()``, the caller's cleanup releases the permit, and the next
    label in the same ``acquire_one_of`` call can succeed.
    """
    label_id_b = _label_to_lock_id("b")
    # First conn raises on execute; second conn is healthy.
    conn_broken = _FakeConn(scripted_results={}, raise_on="execute")
    conn_ok = _FakeConn(scripted_results={label_id_b: True})
    pool = _FakePool([conn_broken, conn_ok])
    # Pool size 1: any permit leak deadlocks the next acquire.
    lock = _make_lock(pool, max_pool_size=1)

    async def _run():
        # Attempt order: "a" -> worker raises; "b" should still succeed
        # because the permit must be released after "a" fails.
        label, handle = await asyncio.wait_for(
            lock.acquire_one_of(
                ["a", "b"], timeout_s=2.0, poll_interval_s=0.05,
            ),
            timeout=3.0,
        )
        assert label == "b"
        await handle.release()

    asyncio.run(_run())

    # Both conns were checked out, both returned. Broken conn was discarded
    # (close=True); healthy conn was recycled (close=False).
    broken_returns = [(c, close) for c, close in pool.returns if c is conn_broken]
    ok_returns = [(c, close) for c, close in pool.returns if c is conn_ok]
    assert broken_returns == [(conn_broken, True)], (
        f"broken conn must be discarded with close=True, got {broken_returns!r}"
    )
    assert ok_returns == [(conn_ok, False)], (
        f"healthy conn must be returned with close=False, got {ok_returns!r}"
    )


def test_release_waits_for_commit_thread_before_returning() -> None:
    """Regression for the same race in ``release()``: cancellation between
    the commit ``await`` and the ``putconn`` ``await`` could let ``putconn``
    run while the commit was still executing on the conn. After the fix,
    ``release`` bundles commit + putconn into one worker call and uses the
    shield-loop, so the conn is only ever touched by one thread at a time.
    """
    label_id = _label_to_lock_id("k")
    pool = _FakePool([_FakeConn(scripted_results={label_id: True})])
    lock = _make_lock(pool, max_pool_size=1)

    slow_event = threading.Event()
    release_finished = threading.Event()
    real_release = provider_b_pg_lock._release_blocking

    def _slow_release(target_pool, conn):
        slow_event.wait(timeout=1.0)
        try:
            return real_release(target_pool, conn)
        finally:
            release_finished.set()

    async def _run():
        _, handle = await lock.acquire_one_of(
            ["k"], timeout_s=0.5, poll_interval_s=0.01,
        )
        with patch.object(
            provider_b_pg_lock, "_release_blocking", side_effect=_slow_release,
        ):
            release_task = asyncio.create_task(handle.release())
            await asyncio.sleep(0.05)  # let worker enter slow_release
            release_task.cancel()
            slow_event.set()
            try:
                await release_task
            except (asyncio.CancelledError, BaseException):
                pass

        assert release_finished.is_set(), (
            "release returned before worker completed — would race putconn"
        )
        # Conn was returned exactly once.
        matching = [r for r in pool.returns if r[0] is pool.checkouts[0]]
        assert len(matching) == 1, (
            f"expected exactly one putconn during release, got {matching!r}"
        )

    asyncio.run(_run())


def test_release_does_not_double_putconn_on_commit_failure() -> None:
    """Regression for Finding 3: the old ``_commit_and_return`` called
    ``putconn(close=True)`` on commit failure and then re-raised, after which
    ``release()`` caught the exception and called ``putconn`` again. After
    the fix, ``putconn`` runs exactly once per acquired connection.
    """
    label_id = _label_to_lock_id("a")
    conn = _FakeConn(scripted_results={label_id: True}, raise_on="commit")
    pool = _FakePool([conn])
    lock = _make_lock(pool, max_pool_size=2)

    _, handle = asyncio.run(
        lock.acquire_one_of(["a"], timeout_s=0.5, poll_interval_s=0.01)
    )
    asyncio.run(handle.release())

    # Exactly one putconn call for this conn — discard with close=True
    # because commit failed.
    matching_returns = [(c, close) for c, close in pool.returns if c is conn]
    assert len(matching_returns) == 1, (
        f"expected exactly one putconn for the conn, got {matching_returns!r}"
    )
    assert matching_returns[0][1] is True, "broken conn must be discarded"


def test_release_on_clean_commit_does_single_putconn_without_close() -> None:
    """Positive case for Finding 3: a successful commit returns the conn to
    the pool exactly once with ``close=False`` so the pool can recycle it.
    """
    label_id = _label_to_lock_id("a")
    conn = _FakeConn(scripted_results={label_id: True})
    pool = _FakePool([conn])
    lock = _make_lock(pool)

    _, handle = asyncio.run(
        lock.acquire_one_of(["a"], timeout_s=0.5, poll_interval_s=0.01)
    )
    asyncio.run(handle.release())

    matching_returns = [(c, close) for c, close in pool.returns if c is conn]
    assert matching_returns == [(conn, False)]
    assert conn.commits == 1


def test_from_env_invalid_pool_size_raises() -> None:
    import os
    with patch.dict(os.environ, {"PROVIDER_B_PG_LOCK_POOL_SIZE": "0"}, clear=False):
        with pytest.raises(Exception):  # EdennConfigurationError
            PgKeyLock.from_env()


def test_from_env_blank_pool_size_uses_default() -> None:
    """GitHub Actions expands missing vars as empty strings when explicitly
    assigned in workflow env. Blank optional tuning vars must still use defaults.
    """
    import os

    fake_pool = _FakePool([_FakeConn(scripted_results={})])
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": "postgresql://demo:REDACTED@example.test:5432/telemetry",
            "PROVIDER_B_PG_LOCK_POOL_SIZE": "",
        },
        clear=False,
    ), patch.object(
        provider_b_pg_lock.PostgresConnectionConfig,
        "from_env",
        return_value=provider_b_pg_lock.PostgresConnectionConfig(dsn="postgres://x"),
    ), patch.object(
        PgKeyLock,
        "_build_pool",
        return_value=fake_pool,
    ):
        lock = PgKeyLock.from_env()

    assert lock._max_pool_size == provider_b_pg_lock.DEFAULT_POOL_SIZE


def test_key_health_from_env_blank_optional_tuning_uses_defaults() -> None:
    import os

    fake_pool = _FakePool([_FakeConn(scripted_results={})])
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": "postgresql://demo:REDACTED@example.test:5432/telemetry",
            "PROVIDER_B_KEY_HEALTH_POOL_SIZE": "",
            "PROVIDER_B_KEY_COOLDOWN_BASE_S": "",
            "PROVIDER_B_KEY_COOLDOWN_MAX_S": "",
        },
        clear=False,
    ), patch.object(
        provider_b_key_health.PostgresConnectionConfig,
        "from_env",
        return_value=provider_b_key_health.PostgresConnectionConfig(dsn="postgres://x"),
    ), patch.object(
        PgProviderBKeyHealthStore,
        "_build_pool",
        return_value=fake_pool,
    ):
        store = PgProviderBKeyHealthStore.from_env()

    assert store._cooldown_base_s == provider_b_key_health.DEFAULT_COOLDOWN_BASE_S
    assert store._cooldown_max_s == provider_b_key_health.DEFAULT_COOLDOWN_MAX_S


def test_from_env_propagates_pool_construction_failure() -> None:
    """If psycopg2 raises while building the pool, PgKeyLock.from_env must raise."""
    with patch.object(
        provider_b_pg_lock.PostgresConnectionConfig, "from_env",
        side_effect=RuntimeError("no PG"),
    ):
        with pytest.raises(RuntimeError, match="no PG"):
            PgKeyLock.from_env()


def test_from_env_closes_pool_on_startup_check_failure() -> None:
    """If SELECT 1 fails, the partial pool must be torn down before raising."""
    fake_pool = _FakePool([])
    fake_pool._getconn_raises = RuntimeError("conn refused")
    with patch.object(
        provider_b_pg_lock.PostgresConnectionConfig, "from_env",
        return_value=provider_b_pg_lock.PostgresConnectionConfig(dsn="postgres://x"),
    ), patch.object(
        PgKeyLock, "_build_pool", return_value=fake_pool,
    ):
        with pytest.raises(RuntimeError, match="conn refused"):
            PgKeyLock.from_env()
    assert fake_pool.closeall_called is True
