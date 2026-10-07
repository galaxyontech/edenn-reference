"""The schema, checked without a database.

A migration is the one piece of code that runs exactly once against real data,
so its mistakes are the expensive kind. These tests read the SQL as text and
assert the properties that matter — they connect to nothing, so they run in CI
and on a laptop with no Postgres and no credentials.

They are not a substitute for applying it against a real database once, in a
staging copy. They are the gate that catches the obvious wrongness first.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

MIGRATIONS = (
    Path(__file__).resolve().parent.parent / "persistence" / "migrations"
)
USERS_SQL = (MIGRATIONS / "003_users.sql").read_text(encoding="utf-8")
VERSION_SQL = (MIGRATIONS / "004_session_version.sql").read_text(encoding="utf-8")
SESSIONS_SQL = (MIGRATIONS / "001_agentic_audio.sql").read_text(encoding="utf-8")
COLLAB_SQL = (MIGRATIONS / "002_collab.sql").read_text(encoding="utf-8")
JOBS_SQL = (MIGRATIONS / "005_jobs.sql").read_text(encoding="utf-8")
ACCOUNTS_SQL = (MIGRATIONS / "006_accounts.sql").read_text(encoding="utf-8")
METER_SQL = (MIGRATIONS / "007_usage_meter.sql").read_text(encoding="utf-8")


def _statements(sql: str) -> list[str]:
    """Statements, with comments stripped — so a rule in a comment never counts
    as a rule that was actually written.

    A ``DO $$ ... $$`` block is kept whole: it is full of semicolons, and
    splitting it would invent statements nobody wrote and then judge them.
    """
    without_comments = re.sub(r"--[^\n]*", "", sql)
    out: list[str] = []
    for chunk in re.split(r"(DO \$\$.*?\$\$)", without_comments, flags=re.S):
        if chunk.startswith("DO $$"):
            out.append(chunk.strip())
            continue
        out.extend(s.strip() for s in chunk.split(";") if s.strip())
    return out


# ---------------------------------------------------------------------------#
# re-runnable                                                                 #
# ---------------------------------------------------------------------------#


@pytest.mark.parametrize(
    "name,sql",
    [
        ("001", SESSIONS_SQL),
        ("002", COLLAB_SQL),
        ("003", USERS_SQL),
        ("004", VERSION_SQL),
        ("005", JOBS_SQL),
        ("006", ACCOUNTS_SQL),
        ("007", METER_SQL),
    ],
)
def test_every_migration_can_be_applied_twice(name: str, sql: str) -> None:
    """``ensure_schema`` runs these on every cold start, so a statement that
    fails the second time takes the whole service down on a restart."""
    for statement in _statements(sql):
        head = statement.upper()
        if head.startswith("CREATE TABLE"):
            assert "IF NOT EXISTS" in head, f"{name}: {statement[:60]}"
        elif head.startswith("CREATE INDEX") or head.startswith("CREATE UNIQUE INDEX"):
            assert "IF NOT EXISTS" in head, f"{name}: {statement[:60]}"
        elif head.startswith("ALTER TABLE"):
            # An ALTER is the one shape that cannot be re-derived from scratch,
            # so it has to guard itself.
            assert "IF NOT EXISTS" in head, f"{name}: {statement[:60]}"
        elif head.startswith("DO $$"):
            # Postgres has no ADD CONSTRAINT IF NOT EXISTS, so the guard is a
            # catalog check — which is the same promise written differently.
            assert "IF NOT EXISTS" in head or "IF EXISTS" in head, (
                f"{name}: an unguarded DO block: {statement[:60]}"
            )
        elif head.startswith("UPDATE"):
            # A backfill by UPDATE is re-runnable when it only touches rows
            # that still need it.
            assert "WHERE" in head, (
                f"{name}: an unbounded UPDATE re-runs over the whole table: "
                f"{statement[:60]}"
            )
        elif head.startswith("INSERT"):
            assert "ON CONFLICT" in head, (
                f"{name}: a backfill without ON CONFLICT fails on the second run: "
                f"{statement[:60]}"
            )


# ---------------------------------------------------------------------------#
# the users table                                                             #
# ---------------------------------------------------------------------------#


def test_the_users_table_is_keyed_on_the_principal() -> None:
    """``user_id`` holds exactly what ``resolve_caller`` returns, so ownership
    needs no translation layer."""
    assert "CREATE TABLE IF NOT EXISTS agentic_audio_users" in USERS_SQL
    assert re.search(r"user_id\s+TEXT\s+PRIMARY KEY", USERS_SQL)


def test_nothing_narrows_what_a_principal_may_look_like() -> None:
    """A CHECK on the id format is a login that cannot happen: uids, legacy
    token names and future identity sources do not share a shape."""
    live = re.sub(r"--[^\n]*", "", USERS_SQL)
    assert "CHECK" not in live.upper(), "a CHECK constraint narrows the principal"


def test_the_sessions_list_finally_has_its_index() -> None:
    """Every "my sessions" was a sequential scan over every session anyone had
    ever created — the most-hit query in the product, unindexed."""
    assert "agentic_audio_sessions_creator_idx" in USERS_SQL
    assert re.search(
        r"ON agentic_audio_sessions\(creator_user_id, created_at DESC\)", USERS_SQL
    )


def test_the_foreign_key_is_deliberately_not_added_yet() -> None:
    """Existing rows carry creator ids that predate any users table, so adding
    the constraint in the same migration as the backfill would either fail or
    force inventing a user for every historical value. It is written down, as a
    comment, with the order that works."""
    live = re.sub(r"--[^\n]*", "", USERS_SQL)
    assert "agentic_audio_sessions_creator_fkey" not in live, (
        "the FK is being added in the same migration as the backfill"
    )
    assert "agentic_audio_sessions_creator_fkey" in USERS_SQL, (
        "the follow-up migration is not documented anywhere"
    )


def test_the_documented_foreign_key_does_not_cascade_deletes() -> None:
    """Deleting an account must not silently destroy the work. What happens to a
    departed user's sessions is a product decision, not a database default."""
    assert "ON DELETE SET NULL" in USERS_SQL
    fk_block = USERS_SQL[USERS_SQL.index("agentic_audio_sessions_creator_fkey"):]
    assert "ON DELETE CASCADE" not in fk_block[:400]


