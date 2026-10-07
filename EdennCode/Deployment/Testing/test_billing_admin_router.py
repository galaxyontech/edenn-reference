"""Billing admin endpoints: accounts CRUD, recharge, transactions, pricing."""
from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.billing.account_index import AccountIndexStore
from EdennCode.Deployment.billing.admin_router import create_billing_admin_router
from EdennCode.Deployment.billing.stores import (
    AccountStore,
    PricingStore,
    WalletTxnStore,
    rmb_to_micros,
)
from EdennCode.Deployment.Testing.test_billing_stores import FakeBillingTable

ADMIN = {"x-admin-secret": "adm-test"}


def _client(*, account_store="default", txn_store="default",
            pricing_store="default", admin_secret="adm-test"):
    logger = logging.getLogger("t")
    accounts = (AccountStore(FakeBillingTable(), logger=logger)
                if account_store == "default" else account_store)
    txns = (WalletTxnStore(FakeBillingTable(), logger=logger)
            if txn_store == "default" else txn_store)
    pricing = (PricingStore(FakeBillingTable(), logger=logger)
               if pricing_store == "default" else pricing_store)
    app = FastAPI()
    app.include_router(create_billing_admin_router(
        account_store=accounts, txn_store=txns, pricing_store=pricing,
        admin_secret=admin_secret, logger=logger))
    return TestClient(app), accounts, txns, pricing


def _client_with_index(*, account_store="default", txn_store="default",
                       pricing_store="default", admin_secret="adm-test",
                       with_index: bool = True):
    """Same as `_client`, plus an AccountIndexStore as the last tuple element.

    Separate from `_client` so the pre-existing tests keep exercising the
    no-index wiring (index_store defaults to None) unchanged.
    """
    logger = logging.getLogger("t")
    accounts = (AccountStore(FakeBillingTable(), logger=logger)
                if account_store == "default" else account_store)
    txns = (WalletTxnStore(FakeBillingTable(), logger=logger)
            if txn_store == "default" else txn_store)
    pricing = (PricingStore(FakeBillingTable(), logger=logger)
               if pricing_store == "default" else pricing_store)
    index = (AccountIndexStore(FakeBillingTable(), logger=logger)
             if with_index else None)
    app = FastAPI()
    app.include_router(create_billing_admin_router(
        account_store=accounts, txn_store=txns, pricing_store=pricing,
        admin_secret=admin_secret, logger=logger, index_store=index))
    return TestClient(app), accounts, txns, pricing, index


ACCOUNT_BODY = {
    "account_id": "acct-enterprise", "registered_name": "示例传媒科技有限公司",
    "entity_type": "company", "id_number": "91000000MA0EXAMPLE",
    "address": "北京市西城区", "email": "ops@example.invalid", "phone": "+86-10-8888-0000",
}


class TestGuard:
    def test_wrong_secret_401_and_missing_503(self):
        client, *_ = _client()
        assert client.get("/api/v1/admin/accounts",
                          headers={"x-admin-secret": "nope"}).status_code == 401
        client_off, *_ = _client(admin_secret=None)
        resp = client_off.get("/api/v1/admin/accounts", headers=ADMIN)
        assert resp.status_code == 503
        assert resp.json()["code"] == "admin_disabled"

    def test_store_missing_503(self):
        client, *_ = _client(account_store=None)
        resp = client.get("/api/v1/admin/accounts", headers=ADMIN)
        assert resp.status_code == 503
        assert resp.json()["code"] == "billing_unavailable"


