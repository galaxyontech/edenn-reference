"""Admin router tests: secret guard + key lifecycle + usage reads."""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.auth.admin_router import create_admin_router
from EdennCode.Deployment.auth.key_store import ApiKeyStore
from EdennCode.Deployment.Testing.test_auth_key_store import FakeTableClient
from EdennCode.Deployment.Testing.test_auth_usage_recorder import FakeUsageTable
from EdennCode.Deployment.auth.usage_recorder import UsageRecorder

SECRET = "admin-secret-1"
HDR = {"x-admin-secret": SECRET}


def _client(admin_secret=SECRET, key_store="default", usage=None) -> TestClient:
    if key_store == "default":
        key_store = ApiKeyStore(FakeTableClient())
    recorder = usage or UsageRecorder(
        FakeUsageTable(), auth_mode="enforce", logger=logging.getLogger("t"),
        emit=lambda a: None,
    )
    app = FastAPI()
    app.include_router(create_admin_router(
        key_store=key_store, usage_recorder=recorder,
        admin_secret=admin_secret, logger=logging.getLogger("t"),
    ))
    return TestClient(app)


def test_no_secret_configured_503():
    client = _client(admin_secret=None)
    resp = client.get("/api/v1/admin/keys", headers=HDR)
    assert resp.status_code == 503
    # Flat body shape, matching the auth middleware's error responses.
    assert resp.json()["code"] == "admin_disabled"


def test_wrong_secret_401():
    client = _client()
    resp = client.get("/api/v1/admin/keys", headers={"x-admin-secret": "wrong"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_admin_secret"


def test_missing_secret_401():
    client = _client()
    resp = client.get("/api/v1/admin/keys")
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_admin_secret"


def test_mint_list_revoke_lifecycle():
    client = _client()
    minted = client.post("/api/v1/admin/keys", json={"user_id": "user-1", "note": "n"},
                         headers=HDR)
    assert minted.status_code == 200, minted.text
    body = minted.json()
    assert body["api_key"].startswith("sk-") and body["user_id"] == "user-1"
    key_prefix = body["key_prefix"]

    listed = client.get("/api/v1/admin/keys?user_id=user-1", headers=HDR).json()
    assert len(listed["keys"]) == 1
    assert listed["keys"][0]["key_prefix"] == key_prefix
    assert "api_key" not in listed["keys"][0]
    assert "key_hash" not in listed["keys"][0]

    revoked = client.delete(f"/api/v1/admin/keys/{key_prefix}", headers=HDR)
    assert revoked.status_code == 200 and revoked.json() == {"revoked": True}
    second_revoke = client.delete(f"/api/v1/admin/keys/{key_prefix}", headers=HDR)
    assert second_revoke.status_code == 404
    # Flat body shape (not a bare string, not nested under "detail").
    assert second_revoke.json()["code"] == "key_not_found"
    assert key_prefix in second_revoke.json()["detail"]


def test_mint_rejects_unsafe_user_id_charset():
    """user_id becomes a Table Storage PartitionKey and an OData filter literal;
    `/ \\ # ?` break writes and `'` breaks the query filter."""
    client = _client()
    resp = client.post(
        "/api/v1/admin/keys", json={"user_id": "bad/user#1", "note": "n"}, headers=HDR,
    )
    assert resp.status_code == 422, resp.text


def test_usage_endpoint_totals():
    import asyncio
    table = FakeUsageTable()
    recorder = UsageRecorder(table, auth_mode="enforce",
                             logger=logging.getLogger("t"), emit=lambda a: None)

    async def seed():
        from EdennCode.Deployment.auth.key_store import Principal
        p = Principal(user_id="user-1", key_prefix="sk-abc123def")
        recorder.record_job(job_id="j1", endpoint="/e", status="completed", principal=p,
                            token_usage={"prompt_tokens": 100, "completion_tokens": 50,
                                         "total_tokens": 150})
        await asyncio.sleep(0.05)

    asyncio.run(seed())
    client = _client(usage=recorder)
    resp = client.get("/api/v1/admin/usage?user_id=user-1", headers=HDR)
    assert resp.status_code == 200
    body = resp.json()
    assert body["totals"]["jobs"] == 1
    assert body["totals"]["total_tokens"] == 150


def test_usage_from_to_window_filters_before_limit():
    """An older date window must not be truncated away by the caller's limit."""
    table = FakeUsageTable()
    # RowKey asc sort = newest first (inverted-timestamp convention).
    for order, (job_id, ts) in enumerate([
        ("j-new", "2026-07-03T00:00:00Z"),
        ("j-mid", "2026-07-02T00:00:00Z"),
        ("j-old", "2026-07-01T00:00:00Z"),
    ]):
        table.rows.append({
            "PartitionKey": "user-1",
            "RowKey": f"{order:020d}-{job_id}",
            "job_id": job_id,
            "timestamp_utc": ts,
            "total_tokens": 10,
            "total_cost_usd": 0.5,
        })
    recorder = UsageRecorder(table, auth_mode="enforce",
                             logger=logging.getLogger("t"), emit=lambda a: None)
    client = _client(usage=recorder)

    # Window matches only the middle row. With limit=1, the old behavior
    # truncated to the newest row BEFORE filtering -> empty result.
    resp = client.get(
        "/api/v1/admin/usage",
        params={"user_id": "user-1", "limit": 1,
                "from": "2026-07-02T00:00:00Z", "to": "2026-07-03T00:00:00Z"},
        headers=HDR,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["totals"]["jobs"] == 1
    assert body["rows"][0]["job_id"] == "j-mid"
    assert body["totals"]["total_tokens"] == 10
