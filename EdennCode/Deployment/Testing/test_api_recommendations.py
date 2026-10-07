"""Endpoint tests for GET /api/v1/recommendations.

Validation tests use FastAPI's TestClient with the workflow mocked.
The end-to-end live-DB test is in
EdennCode/WorkflowFactory/RecommendationWorkflow/Testing/test_workflow.py.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.api_recommendations import create_recommendations_router
from EdennCode.WorkflowFactory.RecommendationWorkflow import (
    RecommendationWorkflowResult,
)


class _StubLogger:
    def exception(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


class _StubContext:
    logger = _StubLogger()


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(create_recommendations_router(_StubContext()))
    # The route reads DATABASE_URL when actually constructing the workflow,
    # so set a dummy value for tests that don't mock the workflow.
    os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
    return TestClient(app)


def test_rejects_request_with_neither_user_id_nor_global(client: TestClient) -> None:
    resp = client.get("/api/v1/recommendations")
    assert resp.status_code == 400
    assert "exactly one" in resp.json()["detail"].lower()


def test_rejects_request_with_both_user_id_and_global(client: TestClient) -> None:
    resp = client.get("/api/v1/recommendations?user_id=u1&global=true")
    assert resp.status_code == 400


def test_rejects_limit_above_max(client: TestClient) -> None:
    resp = client.get("/api/v1/recommendations?global=true&limit=51")
    assert resp.status_code == 422  # FastAPI Query(le=50) → 422 unprocessable


def test_response_includes_version_field_in_dev_format() -> None:
    """version is always 'dev-<IMAGE_TAG>'; defaults to 'dev-local' when env unset."""

    fake_result = RecommendationWorkflowResult(
        recommendations=[],
        is_cold_start=False,
        profile_history_count=0,
        mode="global",
    )

    async def _fake_run(self, _input):
        return fake_result

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("IMAGE_TAG", None)
        os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
        app = FastAPI()
        app.include_router(create_recommendations_router(_StubContext()))
        client = TestClient(app)
        with patch(
            "EdennCode.Deployment.api_recommendations.RecommendationWorkflow.run",
            new=_fake_run,
        ):
            resp = client.get("/api/v1/recommendations?global=true&limit=5")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["version"] == "dev-local"
        assert body["mode"] == "global"
        assert body["recommendations"] == []


def test_response_includes_version_with_image_tag_when_set() -> None:
    fake_result = RecommendationWorkflowResult(
        recommendations=[],
        is_cold_start=False,
        profile_history_count=0,
        mode="global",
    )

    async def _fake_run(self, _input):
        return fake_result

    with patch.dict(os.environ, {"IMAGE_TAG": "abc1234", "DATABASE_URL": "postgresql://test/test"}):
        app = FastAPI()
        app.include_router(create_recommendations_router(_StubContext()))
        client = TestClient(app)
        with patch(
            "EdennCode.Deployment.api_recommendations.RecommendationWorkflow.run",
            new=_fake_run,
        ):
            resp = client.get("/api/v1/recommendations?global=true&limit=5")
        assert resp.json()["version"] == "dev-abc1234"


def test_cold_start_user_returns_empty_list_with_200() -> None:
    fake_result = RecommendationWorkflowResult(
        recommendations=[],
        is_cold_start=True,
        profile_history_count=0,
        mode="user",
    )

    async def _fake_run(self, _input):
        return fake_result

    os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
    app = FastAPI()
    app.include_router(create_recommendations_router(_StubContext()))
    client = TestClient(app)
    with patch(
        "EdennCode.Deployment.api_recommendations.RecommendationWorkflow.run",
        new=_fake_run,
    ):
        resp = client.get("/api/v1/recommendations?user_id=brand-new-user")
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_cold_start"] is True
    assert body["recommendations"] == []
    assert body["mode"] == "user"
