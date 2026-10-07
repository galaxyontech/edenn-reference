import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.asyncio
async def test_seed_inserts_runs_for_succeeded_requests(db_conn):
    # Reset pipeline tables (CASCADE protects requests)
    await db_conn.execute("DELETE FROM pipeline_runs")

    # Sanity: requests must already have been seeded (Task 5)
    n_succeeded_requests = await db_conn.fetchval(
        "SELECT count(*) FROM requests WHERE status='succeeded'"
    )
    assert n_succeeded_requests > 0, "Run scripts.seed_requests first"

    result = subprocess.run(
        [sys.executable, "-m", "EdennCode.Scripts.seed_pipeline_runs"],
        cwd=REPO_ROOT, env={**os.environ},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"seed failed:\n{result.stdout}\n{result.stderr}"

    n_runs = await db_conn.fetchval("SELECT count(*) FROM pipeline_runs")
    assert n_runs >= 30, f"expected >=30 runs, got {n_runs}"


@pytest.mark.asyncio
async def test_each_run_has_stages(db_conn):
    rows = await db_conn.fetch(
        """
        SELECT pr.run_id, count(ps.stage_id) AS n_stages
        FROM pipeline_runs pr
        LEFT JOIN pipeline_stages ps ON ps.run_id = pr.run_id
        GROUP BY pr.run_id
        """
    )
    assert rows, "no runs found"
    for r in rows:
        assert r["n_stages"] >= 3, (
            f"run {r['run_id']} has only {r['n_stages']} stages"
        )


@pytest.mark.asyncio
async def test_video_music_runs_have_summary_and_music_prompt(db_conn):
    rows = await db_conn.fetch(
        """
        SELECT video_summary, music_prompt, music_provider
        FROM pipeline_runs
        WHERE workflow_type='video_music' AND status='succeeded'
        """
    )
    assert rows, "no successful video_music runs"
    for r in rows:
        assert r["video_summary"], "video_summary should be populated"
        assert r["music_prompt"], "music_prompt should be populated"
        assert r["music_provider"] in ("provider_a", "provider_c"), (
            f"unexpected provider: {r['music_provider']}"
        )


@pytest.mark.asyncio
async def test_summary_tsv_generated(db_conn):
    """Generated tsvector column populates from video_summary + music_prompt fields."""
    rows = await db_conn.fetch(
        """
        SELECT summary_tsv FROM pipeline_runs
        WHERE video_summary IS NOT NULL AND status='succeeded'
        LIMIT 5
        """
    )
    for r in rows:
        # tsvector returned as string by asyncpg; just non-empty check
        assert r["summary_tsv"] and len(str(r["summary_tsv"])) > 5
