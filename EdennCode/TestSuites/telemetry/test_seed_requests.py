import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.asyncio
async def test_seed_inserts_at_least_50_rows(db_conn):
    # Clean slate
    await db_conn.execute("DELETE FROM requests")

    result = subprocess.run(
        [sys.executable, "-m", "EdennCode.Scripts.seed_requests"],
        cwd=REPO_ROOT, env={**os.environ},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"seed failed:\n{result.stdout}\n{result.stderr}"

    count = await db_conn.fetchval("SELECT count(*) FROM requests")
    assert count >= 50, f"expected >=50 rows, got {count}"


@pytest.mark.asyncio
async def test_seeded_rows_have_extracted_intent(db_conn):
    n_with_intent = await db_conn.fetchval(
        "SELECT count(*) FROM requests WHERE extracted_intent IS NOT NULL"
    )
    assert n_with_intent >= 30, (
        f"expected most seeded rows to have extracted_intent, got {n_with_intent}"
    )


@pytest.mark.asyncio
async def test_intent_gin_filter_works(db_conn):
    """Bonus deliverable from spec §11 Track A: GIN index supports JSONB containment."""
    rows = await db_conn.fetch(
        "SELECT request_id FROM requests WHERE extracted_intent @> $1::jsonb",
        '{"mood": "energetic"}',
    )
    assert len(rows) >= 1, "expected at least one 'energetic' mood row in seed"
