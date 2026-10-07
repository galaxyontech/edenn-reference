"""Process-wide, lazily-built Postgres connection pool for the API tier.

The client-facing API process runs many short repository / queue / store
operations per request. With the default ``PostgresClient.from_env`` factory each
one opens a fresh TLS connection against the shared database and tears it down —
so a client polling N jobs sustains a stream of new connections per second, which
can exhaust the shared server's connection slots and starve the worker fleet.

This module hands out a single process-wide :class:`PostgresConnectionPool` whose
``client`` is a drop-in ``client_factory`` for the repository / queue / stores.
Those operations then reuse warm connections instead of reconnecting.

The pool is built lazily on first use — construction is deferred until the first
borrow — so importing the app and wiring the routers never requires DB
connectivity (tests and no-DB environments). ``minconn=1`` (as the workers use)
is what actually enables reuse: psycopg2's pool only keeps a returned connection
when fewer than ``minconn`` are free, so ``minconn=0`` would silently close every
connection on release and reconnect on the next borrow. The underlying pool is
thread-safe, which matters because the API serves sync endpoints on a threadpool.
"""
from __future__ import annotations

import os
import threading
from typing import Optional

from EdennCode.Deployment.postgres_wrapper import (
    PostgresClient,
    PostgresConnectionConfig,
    PostgresConnectionPool,
)

_DEFAULT_MAX_CONN = 10

_lock = threading.Lock()
_pool: Optional[PostgresConnectionPool] = None


def _max_conn() -> int:
    raw = os.getenv("API_PG_POOL_MAX_CONN", "").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 0
    return value if value >= 1 else _DEFAULT_MAX_CONN


def get_api_pg_pool() -> PostgresConnectionPool:
    """Return the process-wide pool, building it once on first call."""
    global _pool
    pool = _pool
    if pool is None:
        with _lock:
            pool = _pool
            if pool is None:
                pool = PostgresConnectionPool(
                    PostgresConnectionConfig.from_env(),
                    minconn=1,
                    maxconn=_max_conn(),
                )
                _pool = pool
    return pool


def api_pg_client() -> PostgresClient:
    """Pooled ``client_factory``: a drop-in for ``PostgresClient.from_env``."""
    return get_api_pg_pool().client()


def reset_api_pg_pool_for_tests() -> None:
    """Dispose of the process pool (test isolation only)."""
    global _pool
    with _lock:
        if _pool is not None:
            try:
                _pool.closeall()
            except Exception:
                pass
        _pool = None


__all__ = ["get_api_pg_pool", "api_pg_client", "reset_api_pg_pool_for_tests"]
