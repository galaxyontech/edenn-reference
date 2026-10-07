"""User-facing account endpoints: balance + itemized usage (详单)."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from EdennCode.Deployment.auth.key_store import Principal
from EdennCode.Deployment.billing.account_router import (
    USER_USAGE_FIELDS,
    create_account_router,
)
from EdennCode.Deployment.billing.stores import AccountStore
from EdennCode.Deployment.Testing.test_auth_usage_recorder import (
    FakeUsageTable,
    _recorder,
)
from EdennCode.Deployment.Testing.test_billing_stores import FakeBillingTable

PRINCIPAL = Principal(user_id="user-1", key_prefix="sk-abc123def")


def _client(*, principal: Optional[Principal] = PRINCIPAL,
            account_store="default", usage_recorder="default"):
    logger = logging.getLogger("t")
    accounts = (AccountStore(FakeBillingTable(), logger=logger)
                if account_store == "default" else account_store)
    if usage_recorder == "default":
        usage_recorder, _ = _recorder(FakeUsageTable())
    app = FastAPI()

    @app.middleware("http")
    async def plant_principal(request: Request, call_next):
        request.state.principal = principal
        return await call_next(request)

    app.include_router(create_account_router(
        account_store=accounts, usage_recorder=usage_recorder, logger=logger))
    return TestClient(app), accounts, usage_recorder


class TestAuthRequired:
    def test_both_endpoints_401_without_principal(self):
        client, *_ = _client(principal=None)
        for path in ("/api/v1/account/balance", "/api/v1/account/usage"):
            resp = client.get(path)
            assert resp.status_code == 401, path
            assert resp.json()["code"] == "api_key_required"


class TestBalance:
    def test_balance_happy_path(self):
        client, accounts, _ = _client()
        asyncio.run(accounts.create(account_id="user-1", registered_name="A公司",
                                    entity_type="company"))
        asyncio.run(accounts.adjust_balance("user-1", 749_500_000))
        body = client.get("/api/v1/account/balance").json()
        assert body == {
            "account_id": "user-1",
            "registered_name": "A公司",
            "balance_usd": 749.5,
            "is_active": True,
            "updated_at": body["updated_at"],
            "balance_warning": None,
        }
        assert body["updated_at"]

    def test_balance_missing_account_404(self):
        client, *_ = _client()
        resp = client.get("/api/v1/account/balance")
        assert resp.status_code == 404
        assert resp.json()["code"] == "account_not_found"

    def test_balance_no_store_503(self):
        client, *_ = _client(account_store=None)
        resp = client.get("/api/v1/account/balance")
        assert resp.status_code == 503
        assert resp.json()["code"] == "billing_unavailable"


def _seed_rows(usage_recorder) -> None:
    usage_recorder._table_client.rows = [
        {"PartitionKey": "user-1", "RowKey": "a", "job_id": "j-new",
         "endpoint": "/api/v2/jobs/multi-image-music", "status": "completed",
         "model_spec": "edenn_basic", "music_provider": "edenn_basic",
         "timestamp_utc": "2026-07-20T02:00:00+00:00", "latency_ms": 120000,
         "prompt_tokens": 900, "completion_tokens": 100, "total_tokens": 1000,
         "token_cost_usd": 0.01, "generation_cost_usd": 0.065,
         "total_cost_usd": 0.075, "video_duration_s": 15.0,
         "billing_mode": "per_second", "billed_units": 15,
         "unit_price_usd": 0.5, "billed_amount_usd": 7.5,
         "billed_amount_micros": "7500000",
         "auth_mode": "enforce", "key_prefix": "sk-abc123def"},
        {"PartitionKey": "user-1", "RowKey": "b", "job_id": "j-old",
         "endpoint": "/api/v1/jobs/video", "status": "completed",
         "model_spec": "edenn_enhanced", "music_provider": "edenn_enhanced",
         "timestamp_utc": "2026-07-19T01:00:00+00:00", "latency_ms": 90000,
         "prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600,
         "token_cost_usd": 0.006, "generation_cost_usd": 0.13,
         "total_cost_usd": 0.136,
         "auth_mode": "enforce", "key_prefix": "sk-abc123def"},
    ]


class TestUsage:
    def test_usage_sanitized_projection_and_totals(self):
        client, _, usage_recorder = _client()
        _seed_rows(usage_recorder)
        body = client.get("/api/v1/account/usage").json()
        assert [r["job_id"] for r in body["rows"]] == ["j-new", "j-old"]
        new_row = body["rows"][0]
        assert set(new_row) == set(USER_USAGE_FIELDS)
        # Infra fields stay hidden; key_prefix is deliberately exposed (it is
        # the caller's own key handle, needed for per-key spend splits).
        for forbidden in ("PartitionKey", "RowKey", "auth_mode"):
            assert forbidden not in new_row
        assert new_row["key_prefix"] == "sk-abc123def"
        assert new_row["billed_amount_usd"] == 7.5
        assert new_row["video_duration_s"] == 15.0
        # Pre-billing row renders with zero/blank defaults instead of crashing.
        old_row = body["rows"][1]
        assert old_row["billing_mode"] == ""
        assert old_row["billed_amount_usd"] == 0.0
        assert old_row["video_duration_s"] is None
        totals = body["totals"]
        assert totals["jobs"] == 2
        assert totals["total_tokens"] == 1600
        assert totals["total_cost_usd"] == 0.211
        assert totals["total_billed_usd"] == 7.5
        assert totals["by_key"]["sk-abc123def"]["jobs"] == 2

    def test_usage_window_filter(self):
        client, _, usage_recorder = _client()
        _seed_rows(usage_recorder)
        body = client.get(
            "/api/v1/account/usage",
            params={"from": "2026-07-20T00:00:00+00:00"},
        ).json()
        assert [r["job_id"] for r in body["rows"]] == ["j-new"]
        assert body["totals"]["jobs"] == 1

    def test_usage_scoped_to_principal_partition(self):
        client, _, usage_recorder = _client()
        _seed_rows(usage_recorder)
        usage_recorder._table_client.rows.append(
            {"PartitionKey": "someone-else", "RowKey": "c", "job_id": "j-other",
             "timestamp_utc": "2026-07-20T03:00:00+00:00", "total_tokens": 1}
        )
        body = client.get("/api/v1/account/usage").json()
        assert {r["job_id"] for r in body["rows"]} == {"j-new", "j-old"}

    def test_usage_without_recorder_returns_empty(self):
        client, *_ = _client(usage_recorder=None)
        body = client.get("/api/v1/account/usage").json()
        # Same shape as a real response, so a client never has to branch on
        # whether the recorder happened to be configured.
        assert body == {"rows": [], "totals": {"jobs": 0, "total_tokens": 0,
                                               "total_cost_usd": 0.0,
                                               "total_billed_usd": 0.0,
                                               "by_key": {}, "by_model": {},
                                               "by_key_model": {}},
                        "page": {"limit": 200, "offset": 0, "returned": 0,
                                 "total_rows": 0, "has_more": False}}

    def test_usage_rows_show_key_prefix_and_filter_by_it(self):
        client, _, usage_recorder = _client()
        _seed_rows(usage_recorder)
        usage_recorder._table_client.rows[0]["key_prefix"] = "sk-abc123def"
        usage_recorder._table_client.rows[1]["key_prefix"] = "sk-zzz999yyy"
        body = client.get("/api/v1/account/usage").json()
        assert body["rows"][0]["key_prefix"] == "sk-abc123def"
        assert set(body["totals"]["by_key"]) == {"sk-abc123def", "sk-zzz999yyy"}
        filtered = client.get("/api/v1/account/usage",
                              params={"key_prefix": "sk-zzz999yyy"}).json()
        assert [r["job_id"] for r in filtered["rows"]] == ["j-old"]
        assert filtered["totals"]["jobs"] == 1


class TestBalanceWarning:
    def test_balance_endpoint_reports_warning(self):
        client, accounts, _ = _client()
        asyncio.run(accounts.create(account_id="user-1", registered_name="A"))
        asyncio.run(accounts.adjust_balance("user-1", 10_000_000,
                                            recharge_micros=10_000_000))
        asyncio.run(accounts.adjust_balance("user-1", -9_500_000))
        resp = client.get("/api/v1/account/balance")
        assert resp.headers.get("X-Edenn-Balance-Warning") == "low"
        body = resp.json()
        assert body["balance_warning"]["threshold_usd"] == 1.0
        assert body["balance_usd"] == 0.5

    def test_balance_endpoint_healthy_account_no_warning(self):
        client, accounts, _ = _client()
        asyncio.run(accounts.create(account_id="user-1", registered_name="A"))
        asyncio.run(accounts.adjust_balance("user-1", 10_000_000,
                                            recharge_micros=10_000_000))
        resp = client.get("/api/v1/account/balance")
        assert "X-Edenn-Balance-Warning" not in resp.headers
        assert resp.json()["balance_warning"] is None


class TestBilledAmountProjection:
    """The ledger stores micros; the 详单 promises dollars."""

    def test_billed_usd_is_derived_from_micros(self):
        # `billed_amount_usd` is never written to the row — only
        # `billed_amount_micros` is. Projecting the declared field straight from
        # the entity therefore handed every customer a bill of $0.00 per job
        # while the grand total was right, which is exactly the kind of
        # disagreement that makes people distrust the whole page.
        from EdennCode.Deployment.billing.account_router import _project_row

        row = _project_row({"job_id": "j1", "billed_amount_micros": "1500000"})
        assert row["billed_amount_usd"] == 1.5

    def test_an_explicit_usd_value_still_wins(self):
        from EdennCode.Deployment.billing.account_router import _project_row

        row = _project_row({"job_id": "j1", "billed_amount_micros": "1500000",
                            "billed_amount_usd": 2.0})
        assert row["billed_amount_usd"] == 2.0

    def test_missing_and_unreadable_micros_are_zero(self):
        from EdennCode.Deployment.billing.account_router import _project_row

        assert _project_row({"job_id": "j1"})["billed_amount_usd"] == 0.0
        assert _project_row({"job_id": "j1",
                             "billed_amount_micros": ""})["billed_amount_usd"] == 0.0
        assert _project_row({"job_id": "j1",
                             "billed_amount_micros": "oops"})["billed_amount_usd"] == 0.0

    def test_negative_micros_survive(self):
        # Corrections are recorded as reversing entries, not edits.
        from EdennCode.Deployment.billing.account_router import _project_row

        row = _project_row({"job_id": "j1", "billed_amount_micros": "-250000"})
        assert row["billed_amount_usd"] == -0.25