class TestAccounts:
    def test_account_id_server_generated_when_omitted(self):
        client, *_ = _client()
        resp = client.post("/api/v1/admin/accounts",
                           json={"registered_name": "Acme 文化"}, headers=ADMIN)
        assert resp.status_code == 200
        acct = resp.json()["account_id"]
        assert acct.startswith("acct_") and len(acct) > 10
        # the generated id is a real, fetchable account
        assert client.get(f"/api/v1/admin/accounts/{acct}",
                          headers=ADMIN).status_code == 200

    def test_explicit_account_id_still_honored(self):
        client, *_ = _client()
        resp = client.post("/api/v1/admin/accounts",
                           json={"account_id": "910000000000",
                                 "registered_name": "示例传媒"}, headers=ADMIN)
        assert resp.json()["account_id"] == "910000000000"

    def test_create_get_list_patch(self):
        client, *_ = _client()
        resp = client.post("/api/v1/admin/accounts", json=ACCOUNT_BODY, headers=ADMIN)
        assert resp.status_code == 200
        body = resp.json()
        assert body["account_id"] == "acct-enterprise"
        assert body["balance_usd"] == 0.0
        assert body["is_active"] is True

        got = client.get("/api/v1/admin/accounts/acct-enterprise", headers=ADMIN).json()
        assert got["registered_name"] == ACCOUNT_BODY["registered_name"]
        assert got["id_number"] == ACCOUNT_BODY["id_number"]

        listed = client.get("/api/v1/admin/accounts", headers=ADMIN).json()
        assert [a["account_id"] for a in listed["accounts"]] == ["acct-enterprise"]

        patched = client.patch("/api/v1/admin/accounts/acct-enterprise",
                               json={"phone": "+86-10-9999-0000",
                                     "is_active": False},
                               headers=ADMIN).json()
        assert patched["phone"] == "+86-10-9999-0000"
        assert patched["is_active"] is False
        assert patched["email"] == ACCOUNT_BODY["email"]

    def test_create_conflict_409_and_get_missing_404(self):
        client, *_ = _client()
        client.post("/api/v1/admin/accounts", json=ACCOUNT_BODY, headers=ADMIN)
        dup = client.post("/api/v1/admin/accounts", json=ACCOUNT_BODY, headers=ADMIN)
        assert dup.status_code == 409
        assert dup.json()["code"] == "account_exists"
        missing = client.get("/api/v1/admin/accounts/ghost", headers=ADMIN)
        assert missing.status_code == 404
        assert missing.json()["code"] == "account_not_found"
        assert client.patch("/api/v1/admin/accounts/ghost", json={"phone": "1"},
                            headers=ADMIN).status_code == 404

    def test_create_validates_account_id_charset_and_name(self):
        client, *_ = _client()
        bad_id = client.post("/api/v1/admin/accounts",
                             json={"account_id": "bad/id", "registered_name": "X"},
                             headers=ADMIN)
        assert bad_id.status_code == 422
        no_name = client.post("/api/v1/admin/accounts",
                              json={"account_id": "ok", "registered_name": ""},
                              headers=ADMIN)
        assert no_name.status_code == 422


