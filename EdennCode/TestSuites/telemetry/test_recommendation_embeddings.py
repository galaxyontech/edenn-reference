import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.asyncio
async def test_backfill_fills_generation_job_embeddings(db_conn):
    # We assert eligible rows exist (>0), but not that they are NULL — within
    # one pytest session the idempotency test or the snapshot test may have
    # already triggered the script and filled these. The real assertion is
    # `n_null_after == 0` below.
    n_eligible = await db_conn.fetchval(
        """
        SELECT count(*) FROM generation_job
        WHERE user_prompt IS NOT NULL
          AND user_prompt <> ''
        """
    )
    assert n_eligible > 0, (
        "no generation_job rows have a non-empty user_prompt; expected seed rows to exist"
    )

    result = subprocess.run(
        [sys.executable, "-m", "EdennCode.Scripts.backfill_recommendation_embeddings"],
        cwd=REPO_ROOT,
        env={**os.environ},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"backfill failed:\n{result.stdout}\n{result.stderr}"

    n_null_after = await db_conn.fetchval(
        """
        SELECT count(*) FROM generation_job
        WHERE user_prompt IS NOT NULL
          AND user_prompt <> ''
          AND user_prompt_embedding IS NULL
"""
    )
    assert n_null_after == 0, (
        f"{n_null_after} generation_job rows still missing user_prompt_embedding"
    )


@pytest.mark.asyncio
async def test_backfill_fills_snapshot_music_embeddings(db_conn):
    # No `n_null_before > 0` precondition here: the script is single-invocation
    # and fills both generation_job AND creative_feature_snapshot in one run, so
    # if test_backfill_fills_generation_job_embeddings ran earlier in this
    # session, it already filled this column too. We rely on
    # test_backfill_is_idempotent to assert the no-op behavior. Here we just
    # assert the after-state is correct.
    n_total_with_prompt = await db_conn.fetchval(
        """
        SELECT count(*) FROM creative_feature_snapshot
        WHERE music_prompt_json IS NOT NULL
          AND music_prompt_json::text <> '{}'::text
        """
    )
    assert n_total_with_prompt > 0, (
        "no creative_feature_snapshot rows have non-empty music_prompt_json; "
        "expected seed rows to exist"
    )

    result = subprocess.run(
        [sys.executable, "-m", "EdennCode.Scripts.backfill_recommendation_embeddings"],
        cwd=REPO_ROOT,
        env={**os.environ},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"backfill failed:\n{result.stdout}\n{result.stderr}"

    n_null_after = await db_conn.fetchval(
        """
        SELECT count(*) FROM creative_feature_snapshot
        WHERE music_prompt_json IS NOT NULL
          AND music_prompt_json::text <> '{}'::text
          AND music_embedding IS NULL
        """
    )
    assert n_null_after == 0, (
        f"{n_null_after} creative_feature_snapshot rows still missing music_embedding"
    )


@pytest.mark.asyncio
async def test_embedding_dim_is_1536_on_recommendation_tables(db_conn):
    job_row = await db_conn.fetchrow(
        """
        SELECT user_prompt_embedding FROM generation_job
        WHERE user_prompt_embedding IS NOT NULL LIMIT 1
        """
    )
    assert job_row is not None, "no generation_job rows with user_prompt_embedding populated"
    assert len(job_row["user_prompt_embedding"]) == 1536

    snap_row = await db_conn.fetchrow(
        """
        SELECT music_embedding FROM creative_feature_snapshot
        WHERE music_embedding IS NOT NULL LIMIT 1
        """
    )
    assert snap_row is not None, "no creative_feature_snapshot rows with music_embedding populated"
    assert len(snap_row["music_embedding"]) == 1536


@pytest.mark.asyncio
async def test_backfill_is_idempotent(db_conn):
    # Precondition: previous tests should have filled everything; if not, fill now
    # so this test asserts the no-op behavior of a second run.
    result = subprocess.run(
        [sys.executable, "-m", "EdennCode.Scripts.backfill_recommendation_embeddings"],
        cwd=REPO_ROOT,
        env={**os.environ},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"first run failed:\n{result.stdout}\n{result.stderr}"

    result2 = subprocess.run(
        [sys.executable, "-m", "EdennCode.Scripts.backfill_recommendation_embeddings"],
        cwd=REPO_ROOT,
        env={**os.environ},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result2.returncode == 0, f"second run failed:\n{result2.stdout}\n{result2.stderr}"

    n_job_null = await db_conn.fetchval(
        """
        SELECT count(*) FROM generation_job
        WHERE user_prompt IS NOT NULL
          AND user_prompt <> ''
          AND user_prompt_embedding IS NULL
"""
    )
    n_snap_null = await db_conn.fetchval(
        """
        SELECT count(*) FROM creative_feature_snapshot
        WHERE music_prompt_json IS NOT NULL
          AND music_prompt_json::text <> '{}'::text
          AND music_embedding IS NULL
        """
    )
    assert n_job_null == 0, f"{n_job_null} generation_job rows still NULL after second run"
    assert n_snap_null == 0, f"{n_snap_null} snapshot rows still NULL after second run"
