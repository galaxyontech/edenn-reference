import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.asyncio
async def test_backfill_fills_user_prompt_embeddings(db_conn):
    n_null_before = await db_conn.fetchval(
        """
        SELECT count(*) FROM requests
        WHERE user_prompt IS NOT NULL AND user_prompt_embedding IS NULL
        """
    )
    assert n_null_before > 0, "no requests need embedding; run seed_requests first"

    result = subprocess.run(
        [sys.executable, "-m", "EdennCode.Scripts.backfill_embeddings", "--table", "requests"],
        cwd=REPO_ROOT, env={**os.environ},
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"backfill failed:\n{result.stdout}\n{result.stderr}"

    n_null_after = await db_conn.fetchval(
        """
        SELECT count(*) FROM requests
        WHERE user_prompt IS NOT NULL AND user_prompt_embedding IS NULL
        """
    )
    assert n_null_after == 0, f"{n_null_after} requests still missing embedding"


@pytest.mark.asyncio
async def test_backfill_fills_pipeline_embeddings(db_conn):
    n_null_before = await db_conn.fetchval(
        """
        SELECT count(*) FROM pipeline_runs
        WHERE video_summary IS NOT NULL AND video_summary_embedding IS NULL
        """
    )
    assert n_null_before > 0, "no pipeline_runs need embedding; run seed_pipeline_runs first"

    result = subprocess.run(
        [sys.executable, "-m", "EdennCode.Scripts.backfill_embeddings", "--table", "pipeline_runs"],
        cwd=REPO_ROOT, env={**os.environ},
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"backfill failed:\n{result.stdout}\n{result.stderr}"

    n_null_after = await db_conn.fetchval(
        """
        SELECT count(*) FROM pipeline_runs
        WHERE video_summary IS NOT NULL AND video_summary_embedding IS NULL
        """
    )
    assert n_null_after == 0, f"{n_null_after} pipeline_runs still missing video_summary_embedding"


@pytest.mark.asyncio
async def test_embedding_dim_is_1536(db_conn):
    row = await db_conn.fetchrow(
        """
        SELECT user_prompt_embedding FROM requests
        WHERE user_prompt_embedding IS NOT NULL LIMIT 1
        """
    )
    assert row is not None
    # pgvector adapter returns numpy array
    assert len(row["user_prompt_embedding"]) == 1536
