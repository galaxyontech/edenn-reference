"""The migration runner, against a real database.

Applied-once, ordered, one replica at a time. Every property here is about what
Postgres does — advisory locks, transaction rollback, unique constraints — so
these run against a throwaway database and skip without one. See
``Testing/README.md``.
"""

from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from EdennCode.EdennAgent.AgenticAudio.persistence import migrator

DSN = os.getenv("EDENN_TEST_PG_DSN", "").strip()

pytestmark = pytest.mark.skipif(
    not DSN, reason="set EDENN_TEST_PG_DSN to a throwaway Postgres to run these"
)


def _factory():
    from EdennCode.Deployment.postgres_wrapper import (
        PostgresClient,
        PostgresConnectionConfig,
    )

    parsed = urlparse(DSN)

    def make() -> Any:
        return PostgresClient(
            PostgresConnectionConfig(
                host=parsed.hostname or "127.0.0.1",
                port=parsed.port or 5432,
                database=(parsed.path or "/postgres").lstrip("/"),
                user=parsed.username or "postgres",
                password=parsed.password,
                sslmode="disable",
                connect_timeout=10,
            )
        )

    return make


@pytest.fixture()
def factory():
    return _factory()


@pytest.fixture()
def table() -> str:
    """A unique table name per test, so tests share a database without sharing
    state."""
    return f"mig_probe_{uuid.uuid4().hex[:10]}"


@pytest.fixture(autouse=True)
def _clean_ledger(factory):
    yield
    # Leave the database as we found it: the ledger is global to the database
    # and a leftover row would make the next run think work was already done.
    with factory() as client:
        client.run_sql(
            "DELETE FROM agentic_audio_schema_migrations WHERE name LIKE %s",
            params=["zz_test_%"],
        )


def _mig(name: str, sql: str) -> migrator.Migration:
    return migrator.Migration(name=f"zz_test_{name}", sql=sql)


# ---------------------------------------------------------------------------#
# applied once, in order                                                      #
# ---------------------------------------------------------------------------#


def test_a_migration_runs_once_and_is_recorded(factory, table) -> None:
    m = _mig("001.sql", f"CREATE TABLE {table} (id INT)")
    assert migrator.apply_all(factory, [m]) == [m.name]
    # Second call is a no-op — the point of the ledger.
    assert migrator.apply_all(factory, [m]) == []
    assert m.name in migrator.applied_names(factory)

    with factory() as client:
        client.run_sql(f"DROP TABLE IF EXISTS {table}")


def test_a_statement_that_is_not_idempotent_is_now_safe(factory, table) -> None:
    """The old runner replayed every file on every cold start, so the schema
    could only ever contain statements that survive being run twice. This is a
    plain CREATE TABLE — it would fail on the second run without the ledger."""
    m = _mig("002.sql", f"CREATE TABLE {table} (id INT)")
    migrator.apply_all(factory, [m])
    migrator.apply_all(factory, [m])  # must not raise
    with factory() as client:
        client.run_sql(f"DROP TABLE IF EXISTS {table}")


def test_migrations_apply_in_filename_order(factory, table) -> None:
    """The numeric prefix IS the order: a later file may depend on an earlier
    one having run."""
    create = _mig("003_a.sql", f"CREATE TABLE {table} (id INT)")
    alter = _mig("003_b.sql", f"ALTER TABLE {table} ADD COLUMN label TEXT")
    ran = migrator.apply_all(factory, [create, alter])
    assert ran == [create.name, alter.name]
    with factory() as client:
        client.run_sql(f"DROP TABLE IF EXISTS {table}")


def test_only_the_pending_ones_run(factory, table) -> None:
    first = _mig("004_a.sql", f"CREATE TABLE {table} (id INT)")
    migrator.apply_all(factory, [first])
    second = _mig("004_b.sql", f"ALTER TABLE {table} ADD COLUMN note TEXT")
    assert migrator.apply_all(factory, [first, second]) == [second.name]
    with factory() as client:
        client.run_sql(f"DROP TABLE IF EXISTS {table}")


# ---------------------------------------------------------------------------#
# failure                                                                     #
# ---------------------------------------------------------------------------#