# ---------------------------------------------------------------------------#
# the backfill                                                                #
# ---------------------------------------------------------------------------#


def test_the_backfill_covers_owners_and_collaborators_alike() -> None:
    """Somebody who was only ever invited to another person's session is still a
    user, and would otherwise be missing from the table."""
    assert "FROM agentic_audio_sessions" in USERS_SQL
    assert "FROM agentic_audio_participants" in USERS_SQL


def test_the_backfill_skips_empty_creators() -> None:
    """Sessions created while auth was off carry NULL, and a NULL user row would
    be a user nobody can ever be."""
    assert USERS_SQL.count("IS NOT NULL AND") >= 2
    assert USERS_SQL.count("<> ''") >= 2


def test_backfilled_rows_are_marked_as_legacy() -> None:
    """So a migration can find every account still resting on a static token."""
    assert "'legacy_token'" in USERS_SQL


def test_the_users_migration_sorts_after_the_tables_it_reads() -> None:
    """Its backfill selects from the tables 001 and 002 create.

    The order used to be three hand-written calls in a fixed sequence; it is now
    the filenames, applied in sorted order by the runner. This asserts the
    guarantee rather than the mechanism, so it survives the next change to how
    migrations are applied.
    """
    from EdennCode.EdennAgent.AgenticAudio.persistence.migrator import discover

    names = [m.name for m in discover(MIGRATIONS)]
    assert names.index("003_users.sql") > names.index("001_agentic_audio.sql")
    assert names.index("003_users.sql") > names.index("002_collab.sql")


def test_the_version_alter_sorts_after_the_table_it_alters() -> None:
    from EdennCode.EdennAgent.AgenticAudio.persistence.migrator import discover

    names = [m.name for m in discover(MIGRATIONS)]
    assert names.index("004_session_version.sql") > names.index("001_agentic_audio.sql")


# ---------------------------------------------------------------------------#
# accounts, actors, and the foreign key 003 postponed                          #
# ---------------------------------------------------------------------------#


def test_an_unlinked_user_is_null_rather_than_a_made_up_account() -> None:
    """A uid that has never signed up genuinely has no account, and a lookup is
    allowed to be unavailable. Both are NULL, and NULL means "not
    established" — a DEFAULT here would turn "we don't know" into a fact."""
    column = re.search(
        r"ADD COLUMN IF NOT EXISTS account_id[^;]*", ACCOUNTS_SQL
    )
    assert column, "the users table never gains an account"
    assert "NOT NULL" not in column.group(0).upper()
    assert "DEFAULT" not in column.group(0).upper()


def test_a_job_records_who_ran_it_as_well_as_whose_session_it_is() -> None:
    assert "ADD COLUMN IF NOT EXISTS actor_user_id" in ACCOUNTS_SQL
    backfill = re.search(
        r"UPDATE agentic_audio_jobs[^;]*", ACCOUNTS_SQL
    ).group(0)
    assert "actor_user_id = creator_user_id" in backfill
    assert "WHERE actor_user_id IS NULL" in backfill, (
        "the backfill would overwrite an actor a newer row already recorded"
    )


