"""The console's runtime configuration endpoint.

Serving Firebase's public client config from the API instead of baking it into
the bundle keeps one copy of those values — the same env block that holds
``FIREBASE_PROJECT_ID``, which is what the backend verifies tokens against. Two
copies drift, and the failure that drift produces is a token minted for one
project being checked against another: an opaque 401 with nothing in either log
pointing at the mismatch.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.auth.middleware import (
    create_auth_middleware,
    is_exempt_path,
)
from EdennCode.Deployment.console_static import (
    CONSOLE_CONFIG_PATH,
    create_console_config_router,
)

FULL = SimpleNamespace(
    firebase_project_id="edenn-console",
    firebase_api_key="AIzaSyFAKEFAKEFAKEFAKEFAKEFAKEFAKE",
    firebase_auth_domain="edenn-console.firebaseapp.com",
    firebase_app_id="1:123456789:web:abc123",
    firebase_messaging_sender_id="123456789",
    # Present on the real settings object, and must never be echoed.
    api_admin_secret="super-secret",
    admin_phone_numbers="13800138000",
    admin_firebase_uids="kQ2mZ8xVbNfR4t",
    storage_account_key="storage-key",
)


def _client(settings=FULL):
    app = FastAPI()
    app.include_router(create_console_config_router(
        settings=settings, logger=logging.getLogger("t")))
    return TestClient(app)


class TestPayload:
    def test_serves_the_five_public_values(self):
        body = _client().get(CONSOLE_CONFIG_PATH).json()
        assert body["configured"] is True
        assert body["firebase"] == {
            "projectId": "edenn-console",
            "apiKey": "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKEFAKE",
            "authDomain": "edenn-console.firebaseapp.com",
            "appId": "1:123456789:web:abc123",
            "messagingSenderId": "123456789",
        }

    def test_auth_domain_defaults_to_the_firebase_convention(self):
        settings = SimpleNamespace(**{**vars(FULL), "firebase_auth_domain": ""})
        body = _client(settings).get(CONSOLE_CONFIG_PATH).json()
        assert body["firebase"]["authDomain"] == "edenn-console.firebaseapp.com"

    def test_unconfigured_says_so_instead_of_failing(self):
        # The page then renders "console not configured" rather than a blank
        # screen with a console error nobody will read.
        settings = SimpleNamespace(**{**vars(FULL), "firebase_project_id": ""})
        response = _client(settings).get(CONSOLE_CONFIG_PATH)
        assert response.status_code == 200
        assert response.json() == {"configured": False, "firebase": None}

    def test_a_missing_api_key_is_also_unconfigured(self):
        settings = SimpleNamespace(**{**vars(FULL), "firebase_api_key": ""})
        assert _client(settings).get(CONSOLE_CONFIG_PATH).json()["configured"] is False


class TestLeakage:
    def test_no_secret_reaches_the_response(self):
        """This endpoint is public. Everything in it is public by definition."""
        raw = _client().get(CONSOLE_CONFIG_PATH).text
        for secret in ("super-secret", "storage-key", "13800138000",
                       "kQ2mZ8xVbNfR4t"):
            assert secret not in raw

    def test_the_response_has_exactly_two_top_level_keys(self):
        # A snapshot of the whole settings object would leak on the next field
        # anyone adds; the payload is built key by key on purpose.
        assert set(_client().get(CONSOLE_CONFIG_PATH).json()) == {
            "configured", "firebase"}


class TestReachability:
    def test_the_path_is_auth_exempt(self):
        # Whoever needs this has not signed in yet — that is what it is for.
        assert is_exempt_path(CONSOLE_CONFIG_PATH) is True

    def test_it_answers_under_enforce(self):
        app = FastAPI()
        app.middleware("http")(create_auth_middleware(
            mode="enforce", key_store=None, logger=logging.getLogger("t")))
        app.include_router(create_console_config_router(
            settings=FULL, logger=logging.getLogger("t")))
        assert TestClient(app).get(CONSOLE_CONFIG_PATH).status_code == 200
