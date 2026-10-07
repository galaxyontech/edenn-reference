"""Auth-derived user_id wiring for GET /api/v1/recommendations.

Verifies the Task 10 override: in enforce mode, a valid API key fills in a
missing ?user_id from the resolved principal (via resolve_user_id) so the
XOR guard passes without the caller supplying ?user_id explicitly. The
?global=true path stays untouched.
"""
from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.api_recommendations import create_recommendations_router
from EdennCode.Deployment.auth.middleware import create_auth_middleware
from EdennCode.Deployment.Testing.test_auth_middleware import PRINCIPAL, StubKeyStore
from EdennCode.WorkflowFactory.RecommendationWorkflow import RecommendationWorkflowResult


class _StubLogger:
    def exception(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


class _StubContext:
    logger = _StubLogger()


@pytest.fixture(autouse=True)
def _database_url_env(monkeypatch):
    # _resolve_dsn() checks DATABASE_URL before the workflow is even
    # constructed; the workflow itself is mocked out below.
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")


def _enforce_app() -> FastAPI:
    app = FastAPI()
    # Auth middleware wired the same way api.py wires it: enforce mode with a
    # key store that resolves any presented key to PRINCIPAL (user_id="user-1").
    app.middleware("http")(
        create_auth_middleware(
            mode="enforce",
            key_store=StubKeyStore(PRINCIPAL),
            logger=logging.getLogger("t"),
        )
    )
    app.include_router(create_recommendations_router(_StubContext()))
    return app


def _mock_workflow_run(monkeypatch):
    """Patch RecommendationWorkflow.run to capture the input it was called with."""
    captured: dict = {}

    async def _fake_run(self, workflow_input):
        captured["input"] = workflow_input
        return RecommendationWorkflowResult(
            recommendations=[],
            is_cold_start=False,
            profile_history_count=0,
            mode="global" if workflow_input.global_mode else "user",
        )

    monkeypatch.setattr(
        "EdennCode.Deployment.api_recommendations.RecommendationWorkflow.run",
        _fake_run,
    )
    return captured


def test_missing_user_id_resolved_from_key_in_enforce_mode(monkeypatch):
    captured = _mock_workflow_run(monkeypatch)
    client = TestClient(_enforce_app())

    resp = client.get(
        "/api/v1/recommendations",
        headers={"Authorization": "Bearer sk-anything"},
    )

    assert resp.status_code == 200, resp.text
    assert captured["input"].user_id == "user-1"
    assert captured["input"].global_mode is False


def test_global_true_preserved_with_auth_header(monkeypatch):
    captured = _mock_workflow_run(monkeypatch)
    client = TestClient(_enforce_app())

    resp = client.get(
        "/api/v1/recommendations?global=true",
        headers={"Authorization": "Bearer sk-anything"},
    )

    assert resp.status_code == 200, resp.text
    assert captured["input"].global_mode is True
    assert captured["input"].user_id is None