def test_the_postponed_foreign_key_is_finally_added() -> None:
    live = re.sub(r"--[^\n]*", "", ACCOUNTS_SQL)
    assert "agentic_audio_sessions_creator_fkey" in live
    assert "ON DELETE SET NULL" in live, (
        "deleting an account must not silently destroy the work"
    )
    assert "ON DELETE CASCADE" not in live


def test_the_rows_the_constraint_would_reject_are_dealt_with_first() -> None:
    """The reason 003 postponed it. Two shapes exist that no users table can
    satisfy: a creator with no user row, and the EMPTY creator a session gets
    when it is made with auth off. The first is re-backfilled, the second is
    normalised to NULL — which every reader already treats it as."""
    live = re.sub(r"--[^\n]*", "", ACCOUNTS_SQL)
    backfill = live.index("INSERT INTO agentic_audio_users")
    normalise = live.index("UPDATE agentic_audio_sessions SET creator_user_id = NULL")
    constraint = live.index("agentic_audio_sessions_creator_fkey")

    assert backfill < constraint, "the constraint is added before the backfill"
    assert normalise < constraint, "empty creators would fail the constraint"


def test_the_accounts_migration_sorts_after_the_tables_it_alters() -> None:
    names = sorted(p.name for p in MIGRATIONS.glob("*.sql"))
    assert names.index("006_accounts.sql") > names.index("003_users.sql")
    assert names.index("006_accounts.sql") > names.index("005_jobs.sql")


# ---------------------------------------------------------------------------#
# the usage meter: the bill outlives the footage, and describes none of it     #
# ---------------------------------------------------------------------------#


def test_every_sql_file_is_covered_by_the_re_runnable_test() -> None:
    """The real failure mode of the test above is the next person adding 008
    and not adding it here."""
    covered = {"001", "002", "003", "004", "005", "006", "007"}
    on_disk = {p.name[:3] for p in MIGRATIONS.glob("*.sql")}
    assert on_disk == covered, f"a migration nobody checks: {on_disk - covered}"


def test_the_usage_row_outlives_the_session_it_describes() -> None:
    """Jobs cascade away with their session and the retention sweep deletes
    them on a 7-day window the CUSTOMER controls. A usage row that cascaded too
    would delete the record of money already spent, a week after spending it."""
    live = re.sub(r"--[^\n]*", "", METER_SQL)
    references = re.findall(r"REFERENCES\s+(\w+)", live)

    assert references == ["agentic_audio_sessions"], (
        f"the meter is tied to something that can delete it: {references}"
    )
    assert "ON DELETE SET NULL" in live
    assert "ON DELETE CASCADE" not in live
    # ...and the un-linking must be cheap, because retention deletes in batches.
    assert "agentic_audio_usage_session_idx" in live


def test_a_usage_row_cannot_describe_the_footage() -> None:
    """What makes keeping the row safe: it carries counts, not content. If a
    column could not be printed on an invoice, it does not belong here."""
    live = re.sub(r"--[^\n]*", "", METER_SQL)
    table = live.split("CREATE TABLE IF NOT EXISTS agentic_audio_usage (")[1]
    table = table.split("CREATE UNIQUE INDEX")[0]
    columns = set(re.findall(r"^\s{2}(\w+)\s+[A-Z]", table, re.M))

    banned = {
        "prompt", "script", "title", "description", "label", "url", "blob",
        "filename", "lyrics", "scene", "error", "content", "message",
    }
    assert not (columns & banned), f"content on a row that outlives deletion: {columns & banned}"
    # The two that LOOK like content and are not: a count of characters and a
    # code from a frozen catalogue.
    assert "text_chars" in columns and "note_code" in columns


def test_money_is_not_in_the_meter() -> None:
    """An empty money column is an invitation to fill it with the placeholder
    rate that is already in the repo. Pricing is a separate table, later."""
    live = re.sub(r"--[^\n]*", "", METER_SQL).lower()
    for money in ("price", "cost", "micros", "amount", "charge", "credit"):
        assert money not in live, f"the meter carries money: {money}"


def test_an_unspent_row_cannot_carry_a_quantity() -> None:
    """"Nothing was bought" has to mean nothing was bought. A quantity on an
    unspent row is an invented charge, and the database is a better place to
    refuse that than a code review."""
    assert "agentic_audio_usage_no_spend_ck" in METER_SQL
    guard = METER_SQL.split("agentic_audio_usage_no_spend_ck")[1].split("),")[0]
    for column in ("provider_calls", "lm_input_tokens", "items", "text_chars"):
        assert column in guard


def test_the_open_worklist_never_walks_the_closed_rows() -> None:
    """Closed rows accumulate forever; the alarm query must not read them."""
    assert "agentic_audio_usage_open_idx" in METER_SQL
    index = METER_SQL.split("agentic_audio_usage_open_idx")[1].split(";")[0]
    assert "WHERE state = 'open'" in index
