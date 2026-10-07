"""Console sessions: a Firebase ID token standing in for an API key.

Two properties carry the security weight here and are asserted directly:
a session is only honored on console paths (never on a billable endpoint), and
admin scope comes from a server-side allowlist — never from anything the token
or the caller can set.
"""
from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from EdennCode.Deployment.auth.console_session import (
    ConsoleSessionResolver,
    ConsoleSessionUnavailable,
    parse_allowlist,
)
from EdennCode.Deployment.auth.firebase_verifier import VerifiedIdentity
from EdennCode.Deployment.auth.key_store import ApiKeyStore
from EdennCode.Deployment.auth.middleware import create_auth_middleware
from EdennCode.Deployment.billing.account_index import (
    INDEX_KIND_FIREBASE,
    AccountIndexStore,
)
from EdennCode.Deployment.Testing.test_auth_key_store import FakeTableClient
from EdennCode.Deployment.Testing.test_auth_signup_verified import FakeVerifier
from EdennCode.Deployment.Testing.test_billing_stores import FakeBillingTable

UID = "kQ2mZ8xVbNfR4tYuIoPaSdFgHjK1"
ADMIN_UID = "aDmIn0000000000000000000000A"
# Reserved synthetic test number, never a real one: the token carries it in
# E.164, the admin allowlist below is typed the way a human types it.
PHONE = "+8613000000000"
ACCOUNT = "acct_0123456789abcdef01234567"


def _identity(uid: str = UID, phone: str = PHONE) -> VerifiedIdentity:
    return VerifiedIdentity(uid=uid, phone_number=phone, provider="phone")


def _resolver(*, admin_uids=(), admin_phones=(), index=None, verifier="default"):
    logger = logging.getLogger("t")
    index = index if index is not None else AccountIndexStore(
        FakeBillingTable(), logger=logger)
    if verifier == "default":
        verifier = FakeVerifier({
            "good": _identity(),
            "admin": _identity(ADMIN_UID, "+819012345678"),
        })
    resolver = ConsoleSessionResolver(
        verifier=verifier, index_store=index, admin_uids=admin_uids,
        admin_phones=admin_phones, logger=logger)
    return resolver, index


class TestAllowlistParsing:
    def test_splits_and_trims(self):
        assert parse_allowlist(" a , b ,, c ") == ("a", "b", "c")

    def test_empty_is_empty(self):
        assert parse_allowlist("") == ()
        assert parse_allowlist(None) == ()


class TestResolution:
    def test_known_uid_resolves_to_its_account(self):
        resolver, index = _resolver()
        asyncio.run(index.claim(INDEX_KIND_FIREBASE, UID, ACCOUNT))
        session = asyncio.run(resolver.resolve("good"))
        assert session is not None
        assert session.uid == UID
        assert session.account_id == ACCOUNT
        assert session.phone_number == PHONE
        assert session.is_admin is False

    def test_verified_but_unregistered_uid_has_no_account(self):
        # Signed in, never signed up: a real state the console must handle,
        # not an error.
        resolver, _ = _resolver()
        session = asyncio.run(resolver.resolve("good"))
        assert session is not None
        assert session.account_id is None

    def test_bad_token_resolves_to_nothing(self):
        resolver, _ = _resolver()
        assert asyncio.run(resolver.resolve("forged")) is None

    def test_blank_token_resolves_to_nothing(self):
        resolver, _ = _resolver()
        assert asyncio.run(resolver.resolve("")) is None

    def test_no_verifier_means_no_sessions(self):
        resolver, _ = _resolver(verifier=None)
        assert asyncio.run(resolver.resolve("good")) is None

    def test_index_outage_fails_closed(self):
        # Answering "no account" while storage is down would tell a paying
        # customer to sign up again. 503 is the honest answer.
        logger = logging.getLogger("t")
        table = FakeBillingTable()
        table.fail_reads = True
        resolver, _ = _resolver(index=AccountIndexStore(table, logger=logger))
        with pytest.raises(ConsoleSessionUnavailable):
            asyncio.run(resolver.resolve("good"))