class TestRecharge:
    def test_recharge_and_adjustment_flow(self):
        client, accounts, txns, _ = _client()
        client.post("/api/v1/admin/accounts", json=ACCOUNT_BODY, headers=ADMIN)
        top_up = client.post("/api/v1/admin/accounts/acct-enterprise/recharge",
                             json={"amount_usd": 1000, "note": "首充"},
                             headers=ADMIN)
        assert top_up.status_code == 200
        body = top_up.json()
        assert body["balance_usd"] == 1000.0
        assert body["txn_type"] == "recharge"
        assert body["txn_id"]

        adj = client.post("/api/v1/admin/accounts/acct-enterprise/recharge",
                          json={"amount_usd": -250.5, "note": "correction"},
                          headers=ADMIN).json()
        assert adj["balance_usd"] == 749.5
        assert adj["txn_type"] == "adjustment"

        body = client.get("/api/v1/admin/accounts/acct-enterprise/transactions",
                          headers=ADMIN).json()
        listed = body["transactions"]
        assert [t["txn_type"] for t in listed] == ["adjustment", "recharge"]
        assert listed[0]["amount_usd"] == -250.5
        assert listed[0]["balance_after_usd"] == 749.5
        assert body["page"] == {"limit": 200, "offset": 0, "returned": 2,
                                "total_rows": 2, "has_more": False}

        # pagination: one row per page, totals/count stable across pages
        p1 = client.get("/api/v1/admin/accounts/acct-enterprise/transactions?limit=1&offset=0",
                        headers=ADMIN).json()
        p2 = client.get("/api/v1/admin/accounts/acct-enterprise/transactions?limit=1&offset=1",
                        headers=ADMIN).json()
        assert p1["page"]["has_more"] is True and p2["page"]["has_more"] is False
        assert p1["transactions"][0]["txn_type"] == "adjustment"
        assert p2["transactions"][0]["txn_type"] == "recharge"

    def test_recharge_validation_and_missing_account(self):
        client, *_ = _client()
        client.post("/api/v1/admin/accounts", json=ACCOUNT_BODY, headers=ADMIN)
        zero = client.post("/api/v1/admin/accounts/acct-enterprise/recharge",
                           json={"amount_usd": 0}, headers=ADMIN)
        assert zero.status_code == 400
        assert zero.json()["code"] == "invalid_amount"
        too_fine = client.post("/api/v1/admin/accounts/acct-enterprise/recharge",
                               json={"amount_usd": 0.0000001}, headers=ADMIN)
        assert too_fine.status_code == 400
        ghost = client.post("/api/v1/admin/accounts/ghost/recharge",
                            json={"amount_usd": 10}, headers=ADMIN)
        assert ghost.status_code == 404

    def test_recharge_invalidates_balance_cache(self):
        client, accounts, *_ = _client()
        client.post("/api/v1/admin/accounts", json=ACCOUNT_BODY, headers=ADMIN)
        before = asyncio.run(accounts.get_balance_cached("acct-enterprise"))
        assert (before.balance_micros, before.is_active) == (0, True)
        client.post("/api/v1/admin/accounts/acct-enterprise/recharge",
                    json={"amount_usd": 5}, headers=ADMIN)
        # Same-process cache must see the new balance immediately.
        after = asyncio.run(accounts.get_balance_cached("acct-enterprise"))
        assert (after.balance_micros, after.is_active) == (5_000_000, True)


class TestPricing:
    def test_put_get_delete_pricing(self):
        client, *_ = _client()
        put = client.put("/api/v1/admin/pricing/video_music",
                         json={"billing_mode": "per_request",
                               "unit_price_usd": 10, "note": "企业客户 按次"},
                         headers=ADMIN)
        assert put.status_code == 200
        assert put.json()["unit_price_usd"] == 10.0
        client.put("/api/v1/admin/pricing/image_music",
                   json={"billing_mode": "per_second",
                         "unit_price_usd": 0.5},
                   headers=ADMIN)
        listed = client.get("/api/v1/admin/pricing", headers=ADMIN).json()["pricing"]
        assert [p["price_key"] for p in listed] == ["image_music", "video_music"]
        assert [(p["unit_seconds"], p["min_billable_seconds"]) for p in listed] \
            == [(1, 0), (1, 0)]
        assert client.delete("/api/v1/admin/pricing/video_music",
                             headers=ADMIN).json() == {"deleted": True}
        gone = client.delete("/api/v1/admin/pricing/video_music", headers=ADMIN)
        assert gone.status_code == 404
        assert gone.json()["code"] == "price_key_not_found"

    def test_pricing_validation(self):
        client, *_ = _client()
        bad_key = client.put("/api/v1/admin/pricing/Video-Music!",
                             json={"billing_mode": "per_request",
                                   "unit_price_usd": 1},
                             headers=ADMIN)
        assert bad_key.status_code == 400
        assert bad_key.json()["code"] == "invalid_price_key"
        bad_mode = client.put("/api/v1/admin/pricing/video_music",
                              json={"billing_mode": "per_minute",
                                    "unit_price_usd": 1},
                              headers=ADMIN)
        assert bad_mode.status_code == 422
        bad_price = client.put("/api/v1/admin/pricing/video_music",
                               json={"billing_mode": "per_request",
                                     "unit_price_usd": 0},
                               headers=ADMIN)
        assert bad_price.status_code == 422
        modelspec_key = client.put("/api/v1/admin/pricing/image_music:edenn_studio",
                                   json={"billing_mode": "per_second",
                                         "unit_price_usd": 0.9},
                                   headers=ADMIN)
        assert modelspec_key.status_code == 200

    def test_put_pricing_sets_duration_metering(self):
        client, *_, pricing = _client()
        resp = client.put("/api/v1/admin/pricing/video_music",
                          json={"billing_mode": "per_second",
                                "unit_price_rmb": 0.9, "unit_seconds": 30,
                                "note": "视频配乐 ¥0.9 / 每 30 秒"},
                          headers=ADMIN)
        assert resp.status_code == 200
        assert resp.json()["unit_seconds"] == 30
        stored = asyncio.run(pricing.resolve("video_music", None))
        assert (stored.unit_seconds, stored.min_billable_seconds) == (30, 0)

    def test_put_pricing_rejects_metering_on_per_request(self):
        """Accepting it would store a rule that can never fire, and read back
        as though it had."""
        client, *_ = _client()
        resp = client.put("/api/v1/admin/pricing/video_music",
                          json={"billing_mode": "per_request",
                                "unit_price_usd": 1, "unit_seconds": 30},
                          headers=ADMIN)
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_metering"

    def test_put_pricing_accepts_rmb_and_converts(self):
        # _client() wires create_billing_admin_router with its default
        # rmb_per_usd=7.0, matching the rate used below.
        client, *_ = _client()
        resp = client.put("/api/v1/admin/pricing/video_music",
                          json={"billing_mode": "per_request",
                                "unit_price_rmb": 0.9,
                                "teaser_unit_price_rmb": 0.45},
                          headers=ADMIN)
        assert resp.status_code == 200
        body = resp.json()
        assert body["unit_price_micros"] == str(rmb_to_micros("0.9", 7.0))
        assert body["teaser_unit_price_micros"] == str(64_286)

    def test_put_pricing_rejects_both_usd_and_rmb(self):
        client, *_ = _client()
        resp = client.put("/api/v1/admin/pricing/video_music",
                          json={"billing_mode": "per_request",
                                "unit_price_usd": 0.1, "unit_price_rmb": 0.9},
                          headers=ADMIN)
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_amount"


