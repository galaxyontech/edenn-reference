"""Tests for the API-key auth middleware, mode resolution, and settings plumbing."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from EdennCode.Deployment.auth.key_store import KeyStoreUnavailable, Principal
from EdennCode.Deployment.auth.middleware import (
    create_auth_middleware,
    get_principal,
    is_exempt_path,
    resolve_auth_mode,
    resolve_user_id,
)


class TestAuthSettings:
    def test_settings_default_auth_fields(self, monkeypatch):
        for key in ("AUTH_MODE", "AUTH_TABLE_NAMESPACE", "API_ADMIN_SECRET",
                    "APPLICATIONINSIGHTS_CONNECTION_STRING"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("AZURE_ENDPOINT", "https://example.model_gateway.azure.com")
        monkeypatch.setenv("AZURE_API_KEY", "test-key")
        monkeypatch.setenv("AZURE_MODEL", "test-deploy")
        from EdennCode.Deployment.settings import DeploymentSettings
        settings = DeploymentSettings.from_env()
        assert settings.auth_mode == ""
        assert settings.auth_table_namespace == ""
        assert settings.api_admin_secret is None
        assert settings.appinsights_connection_string is None

    def test_settings_reads_auth_env(self, monkeypatch):
        monkeypatch.setenv("AZURE_ENDPOINT", "https://example.model_gateway.azure.com")
        monkeypatch.setenv("AZURE_API_KEY", "test-key")
        monkeypatch.setenv("AZURE_MODEL", "test-deploy")
        monkeypatch.setenv("AUTH_MODE", "enforce")
        monkeypatch.setenv("AUTH_TABLE_NAMESPACE", "dev")
        monkeypatch.setenv("API_ADMIN_SECRET", "adm-secret")
        monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=x")
        from EdennCode.Deployment.settings import DeploymentSettings
        settings = DeploymentSettings.from_env()
        assert settings.auth_mode == "enforce"
        assert settings.auth_table_namespace == "dev"
        assert settings.api_admin_secret == "adm-secret"
        assert settings.appinsights_connection_string == "InstrumentationKey=x"


class StubKeyStore:
    def __init__(self, principal: Optional[Principal] = None, *, unavailable: bool = False):
        self.principal = principal
        self.unavailable = unavailable
        self.lookups: list[str] = []

    async def lookup(self, presented_key: str) -> Optional[Principal]:
        self.lookups.append(presented_key)
        if self.unavailable:
            raise KeyStoreUnavailable("down")
        return self.principal


def _app(mode: str, key_store) -> FastAPI:
    app = FastAPI()
    app.middleware("http")(
        create_auth_middleware(mode=mode, key_store=key_store, logger=logging.getLogger("t"))
    )

    @app.get("/healthz")
    def health():
        return {"ok": True}

    @app.post("/api/v1/jobs/video")
    def job(request: Request):
        principal = get_principal(request)
        return {
            "principal": principal.user_id if principal else None,
            "resolved": resolve_user_id(request, "form-user"),
        }

    return app


PRINCIPAL = Principal(user_id="user-1", key_prefix="sk-abc123def")


class TestResolveAuthMode:
    def test_modes(self):
        assert resolve_auth_mode("") == "off"
        assert resolve_auth_mode("off") == "off"
        assert resolve_auth_mode("LOG") == "log"
        assert resolve_auth_mode(" enforce ") == "enforce"
        assert resolve_auth_mode("bogus") == "off"


class TestExemptPaths:
    def test_exempt(self):
        for path in (
            "/healthz", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json",
            "/api/v1/docs", "/api/v1/docs/some-slug",
            "/api/v1/docs/some-slug/examples/ex1",
            "/api/v1/provider_callbacks/provider_c", "/provider_c/callback",
        ):
            assert is_exempt_path(path), path

    def test_not_exempt(self):
        for path in (
            "/api/v1/jobs/video", "/api/v2/jobs/x", "/api/v1/recommendations",
            # Agentic Audio left this app (standalone product, own deployment,
            # own auth). If someone re-mounts it here, it must NOT ride back in
            # on a stale exemption — it goes through auth review like anything
            # else.
            "/api/v2/agentic/audio/app/", "/api/v2/agentic/audio/sessions",
        ):
            assert not is_exempt_path(path), path


class TestEnforceMode:
    def test_missing_header_401(self):
        client = TestClient(_app("enforce", StubKeyStore(PRINCIPAL)))
        resp = client.post("/api/v1/jobs/video")
        assert resp.status_code == 401
        assert resp.json()["code"] == "missing_api_key"

    def test_malformed_header_401(self):
        client = TestClient(_app("enforce", StubKeyStore(PRINCIPAL)))
        resp = client.post("/api/v1/jobs/video", headers={"Authorization": "sk-raw-token"})
        assert resp.status_code == 401
        assert resp.json()["code"] == "invalid_authorization_header"

    def test_unknown_key_401(self):
        client = TestClient(_app("enforce", StubKeyStore(None)))
        resp = client.post("/api/v1/jobs/video", headers={"Authorization": "Bearer sk-nope"})
        assert resp.status_code == 401
        assert resp.json()["code"] == "invalid_api_key"

    def test_valid_key_attaches_principal_and_overrides_user(self):
        client = TestClient(_app("enforce", StubKeyStore(PRINCIPAL)))
        resp = client.post("/api/v1/jobs/video", headers={"Authorization": "Bearer sk-good"})
        assert resp.status_code == 200
        assert resp.json() == {"principal": "user-1", "resolved": "user-1"}

    def test_exempt_path_needs_no_key(self):
        client = TestClient(_app("enforce", StubKeyStore(None)))
        assert client.get("/healthz").status_code == 200

    def test_options_needs_no_key(self):
        client = TestClient(_app("enforce", StubKeyStore(None)))
        resp = client.options("/api/v1/jobs/video")
        assert resp.status_code != 401

    def test_store_down_fails_closed_503(self):
        client = TestClient(_app("enforce", StubKeyStore(unavailable=True)))
        resp = client.post("/api/v1/jobs/video", headers={"Authorization": "Bearer sk-x"})
        assert resp.status_code == 503
        assert resp.json()["code"] == "auth_unavailable"

    def test_no_store_configured_fails_closed_503(self):
        client = TestClient(_app("enforce", None))
        resp = client.post("/api/v1/jobs/video", headers={"Authorization": "Bearer sk-x"})
        assert resp.status_code == 503


class TestLogMode:
    def test_missing_key_passes_through_without_principal(self):
        client = TestClient(_app("log", StubKeyStore(PRINCIPAL)))
        resp = client.post("/api/v1/jobs/video")
        assert resp.status_code == 200
        assert resp.json() == {"principal": None, "resolved": "form-user"}

    def test_valid_key_attaches_principal_but_form_user_wins(self):
        client = TestClient(_app("log", StubKeyStore(PRINCIPAL)))
        resp = client.post("/api/v1/jobs/video", headers={"Authorization": "Bearer sk-good"})
        assert resp.status_code == 200
        assert resp.json() == {"principal": "user-1", "resolved": "form-user"}

    def test_store_down_passes_through(self):
        client = TestClient(_app("log", StubKeyStore(unavailable=True)))
        resp = client.post("/api/v1/jobs/video", headers={"Authorization": "Bearer sk-x"})
        assert resp.status_code == 200


class TestOffMode:
    # Spec 2026-07-20 billing §4.7: off mode now ATTACHES identity when a key
    # is presented (usage attribution + /api/v1/account/* work uniformly) but
    # still never rejects anything.
    def test_off_mode_attaches_principal_without_enforcing(self):
        store = StubKeyStore(PRINCIPAL)
        client = TestClient(_app("off", store))
        resp = client.post("/api/v1/jobs/video", headers={"Authorization": "Bearer sk-good"})
        assert resp.status_code == 200
        assert store.lookups == ["sk-good"]
        # Identity attached, but enforce-mode override does NOT apply in off.
        assert resp.json() == {"principal": "user-1", "resolved": "form-user"}