class TestAdminScope:
    def test_uid_allowlist_grants_admin(self):
        resolver, _ = _resolver(admin_uids=(ADMIN_UID,))
        assert asyncio.run(resolver.resolve("admin")).is_admin is True
        assert asyncio.run(resolver.resolve("good")).is_admin is False

    def test_phone_allowlist_grants_admin_across_formats(self):
        # The allowlist is written by a human; the token carries E.164. Same
        # reserved number as PHONE, spelled the national way.
        resolver, _ = _resolver(admin_phones=("130 0000 0000",))
        assert asyncio.run(resolver.resolve("good")).is_admin is True

    def test_empty_allowlists_grant_nobody(self):
        resolver, _ = _resolver()
        assert asyncio.run(resolver.resolve("admin")).is_admin is False

    def test_uid_matching_is_case_sensitive(self):
        # Firebase UIDs are case-sensitive; a case-folding allowlist would
        # hand admin to a different, attacker-registerable user.
        resolver, _ = _resolver(admin_uids=(ADMIN_UID.lower(),))
        assert asyncio.run(resolver.resolve("admin")).is_admin is False


def _app(*, mode: str = "enforce", admin_uids=()):
    """Middleware over a probe route, a billable route and an admin route."""
    logger = logging.getLogger("t")
    key_store = ApiKeyStore(FakeTableClient(), logger=logger)
    index = AccountIndexStore(FakeBillingTable(), logger=logger)
    resolver, _ = _resolver(admin_uids=admin_uids, index=index)
    app = FastAPI()
    app.middleware("http")(create_auth_middleware(
        mode=mode, key_store=key_store, logger=logger,
        console_session_resolver=resolver))

    def _state(request: Request) -> dict:
        session = getattr(request.state, "console_session", None)
        principal = getattr(request.state, "principal", None)
        return {
            "account_id": principal.user_id if principal else None,
            "key_prefix": principal.key_prefix if principal else None,
            "session_uid": session.uid if session else None,
            "is_admin": session.is_admin if session else None,
        }

    @app.get("/api/v1/account/balance")
    def balance(request: Request):
        return _state(request)

    @app.get("/api/v1/admin/accounts")
    def admin_accounts(request: Request):
        return _state(request)

    @app.post("/api/v1/jobs/video")
    def submit(request: Request):
        return _state(request)

    return app, key_store, index


class TestMiddlewareIntegration:
    def test_session_token_authenticates_an_account_path(self):
        app, _, index = _app()
        asyncio.run(index.claim(INDEX_KIND_FIREBASE, UID, ACCOUNT))
        resp = TestClient(app).get(
            "/api/v1/account/balance",
            headers={"Authorization": "Bearer good"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["account_id"] == ACCOUNT
        # No key was used, so nothing should be attributed to one.
        assert resp.json()["key_prefix"] == ""

    def test_session_token_cannot_submit_a_billable_job(self):
        """A console session must never be a spending credential."""
        app, _, index = _app()
        asyncio.run(index.claim(INDEX_KIND_FIREBASE, UID, ACCOUNT))
        resp = TestClient(app).post(
            "/api/v1/jobs/video", headers={"Authorization": "Bearer good"})
        assert resp.status_code == 401
        assert resp.json()["code"] == "invalid_api_key"

    def test_api_keys_still_work_on_account_paths(self):
        app, key_store, _ = _app()
        plaintext, record = asyncio.run(key_store.mint(user_id="acct_legacy"))
        resp = TestClient(app).get(
            "/api/v1/account/balance",
            headers={"Authorization": f"Bearer {plaintext}"})
        assert resp.status_code == 200
        assert resp.json()["account_id"] == "acct_legacy"
        assert resp.json()["key_prefix"] == record.key_prefix
        assert resp.json()["session_uid"] is None

    def test_signed_in_without_an_account_reaches_the_route(self):
        # The router decides what to say (404 signup_required); the middleware
        # must not turn "no account yet" into "bad credentials".
        app, _, _ = _app()
        resp = TestClient(app).get(
            "/api/v1/account/balance",
            headers={"Authorization": "Bearer good"})
        assert resp.status_code == 200
        assert resp.json()["account_id"] is None
        assert resp.json()["session_uid"] == UID

    def test_forged_token_is_rejected_under_enforce(self):
        app, _, _ = _app()
        resp = TestClient(app).get(
            "/api/v1/account/balance",
            headers={"Authorization": "Bearer forged"})
        assert resp.status_code == 401

    def test_admin_path_carries_the_session(self):
        app, _, _ = _app(admin_uids=(ADMIN_UID,))
        resp = TestClient(app).get(
            "/api/v1/admin/accounts",
            headers={"Authorization": "Bearer admin"})
        assert resp.status_code == 200
        assert resp.json()["is_admin"] is True

    def test_admin_path_stays_reachable_with_no_credentials(self):
        # It is guarded by x-admin-secret inside the router; the middleware's
        # exemption must not change just because sessions exist now.
        app, _, _ = _app()
        resp = TestClient(app).get("/api/v1/admin/accounts")
        assert resp.status_code == 200
        assert resp.json()["session_uid"] is None
