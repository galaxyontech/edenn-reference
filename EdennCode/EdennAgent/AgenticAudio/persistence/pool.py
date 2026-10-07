"""One pooled connection factory for the studio's repositories.

Every repository operation opened a NEW connection: connect, TLS handshake,
authenticate, run one statement, close. On a managed Postgres that handshake
costs more than most of the queries do, and the session list alone makes several
per page load.

``PostgresConnectionPool`` already existed for the pipeline workers and its
``client`` method is deliberately shaped as a drop-in for the ``client_factory``
the repositories take. The studio simply never used it.

The pool is process-wide and created on first use, because the repositories are
constructed before the environment is necessarily complete, and a pool built at
import time would connect during a module import.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_pool: Optional[Any] = None


def _max_connections() -> int:
    """Small on purpose.

    Every borrow is taken and returned inside one synchronous repository call,
    so borrows do not overlap within a request; the ceiling is there to stop a
    runaway from exhausting the database's own connection limit, which is a
    shared resource other services also draw on.
    """
    try:
        return max(1, int(os.getenv("AGENTIC_AUDIO_PG_MAX_CONNECTIONS", "8")))
    except ValueError:
        return 8


def pooled_client() -> Any:
    """Borrow a client from the shared pool.

    Falls back to a per-operation connection if a pool cannot be built — a
    degraded but working database is better than a service that will not start,
    and the fallback is exactly the old behaviour.
    """
    global _pool
    from EdennCode.Deployment.postgres_wrapper import (
        PostgresClient,
        PostgresConnectionConfig,
        PostgresConnectionPool,
    )
    from ..config import database_target

    # The studio's OWN target, from its own namespace. Never the repo's `.env`:
    # that file points at the platform's databases, and "the environment will
    # say where to connect" is the exact line of thinking that ran migrations
    # against a production server this month.
    target = database_target()
    if target is None:
        raise RuntimeError(
            "No AGENTIC_AUDIO_PG_HOST configured. The Postgres repositories "
            "need the studio's own database; for a database-less run use the "
            "in-memory repositories instead."
        )
    config = PostgresConnectionConfig(
        host=target.host,
        port=target.port,
        database=target.database,
        user=target.user,
        password=target.password,
        sslmode=target.sslmode,
        application_name="edenn-agentic-audio",
    )

    if _pool is None:
        with _lock:
            if _pool is None:
                try:
                    _pool = PostgresConnectionPool(
                        config,
                        minconn=1,
                        maxconn=_max_connections(),
                    )
                except Exception as exc:  # noqa: BLE001 - fall back, do not fail
                    logger.warning(
                        "connection pool unavailable (%s); falling back to a "
                        "connection per operation",
                        exc,
                    )
                    return PostgresClient(config)
    return _pool.client()


def reset_for_tests() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            try:
                _pool.closeall()
            except Exception:  # noqa: BLE001
                pass
        _pool = None


__all__ = ["pooled_client", "reset_for_tests"]
