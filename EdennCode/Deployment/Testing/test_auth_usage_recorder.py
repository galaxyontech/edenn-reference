"""UsageRecorder unit tests: ledger rows, cost derivation, never-raise contract."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from EdennCode.Deployment.auth.key_store import Principal
from EdennCode.Deployment.auth import usage_recorder as ur
from EdennCode.Deployment.auth.usage_recorder import (
    UsageRecorder,
    music_unit_cost_usd,
    record_v2_job_usage,
)


class FakeUsageTable:
    def __init__(self, *, fail: bool = False):
        self.rows: list[dict[str, Any]] = []
        self.fail = fail

    def upsert_entity(self, entity: dict[str, Any]) -> None:
        if self.fail:
            raise ConnectionError("table down")
        self.rows.append(dict(entity))

    def query_entities(self, query_filter: str):
        # Honors the only filter shape the recorder emits: PartitionKey eq 'X'
        # (the real table filters server-side; a fake returning foreign
        # partitions would mask scoping bugs).
        pk = None
        if "PartitionKey eq " in (query_filter or ""):
            pk = query_filter.split("PartitionKey eq ")[1].strip().strip("'")
        return [dict(r) for r in self.rows
                if pk is None or str(r.get("PartitionKey")) == pk]


@pytest.fixture(autouse=True)
def _reset_singleton(monkeypatch):
    ur.set_usage_recorder_override(None)
    for key in ("MUSIC_UNIT_COST_PROVIDER_B", "MUSIC_UNIT_COST_PROVIDER_C",
                "MUSIC_UNIT_COST_PROVIDER_A"):
        monkeypatch.delenv(key, raising=False)
    yield
    ur.set_usage_recorder_override(None)


def _recorder(table=None, emitted=None):
    emitted = emitted if emitted is not None else []
    return (
        UsageRecorder(
            table if table is not None else FakeUsageTable(),
            auth_mode="enforce",
            logger=logging.getLogger("t"),
            emit=emitted.append,
        ),
        emitted,
    )


COST = SimpleNamespace(creation_cost=0.13, creation_times=2, token_num=1500, token_cost=0.0198)
PRINCIPAL = Principal(user_id="user-1", key_prefix="sk-abc123def")


def test_unit_cost_defaults_and_env(monkeypatch):
    assert music_unit_cost_usd("provider_b") == 0.065
    assert music_unit_cost_usd(None) == 0.065
    monkeypatch.setenv("MUSIC_UNIT_COST_PROVIDER_C", "0.08")
    assert music_unit_cost_usd("provider_c") == 0.08


def test_unit_cost_resolves_env_then_price_table_then_default(monkeypatch):
    # Every known provider has an explicit price-table entry.
    for provider in ("provider_a", "provider_b", "provider_c", "provider_d"):
        assert provider in ur.PROVIDER_UNIT_COST_USD
        assert music_unit_cost_usd(provider) == ur.PROVIDER_UNIT_COST_USD[provider]
    # Case-insensitive table lookup.
    assert music_unit_cost_usd("ProviderD") == ur.PROVIDER_UNIT_COST_USD["provider_d"]
    # Price-table correction flows through without env involvement.
    monkeypatch.setitem(ur.PROVIDER_UNIT_COST_USD, "provider_d", 0.021)
    assert music_unit_cost_usd("provider_d") == 0.021
    # Env override beats the price table.
    monkeypatch.setenv("MUSIC_UNIT_COST_PROVIDER_D", "0.03")
    assert music_unit_cost_usd("provider_d") == 0.03
    # Unknown provider falls back to the flat default.
    assert music_unit_cost_usd("someday-provider") == ur.DEFAULT_MUSIC_UNIT_COST_USD


def test_record_job_provider_override_beats_modelspec(monkeypatch):
    monkeypatch.setitem(ur.PROVIDER_UNIT_COST_USD, "provider_d", 0.02)

    async def run():
        table = FakeUsageTable()
        recorder, _ = _recorder(table)
        recorder.record_job(
            job_id="job-mm", endpoint="/api/v2/jobs/video-music", status="completed",
            principal=PRINCIPAL, model_spec="edenn_basic",  # maps to provider_a
            cost_metadata=SimpleNamespace(
                creation_cost=0.0, creation_times=3, token_num=0, token_cost=0.01),
            provider="provider_d",  # explicit identity wins
        )
        await asyncio.sleep(0.05)
        return table

    table = asyncio.run(run())
    row = table.rows[0]
    assert row["music_provider"] == "edenn_agentic"  # billed as provider_d, stored branded
    assert row["generation_unit_cost_usd"] == pytest.approx(0.02)
    assert row["generation_cost_usd"] == pytest.approx(3 * 0.02)
    assert row["total_cost_usd"] == pytest.approx(3 * 0.02 + 0.01)


def test_record_job_writes_row_and_event():
    async def run():
        table = FakeUsageTable()
        recorder, emitted = _recorder(table)
        recorder.record_job(
            job_id="job-1", endpoint="/api/v1/jobs/video", status="completed",
            principal=PRINCIPAL, model_spec="edenn_enhanced",
            token_usage={"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
            cost_metadata=COST,
        )
        await asyncio.sleep(0.05)  # let the fire-and-forget task land
        return table, emitted

    table, emitted = asyncio.run(run())
    assert len(table.rows) == 1
    row = table.rows[0]
    assert row["PartitionKey"] == "user-1"
    assert row["job_id"] == "job-1"
    assert row["prompt_tokens"] == 1000 and row["completion_tokens"] == 500
    assert row["music_provider"] == "edenn_enhanced"  # branded, never the vendor
    assert row["generation_call_count"] == 2
    assert row["generation_cost_usd"] == pytest.approx(0.13)
    assert row["token_cost_usd"] == pytest.approx(0.0198)
    assert row["total_cost_usd"] == pytest.approx(0.13 + 0.0198)
    assert row["auth_mode"] == "enforce"
    assert len(emitted) == 1 and emitted[0]["edenn.job_id"] == "job-1"


def test_record_job_anonymous_partition_and_token_num_fallback():
    async def run():
        table = FakeUsageTable()
        recorder, _ = _recorder(table)
        recorder.record_job(
            job_id="job-2", endpoint="/api/v2/jobs/video-music", status="completed",
            principal=None, model_spec="edenn_basic",
            token_usage=None, cost_metadata=COST,
        )
        await asyncio.sleep(0.05)
        return table

    table = asyncio.run(run())
    row = table.rows[0]
    assert row["PartitionKey"] == "anonymous"
    assert row["total_tokens"] == 1500 and row["prompt_tokens"] == 0


def test_record_job_never_raises_when_table_down():
    async def run():
        recorder, emitted = _recorder(FakeUsageTable(fail=True))
        recorder.record_job(
            job_id="job-3", endpoint="/api/v1/jobs/video", status="completed",
            principal=PRINCIPAL, cost_metadata=COST,
        )
        await asyncio.sleep(0.05)
        return emitted

    emitted = asyncio.run(run())  # no exception surfaced anywhere
    assert len(emitted) == 1  # event still emitted


def test_record_job_without_running_loop_writes_inline():
    table = FakeUsageTable()
    recorder, _ = _recorder(table)
    recorder.record_job(
        job_id="job-4", endpoint="/api/v1/jobs/video", status="failed",
        principal=PRINCIPAL,
    )
    assert len(table.rows) == 1
    assert table.rows[0]["status"] == "failed"
    assert table.rows[0]["total_tokens"] == 0


def test_record_v2_job_usage_reads_principal_from_request_json():
    async def run():
        table = FakeUsageTable()
        recorder, _ = _recorder(table)
        ur.set_usage_recorder_override(recorder)
        job = SimpleNamespace(request_json={
            "auth_user_id": "user-9", "auth_key_prefix": "sk-zzz111aaa"})
        record_v2_job_usage(
            job, status="completed", endpoint="/api/v2/jobs/video-music",
            result_json={"modelspec": "edenn_studio",
                         "cost_metadata": {"creation_cost": 0.065, "creation_times": 1,
                                           "token_num": 800, "token_cost": 0.01}},
        )
        await asyncio.sleep(0.05)
        return table

    table = asyncio.run(run())
    row = table.rows[0]
    assert row["PartitionKey"] == "user-9"
    assert row["music_provider"] == "edenn_studio"  # branded, never the vendor
    assert row["total_tokens"] == 800


def test_record_v2_job_usage_passes_explicit_provider(monkeypatch):
    monkeypatch.setitem(ur.PROVIDER_UNIT_COST_USD, "provider_d", 0.02)

    async def run():
        table = FakeUsageTable()
        recorder, _ = _recorder(table)
        ur.set_usage_recorder_override(recorder)
        job = SimpleNamespace(request_json={
            "auth_user_id": "user-9", "auth_key_prefix": "sk-zzz111aaa"})
        record_v2_job_usage(
            job, status="completed", endpoint="/api/v2/jobs/video-music",
            result_json={
                "modelspec": "edenn_basic",  # would map to provider_a
                "music_provider": "provider_d",  # explicit identity wins
                "cost_metadata": {"creation_cost": 0.0, "creation_times": 1,
                                  "token_num": 0, "token_cost": 0.0},
            },
        )
        await asyncio.sleep(0.05)
        return table

    table = asyncio.run(run())
    row = table.rows[0]
    assert row["music_provider"] == "edenn_agentic"
    assert row["generation_unit_cost_usd"] == pytest.approx(0.02)


def test_no_vendor_name_ever_persisted_or_emitted():
    """Rows and telemetry events must never contain upstream vendor names."""
    vendors = ("provider_a", "provider_b", "provider_c", "provider_d")

    async def run():
        table = FakeUsageTable()
        recorder, emitted = _recorder(table)
        for spec in ("edenn_basic", "edenn_enhanced", "edenn_studio"):
            recorder.record_job(
                job_id=f"job-{spec}", endpoint="/api/v2/jobs/video-music",
                status="completed", principal=PRINCIPAL, model_spec=spec,
                cost_metadata=COST,
            )
        recorder.record_job(  # explicit-provider path
            job_id="job-agentic", endpoint="/api/v2/jobs/video-music",
            status="completed", principal=PRINCIPAL, provider="provider_d",
            cost_metadata=COST,
        )
        recorder.record_job(  # unknown provider must not leak its name either
            job_id="job-unknown", endpoint="/api/v2/jobs/video-music",
            status="completed", principal=PRINCIPAL, provider="somevendor",
            cost_metadata=COST,
        )
        await asyncio.sleep(0.05)
        return table, emitted

    table, emitted = asyncio.run(run())
    assert len(table.rows) == 5
    for blob in [str(sorted(r.items())) for r in table.rows] + [
        str(sorted(e.items())) for e in emitted
    ]:
        lowered = blob.lower()
        for vendor in vendors + ("somevendor",):
            assert vendor not in lowered, f"vendor name '{vendor}' leaked: {blob[:200]}"
    by_job = {r["job_id"]: r["music_provider"] for r in table.rows}
    assert by_job["job-edenn_basic"] == "edenn_basic"
    assert by_job["job-edenn_enhanced"] == "edenn_enhanced"
    assert by_job["job-edenn_studio"] == "edenn_studio"
    assert by_job["job-agentic"] == "edenn_agentic"
    assert by_job["job-unknown"] == "edenn_other"


def test_record_v2_job_usage_no_singleton_is_noop():
    job = SimpleNamespace(request_json={})
    record_v2_job_usage(job, status="failed", endpoint="/api/v2/jobs/video-music")


def test_query_usage_newest_first():
    async def run():
        table = FakeUsageTable()
        recorder, _ = _recorder(table)
        recorder.record_job(job_id="old", endpoint="/e", status="completed",
                            principal=PRINCIPAL)
        await asyncio.sleep(0.01)
        recorder.record_job(job_id="new", endpoint="/e", status="completed",
                            principal=PRINCIPAL)
        await asyncio.sleep(0.05)
        return await recorder.query_usage(user_id="user-1")

    rows = asyncio.run(run())
    assert [r["job_id"] for r in rows] == ["new", "old"]


# -- billing integration --------------------------------------------------

from EdennCode.Deployment.billing import set_billing_override  # noqa: E402
from EdennCode.Deployment.billing.engine import BillingEngine  # noqa: E402
from EdennCode.Deployment.billing.stores import (  # noqa: E402
    AccountStore,
    PricingStore,
    WalletTxnStore,
)
from EdennCode.Deployment.Testing.test_billing_stores import (  # noqa: E402
    FakeBillingTable,
)


@pytest.fixture(autouse=True)
def _reset_billing_singleton():
    set_billing_override(None)
    yield
    set_billing_override(None)


def _billing_engine(mode: str):
    accounts = AccountStore(FakeBillingTable(), logger=logging.getLogger("t"))
    txns = WalletTxnStore(FakeBillingTable(), logger=logging.getLogger("t"))
    pricing = PricingStore(FakeBillingTable(), logger=logging.getLogger("t"))
    engine = BillingEngine(mode=mode, account_store=accounts, txn_store=txns,
                           pricing_store=pricing, logger=logging.getLogger("t"))
    return engine, accounts, txns, pricing


async def _drain_tasks():
    pending = asyncio.all_tasks() - {asyncio.current_task()}
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def test_record_job_billing_log_mode_enriches_without_debit():
    engine, accounts, txns, pricing = _billing_engine("log")
    asyncio.run(pricing.upsert(price_key="video_music",
                               billing_mode="per_request",
                               unit_price_micros=10_000_000))
    asyncio.run(accounts.create(account_id="user-1", registered_name="A"))
    set_billing_override(engine)

    async def run():
        table = FakeUsageTable()
        recorder, emitted = _recorder(table)
        recorder.record_job(
            job_id="job-b1", endpoint="/api/v1/jobs/video", status="completed",
            principal=PRINCIPAL, model_spec="edenn_basic", cost_metadata=COST,
        )
        await _drain_tasks()
        return table, emitted

    table, emitted = asyncio.run(run())
    row = table.rows[0]
    assert row["billing_mode"] == "per_request"
    assert row["billed_units"] == 1
    assert row["unit_price_usd"] == 10.0
    assert row["billed_amount_usd"] == 10.0
    assert row["billed_amount_micros"] == "10000000"
    assert emitted[0]["edenn.billed_amount_usd"] == 10.0
    # log mode: computed, but never debited
    assert asyncio.run(accounts.get("user-1")).balance_micros == 0
    assert asyncio.run(txns.list_txns("user-1"))[0] == []


def test_record_job_billing_enforce_debits_once():
    engine, accounts, txns, pricing = _billing_engine("enforce")
    asyncio.run(pricing.upsert(price_key="video_music",
                               billing_mode="per_request",
                               unit_price_micros=10_000_000))
    asyncio.run(accounts.create(account_id="user-1", registered_name="A"))
    asyncio.run(accounts.adjust_balance("user-1", 100_000_000))
    set_billing_override(engine)

    async def run():
        table = FakeUsageTable()
        recorder, _ = _recorder(table)
        recorder.record_job(
            job_id="job-b2", endpoint="/api/v1/jobs/video", status="completed",
            principal=PRINCIPAL, model_spec="edenn_basic", cost_metadata=COST,
        )
        await _drain_tasks()
        # terminal recording retried (e.g. duplicate callback) must not double-debit
        recorder.record_job(
            job_id="job-b2", endpoint="/api/v1/jobs/video", status="completed",
            principal=PRINCIPAL, model_spec="edenn_basic", cost_metadata=COST,
        )
        await _drain_tasks()
        return table

    asyncio.run(run())
    assert asyncio.run(accounts.get("user-1")).balance_micros == 90_000_000
    assert len(asyncio.run(txns.list_txns("user-1"))[0]) == 1


def test_record_job_billing_enforce_anonymous_no_debit():
    engine, accounts, txns, pricing = _billing_engine("enforce")
    asyncio.run(pricing.upsert(price_key="video_music",
                               billing_mode="per_request",
                               unit_price_micros=10_000_000))
    set_billing_override(engine)

    async def run():
        table = FakeUsageTable()
        recorder, _ = _recorder(table)
        recorder.record_job(
            job_id="job-b3", endpoint="/api/v1/jobs/video", status="completed",
            principal=None, model_spec="edenn_basic", cost_metadata=COST,
        )
        await _drain_tasks()
        return table

    table = asyncio.run(run())
    assert table.rows[0]["billing_mode"] == "per_request"  # enriched
    assert asyncio.run(txns.list_txns("anonymous"))[0] == []  # nobody debited


def test_record_job_bills_teaser_price_for_new_account():
    # The recorder must hand the paying account to the engine, otherwise a
    # teaser-eligible account silently pays the standard price.
    engine, accounts, _, pricing = _billing_engine("log")
    asyncio.run(pricing.upsert(price_key="video_music",
                               billing_mode="per_request",
                               unit_price_micros=10_000_000,
                               teaser_unit_price_micros=4_000_000))
    # Freshly created -> created_at is now -> inside the teaser window.
    asyncio.run(accounts.create(account_id="user-1", registered_name="A"))
    set_billing_override(engine)

    async def run():
        table = FakeUsageTable()
        recorder, _ = _recorder(table)
        recorder.record_job(
            job_id="job-b6", endpoint="/api/v1/jobs/video", status="completed",
            principal=PRINCIPAL, model_spec="edenn_basic", cost_metadata=COST,
        )
        await _drain_tasks()
        return table

    row = asyncio.run(run()).rows[0]
    assert row["price_track"] == "teaser"
    assert row["unit_price_usd"] == 4.0
    assert row["billed_amount_micros"] == "4000000"


def test_record_job_billing_off_row_shape_unchanged():
    engine, _, _, pricing = _billing_engine("off")
    asyncio.run(pricing.upsert(price_key="video_music",
                               billing_mode="per_request",
                               unit_price_micros=10_000_000))
    set_billing_override(engine)

    async def run():
        table = FakeUsageTable()
        recorder, emitted = _recorder(table)
        recorder.record_job(
            job_id="job-b4", endpoint="/api/v1/jobs/video", status="completed",
            principal=PRINCIPAL, cost_metadata=COST,
        )
        emitted_synchronously = len(emitted) == 1  # off keeps the P1 sync path
        await _drain_tasks()
        return table, emitted_synchronously

    table, emitted_synchronously = asyncio.run(run())
    assert emitted_synchronously
    assert "billing_mode" not in table.rows[0]
    assert "billed_amount_micros" not in table.rows[0]


def test_record_job_video_duration_lands_in_row_sync_context():
    # No running loop: the whole billing path runs inline via asyncio.run.
    engine, accounts, txns, pricing = _billing_engine("enforce")
    asyncio.run(pricing.upsert(price_key="image_music",
                               billing_mode="per_second",
                               unit_price_micros=500_000))
    asyncio.run(accounts.create(account_id="user-1", registered_name="A"))
    table = FakeUsageTable()
    recorder, _ = _recorder(table)
    set_billing_override(engine)
    recorder.record_job(
        job_id="job-b5", endpoint="/api/v1/jobs/multi-image", status="completed",
        principal=PRINCIPAL, model_spec="edenn_basic", cost_metadata=COST,
        video_duration_s=12.7,
    )
    row = table.rows[0]
    assert row["video_duration_s"] == 12.7
    assert row["billed_units"] == 13
    assert row["billed_amount_usd"] == 6.5
    assert asyncio.run(accounts.get("user-1")).balance_micros == -6_500_000


def test_record_v2_extracts_duration_from_result_geometry():
    engine, accounts, txns, pricing = _billing_engine("enforce")
    asyncio.run(pricing.upsert(price_key="image_music",
                               billing_mode="per_second",
                               unit_price_micros=500_000))
    asyncio.run(accounts.create(account_id="user-9", registered_name="B"))
    asyncio.run(accounts.adjust_balance("user-9", 100_000_000))
    set_billing_override(engine)

    async def run():
        table = FakeUsageTable()
        recorder, _ = _recorder(table)
        ur.set_usage_recorder_override(recorder)
        job = SimpleNamespace(request_json={
            "auth_user_id": "user-9", "auth_key_prefix": "sk-zzz111aaa"})
        record_v2_job_usage(
            job, status="completed", endpoint="/api/v2/jobs/multi-image-music",
            result_json={"modelspec": "edenn_basic",
                         "video_metadata": {"geometry": {"duration": 30.5}},
                         "cost_metadata": {"creation_cost": 0.065,
                                           "creation_times": 1,
                                           "token_num": 100, "token_cost": 0.001}},
        )
        await _drain_tasks()
        return table

    table = asyncio.run(run())
    row = table.rows[0]
    assert row["video_duration_s"] == 30.5
    assert row["billed_units"] == 31
    assert row["billed_amount_micros"] == str(31 * 500_000)
    assert asyncio.run(accounts.get("user-9")).balance_micros == (
        100_000_000 - 31 * 500_000
    )


def test_record_v2_malformed_geometry_is_safe():
    async def run():
        table = FakeUsageTable()
        recorder, _ = _recorder(table)
        ur.set_usage_recorder_override(recorder)
        job = SimpleNamespace(request_json={"auth_user_id": "user-9"})
        for result_json in (
            {"video_metadata": {"geometry": None}},
            {"video_metadata": None},
            {"video_metadata": {"geometry": {"duration": "not-a-number"}}},
            {},
        ):
            record_v2_job_usage(
                job, status="completed",
                endpoint="/api/v2/jobs/multi-image-music",
                result_json=result_json,
            )
        await asyncio.sleep(0.05)
        return table

    table = asyncio.run(run())
    assert len(table.rows) == 4  # every malformed shape still recorded


def test_query_usage_window_filters_and_billed_totals():
    async def run():
        table = FakeUsageTable()
        recorder, _ = _recorder(table)
        table.rows = [
            {"PartitionKey": "user-1", "RowKey": "a", "job_id": "j1",
             "timestamp_utc": "2026-07-19T00:00:00+00:00", "total_tokens": 100,
             "total_cost_usd": 0.1, "billed_amount_micros": "10000000"},
            {"PartitionKey": "user-1", "RowKey": "b", "job_id": "j2",
             "timestamp_utc": "2026-07-20T00:00:00+00:00", "total_tokens": 200,
             "total_cost_usd": 0.2, "billed_amount_micros": "5500000"},
            {"PartitionKey": "user-1", "RowKey": "c", "job_id": "j3",
             "timestamp_utc": "2026-07-21T00:00:00+00:00", "total_tokens": 400,
             "total_cost_usd": 0.4},  # pre-billing row: no billed field
        ]
        return await ur.query_usage_window(
            recorder, user_id="user-1",
            from_ts="2026-07-20T00:00:00+00:00", to_ts=None, limit=10)

    rows, totals, _ = asyncio.run(run())
    assert {r["job_id"] for r in rows} == {"j2", "j3"}
    assert totals["jobs"] == 2
    assert totals["total_tokens"] == 600
    assert totals["total_cost_usd"] == 0.6
    assert totals["total_billed_usd"] == 5.5


def _multi_key_rows():
    return [
        {"PartitionKey": "user-1", "RowKey": "a", "job_id": "j1",
         "key_prefix": "sk-AAAAAAAAA",
         "timestamp_utc": "2026-07-20T00:00:00+00:00", "total_tokens": 100,
         "total_cost_usd": 0.1, "billed_amount_micros": "10000000"},
        {"PartitionKey": "user-1", "RowKey": "b", "job_id": "j2",
         "key_prefix": "sk-BBBBBBBBB",
         "timestamp_utc": "2026-07-21T00:00:00+00:00", "total_tokens": 200,
         "total_cost_usd": 0.2, "billed_amount_micros": "4500000"},
        {"PartitionKey": "user-1", "RowKey": "c", "job_id": "j3",
         "key_prefix": "sk-AAAAAAAAA",
         "timestamp_utc": "2026-07-22T00:00:00+00:00", "total_tokens": 400,
         "total_cost_usd": 0.4, "billed_amount_micros": "10000000"},
        {"PartitionKey": "user-1", "RowKey": "d", "job_id": "j4",
         "key_prefix": "",  # off-mode / legacy row without attribution
         "timestamp_utc": "2026-07-22T01:00:00+00:00", "total_tokens": 50,
         "total_cost_usd": 0.05, "billed_amount_micros": "500000"},
    ]


def test_query_usage_window_by_key_breakdown():
    async def run():
        table = FakeUsageTable()
        recorder, _ = _recorder(table)
        table.rows = _multi_key_rows()
        return await ur.query_usage_window(recorder, user_id="user-1", limit=10)

    _, totals, _ = asyncio.run(run())
    by_key = totals["by_key"]
    assert by_key["sk-AAAAAAAAA"] == {
        "jobs": 2, "total_tokens": 500,
        "total_cost_usd": 0.5, "total_billed_usd": 20.0}
    assert by_key["sk-BBBBBBBBB"]["total_billed_usd"] == 4.5
    assert by_key["unattributed"]["total_billed_usd"] == 0.5


def _multi_model_rows():
    """Two keys across two models, plus one row whose path set no model_spec."""
    def _row(rk, key, spec, micros, tokens):
        return {"PartitionKey": "user-1", "RowKey": rk, "job_id": rk,
                "key_prefix": key, "model_spec": spec,
                "timestamp_utc": f"2026-07-2{rk}T00:00:00+00:00",
                "total_tokens": tokens, "total_cost_usd": 0.1,
                "billed_amount_micros": str(micros)}

    return [
        _row("1", "sk-AAAAAAAAA", "edenn_basic", 1_000_000, 100),
        _row("2", "sk-AAAAAAAAA", "edenn_basic", 2_000_000, 200),
        _row("3", "sk-AAAAAAAAA", "edenn_enhanced", 4_000_000, 300),
        _row("4", "sk-BBBBBBBBB", "edenn_basic", 8_000_000, 400),
        # provider_d-style path: provider chosen directly, no modelspec recorded
        _row("5", "sk-AAAAAAAAA", "", 500_000, 50),
    ]


def test_query_usage_window_by_model_breakdown():
    async def run():
        table = FakeUsageTable()
        recorder, _ = _recorder(table)
        table.rows = _multi_model_rows()
        return await ur.query_usage_window(recorder, user_id="user-1", limit=10)

    _, totals, _ = asyncio.run(run())
    by_model = totals["by_model"]
    assert by_model["edenn_basic"] == {
        "jobs": 3, "total_tokens": 700,
        "total_cost_usd": 0.3, "total_billed_usd": 11.0}
    assert by_model["edenn_enhanced"]["total_billed_usd"] == 4.0
    # A row with no model_spec gets its own bucket rather than an empty label.
    assert by_model["unspecified"]["total_billed_usd"] == 0.5
    # Every breakdown must reconcile to the grand total.
    assert sum(b["total_billed_usd"] for b in by_model.values()) == \
        totals["total_billed_usd"] == 15.5


def test_query_usage_window_by_key_model_cross_cut():
    """The breakdown a customer reconciles against: this key, on that model."""
    async def run():
        table = FakeUsageTable()
        recorder, _ = _recorder(table)
        table.rows = _multi_model_rows()
        return await ur.query_usage_window(recorder, user_id="user-1", limit=10)

    _, totals, _ = asyncio.run(run())
    cross = totals["by_key_model"]
    assert cross["sk-AAAAAAAAA|edenn_basic"] == {
        "jobs": 2, "total_tokens": 300,
        "total_cost_usd": 0.2, "total_billed_usd": 3.0}
    assert cross["sk-AAAAAAAAA|edenn_enhanced"]["total_billed_usd"] == 4.0
    assert cross["sk-BBBBBBBBB|edenn_basic"]["total_billed_usd"] == 8.0
    assert cross["sk-AAAAAAAAA|unspecified"]["total_billed_usd"] == 0.5
    # The same spend, sliced three ways, must add up to the same number.
    for store in ("by_key", "by_model", "by_key_model"):
        assert sum(b["total_billed_usd"] for b in totals[store].values()) == 15.5
    assert sum(b["jobs"] for b in cross.values()) == totals["jobs"] == 5


def test_query_usage_window_breakdowns_respect_the_key_filter():
    async def run():
        table = FakeUsageTable()
        recorder, _ = _recorder(table)
        table.rows = _multi_model_rows()
        return await ur.query_usage_window(
            recorder, user_id="user-1", key_prefix="sk-BBBBBBBBB", limit=10)

    _, totals, _ = asyncio.run(run())
    assert set(totals["by_model"]) == {"edenn_basic"}
    assert set(totals["by_key_model"]) == {"sk-BBBBBBBBB|edenn_basic"}
    assert totals["total_billed_usd"] == 8.0


def test_query_usage_window_key_prefix_filter():
    async def run():
        table = FakeUsageTable()
        recorder, _ = _recorder(table)
        table.rows = _multi_key_rows()
        return await ur.query_usage_window(
            recorder, user_id="user-1", key_prefix="sk-AAAAAAAAA", limit=10)

    rows, totals, _ = asyncio.run(run())
    assert {r["job_id"] for r in rows} == {"j1", "j3"}
    assert totals["jobs"] == 2
    assert totals["total_billed_usd"] == 20.0
    assert set(totals["by_key"]) == {"sk-AAAAAAAAA"}


def test_query_usage_window_pagination_with_stable_totals():
    async def run(offset):
        table = FakeUsageTable()
        recorder, _ = _recorder(table)
        table.rows = _multi_key_rows()  # 4 rows total
        return await ur.query_usage_window(
            recorder, user_id="user-1", limit=2, offset=offset)

    page1_rows, page1_totals, page1 = asyncio.run(run(0))
    page2_rows, page2_totals, page2 = asyncio.run(run(2))
    # Pages slice the same newest-first ordering without overlap.
    assert len(page1_rows) == 2 and len(page2_rows) == 2
    assert {r["job_id"] for r in page1_rows} | {r["job_id"] for r in page2_rows} \
        == {"j1", "j2", "j3", "j4"}
    assert not ({r["job_id"] for r in page1_rows} & {r["job_id"] for r in page2_rows})
    # Totals cover the WHOLE filtered set on every page (stable across pages).
    assert page1_totals == page2_totals
    assert page1_totals["jobs"] == 4
    # Page envelopes.
    assert page1 == {"limit": 2, "offset": 0, "returned": 2,
                     "total_rows": 4, "has_more": True}
    assert page2["has_more"] is False and page2["offset"] == 2
    # Past-the-end offset: empty page, same totals.
    empty_rows, empty_totals, empty_page = asyncio.run(run(10))
    assert empty_rows == [] and empty_totals["jobs"] == 4
    assert empty_page["returned"] == 0 and empty_page["has_more"] is False


def test_query_usage_window_single_day_via_date_only_bounds():
    async def run():
        table = FakeUsageTable()
        recorder, _ = _recorder(table)
        table.rows = _multi_key_rows()
        # Date-only strings give an exact single day (from inclusive, to exclusive).
        return await ur.query_usage_window(
            recorder, user_id="user-1", from_ts="2026-07-22", to_ts="2026-07-23")

    rows, totals, _ = asyncio.run(run())
    assert {r["job_id"] for r in rows} == {"j3", "j4"}
    assert totals["jobs"] == 2
