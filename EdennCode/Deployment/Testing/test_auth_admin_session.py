"""Admin console: an allowlisted Firebase session in place of the admin secret.

The secret can read every customer's ledger and revoke anyone's key. Putting it
in a browser would mean shipping it in every XHR header, so the console
authenticates admins the same way it authenticates customers — a phone login —
and the *server* decides who is an admin.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from EdennCode.Deployment.auth.admin_router import create_admin_router
from EdennCode.Deployment.auth.console_session import ConsoleSession
from EdennCode.Deployment.auth.key_store import ApiKeyStore
from EdennCode.Deployment.billing.admin_router import create_billing_admin_router
from EdennCode.Deployment.billing.stores import AccountStore, WalletTxnStore
from EdennCode.Deployment.Testing.test_auth_key_store import FakeTableClient
from EdennCode.Deployment.Testing.test_auth_usage_recorder import (
    FakeUsageTable,
    _recorder,
)
from EdennCode.Deployment.Testing.test_billing_stores import FakeBillingTable

SECRET = "admin-secret-for-tests"
ADMIN = ConsoleSession(uid="admin-uid", phone_number="+15550100",
                       account_id="acct_admin", is_admin=True)
CUSTOMER = ConsoleSession(uid="cust-uid", phone_number="+819012345678",
                          account_id="acct_cust", is_admin=False)


def _plant(app: FastAPI, session: Optional[ConsoleSession]) -> None:
    @app.middleware("http")
    async def plant(request: Request, call_next):
        request.state.console_session = session
        return await call_next(request)


def _keys_app(session: Optional[ConsoleSession] = None,
              secret: Optional[str] = SECRET):
    logger = logging.getLogger("t")
    key_store = ApiKeyStore(FakeTableClient(), logger=logger)
    usage_recorder, _ = _recorder(FakeUsageTable())
    app = FastAPI()
    _plant(app, session)
    app.include_router(create_admin_router(
        key_store=key_store, usage_recorder=usage_recorder,
        admin_secret=secret, logger=logger))
    return TestClient(app), key_store


def _billing_app(session: Optional[ConsoleSession] = None,
                 secret: Optional[str] = SECRET):
    logger = logging.getLogger("t")
    accounts = AccountStore(FakeBillingTable(), logger=logger)
    txns = WalletTxnStore(FakeBillingTable(), logger=logger)
    usage_recorder, usage_table = _recorder(FakeUsageTable())
    app = FastAPI()
    _plant(app, session)
    app.include_router(create_billing_admin_router(
        account_store=accounts, txn_store=txns, pricing_store=None,
        admin_secret=secret, logger=logger, usage_recorder=usage_recorder))
    return TestClient(app), accounts, usage_recorder


class TestSessionInPlaceOfTheSecret:
    def test_allowlisted_session_passes_without_the_secret(self):
        client, _ = _keys_app(ADMIN)
        assert client.get("/api/v1/admin/keys").status_code == 200

    def test_ordinary_customer_session_does_not(self):
        client, _ = _keys_app(CUSTOMER)
        resp = client.get("/api/v1/admin/keys")
        assert resp.status_code == 401
        assert resp.json()["code"] == "invalid_admin_secret"

    def test_no_credentials_at_all_is_still_401(self):
        client, _ = _keys_app(None)
        assert client.get("/api/v1/admin/keys").status_code == 401

    def test_the_secret_still_works(self):
        client, _ = _keys_app(None)
        resp = client.get("/api/v1/admin/keys",
                          headers={"x-admin-secret": SECRET})
        assert resp.status_code == 200

    def test_a_wrong_secret_is_still_rejected(self):
        client, _ = _keys_app(None)
        assert client.get("/api/v1/admin/keys",
                          headers={"x-admin-secret": "nope"}).status_code == 401

    def test_session_admin_works_even_with_no_secret_configured(self):
        # A deployment can now run the console without an API_ADMIN_SECRET at
        # all; the allowlist is the credential.
        client, _ = _keys_app(ADMIN, secret=None)
        assert client.get("/api/v1/admin/keys").status_code == 200

    def test_no_secret_and_no_session_is_disabled_not_open(self):
        client, _ = _keys_app(None, secret=None)
        resp = client.get("/api/v1/admin/keys")
        assert resp.status_code == 503
        assert resp.json()["code"] == "admin_disabled"

    def test_billing_router_honors_the_session_too(self):
        client, _, _ = _billing_app(ADMIN)
        assert client.get("/api/v1/admin/accounts").status_code == 200

    def test_billing_router_refuses_a_customer_session(self):
        client, _, _ = _billing_app(CUSTOMER)
        assert client.get("/api/v1/admin/accounts").status_code == 401

    def test_write_endpoints_are_guarded_the_same_way(self):
        client, _, _ = _billing_app(CUSTOMER)
        resp = client.post("/api/v1/admin/accounts",
                           json={"account_id": "acct_x", "registered_name": "X"})
        assert resp.status_code == 401


class TestKeyInventory:
    def test_admin_key_listing_exposes_last_used(self):
        """Without it nobody can tell a dead key from a busy one."""
        client, key_store = _keys_app(ADMIN)
        asyncio.run(key_store.mint(user_id="acct_1", note="prod"))
        row = client.get("/api/v1/admin/keys").json()["keys"][0]
        assert "last_used_at" in row
        assert row["user_id"] == "acct_1"

    def test_listing_never_leaks_key_material(self):
        client, key_store = _keys_app(ADMIN)
        plaintext, record = asyncio.run(key_store.mint(user_id="acct_1"))
        raw = client.get("/api/v1/admin/keys").text
        assert plaintext not in raw
        assert record.key_hash not in raw


def _job(recorder, *, user_id: str, job_id: str, billed_micros: int,
         tokens: int = 100, ts: str = "2026-07-20T10:00:00+00:00"):
    recorder._write_row_sync({
        "PartitionKey": user_id,
        "RowKey": f"row-{job_id}",
        "job_id": job_id,
        "endpoint": "/api/v1/jobs/video",
        "status": "success",
        "timestamp_utc": ts,
        "total_tokens": tokens,
        "total_cost_usd": 0.5,
        "billed_amount_micros": str(billed_micros),
        "model_spec": "edenn_basic",
        "key_prefix": "sk-aaaaaaaaa",
    })


class TestUsageSummary:
    def test_rolls_up_every_account_and_the_platform(self):
        client, accounts, recorder = _billing_app(ADMIN)
        asyncio.run(accounts.create(account_id="acct_a", registered_name="A公司",
                                    entity_type="company"))
        asyncio.run(accounts.create(account_id="acct_b", registered_name="B公司",
                                    entity_type="company"))
        asyncio.run(accounts.adjust_balance("acct_a", 10_000_000))
        _job(recorder, user_id="acct_a", job_id="j1", billed_micros=1_500_000)
        _job(recorder, user_id="acct_a", job_id="j2", billed_micros=500_000)
        _job(recorder, user_id="acct_b", job_id="j3", billed_micros=250_000)

        body = client.get("/api/v1/admin/usage-summary").json()
        rows = {r["account_id"]: r for r in body["accounts"]}
        assert rows["acct_a"]["jobs"] == 2
        assert rows["acct_a"]["total_billed_usd"] == 2.0
        assert rows["acct_a"]["balance_usd"] == 10.0
        assert rows["acct_a"]["registered_name"] == "A公司"
        assert rows["acct_b"]["jobs"] == 1
        assert body["totals"]["jobs"] == 3
        assert body["totals"]["total_billed_usd"] == 2.25
        assert body["totals"]["accounts"] == 2

    def test_window_bounds_are_applied(self):
        client, accounts, recorder = _billing_app(ADMIN)
        asyncio.run(accounts.create(account_id="acct_a", registered_name="A",
                                    entity_type="company"))
        _job(recorder, user_id="acct_a", job_id="old", billed_micros=1_000_000,
             ts="2026-06-01T00:00:00+00:00")
        _job(recorder, user_id="acct_a", job_id="new", billed_micros=2_000_000,
             ts="2026-07-20T00:00:00+00:00")
        body = client.get(
            "/api/v1/admin/usage-summary?from=2026-07-01&to=2026-08-01").json()
        assert body["totals"]["jobs"] == 1
        assert body["totals"]["total_billed_usd"] == 2.0

    def test_truncation_is_reported_rather_than_silent(self):
        """A silent top-N reads as 'this is everything'. It isn't."""
        client, accounts, _ = _billing_app(ADMIN)
        for i in range(5):
            asyncio.run(accounts.create(account_id=f"acct_{i}",
                                        registered_name=f"A{i}",
                                        entity_type="company"))
        body = client.get("/api/v1/admin/usage-summary?max_accounts=2").json()
        assert len(body["accounts"]) == 2
        assert body["truncated"] is True
        assert body["accounts_scanned"] == 2

    def test_not_truncated_when_everything_fits(self):
        client, accounts, _ = _billing_app(ADMIN)
        asyncio.run(accounts.create(account_id="acct_0", registered_name="A",
                                    entity_type="company"))
        body = client.get("/api/v1/admin/usage-summary").json()
        assert body["truncated"] is False

    def test_it_is_admin_only(self):
        client, _, _ = _billing_app(CUSTOMER)
        assert client.get("/api/v1/admin/usage-summary").status_code == 401