class TestAccountLookup:
    """Customers only know their phone/email, so admins must resolve that to an
    account before they can recharge it."""

    def _seeded(self):
        """(client, accounts, index, account_id) with one indexed account."""
        import asyncio

        from EdennCode.Deployment.billing.account_index import (
            INDEX_KIND_EMAIL, INDEX_KIND_PHONE,
        )

        client, accounts, _, _, index = _client_with_index()
        asyncio.run(accounts.create(account_id="acct_seed",
                                    registered_name="种子公司",
                                    phone="13800138000",
                                    email="ops@example.com"))
        asyncio.run(index.claim(INDEX_KIND_PHONE, "13800138000", "acct_seed"))
        asyncio.run(index.claim(INDEX_KIND_EMAIL, "ops@example.com",
                                "acct_seed"))
        return client, accounts, index, "acct_seed"

    def test_lookup_by_email(self):
        client, *_ = self._seeded()
        resp = client.get("/api/v1/admin/account-lookup",
                          params={"email": "ops@example.com"}, headers=ADMIN)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["account_id"] == "acct_seed"
        assert body["registered_name"] == "种子公司"
        assert "balance_usd" in body          # admin needs it before recharging

    def test_lookup_by_phone(self):
        client, *_ = self._seeded()
        resp = client.get("/api/v1/admin/account-lookup",
                          params={"phone": "13800138000"}, headers=ADMIN)
        assert resp.status_code == 200
        assert resp.json()["account_id"] == "acct_seed"

    def test_lookup_normalizes_the_input(self):
        # An admin pastes whatever the customer wrote.
        client, *_ = self._seeded()
        for params in ({"phone": "+86 138-0013-8000"},
                       {"email": "  OPS@Example.COM "}):
            resp = client.get("/api/v1/admin/account-lookup",
                              params=params, headers=ADMIN)
            assert resp.status_code == 200, params
            assert resp.json()["account_id"] == "acct_seed"

    def test_unknown_contact_404(self):
        client, *_ = self._seeded()
        resp = client.get("/api/v1/admin/account-lookup",
                          params={"email": "nobody@example.com"}, headers=ADMIN)
        assert resp.status_code == 404
        assert resp.json()["code"] == "account_not_found"

    def test_both_or_neither_parameter_400(self):
        client, *_ = self._seeded()
        for params in ({"phone": "13800138000", "email": "ops@example.com"},
                       {}):
            resp = client.get("/api/v1/admin/account-lookup",
                              params=params, headers=ADMIN)
            assert resp.status_code == 400, params
            assert resp.json()["code"] == "invalid_lookup"

    def test_blank_parameter_is_treated_as_absent(self):
        client, *_ = self._seeded()
        resp = client.get("/api/v1/admin/account-lookup",
                          params={"email": "   "}, headers=ADMIN)
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_lookup"

    # The next two pin a deliberate asymmetry that follows from comparing
    # *normalized* values (which is what makes blank-is-absent work above):
    # normalize_phone strips non-digits, so garbage phone -> "" -> absent, while
    # normalize_email only strips and lowercases, so garbage email stays
    # truthy -> present. Pinned because a future normalization tweak could flip
    # either direction silently. Neither case can return a *wrong* account.

    def test_unparseable_phone_beside_valid_email_serves_the_email(self):
        client, *_ = self._seeded()
        resp = client.get("/api/v1/admin/account-lookup",
                          params={"phone": "---",
                                  "email": "ops@example.com"}, headers=ADMIN)
        assert resp.status_code == 200, resp.text
        assert resp.json()["account_id"] == "acct_seed"

    def test_garbage_email_beside_valid_phone_is_400(self):
        client, *_ = self._seeded()
        resp = client.get("/api/v1/admin/account-lookup",
                          params={"phone": "13800138000",
                                  "email": "---"}, headers=ADMIN)
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "invalid_lookup"

    def test_requires_admin_secret(self):
        client, *_ = self._seeded()
        resp = client.get("/api/v1/admin/account-lookup",
                          params={"email": "ops@example.com"})
        assert resp.status_code == 401
        assert resp.json()["code"] == "invalid_admin_secret"

    def test_dangling_index_entry_is_404_not_a_phantom_account(self, caplog):
        import asyncio

        from EdennCode.Deployment.billing.account_index import INDEX_KIND_EMAIL

        client, _, index, _ = self._seeded()
        asyncio.run(index.claim(INDEX_KIND_EMAIL, "ghost@example.com",
                                "acct_does_not_exist"))
        with caplog.at_level(logging.WARNING, logger="t"):
            resp = client.get("/api/v1/admin/account-lookup",
                              params={"email": "ghost@example.com"},
                              headers=ADMIN)
        assert resp.status_code == 404
        assert resp.json()["code"] == "account_not_found"
        # The warning is a requirement, not decoration: it is the only signal
        # that the index and the accounts table have diverged. Assert on the
        # substance (kind + the orphaned id) so the wording stays free.
        warnings = [r.getMessage() for r in caplog.records
                    if r.levelno >= logging.WARNING]
        assert any(INDEX_KIND_EMAIL in m and "acct_does_not_exist" in m
                   for m in warnings), warnings

    def test_no_index_store_503(self):
        client, *_ = _client_with_index(with_index=False)
        resp = client.get("/api/v1/admin/account-lookup",
                          params={"email": "ops@example.com"}, headers=ADMIN)
        assert resp.status_code == 503
        assert resp.json()["code"] == "billing_unavailable"

    def test_index_storage_outage_503(self):
        # Spec §3.4: "索引或账户存储不可用 → 503 billing_unavailable" covers the
        # outage, not just the unconfigured case. A live Table failure must not
        # escape as a 500.
        client, accounts, index, _ = self._seeded()
        index._table_client.fail_reads = True
        resp = client.get("/api/v1/admin/account-lookup",
                          params={"email": "ops@example.com"}, headers=ADMIN)
        assert resp.status_code == 503, resp.text
        assert resp.json()["code"] == "billing_unavailable"

        # The account read is the second storage hop and must be covered too,
        # otherwise moving it out of the try block would go unnoticed.
        index._table_client.fail_reads = False
        accounts._table_client.fail_reads = True
        resp = client.get("/api/v1/admin/account-lookup",
                          params={"email": "ops@example.com"}, headers=ADMIN)
        assert resp.status_code == 503, resp.text
        assert resp.json()["code"] == "billing_unavailable"
