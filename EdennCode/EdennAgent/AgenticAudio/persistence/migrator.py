"""Applying migrations once, in order, with one replica doing it.

What this replaces: every repository re-ran its whole SQL file on the first
request after a cold start. That works only because every statement was written
to be harmless the second time — so the schema could never contain a statement
that isn't, which is why adding a column meant `ADD COLUMN IF NOT EXISTS` and
why a data backfill had to be phrased as an upsert. There was no record of what
had been applied, so "which version is this database on" had no answer, and
nothing stopped two replicas from running DDL against the same table at the same
moment during a rolling deploy.

Three things fix that:

* a **ledger** — one row per applied migration, so a file is applied once and
  the database can say what it is running;
* an **advisory lock** — one replica applies while the others wait, then find
  the work already done;
* a **checksum** — a file that changed after being applied is a mistake worth
  refusing, because the database no longer matches the source that claims to
  describe it.

Migrations still run at startup rather than from a separate deploy step. That is
a deliberate compromise for a service that is deployed as one container image
and nothing else: it means a fresh database works with no operator ritual. The
lock is what makes it safe.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# One 64-bit key, so every replica of this service contends on the same lock.
# Derived from the name rather than typed as a magic number, because a colliding
# literal in another service would serialise two unrelated deploys.
_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"edenn.agentic_audio.migrations").digest()[:8], "big", signed=True
)

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS agentic_audio_schema_migrations (
  name TEXT PRIMARY KEY,
  checksum TEXT NOT NULL,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


class MigrationChanged(RuntimeError):
    """An already-applied migration file has been edited.

    The database was built by the old text; the repository now claims the new
    text describes it. Editing a migration that has run is how two environments
    end up with silently different schemas, so this refuses rather than guesses.
    """


@dataclass(frozen=True)
class Migration:
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        # Whitespace-insensitive: reformatting a file should not look like a
        # schema change, while any real edit does.
        normalized = re.sub(r"\s+", " ", self.sql).strip()
        return hashlib.sha256(normalized.encode()).hexdigest()[:32]


def discover(directory: Path) -> list[Migration]:
    """Every ``*.sql`` in the directory, in filename order.

    The numeric prefix IS the order, which is why 004 must not be renamed once
    it has run anywhere.
    """
    files = sorted(p for p in directory.glob("*.sql") if p.is_file())
    return [Migration(name=p.name, sql=p.read_text(encoding="utf-8")) for p in files]


def apply_all(
    client_factory: Callable[[], Any],
    migrations: Iterable[Migration],
    *,
    lock_key: int = _LOCK_KEY,
) -> list[str]:
    """Apply whatever has not been applied. Returns the names that ran.

    Safe to call from every replica on every start: the lock serialises them and
    the ledger makes the losers no-ops.
    """
    pending = list(migrations)
    if not pending:
        return []

    applied: list[str] = []
    with client_factory() as client:
        # The ledger has to exist before it can be consulted, and creating it is
        # itself idempotent.
        client.run_sql(LEDGER_DDL)

        # Session-scoped advisory lock. Held for the whole run and released by
        # the unlock below — or by the connection closing, which is what makes a
        # crashed replica release it instead of blocking every future deploy.
        client.run_sql("SELECT pg_advisory_lock(%s)", params=[lock_key])
        try:
            rows = client.run_sql(
                "SELECT name, checksum FROM agentic_audio_schema_migrations"
            )
            seen = {
                str(r["name"]): str(r["checksum"])
                for r in (rows if isinstance(rows, list) else [])
            }

            for migration in pending:
                previous = seen.get(migration.name)
                if previous is not None:
                    if previous != migration.checksum:
                        raise MigrationChanged(
                            f"{migration.name} was applied with a different body "
                            f"({previous} != {migration.checksum}). The database was "
                            "built by the old text; write a NEW migration instead of "
                            "editing one that has run."
                        )
                    continue

                logger.info("applying migration %s", migration.name)
                with client.transaction() as tx:
                    tx.run_sql(migration.sql)
                    tx.run_sql(
                        """
                        INSERT INTO agentic_audio_schema_migrations (name, checksum)
                        VALUES (%s, %s)
                        ON CONFLICT (name) DO NOTHING
                        """,
                        params=[migration.name, migration.checksum],
                    )
                applied.append(migration.name)
        finally:
            client.run_sql("SELECT pg_advisory_unlock(%s)", params=[lock_key])
    return applied


def applied_names(client_factory: Callable[[], Any]) -> list[str]:
    """What this database has on it — the answer that did not exist before."""
    with client_factory() as client:
        client.run_sql(LEDGER_DDL)
        rows = client.run_sql(
            "SELECT name FROM agentic_audio_schema_migrations ORDER BY name"
        )
    return [str(r["name"]) for r in (rows if isinstance(rows, list) else [])]


__all__ = [
    "LEDGER_DDL",
    "Migration",
    "MigrationChanged",
    "apply_all",
    "applied_names",
    "discover",
]
