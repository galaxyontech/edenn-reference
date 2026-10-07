"""Agentic Audio's own configuration. Nothing ambient, nothing shared.

Twice this month a database connection went somewhere other than intended, and
both times the cause was the same line of thinking: "the environment will say
where to connect." The shared helper loads the repo's `.env`, that file points
at production, and it OVERRIDES whatever the caller exported — so code that
believed it was talking to a new, empty database ran DDL against a live one.

This module is the correction, as structure rather than as discipline:

* Agentic Audio reads only ``AGENTIC_AUDIO_*`` variables. It never falls back
  to ``PGHOST``, ``DATABASE_URL`` or the repo's `.env`. An unset database is an
  unset database — the studio runs in-memory, loudly, rather than "helpfully"
  finding someone else's.
* The database config it builds carries the resolved host and database NAME,
  and :func:`assert_connected_where_intended` verifies the LIVE connection
  matches before anything is allowed to write. The check costs one round trip
  and would have caught both incidents at the first statement.
* Hosts belonging to the rest of the platform are refused outright, by name.
  Isolation is the product decision; this is where it is enforced.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

ENV_PREFIX = "AGENTIC_AUDIO_"

# The rest of the platform's database servers, by name. Agentic Audio may never
# connect to these, whatever the environment says: it owns its own database, and
# a config that points here is a config that leaked in from the monolith.
FORBIDDEN_HOST_MARKERS = (
    "telemetry-db",
    "telemetry-db-dev",
    "billing-db",
    "db.example.invalid",
)


class ForbiddenDatabase(RuntimeError):
    """The configured database belongs to the rest of the platform."""


class WrongDatabase(RuntimeError):
    """The live connection does not match the configured target."""


@dataclass(frozen=True)
class DatabaseTarget:
    """Where Agentic Audio's data lives — stated, not discovered."""

    host: str
    port: int
    database: str
    user: str
    password: str
    sslmode: str = "require"

    def describe(self) -> str:
        return f"{self.database}@{self.host}:{self.port}"


def database_target() -> Optional[DatabaseTarget]:
    """The studio's own database, from its own namespace only.

    ``None`` means no database is configured, and the studio runs in-memory.
    That is a supported mode, not an error — what is NOT supported is quietly
    borrowing a connection from the platform's environment.
    """

    host = os.getenv(f"{ENV_PREFIX}PG_HOST", "").strip()
    if not host:
        return None
    for marker in FORBIDDEN_HOST_MARKERS:
        if marker in host:
            raise ForbiddenDatabase(
                f"AGENTIC_AUDIO_PG_HOST points at {host!r}, which belongs to the "
                "rest of the platform. Agentic Audio owns its own database; "
                "point the variable at it."
            )
    return DatabaseTarget(
        host=host,
        port=int(os.getenv(f"{ENV_PREFIX}PG_PORT", "5432") or "5432"),
        database=os.getenv(f"{ENV_PREFIX}PG_DATABASE", "agentic_audio").strip()
        or "agentic_audio",
        user=os.getenv(f"{ENV_PREFIX}PG_USER", "").strip(),
        password=os.getenv(f"{ENV_PREFIX}PG_PASSWORD", ""),
        sslmode=os.getenv(f"{ENV_PREFIX}PG_SSLMODE", "require").strip() or "require",
    )


def client_factory():
    """A connection factory bound to the studio's own target, or ``None``.

    Built on the explicit-config constructor, never ``from_env`` — ``from_env``
    is the function that reads the repo's `.env`, and calling it from anywhere
    under AgenticAudio is exactly the mistake this module exists to prevent.
    A contract test enforces that with a grep, because the difference between
    "we don't" and "we can't" is what a 3am incident turns on.
    """

    target = database_target()
    if target is None:
        return None

    from EdennCode.Deployment.postgres_wrapper import (
        PostgresClient,
        PostgresConnectionConfig,
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

    def make() -> Any:
        client = PostgresClient(config)
        assert_connected_where_intended(client, target)
        return client

    return make


def assert_connected_where_intended(client: Any, target: DatabaseTarget) -> None:
    """Prove the live connection is the intended one before anything writes.

    One round trip. Both incidents this month would have stopped at this line
    instead of after four migrations.
    """

    rows = client.run_sql(
        "SELECT current_database() AS db, current_setting('is_superuser') AS su"
    )
    actual = str(rows[0]["db"]) if rows else ""
    if actual != target.database:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        raise WrongDatabase(
            f"Connected to database {actual!r} but the configured target is "
            f"{target.database!r} ({target.describe()}). Refusing to continue — "
            "this is how a migration ends up on the wrong server."
        )


__all__ = [
    "DatabaseTarget",
    "ENV_PREFIX",
    "FORBIDDEN_HOST_MARKERS",
    "ForbiddenDatabase",
    "WrongDatabase",
    "assert_connected_where_intended",
    "client_factory",
    "database_target",
]
