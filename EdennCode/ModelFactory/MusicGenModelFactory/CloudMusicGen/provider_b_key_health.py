"""Rate-limit cooldown tracking for the ProviderB API key pool.

The advisory lock prevents simultaneous use of the same key. This module
handles the next failure mode: a key can remain rate-limited after the lock is
released, so the next request should skip it for a short cooldown window.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Protocol, TypeVar

import psycopg2.pool

from EdennCode.Deployment.postgres_wrapper import PostgresConnectionConfig
from EdennCode.exceptions import EdennConfigurationError

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN_BASE_S = 60.0
DEFAULT_COOLDOWN_MAX_S = 600.0
DEFAULT_POOL_SIZE = 4
SCHEMA_INIT_LOCK_ID = 4_834_299_052_017_117_301

_T = TypeVar("_T")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise EdennConfigurationError(
            f"{name} must be an integer",
            component="provider_b_key_health",
            operation="from_env",
        ) from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise EdennConfigurationError(
            f"{name} must be a number",
            component="provider_b_key_health",
            operation="from_env",
        ) from exc


class ProviderBKeyHealthStore(Protocol):
    """Small interface for shared per-key cooldown state."""

    async def cooldown_remaining_s(self, label: str) -> float:
        """Return seconds remaining in the key cooldown, or 0 if usable."""
        ...

    async def mark_rate_limited(
        self,
        label: str,
        *,
        reason: str = "",
    ) -> float:
        """Put ``label`` in cooldown and return the applied cooldown seconds."""
        ...

    async def mark_success(self, label: str) -> None:
        """Clear any cooldown/failure history for a key after a clean success."""
        ...


@dataclass
class _CooldownEntry:
    cooldown_until: float
    failure_count: int
    reason: str = ""


class InMemoryProviderBKeyHealthStore:
    """Process-local cooldown state.

    This is correct for unit tests and single-process local runs. Multi-process
    local tests and multi-replica production should use ``PgProviderBKeyHealthStore``.
    """

    def __init__(
        self,
        *,
        cooldown_base_s: float = DEFAULT_COOLDOWN_BASE_S,
        cooldown_max_s: float = DEFAULT_COOLDOWN_MAX_S,
    ) -> None:
        self._cooldown_base_s = max(0.1, float(cooldown_base_s))
        self._cooldown_max_s = max(self._cooldown_base_s, float(cooldown_max_s))
        self._entries: Dict[str, _CooldownEntry] = {}
        self._lock = threading.Lock()

    def _cooldown_for_failure_count(self, failure_count: int) -> float:
        exponent = min(max(0, failure_count - 1), 10)
        return min(self._cooldown_max_s, self._cooldown_base_s * (2 ** exponent))

    async def cooldown_remaining_s(self, label: str) -> float:
        now = time.time()
        with self._lock:
            entry = self._entries.get(label)
            if entry is None:
                return 0.0
            remaining_s = entry.cooldown_until - now
            if remaining_s <= 0:
                self._entries.pop(label, None)
                return 0.0
            return remaining_s

    async def mark_rate_limited(
        self,
        label: str,
        *,
        reason: str = "",
    ) -> float:
        now = time.time()
        with self._lock:
            entry = self._entries.get(label)
            previous_count = (
                entry.failure_count
                if entry is not None and entry.cooldown_until > now
                else 0
            )
            failure_count = previous_count + 1
            cooldown_s = self._cooldown_for_failure_count(failure_count)
            self._entries[label] = _CooldownEntry(
                cooldown_until=now + cooldown_s,
                failure_count=failure_count,
                reason=reason[:500],
            )
            return cooldown_s

    async def mark_success(self, label: str) -> None:
        with self._lock:
            self._entries.pop(label, None)

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        now = time.time()
        with self._lock:
            return {
                label: {
                    "cooldown_remaining_s": max(0.0, entry.cooldown_until - now),
                    "failure_count": entry.failure_count,
                    "reason": entry.reason,
                }
                for label, entry in self._entries.items()
            }


class PgProviderBKeyHealthStore:
    """Postgres-backed cooldown state shared by all workers/replicas."""

    def __init__(
        self,
        *,
        pool: psycopg2.pool.ThreadedConnectionPool,
        cooldown_base_s: float = DEFAULT_COOLDOWN_BASE_S,
        cooldown_max_s: float = DEFAULT_COOLDOWN_MAX_S,
    ) -> None:
        self._pool = pool
        self._cooldown_base_s = max(0.1, float(cooldown_base_s))
        self._cooldown_max_s = max(self._cooldown_base_s, float(cooldown_max_s))

    @classmethod
    def from_env(cls) -> "PgProviderBKeyHealthStore":
        max_pool_size = _env_int("PROVIDER_B_KEY_HEALTH_POOL_SIZE", DEFAULT_POOL_SIZE)
        if max_pool_size < 1:
            raise EdennConfigurationError(
                "PROVIDER_B_KEY_HEALTH_POOL_SIZE must be >= 1",
                component="provider_b_key_health",
                operation="from_env",
            )
        cooldown_base_s = _env_float(
            "PROVIDER_B_KEY_COOLDOWN_BASE_S",
            DEFAULT_COOLDOWN_BASE_S,
        )
        cooldown_max_s = _env_float(
            "PROVIDER_B_KEY_COOLDOWN_MAX_S",
            DEFAULT_COOLDOWN_MAX_S,
        )
        config = PostgresConnectionConfig.from_env()
        pool = cls._build_pool(config, max_pool_size)
        store = cls(
            pool=pool,
            cooldown_base_s=cooldown_base_s,
            cooldown_max_s=cooldown_max_s,
        )
        try:
            store._startup_check()
        except Exception:
            try:
                pool.closeall()
            except Exception:
                logger.exception(
                    "PgProviderBKeyHealthStore: failed to close pool after startup failure"
                )
            raise
        logger.info(
            "PgProviderBKeyHealthStore initialized; max_pool_size=%d cooldown_base_s=%.1f cooldown_max_s=%.1f",
            max_pool_size,
            store._cooldown_base_s,
            store._cooldown_max_s,
        )
        return store

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

    def _execute_blocking(self, fn: Callable[[Any], _T]) -> _T:
        conn = self._pool.getconn()
        close = False
        try:
            result = fn(conn)
            try:
                conn.commit()
            except Exception:
                close = True
                raise
            return result
        except Exception:
            try:
                conn.rollback()
            except Exception:
                close = True
            raise
        finally:
            try:
                self._pool.putconn(conn, close=close)
            except Exception:
                logger.exception(
                    "PgProviderBKeyHealthStore: failed to return connection to pool"
                )

    async def _run_blocking(self, fn: Callable[[Any], _T]) -> _T:
        return await asyncio.to_thread(self._execute_blocking, fn)

    def _startup_check(self) -> None:
        def _init(conn: Any) -> None:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (SCHEMA_INIT_LOCK_ID,),
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS provider_b_key_cooldowns (
                        key_label TEXT PRIMARY KEY,
                        cooldown_until TIMESTAMPTZ NOT NULL,
                        failure_count INTEGER NOT NULL DEFAULT 0,
                        reason TEXT NOT NULL DEFAULT '',
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS provider_b_key_cooldowns_until_idx
                    ON provider_b_key_cooldowns (cooldown_until)
                    """
                )
                cur.execute("SELECT 1")
                cur.fetchone()

        self._execute_blocking(_init)

    async def cooldown_remaining_s(self, label: str) -> float:
        def _query(conn: Any) -> float:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT EXTRACT(EPOCH FROM (cooldown_until - NOW()))
                    FROM provider_b_key_cooldowns
                    WHERE key_label = %s
                      AND cooldown_until > NOW()
                    """,
                    (label,),
                )
                row = cur.fetchone()
            if not row or row[0] is None:
                return 0.0
            return max(0.0, float(row[0]))

        return await self._run_blocking(_query)

    async def mark_rate_limited(
        self,
        label: str,
        *,
        reason: str = "",
    ) -> float:
        def _mark(conn: Any) -> float:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT failure_count, cooldown_until > NOW()
                    FROM provider_b_key_cooldowns
                    WHERE key_label = %s
                    FOR UPDATE
                    """,
                    (label,),
                )
                row = cur.fetchone()
                previous_count = int(row[0]) if row and row[1] else 0
                failure_count = previous_count + 1
                exponent = min(max(0, failure_count - 1), 10)
                cooldown_s = min(
                    self._cooldown_max_s,
                    self._cooldown_base_s * (2 ** exponent),
                )
                cur.execute(
                    """
                    INSERT INTO provider_b_key_cooldowns (
                        key_label,
                        cooldown_until,
                        failure_count,
                        reason,
                        updated_at
                    )
                    VALUES (
                        %s,
                        NOW() + (%s * INTERVAL '1 second'),
                        %s,
                        %s,
                        NOW()
                    )
                    ON CONFLICT (key_label) DO UPDATE SET
                        cooldown_until = EXCLUDED.cooldown_until,
                        failure_count = EXCLUDED.failure_count,
                        reason = EXCLUDED.reason,
                        updated_at = NOW()
                    """,
                    (label, cooldown_s, failure_count, reason[:500]),
                )
                return float(cooldown_s)

        return await self._run_blocking(_mark)

    async def mark_success(self, label: str) -> None:
        def _clear(conn: Any) -> None:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM provider_b_key_cooldowns WHERE key_label = %s",
                    (label,),
                )

        await self._run_blocking(_clear)

    def close(self) -> None:
        try:
            self._pool.closeall()
        except Exception:
            logger.exception("PgProviderBKeyHealthStore: error closing pool")


__all__ = [
    "ProviderBKeyHealthStore",
    "InMemoryProviderBKeyHealthStore",
    "PgProviderBKeyHealthStore",
    "DEFAULT_COOLDOWN_BASE_S",
    "DEFAULT_COOLDOWN_MAX_S",
    "DEFAULT_POOL_SIZE",
]
