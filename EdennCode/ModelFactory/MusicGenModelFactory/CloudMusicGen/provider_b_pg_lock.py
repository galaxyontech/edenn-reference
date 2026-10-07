"""Per-key advisory lock for the ProviderB API key pool, backed by Postgres.

Uses ``pg_try_advisory_xact_lock(int8)`` to coordinate concurrent use of each
upstream API key label across replicas. One label = one lock = one in-flight
provider cycle. The lock is held inside an open psycopg2 transaction; COMMIT
releases it (and a dropped connection also releases it because PostgreSQL
tracks the lock on the session).

Two implementations are exported:

- ``PgKeyLock`` — the real lock used in production.
- ``NullKeyLock`` — a no-op used when locking is explicitly disabled, or as
  the default for direct ``ProviderBMusicProvider(...)`` construction so unit
  tests and ad-hoc scripts don't accidentally require Postgres.

Acquisition strategy is non-blocking try-lock across a list of candidate
labels, falling back to ``asyncio.sleep`` + retry until ``timeout_s`` elapses.
This avoids holding a PG connection in a blocked state — every connection in
the pool is either actively serving a held lock or free.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from typing import Any, List, Optional, Protocol, Tuple

import psycopg2
import psycopg2.pool

from EdennCode.Deployment.postgres_wrapper import PostgresConnectionConfig
from EdennCode.exceptions import (
    EdennConfigurationError,
    EdennProviderTimeoutError,
)

logger = logging.getLogger(__name__)


DEFAULT_TIMEOUT_S = 600.0
DEFAULT_POLL_INTERVAL_S = 0.5
DEFAULT_POOL_SIZE = 8


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise EdennConfigurationError(
            f"{name} must be an integer",
            component="provider_b_pg_lock",
            operation="from_env",
        ) from exc


class LockHandle(Protocol):
    """Handle for a held lock. ``release`` must be idempotent."""

    async def release(self) -> None: ...


class ProviderBKeyLock(Protocol):
    """Interface implemented by both ``PgKeyLock`` and ``NullKeyLock``."""

    async def acquire_one_of(
        self,
        labels: List[str],
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> Tuple[str, LockHandle]:
        ...

    async def acquire(
        self,
        label: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> LockHandle:
        ...


# --- NullKeyLock ------------------------------------------------------------


class _NullLockHandle:
    async def release(self) -> None:
        return None


class NullKeyLock:
    """No-op coordinator. Returns immediately without taking any real lock.

    Used when ``PROVIDER_B_PG_LOCK_ENABLED=false`` or when a caller constructs
    ``ProviderBMusicProvider`` directly without injecting a lock.
    """

    async def acquire_one_of(
        self,
        labels: List[str],
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> Tuple[str, LockHandle]:
        if not labels:
            raise ValueError("acquire_one_of requires at least one label")
        return labels[0], _NullLockHandle()

    async def acquire(
        self,
        label: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> LockHandle:
        return _NullLockHandle()


# --- PgKeyLock --------------------------------------------------------------


def _label_to_lock_id(label: str) -> int:
    """Deterministically hash a label to a signed int64 for pg_advisory_*.

    Postgres advisory locks take bigint keys. Two different labels almost
    certainly hash to different ids (collision probability is astronomical
    at our pool size).
    """
    digest = hashlib.blake2b(label.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


async def _await_to_thread_completion(
    task: "asyncio.Task[Any]",
) -> Optional[asyncio.CancelledError]:
    """Wait for ``task`` to complete, surviving outer cancellation AND
    suppressing the task's own exception until the caller can inspect it.

    asyncio.to_thread is not cancellable — the worker thread keeps running
    regardless. If the outer coroutine receives ``CancelledError`` while a
    DB-touching worker is still executing, naively propagating the cancel
    leaves the worker's resources (connections, transactions) in a state
    the coroutine no longer knows about, OR it lets cleanup code race the
    still-running worker on the same connection.

    This helper shields the task in a loop, recording any outer cancellation
    but not propagating it until the worker has fully completed.

    It also absorbs the task's own exception once the task is ``.done()`` so
    the caller's resource-cleanup branch (which inspects ``task.exception()``)
    always runs. Otherwise a worker exception would propagate through this
    helper before the caller has a chance to release the semaphore permit
    that the worker was operating under.

    The task is guaranteed to be ``.done()`` when this returns; callers
    inspect ``task.exception()`` / ``task.result()`` and re-raise the
    recorded cancellation (or the task's exception) after their own cleanup.
    """
    pending_cancel: Optional[asyncio.CancelledError] = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            pending_cancel = exc
        except BaseException:
            # Either the task itself raised, or something unrelated raised
            # at the await point. If the task is done, the exception came
            # from the task — absorb it so the caller can run cleanup and
            # then inspect task.exception(). If the task is NOT done, the
            # exception is unrelated and must propagate.
            if not task.done():
                raise
    return pending_cancel


def _try_acquire_blocking(
    pool: psycopg2.pool.ThreadedConnectionPool,
    label: str,
) -> Optional[Any]:
    """Worker-thread atomic: getconn + try-lock + return-on-miss/error.

    Returns the held connection on success (caller becomes the owner).
    Returns ``None`` on a clean miss — the connection is rolled back and
    returned to the pool inside this function so the coroutine never sees
    a half-checked-out conn.
    Raises on PG errors, after discarding the conn from the pool.

    Running the entire conn lifecycle inside a single worker call is what
    makes the caller's cancellation handling safe: there's no intermediate
    state visible to async code that an outer ``CancelledError`` could
    interrupt and leak.
    """
    conn = pool.getconn()
    try:
        lock_id = _label_to_lock_id(label)
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (lock_id,))
            row = cur.fetchone()
            acquired = bool(row and row[0])
        if acquired:
            return conn
        # Miss — clear the transaction state and return the conn clean.
        try:
            conn.rollback()
        except Exception:
            # Rollback failed on a presumed-clean conn; treat as broken.
            pool.putconn(conn, close=True)
            return None
        pool.putconn(conn)
        return None
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            pool.putconn(conn, close=True)
        except Exception:
            pass
        raise


def _release_blocking(
    pool: psycopg2.pool.ThreadedConnectionPool,
    conn: Any,
) -> None:
    """Worker-thread atomic: commit (releasing the advisory lock) + putconn.

    On commit failure, attempts rollback and discards the connection from
    the pool. The connection is always disposed of in exactly one place,
    so the caller never has to track whether commit-or-not raced putconn.
    """
    needs_close = False
    try:
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        needs_close = True
    try:
        pool.putconn(conn, close=needs_close)
    except Exception:
        # We're in a worker thread without convenient logger access; rare
        # path. The pool will GC the conn eventually if it stays orphan.
        pass


class _PgLockHandle:
    """Owns one connection + transaction + semaphore permit for a held lock.

    ``release()`` is idempotent — repeat calls are no-ops. On the first call
    it commits the transaction (releasing the advisory lock), returns the
    connection to the pool, and releases the semaphore permit.
    """

    def __init__(
        self,
        *,
        pool: psycopg2.pool.ThreadedConnectionPool,
        conn: Any,
        semaphore: asyncio.Semaphore,
        label: str,
    ) -> None:
        self._pool = pool
        self._conn: Optional[Any] = conn
        self._semaphore = semaphore
        self._label = label
        self._released = False

    async def release(self) -> None:
        """Idempotent release. Bundles commit + putconn into a single worker
        thread so cancellation can't run putconn while commit is still in
        flight on the same connection. After the worker completes, releases
        the semaphore permit unconditionally and re-raises any pending
        outer cancellation.
        """
        if self._released:
            return
        self._released = True
        conn = self._conn
        self._conn = None

        if conn is None:
            try:
                self._semaphore.release()
            except Exception:
                logger.exception(
                    "PgKeyLock: semaphore.release() failed for label=%s",
                    self._label,
                )
            return

        task = asyncio.ensure_future(
            asyncio.to_thread(_release_blocking, self._pool, conn)
        )
        pending_cancel = await _await_to_thread_completion(task)

        if task.exception() is not None:
            logger.error(
                "PgKeyLock: release blocking call raised for label=%s: %s",
                self._label, task.exception(),
                exc_info=task.exception(),
            )

        try:
            self._semaphore.release()
        except Exception:
            logger.exception(
                "PgKeyLock: semaphore.release() failed for label=%s",
                self._label,
            )

        if pending_cancel is not None:
            raise pending_cancel


class PgKeyLock:
    """Postgres-backed per-label coordinator using advisory transaction locks."""

    def __init__(
        self,
        *,
        pool: psycopg2.pool.ThreadedConnectionPool,
        max_pool_size: int,
    ) -> None:
        self._pool = pool
        self._semaphore = asyncio.Semaphore(max_pool_size)
        self._max_pool_size = max_pool_size

    @classmethod
    def from_env(cls) -> "PgKeyLock":
        """Build a ``PgKeyLock`` from env. Raises if Postgres is unreachable."""
        max_pool_size = _env_int("PROVIDER_B_PG_LOCK_POOL_SIZE", DEFAULT_POOL_SIZE)
        if max_pool_size < 1:
            raise EdennConfigurationError(
                "PROVIDER_B_PG_LOCK_POOL_SIZE must be >= 1",
                component="provider_b_pg_lock",
                operation="from_env",
            )
        config = PostgresConnectionConfig.from_env()

        pool = cls._build_pool(config, max_pool_size)
        try:
            cls._startup_check(pool)
        except Exception:
            try:
                pool.closeall()
            except Exception:
                logger.exception(
                    "PgKeyLock: failed to close pool after startup check failure"
                )
            raise

        logger.info(
            "PgKeyLock initialized; max_pool_size=%d", max_pool_size,
        )
        return cls(pool=pool, max_pool_size=max_pool_size)

    @staticmethod
    def _build_pool(
        config: PostgresConnectionConfig,
        max_pool_size: int,
    ) -> psycopg2.pool.ThreadedConnectionPool:
        if config.dsn:
            return psycopg2.pool.ThreadedConnectionPool(
                1,
                max_pool_size,
                config.dsn,
                connect_timeout=config.connect_timeout,
                application_name=config.application_name,
                sslmode=config.sslmode,
            )
        return psycopg2.pool.ThreadedConnectionPool(
            1,
            max_pool_size,
            **config.connect_kwargs(),
        )

    @staticmethod
    def _startup_check(pool: psycopg2.pool.ThreadedConnectionPool) -> None:
        """Verify the pool can issue a trivial query before serving traffic."""
        conn = pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            conn.commit()
        finally:
            pool.putconn(conn)

    async def acquire_one_of(
        self,
        labels: List[str],
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> Tuple[str, LockHandle]:
        if not labels:
            raise ValueError("acquire_one_of requires at least one label")
        deadline = time.monotonic() + max(0.0, timeout_s)
        last_errors: List[str] = []
        while True:
            iter_errors: List[str] = []
            for label in labels:
                try:
                    handle = await self._try_acquire(label, deadline=deadline)
                except Exception as exc:
                    iter_errors.append(f"{label}: {type(exc).__name__}: {exc}")
                    logger.warning(
                        "PgKeyLock: try-lock failed for label=%s: %s",
                        label, exc,
                    )
                    handle = None
                if handle is not None:
                    return label, handle
            # Preserve the most recent batch of errors across iterations so the
            # final timeout message still references real PG failures even if
            # the last iteration short-circuited on the deadline fast path.
            if iter_errors:
                last_errors = iter_errors
            if time.monotonic() >= deadline:
                raise EdennProviderTimeoutError(
                    f"ProviderB PG key lock timed out after {timeout_s:.1f}s; "
                    f"all candidate labels busy or PG errors: "
                    f"{'; '.join(last_errors) or 'none'}",
                    provider_name="provider_b",
                    operation="pg_advisory_lock_acquire",
                    retryable=True,
                    context={
                        "labels": list(labels),
                        "timeout_s": timeout_s,
                    },
                )
            await asyncio.sleep(poll_interval_s)

    async def acquire(
        self,
        label: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> LockHandle:
        _, handle = await self.acquire_one_of(
            [label],
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
        )
        return handle

    async def _try_acquire(
        self,
        label: str,
        *,
        deadline: float,
    ) -> Optional[LockHandle]:
        """One non-blocking lock attempt against ``label``.

        Returns a ``_PgLockHandle`` on success, ``None`` on busy (lock held
        elsewhere OR the semaphore wait timed out before the deadline). Raises
        on PG errors.

        Bounds the semaphore wait by the overall ``deadline`` so a saturated
        pool can't pin a request past ``timeout_s``.

        Conn lifecycle is bundled into a single worker-thread call so
        outer cancellation cannot leave a connection half-checked-out from
        the pool or race a still-running blocking call. If outer cancellation
        occurs after the worker has acquired the lock, the orphan handle is
        released before the cancellation propagates.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(), timeout=remaining,
            )
        except asyncio.TimeoutError:
            return None

        # From here on, we own one semaphore permit. The worker thread takes
        # responsibility for the conn lifecycle until it returns, so the
        # coroutine never sees a half-acquired state. We wait for the worker
        # to fully complete before any cleanup runs, even under cancellation.
        task = asyncio.ensure_future(
            asyncio.to_thread(_try_acquire_blocking, self._pool, label)
        )
        pending_cancel = await _await_to_thread_completion(task)

        if task.exception() is not None:
            # Worker raised — conn was already discarded inside the worker.
            try:
                self._semaphore.release()
            except Exception:
                logger.exception(
                    "PgKeyLock: semaphore.release() failed after worker error"
                )
            if pending_cancel is not None:
                raise pending_cancel
            raise task.exception()

        conn_or_none = task.result()
        if conn_or_none is None:
            # Worker returned the conn to the pool itself on miss.
            try:
                self._semaphore.release()
            except Exception:
                logger.exception(
                    "PgKeyLock: semaphore.release() failed after lock miss"
                )
            if pending_cancel is not None:
                raise pending_cancel
            return None

        # Worker acquired the lock and handed us the live conn.
        handle = _PgLockHandle(
            pool=self._pool,
            conn=conn_or_none,
            semaphore=self._semaphore,
            label=label,
        )
        if pending_cancel is not None:
            # Outer was cancelled after the worker created a live handle.
            # Release the orphan handle before propagating cancellation so
            # the conn and semaphore are accounted for. The release itself
            # also uses the shield-loop pattern internally, so a fresh
            # cancellation during release doesn't leak either.
            release_task = asyncio.ensure_future(handle.release())
            try:
                await _await_to_thread_completion(release_task)
            except Exception:
                logger.exception(
                    "PgKeyLock: orphan handle release wait raised for label=%s",
                    label,
                )
            raise pending_cancel

        return handle

    def close(self) -> None:
        """Close all pool connections. Intended for shutdown/tests."""
        try:
            self._pool.closeall()
        except Exception:
            logger.exception("PgKeyLock: error closing pool")


__all__ = [
    "ProviderBKeyLock",
    "NullKeyLock",
    "PgKeyLock",
    "LockHandle",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_POOL_SIZE",
]
