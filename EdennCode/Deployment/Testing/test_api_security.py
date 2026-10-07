"""Tests for the API security-config helpers (CORS + error-tracker PII).

Covers the production-readiness hardening in
``EdennCode/Deployment/PROD_READINESS_REMEDIATION_PLAN.md``:
- #45: CORS defaulted to ``API_ALLOWED_ORIGINS="*"`` combined with
  ``allow_credentials=True``, which makes the middleware reflect any origin back
  with credentials — an open credentialed cross-origin surface.
- #59: the error tracker was initialized with ``send_default_pii=True``,
  shipping user IPs / request data / prompt text to a third-party service.

The CORS behavior is verified end to end through the *real* Starlette
``CORSMiddleware`` on a real ASGI app driven by ``TestClient`` (real HTTP
round-trips), not just the resolved kwargs.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from EdennCode.Deployment.api_security import error_tracker_send_pii, resolve_cors_policy


# --- resolve_cors_policy: pure resolution ---------------------------------

def test_cors_closed_by_default_when_unset() -> None:
    assert resolve_cors_policy("") is None
    assert resolve_cors_policy("   ") is None
    assert resolve_cors_policy(",, ,") is None


def test_cors_wildcard_never_carries_credentials() -> None:
    policy = resolve_cors_policy("*")
    assert policy is not None
    assert policy["allow_origins"] == ["*"]
    assert policy["allow_credentials"] is False


def test_cors_explicit_origins_allow_credentials() -> None:
    policy = resolve_cors_policy("https://app.example.com, https://admin.example.com")
    assert policy is not None
    assert policy["allow_origins"] == ["https://app.example.com", "https://admin.example.com"]
    assert policy["allow_credentials"] is True


# --- CORS behavior through the real middleware -----------------------------

def _app_with_cors(raw_origins: str) -> FastAPI:
    app = FastAPI()
    policy = resolve_cors_policy(raw_origins)
    if policy is not None:
        app.add_middleware(CORSMiddleware, **policy)

    @app.get("/ping")
    def ping() -> dict:
        return {"ok": True}

    return app


def test_real_middleware_closed_default_emits_no_cors_headers() -> None:
    client = TestClient(_app_with_cors(""))
    resp = client.get("/ping", headers={"Origin": "https://evil.example.com"})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}


def test_real_middleware_wildcard_has_no_credentialed_reflection() -> None:
    client = TestClient(_app_with_cors("*"))
    resp = client.get("/ping", headers={"Origin": "https://evil.example.com"})
    headers = {k.lower(): v for k, v in resp.headers.items()}
    # Public wildcard is fine, but it must NOT reflect the origin with credentials.
    assert headers.get("access-control-allow-origin") == "*"
    assert "access-control-allow-credentials" not in headers


def test_real_middleware_explicit_origin_is_credentialed() -> None:
    client = TestClient(_app_with_cors("https://app.example.com"))
    resp = client.get("/ping", headers={"Origin": "https://app.example.com"})
    headers = {k.lower(): v for k, v in resp.headers.items()}
    assert headers.get("access-control-allow-origin") == "https://app.example.com"
    assert headers.get("access-control-allow-credentials") == "true"


def test_real_middleware_explicit_origin_rejects_other_origin() -> None:
    client = TestClient(_app_with_cors("https://app.example.com"))
    resp = client.get("/ping", headers={"Origin": "https://evil.example.com"})
    headers = {k.lower(): v for k, v in resp.headers.items()}
    # A non-allowlisted origin gets no allow-origin header echoing it back.
    assert headers.get("access-control-allow-origin") != "https://evil.example.com"


# --- error tracker PII flag ------------------------------------------------

def test_error_tracker_pii_defaults_off() -> None:
    assert error_tracker_send_pii("") is False
    assert error_tracker_send_pii("false") is False
    assert error_tracker_send_pii("no") is False
    assert error_tracker_send_pii("0") is False


def test_error_tracker_pii_opt_in() -> None:
    for val in ("1", "true", "TRUE", "yes", "on"):
        assert error_tracker_send_pii(val) is True