def test_a_failing_migration_is_not_recorded_as_applied(factory) -> None:
    """Otherwise a broken deploy marks the work done and the next one skips it,
    leaving a database that permanently disagrees with the ledger."""
    bad = _mig("005.sql", "CREATE TABLE (this is not sql")
    with pytest.raises(Exception):
        migrator.apply_all(factory, [bad])
    assert bad.name not in migrator.applied_names(factory)


def test_a_failing_migration_leaves_no_half_applied_schema(factory, table) -> None:
    """Both statements are in one transaction, so the first must not survive the
    second's failure."""
    bad = _mig(
        "006.sql",
        f"CREATE TABLE {table} (id INT); CREATE TABLE {table} (id INT);",
    )
    with pytest.raises(Exception):
        migrator.apply_all(factory, [bad])
    with factory() as client:
        rows = client.run_sql(
            "SELECT to_regclass(%s) AS present", params=[table]
        )
    assert rows[0]["present"] is None, "a half-applied migration survived"


def test_editing_an_applied_migration_is_refused(factory, table) -> None:
    """The database was built by the old text. Editing a migration that has run
    is how two environments end up with silently different schemas."""
    original = _mig("007.sql", f"CREATE TABLE {table} (id INT)")
    migrator.apply_all(factory, [original])

    edited = migrator.Migration(
        name=original.name, sql=f"CREATE TABLE {table} (id INT, extra TEXT)"
    )
    with pytest.raises(migrator.MigrationChanged) as exc:
        migrator.apply_all(factory, [edited])
    assert "write a NEW migration" in str(exc.value)

    with factory() as client:
        client.run_sql(f"DROP TABLE IF EXISTS {table}")


def test_reformatting_a_migration_is_not_an_edit(factory, table) -> None:
    """Whitespace is not schema — a reformat should not fail a deploy."""
    original = _mig("008.sql", f"CREATE TABLE {table} (id INT)")
    migrator.apply_all(factory, [original])
    reflowed = migrator.Migration(
        name=original.name, sql=f"CREATE TABLE   {table}\n  (id INT)\n"
    )
    assert migrator.apply_all(factory, [reflowed]) == []
    with factory() as client:
        client.run_sql(f"DROP TABLE IF EXISTS {table}")


# ---------------------------------------------------------------------------#
# two replicas                                                                #
# ---------------------------------------------------------------------------#


def test_concurrent_replicas_apply_a_migration_exactly_once(factory, table) -> None:
    """A rolling deploy starts several replicas at once and every one of them
    calls this. Without the lock they run DDL against the same table together.
    """
    m = _mig("009.sql", f"CREATE TABLE {table} (id INT)")
    results: list[list[str]] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(5)

    def run() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(migrator.apply_all(_factory(), [m]))
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"a replica failed to migrate: {errors[:2]}"
    ran = [name for r in results for name in r]
    assert ran == [m.name], f"applied {len(ran)} times, expected once"

    with factory() as client:
        client.run_sql(f"DROP TABLE IF EXISTS {table}")


def test_the_lock_is_released_even_when_a_migration_fails(factory, table) -> None:
    """A failed deploy must not leave the lock held, or every later deploy hangs
    waiting for a replica that is already gone."""
    bad = _mig("010.sql", "CREATE TABLE (nope")
    with pytest.raises(Exception):
        migrator.apply_all(factory, [bad])

    good = _mig("011.sql", f"CREATE TABLE {table} (id INT)")
    assert migrator.apply_all(factory, [good]) == [good.name]
    with factory() as client:
        client.run_sql(f"DROP TABLE IF EXISTS {table}")


# ---------------------------------------------------------------------------#
# discovery                                                                   #
# ---------------------------------------------------------------------------#


def test_discovery_returns_the_real_migrations_in_order() -> None:
    directory = Path(migrator.__file__).resolve().parent / "migrations"
    names = [m.name for m in migrator.discover(directory)]
    assert names == sorted(names)
    assert "001_agentic_audio.sql" in names
    assert "004_session_version.sql" in names


def test_every_shipped_migration_has_a_stable_checksum() -> None:
    """Two runs of the same file must agree, or every start looks like an edit."""
    directory = Path(migrator.__file__).resolve().parent / "migrations"
    first = {m.name: m.checksum for m in migrator.discover(directory)}
    second = {m.name: m.checksum for m in migrator.discover(directory)}
    assert first == second
