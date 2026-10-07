"""BillingEngine tests: mode resolve, product map, computation, enrichment, debit."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from EdennCode.Deployment.billing import (
    get_billing,
    resolve_billing,
    set_billing_override,
)
from EdennCode.Deployment.billing.engine import (
    BILLABLE_PATHS,
    ENDPOINT_PRODUCT,
    BillingComputation,
    BillingEngine,
    billed_units_for_duration,
    resolve_billing_mode,
)
from EdennCode.Deployment.billing.stores import (
    AccountStore,
    PricingStore,
    WalletTxnStore,
)
from EdennCode.Deployment.Testing.test_billing_stores import FakeBillingTable


@pytest.fixture(autouse=True)
def _reset_singleton():
    set_billing_override(None)
    yield
    set_billing_override(None)


def _engine(mode: str = "enforce"):
    accounts = AccountStore(FakeBillingTable(), logger=logging.getLogger("t"))
    txns = WalletTxnStore(FakeBillingTable(), logger=logging.getLogger("t"))
    pricing = PricingStore(FakeBillingTable(), logger=logging.getLogger("t"))
    engine = BillingEngine(
        mode=mode, account_store=accounts, txn_store=txns,
        pricing_store=pricing, logger=logging.getLogger("t"),
    )
    return engine, accounts, txns, pricing


def _seed_video_pricing(pricing: PricingStore, *, micros=10_000_000):
    asyncio.run(pricing.upsert(price_key="video_music", billing_mode="per_request",
                               unit_price_micros=micros))


def _seed_image_pricing(pricing: PricingStore, *, micros=500_000):
    asyncio.run(pricing.upsert(price_key="image_music", billing_mode="per_second",
                               unit_price_micros=micros))


# -- mode + product map ---------------------------------------------------

def test_resolve_billing_mode_defaults_junk_to_off():
    assert resolve_billing_mode("") == "off"
    assert resolve_billing_mode("banana") == "off"
    assert resolve_billing_mode(" ENFORCE ") == "enforce"
    assert resolve_billing_mode("log") == "log"


def test_endpoint_product_map_covers_exactly_the_billable_surface():
    assert ENDPOINT_PRODUCT == {
        "/api/v1/jobs/video": "video_music",
        "/api/v1/jobs/async_video_music_gen": "video_music",
        "/api/v2/jobs/video-music": "video_music",
        "/api/v1/jobs/multi-image": "image_music",
        "/api/v1/jobs/async_multi-image": "image_music",
        "/api/v2/jobs/multi-image-music": "image_music",
        "/api/v1/jobs/audio-creative-edit": "audio_edit",
    }
    assert BILLABLE_PATHS == frozenset(ENDPOINT_PRODUCT)


# -- compute --------------------------------------------------------------

def test_compute_off_mode_returns_none():
    engine, _, _, pricing = _engine(mode="off")
    _seed_video_pricing(pricing)
    assert engine.computes is False
    comp = asyncio.run(engine.compute(
        endpoint="/api/v1/jobs/video", status="completed",
        model_spec="edenn_basic", video_duration_s=10.0))
    assert comp is None


def test_compute_per_request():
    engine, _, _, pricing = _engine(mode="log")
    _seed_video_pricing(pricing)
    comp = asyncio.run(engine.compute(
        endpoint="/api/v2/jobs/video-music", status="completed",
        model_spec="edenn_basic", video_duration_s=42.0))
    assert comp == BillingComputation(
        billing_mode="per_request", billed_units=1,
        unit_price_micros=10_000_000, billed_amount_micros=10_000_000)


def test_compute_per_second_ceils_duration():
    engine, _, _, pricing = _engine()
    _seed_image_pricing(pricing)
    comp = asyncio.run(engine.compute(
        endpoint="/api/v2/jobs/multi-image-music", status="completed",
        model_spec="edenn_basic", video_duration_s=15.34))
    assert comp.billed_units == 16
    assert comp.billed_amount_micros == 16 * 500_000


def test_compute_per_second_missing_duration_bills_zero(caplog):
    engine, _, _, pricing = _engine()
    _seed_image_pricing(pricing)
    with caplog.at_level(logging.WARNING):
        comp = asyncio.run(engine.compute(
            endpoint="/api/v1/jobs/multi-image", status="completed",
            model_spec="edenn_basic", video_duration_s=None))
    assert comp.billed_units == 0
    assert comp.billed_amount_micros == 0
    assert any("duration" in r.message for r in caplog.records)


def test_compute_skips_failed_and_unknown_endpoint_and_unpriced(caplog):
    engine, _, _, pricing = _engine()
    _seed_video_pricing(pricing)
    assert asyncio.run(engine.compute(
        endpoint="/api/v1/jobs/video", status="failed",
        model_spec=None, video_duration_s=None)) is None
    assert asyncio.run(engine.compute(
        endpoint="/api/v1/jobs/vocal-clone", status="completed",
        model_spec=None, video_duration_s=None)) is None
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(engine.compute(
            endpoint="/api/v1/jobs/multi-image", status="completed",
            model_spec="edenn_basic", video_duration_s=9.0)) is None
    assert any("unpriced" in r.message for r in caplog.records)


def test_compute_uses_modelspec_specific_price():
    engine, _, _, pricing = _engine()
    _seed_image_pricing(pricing, micros=500_000)
    asyncio.run(pricing.upsert(price_key="image_music:edenn_studio",
                               billing_mode="per_second",
                               unit_price_micros=900_000))
    comp = asyncio.run(engine.compute(
        endpoint="/api/v2/jobs/multi-image-music", status="completed",
        model_spec="edenn_studio", video_duration_s=10.0))
    assert comp.unit_price_micros == 900_000


# -- duration metering ----------------------------------------------------
#
# The two published rules:
#   视频配乐  费用 = ⌈时长 ÷ 30⌉ × 单价
#   多图配乐  费用 = max(时长, 15) × 单价/秒

@pytest.mark.parametrize("duration,expected_units", [
    (0.4, 1),     # any delivered video is at least one block
    (30.0, 1),    # the boundary belongs to the block below it
    (30.4, 2),
    (31.0, 2),
    (59.0, 2),
    (60.0, 2),
    (61.0, 3),
    (90.0, 3),
    (90.001, 4),
    (118.0, 4),
])
def test_video_music_bills_whole_thirty_second_blocks(duration, expected_units):
    assert billed_units_for_duration(duration, unit_seconds=30) == expected_units


@pytest.mark.parametrize("duration,expected_units", [
    (0.5, 15),    # a two-frame slideshow still bills the 15-second minimum
    (9.0, 15),
    (14.999, 15),
    (15.0, 15),   # the floor is inclusive: 15s bills 15, not 16
    (15.34, 16),  # past the floor it is actual seconds, rounded up as before
    (20.0, 20),
])
def test_multi_image_floors_at_fifteen_seconds(duration, expected_units):
    assert billed_units_for_duration(
        duration, min_billable_seconds=15) == expected_units


def test_block_boundaries_do_not_depend_on_float_representation():
    """Durations arrive as probed floats; a boundary that lands one ULP high
    would double the charge, which is the expensive direction to be wrong in."""
    for exact_multiple, blocks in ((30.0, 1), (60.0, 2), (90.0, 3), (120.0, 4)):
        assert billed_units_for_duration(exact_multiple, unit_seconds=30) == blocks
    assert billed_units_for_duration(0.1 + 0.2, unit_seconds=1) == 1


def test_compute_bills_video_music_in_thirty_second_blocks():
    engine, _, _, pricing = _engine()
    asyncio.run(pricing.upsert(price_key="video_music", billing_mode="per_second",
                               unit_price_micros=128_571, unit_seconds=30))
    comp = asyncio.run(engine.compute(
        endpoint="/api/v2/jobs/video-music", status="completed",
        model_spec="edenn_enhanced", video_duration_s=59.0))
    assert (comp.billed_units, comp.unit_seconds) == (2, 30)
    assert comp.billed_amount_micros == 2 * 128_571


def test_compute_floors_multi_image_at_the_minimum():
    engine, _, _, pricing = _engine()
    asyncio.run(pricing.upsert(price_key="image_music", billing_mode="per_second",
                               unit_price_micros=100_000,
                               min_billable_seconds=15))
    comp = asyncio.run(engine.compute(
        endpoint="/api/v2/jobs/multi-image-music", status="completed",
        model_spec="edenn_basic", video_duration_s=9.0))
    assert (comp.billed_units, comp.min_billable_seconds) == (15, 15)
    assert comp.billed_amount_micros == 15 * 100_000


def test_missing_duration_bills_zero_rather_than_the_floor(caplog):
    """A floor makes a known-short video fair; applied to an unknown duration
    it would invent a charge out of a missing measurement."""
    engine, _, _, pricing = _engine()
    asyncio.run(pricing.upsert(price_key="image_music", billing_mode="per_second",
                               unit_price_micros=100_000,
                               min_billable_seconds=15))
    with caplog.at_level(logging.WARNING):
        comp = asyncio.run(engine.compute(
            endpoint="/api/v1/jobs/multi-image", status="completed",
            model_spec="edenn_basic", video_duration_s=None))
    assert (comp.billed_units, comp.billed_amount_micros) == (0, 0)


def test_metering_comes_from_the_list_row_not_the_discounted_price():
    """A teaser price changes what a unit costs, never what a unit is."""
    engine, accounts, _, pricing = _engine()
    asyncio.run(accounts.create(account_id="a1", registered_name="A"))
    asyncio.run(pricing.upsert(price_key="video_music", billing_mode="per_second",
                               unit_price_micros=128_571,
                               teaser_unit_price_micros=64_286, unit_seconds=30))
    comp = asyncio.run(engine.compute(
        endpoint="/api/v1/jobs/video", status="completed", model_spec=None,
        video_duration_s=61.0, account_id="a1"))
    assert (comp.price_track, comp.unit_seconds) == ("teaser", 30)
    assert comp.billed_units == 3
    assert comp.billed_amount_micros == 3 * 64_286


# -- enrich_row -----------------------------------------------------------

def test_enrich_row_adds_billing_fields_and_noop_on_none():
    row = {"job_id": "j1"}
    BillingEngine.enrich_row(row, None)
    assert row == {"job_id": "j1"}
    comp = BillingComputation(
        billing_mode="per_second", billed_units=16,
        unit_price_micros=500_000, billed_amount_micros=8_000_000)
    BillingEngine.enrich_row(row, comp)
    assert row["billing_mode"] == "per_second"
    assert row["billed_units"] == 16
    assert row["unit_price_usd"] == 0.5
    assert row["billed_amount_usd"] == 8.0
    assert row["billed_amount_micros"] == "8000000"
    assert (row["unit_seconds"], row["min_billable_seconds"]) == (1, 0)


def test_enrich_row_omits_metering_on_per_request_charges():
    row: dict = {}
    BillingEngine.enrich_row(row, BillingComputation(
        billing_mode="per_request", billed_units=1,
        unit_price_micros=10_000_000, billed_amount_micros=10_000_000))
    assert "unit_seconds" not in row
    assert "min_billable_seconds" not in row


# -- debit_for_job --------------------------------------------------------

COMP = BillingComputation(
    billing_mode="per_request", billed_units=1,
    unit_price_micros=10_000_000, billed_amount_micros=10_000_000)


def test_debit_happy_path_writes_txn_and_updates_balance():
    engine, accounts, txns, _ = _engine()
    asyncio.run(accounts.create(account_id="a1", registered_name="A"))
    asyncio.run(accounts.adjust_balance("a1", 25_000_000))
    asyncio.run(engine.debit_for_job(account_id="a1", job_id="j1", comp=COMP))
    assert asyncio.run(accounts.get("a1")).balance_micros == 15_000_000
    rows, _ = asyncio.run(txns.list_txns("a1"))
    assert len(rows) == 1
    assert int(rows[0]["amount_micros"]) == -10_000_000
    assert int(rows[0]["balance_after_micros"]) == 15_000_000


def test_debit_is_idempotent_per_job():
    engine, accounts, txns, _ = _engine()
    asyncio.run(accounts.create(account_id="a1", registered_name="A"))
    asyncio.run(accounts.adjust_balance("a1", 25_000_000))
    asyncio.run(engine.debit_for_job(account_id="a1", job_id="j1", comp=COMP))
    asyncio.run(engine.debit_for_job(account_id="a1", job_id="j1", comp=COMP))
    assert asyncio.run(accounts.get("a1")).balance_micros == 15_000_000
    assert len(asyncio.run(txns.list_txns("a1"))[0]) == 1


def test_debit_missing_account_warns_and_writes_nothing(caplog):
    engine, _, txns, _ = _engine()
    with caplog.at_level(logging.WARNING):
        asyncio.run(engine.debit_for_job(account_id="ghost", job_id="j1", comp=COMP))
    assert asyncio.run(txns.list_txns("ghost"))[0] == []
    assert any("no account" in r.message for r in caplog.records)


def test_debit_storage_failure_never_raises(caplog):
    engine, accounts, txns, _ = _engine()
    asyncio.run(accounts.create(account_id="a1", registered_name="A"))
    txns._table_client.fail_reads = True
    with caplog.at_level(logging.WARNING):
        asyncio.run(engine.debit_for_job(account_id="a1", job_id="j1", comp=COMP))
    assert any("debit failed" in r.message for r in caplog.records)


def test_debit_zero_amount_skips_wallet_write():
    engine, accounts, txns, _ = _engine()
    asyncio.run(accounts.create(account_id="a1", registered_name="A"))
    zero = BillingComputation(billing_mode="per_second", billed_units=0,
                              unit_price_micros=500_000, billed_amount_micros=0)
    asyncio.run(engine.debit_for_job(account_id="a1", job_id="j1", comp=zero))
    assert asyncio.run(accounts.get("a1")).balance_micros == 0
    assert asyncio.run(txns.list_txns("a1"))[0] == []


# -- singleton + settings -------------------------------------------------

def test_singleton_override_and_get():
    engine, _, _, _ = _engine()
    set_billing_override(engine)
    assert get_billing() is engine
    set_billing_override(None)
    assert get_billing() is None


def test_resolve_billing_without_storage_builds_engine_with_none_stores():
    settings = SimpleNamespace(
        billing_mode="log", auth_table_namespace="",
        storage_connection_string=None, storage_account_url=None,
        storage_account_name=None, storage_account_key=None)
    engine = resolve_billing(settings, logging.getLogger("t"))
    assert engine is not None
    assert engine.mode == "log"
    assert engine.account_store is None
    assert engine.pricing_store is None
    # compute degrades to None without a pricing store
    assert asyncio.run(engine.compute(
        endpoint="/api/v1/jobs/video", status="completed",
        model_spec=None, video_duration_s=None)) is None
    # resolve is cached process-wide
    assert get_billing() is engine


def test_settings_from_env_picks_up_billing_mode(monkeypatch):
    from EdennCode.Deployment.settings import DeploymentSettings

    for key, value in {"AZURE_ENDPOINT": "https://x.example.com",
                       "AZURE_MODEL": "gpt", "AZURE_API_KEY": "k",
                       "BILLING_MODE": "enforce"}.items():
        monkeypatch.setenv(key, value)
    assert DeploymentSettings.from_env().billing_mode == "enforce"


# -- teaser price resolution + price_track --------------------------------

def _seed_teaser_video_pricing(pricing, *, standard=128_572, teaser=64_286):
    asyncio.run(pricing.upsert(price_key="video_music", billing_mode="per_request",
                               unit_price_micros=standard,
                               teaser_unit_price_micros=teaser))


def _create_account(accounts, account_id, *, age_days: int):
    asyncio.run(accounts.create(account_id=account_id, registered_name="N"))
    created = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    entity = asyncio.run(accounts._get_entity(account_id))
    entity["created_at"] = created
    accounts._table_client.upsert_entity(
        {k: v for k, v in entity.items() if k != "metadata"})
    accounts.invalidate_balance_cache(account_id)


def test_compute_uses_teaser_price_inside_window():
    engine, accounts, _, pricing = _engine()
    _seed_teaser_video_pricing(pricing)
    _create_account(accounts, "young", age_days=10)
    comp = asyncio.run(engine.compute(
        endpoint="/api/v1/jobs/video", status="completed",
        model_spec=None, video_duration_s=None, account_id="young"))
    assert comp.unit_price_micros == 64_286
    assert comp.price_track == "teaser"


def test_compute_uses_standard_price_after_window():
    engine, accounts, _, pricing = _engine()
    _seed_teaser_video_pricing(pricing)
    _create_account(accounts, "old", age_days=91)
    comp = asyncio.run(engine.compute(
        endpoint="/api/v1/jobs/video", status="completed",
        model_spec=None, video_duration_s=None, account_id="old"))
    assert comp.unit_price_micros == 128_572
    assert comp.price_track == "standard"


def test_compute_standard_when_no_teaser_column_or_no_account():
    engine, accounts, _, pricing = _engine()
    _seed_video_pricing(pricing)  # no teaser column
    _create_account(accounts, "young", age_days=1)
    comp = asyncio.run(engine.compute(
        endpoint="/api/v1/jobs/video", status="completed",
        model_spec=None, video_duration_s=None, account_id="young"))
    assert comp.price_track == "standard"
    # unknown account -> standard, never raises
    _seed_teaser_video_pricing(pricing)
    pricing._cache = None
    comp = asyncio.run(engine.compute(
        endpoint="/api/v1/jobs/video", status="completed",
        model_spec=None, video_duration_s=None, account_id="ghost"))
    assert comp.price_track == "standard"


def test_teaser_price_multiplies_per_second_units():
    # multi-image is billed per second: the teaser rate must drive the whole
    # amount, not just the unit price.
    engine, accounts, _, pricing = _engine()
    asyncio.run(pricing.upsert(price_key="image_music", billing_mode="per_second",
                               unit_price_micros=500_000,
                               teaser_unit_price_micros=200_000))
    _create_account(accounts, "young", age_days=1)
    comp = asyncio.run(engine.compute(
        endpoint="/api/v1/jobs/multi-image", status="completed",
        model_spec=None, video_duration_s=12.7, account_id="young"))
    assert comp.billed_units == 13
    assert comp.unit_price_micros == 200_000
    assert comp.billed_amount_micros == 13 * 200_000
    assert comp.price_track == "teaser"


def test_enrich_row_writes_price_track():
    row: dict = {}
    BillingEngine.enrich_row(row, BillingComputation(
        billing_mode="per_request", billed_units=1,
        unit_price_micros=64_286, billed_amount_micros=64_286,
        price_track="teaser"))
    assert row["price_track"] == "teaser"
