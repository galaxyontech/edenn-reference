"""Billing store tests: micros math, AccountStore CAS wallet, txns, pricing.

Fakes mimic the azure-data-tables sync TableClient surface the stores touch:
create/get/update/upsert/query + etag semantics. Money invariant: every
``*_micros`` value is persisted as a string (Table Storage Int32 ceiling),
parsed to int at the read boundary.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import pytest

from EdennCode.Deployment.billing.stores import (
    MICROS_PER_USD,
    AccountExists,
    AccountStore,
    BillingStoreUnavailable,
    PricingStore,
    WalletTxnStore,
    rmb_to_micros,
    usd_to_micros,
    micros_to_usd,
)


class FakeResourceNotFound(Exception):
    pass


class FakeResourceExists(Exception):
    pass


class FakePreconditionFailed(Exception):
    status_code = 412


class FakeBillingTable:
    """Etag-aware fake of the sync TableClient subset the billing stores use."""

    def __init__(self) -> None:
        self.entities: dict[tuple[str, str], dict[str, Any]] = {}
        self.etags: dict[tuple[str, str], int] = {}
        self.conflict_next = 0  # force N artificial 412s on update_entity
        self.fail_reads = False

    def _key(self, entity_or_pk, rk: Optional[str] = None) -> tuple[str, str]:
        if rk is not None:
            return (str(entity_or_pk), str(rk))
        return (str(entity_or_pk["PartitionKey"]), str(entity_or_pk["RowKey"]))

    def create_entity(self, entity: dict[str, Any]) -> None:
        key = self._key(entity)
        if key in self.entities:
            raise FakeResourceExists(f"exists: {key}")
        self.entities[key] = dict(entity)
        self.etags[key] = 1

    def get_entity(self, partition_key: str, row_key: str):
        if self.fail_reads:
            raise ConnectionError("table down")
        key = (partition_key, row_key)
        if key not in self.entities:
            raise FakeResourceNotFound(f"missing: {key}")
        entity = _EntityDict(self.entities[key])
        entity.metadata = {"etag": str(self.etags[key])}
        return entity

    def update_entity(self, entity: dict[str, Any], *, mode=None, etag=None,
                      match_condition=None) -> None:
        key = self._key(entity)
        if key not in self.entities:
            raise FakeResourceNotFound(f"missing: {key}")
        if self.conflict_next > 0:
            self.conflict_next -= 1
            raise FakePreconditionFailed("forced conflict")
        if etag is not None and str(etag) != str(self.etags[key]):
            raise FakePreconditionFailed(f"stale etag {etag}")
        self.entities[key] = {k: v for k, v in entity.items() if k != "metadata"}
        self.etags[key] += 1

    def upsert_entity(self, entity: dict[str, Any]) -> None:
        key = self._key(entity)
        self.entities[key] = {k: v for k, v in entity.items() if k != "metadata"}
        self.etags[key] = self.etags.get(key, 0) + 1

    def delete_entity(self, partition_key: str, row_key: str) -> None:
        key = (partition_key, row_key)
        if key not in self.entities:
            raise FakeResourceNotFound(f"missing: {key}")
        del self.entities[key]
        self.etags.pop(key, None)

    def query_entities(self, query_filter: str):
        # Honors the only filter shape the stores emit: PartitionKey eq 'X'.
        if self.fail_reads:
            raise ConnectionError("table down")
        pk = None
        if "PartitionKey eq " in (query_filter or ""):
            pk = query_filter.split("PartitionKey eq ")[1].strip().strip("'")
        for (epk, _), entity in list(self.entities.items()):
            if pk is None or epk == pk:
                yield dict(entity)


class _EntityDict(dict):
    metadata: dict[str, Any]


def _store(table: Optional[FakeBillingTable] = None, **kwargs) -> AccountStore:
    return AccountStore(table if table is not None else FakeBillingTable(),
                        logger=logging.getLogger("t"), **kwargs)


# -- micros math ----------------------------------------------------------

def test_usd_micros_round_trip():
    assert usd_to_micros(1) == MICROS_PER_USD
    assert usd_to_micros(0.5) == 500_000
    assert usd_to_micros("2.25") == 2_250_000
    assert usd_to_micros(-3) == -3_000_000
    assert micros_to_usd(2_250_000) == 2.25
    assert micros_to_usd(-500_000) == -0.5


def test_micros_to_usd_rounds_display_to_four_decimals():
    # Display conversion only: micros stay the ground truth.
    assert micros_to_usd(64_286) == 0.0643        # ¥0.45 @ 7.0, 6dp -> 4dp
    assert micros_to_usd(10_000_000) == 10.0
    # ROUND_HALF_UP at the 4th-decimal boundary.
    assert micros_to_usd(50) == 0.0001
    assert micros_to_usd(49) == 0.0


def test_usd_to_micros_rejects_sub_micro_and_huge_and_nan():
    with pytest.raises(ValueError):
        usd_to_micros(0.0000001)  # finer than one micro-USD
    with pytest.raises(ValueError):
        usd_to_micros(1e13)  # beyond int64-safe USD ceiling
    with pytest.raises(ValueError):
        usd_to_micros(float("nan"))
    with pytest.raises(ValueError):
        usd_to_micros(float("inf"))
    with pytest.raises(ValueError):
        usd_to_micros("not-a-number")


# -- AccountStore: profile CRUD ------------------------------------------

def test_account_create_get_round_trip():
    store = _store()
    rec = asyncio.run(store.create(
        account_id="acct-enterprise", registered_name="示例传媒科技有限公司",
        entity_type="company", id_number="91000000MA0EXAMPLE",
        address="北京市", email="ops@example.invalid", phone="+86-10-0000-0000",
        note="pilot"))
    assert rec.account_id == "acct-enterprise"
    assert rec.balance_micros == 0
    assert rec.is_active is True
    got = asyncio.run(store.get("acct-enterprise"))
    assert got is not None
    assert got.registered_name == "示例传媒科技有限公司"
    assert got.entity_type == "company"
    assert got.id_number == "91000000MA0EXAMPLE"
    assert got.created_at and got.updated_at


def test_account_create_duplicate_raises_account_exists():
    store = _store()
    asyncio.run(store.create(account_id="a1", registered_name="A"))
    with pytest.raises(AccountExists):
        asyncio.run(store.create(account_id="a1", registered_name="A again"))


def test_account_get_missing_returns_none():
    assert asyncio.run(_store().get("ghost")) is None


def test_account_list_returns_all():
    store = _store()
    asyncio.run(store.create(account_id="a1", registered_name="A"))
    asyncio.run(store.create(account_id="a2", registered_name="B"))
    ids = {r.account_id for r in asyncio.run(store.list_accounts())}
    assert ids == {"a1", "a2"}


def test_account_update_profile_partial_and_missing():
    store = _store()
    asyncio.run(store.create(account_id="a1", registered_name="A", email="x@y.z"))
    updated = asyncio.run(store.update_profile(
        "a1", {"phone": "123", "is_active": False, "bogus_field": "ignored"}))
    assert updated is not None
    assert updated.phone == "123"
    assert updated.is_active is False
    assert updated.email == "x@y.z"  # untouched
    assert asyncio.run(store.update_profile("ghost", {"phone": "1"})) is None


def test_account_balance_persisted_as_string_micros():
    table = FakeBillingTable()
    store = _store(table)
    asyncio.run(store.create(account_id="a1", registered_name="A"))
    asyncio.run(store.adjust_balance("a1", 1_000 * MICROS_PER_USD))
    raw = table.entities[("ACCOUNT", "a1")]
    assert isinstance(raw["balance_micros"], str)
    assert int(raw["balance_micros"]) == 1_000 * MICROS_PER_USD


# -- AccountStore: cached balance ----------------------------------------

def test_get_balance_cached_serves_from_cache_within_ttl():
    table = FakeBillingTable()
    fake_now = [0.0]
    store = _store(table, cache_ttl_s=15.0, clock=lambda: fake_now[0])
    asyncio.run(store.create(account_id="a1", registered_name="A"))
    asyncio.run(store.adjust_balance("a1", 500))
    first = asyncio.run(store.get_balance_cached("a1"))
    assert (first.balance_micros, first.is_active) == (500, True)
    # Mutate the table under the cache: stale value should be served.
    table.entities[("ACCOUNT", "a1")]["balance_micros"] = "999"
    cached = asyncio.run(store.get_balance_cached("a1"))
    assert (cached.balance_micros, cached.is_active) == (500, True)
    fake_now[0] = 16.0  # past TTL
    refreshed = asyncio.run(store.get_balance_cached("a1"))
    assert (refreshed.balance_micros, refreshed.is_active) == (999, True)


def test_get_balance_cached_missing_and_invalidate():
    table = FakeBillingTable()
    store = _store(table)
    assert asyncio.run(store.get_balance_cached("ghost")) is None
    asyncio.run(store.create(account_id="a1", registered_name="A"))
    asyncio.run(store.adjust_balance("a1", 42))
    snap = asyncio.run(store.get_balance_cached("a1"))
    assert (snap.balance_micros, snap.is_active) == (42, True)
    table.entities[("ACCOUNT", "a1")]["balance_micros"] = "7"
    store.invalidate_balance_cache("a1")
    refreshed = asyncio.run(store.get_balance_cached("a1"))
    assert (refreshed.balance_micros, refreshed.is_active) == (7, True)


def test_get_balance_cached_storage_error_raises_unavailable():
    table = FakeBillingTable()
    store = _store(table)
    table.fail_reads = True
    with pytest.raises(BillingStoreUnavailable):
        asyncio.run(store.get_balance_cached("a1"))


# -- AccountStore: CAS balance adjustments -------------------------------

def test_adjust_balance_happy_path_and_negative_drift():
    store = _store()
    asyncio.run(store.create(account_id="a1", registered_name="A"))
    assert asyncio.run(store.adjust_balance("a1", 1_000_000)) == 1_000_000
    assert asyncio.run(store.adjust_balance("a1", -1_500_000)) == -500_000


def test_adjust_balance_missing_account_returns_none():
    assert asyncio.run(_store().adjust_balance("ghost", 5)) is None


def test_adjust_balance_retries_through_conflicts():
    table = FakeBillingTable()
    store = _store(table)
    asyncio.run(store.create(account_id="a1", registered_name="A"))
    table.conflict_next = 2
    assert asyncio.run(store.adjust_balance("a1", 10, max_attempts=5)) == 10


def test_adjust_balance_exhausted_conflicts_raises():
    table = FakeBillingTable()
    store = _store(table)
    asyncio.run(store.create(account_id="a1", registered_name="A"))
    table.conflict_next = 99
    with pytest.raises(BillingStoreUnavailable):
        asyncio.run(store.adjust_balance("a1", 10, max_attempts=3))


def test_adjust_balance_concurrent_sums_exactly():
    table = FakeBillingTable()
    store = _store(table)
    asyncio.run(store.create(account_id="a1", registered_name="A"))

    async def run_all():
        await asyncio.gather(
            *(store.adjust_balance("a1", 3, max_attempts=20) for _ in range(10))
        )

    asyncio.run(run_all())
    assert int(table.entities[("ACCOUNT", "a1")]["balance_micros"]) == 30


# -- WalletTxnStore -------------------------------------------------------

def _txns(table: Optional[FakeBillingTable] = None) -> WalletTxnStore:
    return WalletTxnStore(table if table is not None else FakeBillingTable(),
                          logger=logging.getLogger("t"))


def test_debit_exists_point_read_and_write_debit():
    store = _txns()
    assert asyncio.run(store.debit_exists("a1", "job-123")) is False
    asyncio.run(store.write_debit("a1", "job-123", -5_000_000, 995_000_000))
    assert asyncio.run(store.debit_exists("a1", "job-123")) is True
    rows, total = asyncio.run(store.list_txns("a1"))
    assert len(rows) == 1 and total == 1
    row = rows[0]
    assert row["txn_type"] == "debit"
    assert int(row["amount_micros"]) == -5_000_000
    assert int(row["balance_after_micros"]) == 995_000_000
    assert row["job_id"] == "job-123"


def test_write_manual_signs_and_txn_types():
    store = _txns()
    txn_id = asyncio.run(store.write_manual("a1", 10_000_000, 10_000_000, "top-up"))
    assert txn_id
    asyncio.run(store.write_manual("a1", -2_000_000, 8_000_000, "correction"))
    rows, _ = asyncio.run(store.list_txns("a1"))
    by_note = {r["note"]: r for r in rows}
    assert by_note["top-up"]["txn_type"] == "recharge"
    assert by_note["correction"]["txn_type"] == "adjustment"


def test_list_txns_newest_first_across_row_shapes_and_scoped():
    table = FakeBillingTable()
    store = _txns(table)

    async def seed():
        await store.write_manual("a1", 100, 100, "first")
        await asyncio.sleep(0.002)
        await store.write_debit("a1", "j1", -30, 70)
        await asyncio.sleep(0.002)
        await store.write_manual("a1", 50, 120, "second")
        await store.write_manual("other", 999, 999, "not-mine")

    asyncio.run(seed())
    rows, total = asyncio.run(store.list_txns("a1"))
    assert total == 3  # scoped to a1, "not-mine" excluded
    assert [r["note"] or r.get("job_id") for r in rows] == ["second", "j1", "first"]
    assert all(r["PartitionKey"] == "a1" for r in rows)

    # pagination: page 2 (offset 1, limit 1) is the middle row, totals stable
    page, total2 = asyncio.run(store.list_txns("a1", limit=1, offset=1))
    assert total2 == 3 and [r.get("job_id") or r["note"] for r in page] == ["j1"]


# -- PricingStore ---------------------------------------------------------

def _pricing(table: Optional[FakeBillingTable] = None, **kwargs) -> PricingStore:
    return PricingStore(table if table is not None else FakeBillingTable(),
                        logger=logging.getLogger("t"), **kwargs)


def test_pricing_upsert_get_all_delete():
    store = _pricing()
    rec = asyncio.run(store.upsert(
        price_key="video_music", billing_mode="per_request",
        unit_price_micros=10_000_000, note="企业客户 按次"))
    assert rec.price_key == "video_music"
    assert rec.unit_price_micros == 10_000_000
    all_rows = asyncio.run(store.get_all())
    assert [r.price_key for r in all_rows] == ["video_music"]
    assert asyncio.run(store.delete("video_music")) is True
    assert asyncio.run(store.delete("video_music")) is False
    assert asyncio.run(store.get_all()) == []


def test_pricing_resolve_specificity():
    store = _pricing()
    asyncio.run(store.upsert(price_key="image_music", billing_mode="per_second",
                             unit_price_micros=500_000))
    asyncio.run(store.upsert(price_key="image_music:edenn_studio",
                             billing_mode="per_second", unit_price_micros=900_000))
    hit = asyncio.run(store.resolve("image_music", "edenn_studio"))
    assert hit is not None and hit.unit_price_micros == 900_000
    fallback = asyncio.run(store.resolve("image_music", "edenn_basic"))
    assert fallback is not None and fallback.unit_price_micros == 500_000
    assert fallback.price_key == "image_music"
    assert asyncio.run(store.resolve("video_music", None)) is None


def test_pricing_resolve_cache_and_invalidation():
    table = FakeBillingTable()
    fake_now = [0.0]
    store = _pricing(table, cache_ttl_s=30.0, clock=lambda: fake_now[0])
    asyncio.run(store.upsert(price_key="video_music", billing_mode="per_request",
                             unit_price_micros=1_000_000))
    assert asyncio.run(store.resolve("video_music", None)).unit_price_micros == 1_000_000
    # Mutate under the cache: stale until TTL, unless a store write invalidates.
    table.entities[("PRICING", "video_music")]["unit_price_micros"] = "2000000"
    assert asyncio.run(store.resolve("video_music", None)).unit_price_micros == 1_000_000
    fake_now[0] = 31.0
    assert asyncio.run(store.resolve("video_music", None)).unit_price_micros == 2_000_000
    asyncio.run(store.upsert(price_key="video_music", billing_mode="per_request",
                             unit_price_micros=3_000_000))
    assert asyncio.run(store.resolve("video_music", None)).unit_price_micros == 3_000_000


# -- BalanceSnapshot / total_recharged_micros ------------------------------

def test_balance_snapshot_carries_account_fields():
    accounts = AccountStore(FakeBillingTable(), logger=logging.getLogger("t"))
    asyncio.run(accounts.create(account_id="acct1", registered_name="N"))
    snap = asyncio.run(accounts.get_balance_cached("acct1"))
    assert snap.balance_micros == 0
    assert snap.is_active is True
    assert snap.created_at  # ISO string
    assert snap.total_recharged_micros == 0


def test_adjust_balance_accumulates_total_recharged_only_when_asked():
    accounts = AccountStore(FakeBillingTable(), logger=logging.getLogger("t"))
    asyncio.run(accounts.create(account_id="acct1", registered_name="N"))
    # recharge: counts toward the total
    asyncio.run(accounts.adjust_balance("acct1", 5_000_000, recharge_micros=5_000_000))
    # debit: does not
    asyncio.run(accounts.adjust_balance("acct1", -2_000_000))
    record = asyncio.run(accounts.get("acct1"))
    assert record.balance_micros == 3_000_000
    assert record.total_recharged_micros == 5_000_000


def test_write_manual_accepts_deterministic_txn_id():
    txns = WalletTxnStore(FakeBillingTable(), logger=logging.getLogger("t"))
    returned = asyncio.run(txns.write_manual(
        "acct1", 1_000_000, 1_000_000, "grant", txn_id="txn-fixed-001"))
    assert returned == "txn-fixed-001"
    assert asyncio.run(txns.txn_exists("acct1", "txn-fixed-001")) is True
    assert asyncio.run(txns.txn_exists("acct1", "txn-missing")) is False


# -- RMB conversion + pricing teaser column --------------------------------

def test_rmb_to_micros_rounds_half_up_at_rate_7():
    assert rmb_to_micros("0.45", 7.0) == 64_286     # 0.0642857... USD
    assert rmb_to_micros("0.35", 7.0) == 50_000     # exactly $0.05
    assert rmb_to_micros(0, 7.0) == 0
    with pytest.raises(ValueError):
        rmb_to_micros("0.45", 0)
    with pytest.raises(ValueError):
        rmb_to_micros("abc", 7.0)


def test_pricing_teaser_column_round_trips_and_clears():
    pricing = PricingStore(FakeBillingTable(), logger=logging.getLogger("t"))
    asyncio.run(pricing.upsert(price_key="video_music", billing_mode="per_request",
                               unit_price_micros=100_000,
                               teaser_unit_price_micros=64_286))
    rec = asyncio.run(pricing.resolve("video_music", None))
    assert rec.teaser_unit_price_micros == 64_286
    assert rec.teaser_unit_price_usd == 0.0643  # display rounds to 4 dp
    # upsert without teaser clears it (empty-string column, MERGE-safe)
    asyncio.run(pricing.upsert(price_key="video_music", billing_mode="per_request",
                               unit_price_micros=100_000))
    pricing._cache = None
    rec = asyncio.run(pricing.resolve("video_music", None))
    assert rec.teaser_unit_price_micros is None


def test_pricing_metering_round_trips_and_defaults_to_per_second():
    pricing = PricingStore(FakeBillingTable(), logger=logging.getLogger("t"))
    asyncio.run(pricing.upsert(price_key="video_music", billing_mode="per_second",
                               unit_price_micros=128_571, unit_seconds=30))
    asyncio.run(pricing.upsert(price_key="image_music", billing_mode="per_second",
                               unit_price_micros=100_000,
                               min_billable_seconds=15))
    video = asyncio.run(pricing.resolve("video_music", None))
    image = asyncio.run(pricing.resolve("image_music", None))
    assert (video.unit_seconds, video.min_billable_seconds) == (30, 0)
    assert (image.unit_seconds, image.min_billable_seconds) == (1, 15)


def test_pricing_row_written_before_metering_reads_as_plain_per_second():
    """Rows predating the columns must keep charging exactly what they charged:
    one unit per second, no floor."""
    table = FakeBillingTable()
    pricing = PricingStore(table, logger=logging.getLogger("t"))
    asyncio.run(asyncio.to_thread(table.upsert_entity, {
        "PartitionKey": "PRICING", "RowKey": "image_music",
        "billing_mode": "per_second", "unit_price_micros": "100000",
        "note": "", "updated_at": "2026-07-20T00:00:00+00:00",
    }))
    rec = asyncio.run(pricing.resolve("image_music", None))
    assert (rec.unit_seconds, rec.min_billable_seconds) == (1, 0)
