"""Integration tests for RecommendationWorkflow against the live Japan DB.

Auto-marked remote_integration via EdennCode/TestSuites/telemetry/conftest.py? No — those
tests only auto-mark by directory. This file lives outside EdennCode/TestSuites/telemetry/ so
we mark explicitly.

Skipped cleanly when DATABASE_URL is not set (no live DB to test against).
"""
from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

from EdennCode.WorkflowFactory.RecommendationWorkflow import (
    BuildUserProfileStage,
    RecommendationWorkflow,
    RecommendationWorkflowInput,
    RetrieveCandidatesStage,
)


load_dotenv()
DSN = os.environ.get("DATABASE_URL") or os.environ.get("TELEMETRY_DATABASE_URL")
pytestmark = [
    pytest.mark.remote_integration,
    pytest.mark.skipif(not DSN, reason="DATABASE_URL not set"),
    pytest.mark.asyncio,
]


async def test_global_mode_returns_top_alignment_creatives():
    workflow = RecommendationWorkflow(dsn=DSN)
    result = await workflow.run(
        RecommendationWorkflowInput(global_mode=True, limit=5),
    )

    assert result.mode == "global"
    assert result.is_cold_start is False
    assert 1 <= len(result.recommendations) <= 5

    # Order is alignment_score DESC; verify monotonic non-increasing.
    scores = [item["score"] for item in result.recommendations]
    assert scores == sorted(scores, reverse=True), f"global results not sorted: {scores}"

    # Slim shape contract.
    first = result.recommendations[0]
    for required in ("creative_id", "music_id", "full_audio_url", "title", "thumbnail_url", "score"):
        assert required in first, f"{required!r} missing from slim payload"


async def test_unknown_user_id_returns_cold_start_empty():
    workflow = RecommendationWorkflow(dsn=DSN)
    result = await workflow.run(
        RecommendationWorkflowInput(user_id="user-that-does-not-exist", limit=5),
    )

    assert result.mode == "user"
    assert result.is_cold_start is True
    assert result.recommendations == []
    assert result.profile_history_count == 0


async def test_input_validation_rejects_both_or_neither():
    with pytest.raises(ValueError):
        RecommendationWorkflowInput(user_id="u", global_mode=True)
    with pytest.raises(ValueError):
        RecommendationWorkflowInput()


async def test_retrieve_global_returns_at_most_limit():
    stage = RetrieveCandidatesStage(dsn=DSN)
    candidates = await stage.run(profile=None, limit=3)
    assert len(candidates) <= 3


async def test_build_profile_for_unknown_user_returns_zero_history():
    stage = BuildUserProfileStage(dsn=DSN)
    profile = await stage.run(user_id="user-that-does-not-exist")
    assert profile.history_count == 0
    assert profile.prompt_centroid is None


async def test_debug_payload_includes_breakdown_in_global_mode():
    workflow = RecommendationWorkflow(dsn=DSN)
    result = await workflow.run(
        RecommendationWorkflowInput(global_mode=True, limit=2, debug=True),
    )
    assert len(result.recommendations) >= 1
    item = result.recommendations[0]
    assert "alignment_score" in item
    assert "music_prompt_json" in item
    assert "cosine_similarity" in item  # None in global mode, but field present
