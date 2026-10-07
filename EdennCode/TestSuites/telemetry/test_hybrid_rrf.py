"""
Integration test: with the seeded corpus, the hybrid RRF query returns
semantically meaningful results and respects filters.

Assumes seed_requests + seed_pipeline_runs + backfill_embeddings have all run.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _run_search(query: str, *args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "EdennCode.Scripts.search", query, *args],
        cwd=REPO_ROOT, env={**os.environ},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, f"search failed:\n{result.stdout}\n{result.stderr}"
    return result.stdout


def test_search_returns_results():
    out = _run_search("upbeat music for sports brand video")
    assert "Top " in out
    assert "score=" in out


def test_sports_query_ranks_sports_rows_high():
    """The 'sports brand' seed row should appear in top 5 for a sports query."""
    out = _run_search("upbeat music for sports brand video", "--top-k", "5")
    assert "sports" in out.lower(), f"expected 'sports' in top results:\n{out}"


def test_luxury_query_ranks_luxury_rows_high():
    """The 'luxury car' seed row should appear in top 5 for a luxury query."""
    out = _run_search("cinematic music for high-end luxury commercial", "--top-k", "5")
    assert "luxury" in out.lower() or "cinematic" in out.lower(), (
        f"expected luxury/cinematic in top results:\n{out}"
    )


def test_workflow_filter_excludes_non_matching():
    """A --workflow value with no rows in the corpus should return zero results.

    The seed populates `video_music`, `audio_creative_edit`, `multi_image`, and
    `video_alignment`. Filtering to a workflow that doesn't exist exercises the
    SQL filter end-to-end: if it's wired wrong the query would still return
    rows. Expecting `(no results)` proves the filter is honored.
    """
    out = _run_search(
        "energetic upbeat music for a sports brand ad",
        "--top-k", "10",
        "--workflow", "nonexistent_test_workflow",
    )
    assert "(no results)" in out, (
        f"expected --workflow filter to exclude all rows, got:\n{out}"
    )
