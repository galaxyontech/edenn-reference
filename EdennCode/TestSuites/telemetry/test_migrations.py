import asyncio
import os
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _resolve_dsn() -> str:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TELEMETRY_DATABASE_URL")
    if not dsn:
        pytest.skip(
            "DATABASE_URL (or TELEMETRY_DATABASE_URL) not set; "
            "telemetry tests require a live Postgres connection"
        )
    return dsn


@pytest.mark.asyncio
async def test_apply_creates_schema_migrations_table():
    dsn = _resolve_dsn()
    # Pre-clean: drop schema_migrations, all migration-created tables, and the vector extension.
    # CASCADE on the extension handles dependent columns (e.g. requests.user_prompt_embedding).
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("DROP TABLE IF EXISTS pipeline_stages CASCADE")
        await conn.execute("DROP TABLE IF EXISTS pipeline_runs CASCADE")
        await conn.execute("DROP TABLE IF EXISTS requests CASCADE")
        await conn.execute("DROP TABLE IF EXISTS schema_migrations")
        await conn.execute("DROP EXTENSION IF EXISTS vector CASCADE")
    finally:
        await conn.close()

    result = subprocess.run(
        [sys.executable, "-m", "EdennCode.Database.migrations.apply"],
        cwd=REPO_ROOT,
        env={**os.environ},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"apply.py failed:\n{result.stdout}\n{result.stderr}"

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch("SELECT version FROM schema_migrations ORDER BY version")
        versions = [r["version"] for r in rows]
        assert "000_extensions" in versions, f"got: {versions}"
    finally:
        await conn.close()

    # Re-seed both tables so downstream tests have a complete corpus regardless
    # of test execution order. This makes the destructive migration test a
    # full reset rather than a partial wipe.
    for module in ("EdennCode.Scripts.seed_requests", "EdennCode.Scripts.seed_pipeline_runs"):
        seed_result = subprocess.run(
            [sys.executable, "-m", module],
            cwd=REPO_ROOT, env={**os.environ},
            capture_output=True, text=True,
        )
        assert seed_result.returncode == 0, (
            f"{module} failed:\n{seed_result.stdout}\n{seed_result.stderr}"
        )

    # Refill embeddings so semantic-search tests don't see NULL vectors.
    backfill_result = subprocess.run(
        [sys.executable, "-m", "EdennCode.Scripts.backfill_embeddings", "--table", "all"],
        cwd=REPO_ROOT, env={**os.environ},
        capture_output=True, text=True, timeout=120,
    )
    assert backfill_result.returncode == 0, (
        f"backfill_embeddings failed:\n{backfill_result.stdout}\n{backfill_result.stderr}"
    )


@pytest.mark.asyncio
async def test_apply_is_idempotent():
    """Running apply.py twice is a no-op the second time."""
    result1 = subprocess.run(
        [sys.executable, "-m", "EdennCode.Database.migrations.apply"],
        cwd=REPO_ROOT, env={**os.environ},
        capture_output=True, text=True,
    )
    assert result1.returncode == 0

    result2 = subprocess.run(
        [sys.executable, "-m", "EdennCode.Database.migrations.apply"],
        cwd=REPO_ROOT, env={**os.environ},
        capture_output=True, text=True,
    )
    assert result2.returncode == 0
    assert "SKIP 000_extensions" in result2.stdout, f"expected SKIP line, got: {result2.stdout}"
