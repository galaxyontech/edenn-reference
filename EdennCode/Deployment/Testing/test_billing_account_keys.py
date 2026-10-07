"""Self-serve API key management from the console.

The authorization rules are the point of this module, so they are asserted
first: only a console session may manage keys (an API key cannot mint its own
successors), and revocation is scoped to the session's account (knowing a
``key_prefix`` must not be enough to disable it).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from EdennCode.Deployment.auth.console_session import ConsoleSession
from EdennCode.Deployment.auth.key_store import (
    ApiKeyRecord,
    ApiKeyStore,
    Principal,
    hash_api_key,
)
from EdennCode.Deployment.billing.account_router import (
    MAX_ACTIVE_KEYS_PER_ACCOUNT,
    create_account_router,
)
from EdennCode.Deployment.billing.stores import AccountStore
from EdennCode.Deployment.Testing.test_auth_key_store import FakeTableClient
from EdennCode.Deployment.Testing.test_auth_usage_recorder import (
    FakeUsageTable,
    _recorder,
)
from EdennCode.Deployment.Testing.test_billing_stores import FakeBillingTable

ACCOUNT = "acct_0123456789abcdef01234567"
UID = "kQ2mZ8xVbNfR4tYuIoPaSdFgHjK1"


def _session(account_id: Optional[str] = ACCOUNT,
             is_admin: bool = False) -> ConsoleSession:
    return ConsoleSession(uid=UID, phone_number="+15550100",
                          account_id=account_id, is_admin=is_admin)


def _client(*, session: Optional[ConsoleSession] = "default",
            principal: Optional[Principal] = "default",
            key_store: object = "default"):
    logger = logging.getLogger("t")
    if session == "default":
        session = _session()
    if principal == "default":
        principal = (Principal(user_id=session.account_id, key_prefix="")
                     if session and session.account_id else None)
    if key_store == "default":
        key_store = ApiKeyStore(FakeTableClient(), logger=logger)
    accounts = AccountStore(FakeBillingTable(), logger=logger)
    usage_recorder, _ = _recorder(FakeUsageTable())
    app = FastAPI()

    @app.middleware("http")
    async def plant(request: Request, call_next):
        request.state.principal = principal
        request.state.console_session = session
        return await call_next(request)

    app.include_router(create_account_router(
        account_store=accounts, usage_recorder=usage_recorder,
        key_store=key_store, logger=logger))
    return TestClient(app), key_store, accounts


class TestListing:
    def test_lists_only_this_accounts_keys(self):
        client, key_store, _ = _client()
        asyncio.run(key_store.mint(user_id=ACCOUNT, note="prod"))
        asyncio.run(key_store.mint(user_id="acct_someone_else", note="theirs"))
        body = client.get("/api/v1/account/keys").json()
        assert len(body["keys"]) == 1
        assert body["keys"][0]["name"] == "prod"

    def test_never_returns_key_material(self):
        client, key_store, _ = _client()
        plaintext, record = asyncio.run(key_store.mint(user_id=ACCOUNT))
        raw = client.get("/api/v1/account/keys").text
        assert plaintext not in raw
        assert record.key_hash not in raw
        assert record.key_prefix in raw

    def test_exposes_the_fields_a_deletion_decision_needs(self):
        client, key_store, _ = _client()
        asyncio.run(key_store.mint(user_id=ACCOUNT, note="prod"))
        row = client.get("/api/v1/account/keys").json()["keys"][0]
        assert set(row) == {"key_prefix", "key_suffix", "name", "created_at",
                            "last_used_at", "is_active", "revoked_at"}

    def test_the_masked_key_ends_the_way_the_real_one_does(self):
        # Prefix alone cannot answer "is the key in my .env the one on this
        # row?" — every key starts with the same three characters and the rest
        # of the head is easy to misread. The tail is what people check.
        client, key_store, _ = _client()
        plaintext, _ = asyncio.run(key_store.mint(user_id=ACCOUNT))
        row = client.get("/api/v1/account/keys").json()["keys"][0]
        assert row["key_suffix"] == plaintext[-4:]
        assert len(row["key_suffix"]) == 4

    def test_a_key_minted_before_suffixes_lists_without_one(self):
        # Every key already in production is this row: the plaintext is gone,
        # so its suffix can never be backfilled. Listing must degrade to an
        # empty string, not 500 and not a fabricated tail.
        client, key_store, _ = _client()
        asyncio.run(key_store.adopt(ApiKeyRecord(
            key_hash=hash_api_key("REDACTED_API_KEY"),
            user_id=ACCOUNT, key_prefix="sk-XbgqH8R8r", note="ancient",
            created_at="2026-01-01T00:00:00+00:00", revoked_at=None,
            is_active=True)))
        row = client.get("/api/v1/account/keys").json()["keys"][0]
        assert row["key_suffix"] == ""

    def test_revoked_keys_are_hidden_unless_asked_for(self):
        client, key_store, _ = _client()
        _, record = asyncio.run(key_store.mint(user_id=ACCOUNT))
        asyncio.run(key_store.mint(user_id=ACCOUNT, note="live"))
        asyncio.run(key_store.revoke_for_user(record.key_prefix, ACCOUNT))
        assert len(client.get("/api/v1/account/keys").json()["keys"]) == 1
        every = client.get("/api/v1/account/keys?include_revoked=true").json()
        assert len(every["keys"]) == 2

    def test_no_key_store_is_503(self):
        client, *_ = _client(key_store=None)
        resp = client.get("/api/v1/account/keys")
        assert resp.status_code == 503
        assert resp.json()["code"] == "auth_unavailable"


class TestCreation:
    def test_creates_a_named_key_shown_exactly_once(self):
        client, key_store, _ = _client()
        resp = client.post("/api/v1/account/keys", json={"name": "staging"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["api_key"].startswith("sk-")
        assert body["key_prefix"] == body["api_key"][:12]
        assert body["name"] == "staging"
        assert body["message"]

        principal = asyncio.run(key_store.lookup(body["api_key"]))
        assert principal.user_id == ACCOUNT
        listed = client.get("/api/v1/account/keys").json()["keys"]
        assert [k["key_prefix"] for k in listed] == [body["key_prefix"]]
        assert body["api_key"] not in client.get("/api/v1/account/keys").text

    def test_name_is_optional(self):
        client, *_ = _client()
        assert client.post("/api/v1/account/keys", json={}).status_code == 200

    def test_overlong_name_is_refused(self):
        client, *_ = _client()
        resp = client.post("/api/v1/account/keys", json={"name": "x" * 600})
        assert resp.status_code == 422

    def test_active_key_count_is_capped(self):
        client, key_store, _ = _client()
        for _ in range(MAX_ACTIVE_KEYS_PER_ACCOUNT):
            asyncio.run(key_store.mint(user_id=ACCOUNT))
        resp = client.post("/api/v1/account/keys", json={"name": "one too many"})
        assert resp.status_code == 409
        assert resp.json()["code"] == "key_limit_reached"

    def test_revoked_keys_do_not_count_towards_the_cap(self):
        client, key_store, _ = _client()
        for _ in range(MAX_ACTIVE_KEYS_PER_ACCOUNT):
            _, record = asyncio.run(key_store.mint(user_id=ACCOUNT))
        asyncio.run(key_store.revoke_for_user(record.key_prefix, ACCOUNT))
        assert client.post("/api/v1/account/keys", json={}).status_code == 200


class TestRevocation:
    def test_owner_revokes_their_own_key(self):
        client, key_store, _ = _client()
        plaintext, record = asyncio.run(key_store.mint(user_id=ACCOUNT))
        resp = client.delete(f"/api/v1/account/keys/{record.key_prefix}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["revoked"] is True
        fresh = ApiKeyStore(key_store._table_client)
        assert asyncio.run(fresh.lookup(plaintext)) is None

    def test_another_accounts_prefix_is_a_404_and_a_no_op(self):
        """key_prefix is printed in 详单 rows — knowing one proves nothing."""
        client, key_store, _ = _client()
        victim, record = asyncio.run(key_store.mint(user_id="acct_victim"))
        resp = client.delete(f"/api/v1/account/keys/{record.key_prefix}")
        assert resp.status_code == 404
        assert resp.json()["code"] == "key_not_found"
        fresh = ApiKeyStore(key_store._table_client)
        assert asyncio.run(fresh.lookup(victim)) is not None

    def test_unknown_and_foreign_prefixes_are_indistinguishable(self):
        # Same status and code, so the endpoint cannot be used to test whether
        # a prefix exists on some other account.
        client, key_store, _ = _client()
        _, record = asyncio.run(key_store.mint(user_id="acct_victim"))
        foreign = client.delete(f"/api/v1/account/keys/{record.key_prefix}")
        missing = client.delete("/api/v1/account/keys/sk-nonexistent")
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json()["code"] == missing.json()["code"] == "key_not_found"

    def test_revoking_twice_is_a_404(self):
        client, key_store, _ = _client()
        _, record = asyncio.run(key_store.mint(user_id=ACCOUNT))
        assert client.delete(
            f"/api/v1/account/keys/{record.key_prefix}").status_code == 200
        assert client.delete(
            f"/api/v1/account/keys/{record.key_prefix}").status_code == 404


class TestCredentialRules:
    def test_an_api_key_cannot_manage_keys(self):
        """A leaked key must not be able to mint successors or revoke siblings."""
        client, key_store, _ = _client(
            session=None, principal=Principal(user_id=ACCOUNT,
                                              key_prefix="sk-abc123def"))
        _, record = asyncio.run(key_store.mint(user_id=ACCOUNT))
        for method, path in (("get", "/api/v1/account/keys"),
                             ("post", "/api/v1/account/keys"),
                             ("delete", f"/api/v1/account/keys/{record.key_prefix}")):
            resp = getattr(client, method)(path)
            assert resp.status_code == 403, path
            assert resp.json()["code"] == "session_required"
        # …and nothing happened.
        assert len(asyncio.run(key_store.list_keys(user_id=ACCOUNT))) == 1
        assert asyncio.run(key_store.list_keys(user_id=ACCOUNT))[0].is_active

    def test_no_credentials_at_all_is_401(self):
        client, *_ = _client(session=None, principal=None)
        resp = client.get("/api/v1/account/keys")
        assert resp.status_code == 401

    def test_signed_in_without_an_account_is_told_to_sign_up(self):
        client, *_ = _client(session=_session(account_id=None), principal=None)
        for method, path in (("get", "/api/v1/account/keys"),
                             ("post", "/api/v1/account/keys")):
            resp = getattr(client, method)(path)
            assert resp.status_code == 404, path
            assert resp.json()["code"] == "signup_required"


class TestReadEndpointsUnderASession:
    def test_balance_reports_signup_required_without_an_account(self):
        client, _, accounts = _client(session=_session(account_id=None),
                                      principal=None)
        resp = client.get("/api/v1/account/balance")
        assert resp.status_code == 404
        assert resp.json()["code"] == "signup_required"

    def test_balance_works_for_a_session_with_an_account(self):
        client, _, accounts = _client()
        asyncio.run(accounts.create(account_id=ACCOUNT, registered_name="A公司",
                                    entity_type="company"))
        body = client.get("/api/v1/account/balance").json()
        assert body["account_id"] == ACCOUNT
        assert body["balance_usd"] == 0.0

    def test_usage_works_for_a_session_with_an_account(self):
        client, *_ = _client()
        body = client.get("/api/v1/account/usage").json()
        assert body["rows"] == []
        assert body["totals"]["jobs"] == 0


class TestRenaming:
    def test_owner_renames_their_key(self):
        client, key_store, _ = _client()
        _, record = asyncio.run(key_store.mint(user_id=ACCOUNT, note="default"))
        resp = client.patch(f"/api/v1/account/keys/{record.key_prefix}",
                            json={"name": "生产环境"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "生产环境"
        assert client.get("/api/v1/account/keys").json()["keys"][0]["name"] == "生产环境"

    def test_another_accounts_prefix_is_a_404_and_a_no_op(self):
        client, key_store, _ = _client()
        _, record = asyncio.run(key_store.mint(user_id="acct_victim", note="prod"))
        resp = client.patch(f"/api/v1/account/keys/{record.key_prefix}",
                            json={"name": "pwned"})
        assert resp.status_code == 404
        assert asyncio.run(
            key_store.list_keys(user_id="acct_victim"))[0].note == "prod"

    def test_an_api_key_cannot_rename(self):
        client, key_store, _ = _client(
            session=None, principal=Principal(user_id=ACCOUNT, key_prefix="sk-x"))
        _, record = asyncio.run(key_store.mint(user_id=ACCOUNT))
        resp = client.patch(f"/api/v1/account/keys/{record.key_prefix}",
                            json={"name": "x"})
        assert resp.status_code == 403
        assert resp.json()["code"] == "session_required"

    def test_a_blank_name_is_refused(self):
        client, key_store, _ = _client()
        _, record = asyncio.run(key_store.mint(user_id=ACCOUNT, note="keep"))
        resp = client.patch(f"/api/v1/account/keys/{record.key_prefix}",
                            json={"name": "   "})
        assert resp.status_code == 400
        assert asyncio.run(key_store.list_keys(user_id=ACCOUNT))[0].note == "keep"
