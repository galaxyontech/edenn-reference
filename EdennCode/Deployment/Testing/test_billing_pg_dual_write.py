"""Dual-write end to end: Table Storage primary, real Postgres secondary.

This is the test that says the migration is safe to switch on. It runs the
ordinary operations a live deployment performs — open an account, mint a key,
recharge, bill a job, revoke a key, claim a contact — through the dual-write
wrappers, then asserts the Postgres copy agrees with the Table Storage
original on every one.

Then it flips the direction and checks the reverse, because a cutover that
cannot be rolled back is not a cutover.

Needs ``BILLING_TEST_DATABASE_URL`` pointed at a throwaway database.
"""
from __future__ import annotations

import logging
import os
import pathlib

import pytest
import pytest_asyncio

asyncpg = pytest.importorskip("asyncpg")

from EdennCode.Deployment.Testing.test_billing_stores import (  # noqa: E402
    FakeBillingTable,
)
from EdennCode.Deployment.auth.key_store import ApiKeyStore  # noqa: E402
from EdennCode.Deployment.billing.account_index import (  # noqa: E402
    AccountIndexStore,
    INDEX_KIND_PHONE,
)
from EdennCode.Deployment.billing.dual_write import (  # noqa: E402
    DualAccountIndexStore,
    DualAccountStore,
    DualApiKeyStore,
    DualPricingStore,
    DualWalletTxnStore,
    build_usage_mirror,
)
from EdennCode.Deployment.billing.pg_key_store import (  # noqa: E402
    PgAccountIndexStore,
    PgApiKeyStore,
)
from EdennCode.Deployment.billing.pg_stores import (  # noqa: E402
    BillingPgPool,
    PgAccountStore,
    PgPricingStore,
    PgUsageLedger,
    PgWalletTxnStore,
)
from EdennCode.Deployment.billing.stores import (  # noqa: E402
    AccountStore,
    PricingStore,
    WalletTxnStore,
)

DSN = os.getenv("BILLING_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.skipif(not DSN, reason="BILLING_TEST_DATABASE_URL not set"),
    pytest.mark.asyncio,
]

_SQL_DIR = pathlib.Path(__file__).resolve().parents[3] / "EdennCode" / "Database" / "billing"
_LOG = logging.getLogger("test")
_ROLES = """
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='billing_rw') THEN
      CREATE ROLE billing_rw NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='billing_readonly') THEN
      CREATE ROLE billing_readonly NOLOGIN; END IF;
END $$;
"""


@pytest_asyncio.fixture
async def pool():
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await conn.execute(_ROLES)
        for name in ("001_schema.sql", "003_unpartition_usage_ledger.sql",
                     "004_seed_pricing.sql", "005_backfill_provenance.sql",
                     "006_key_suffix.sql", "007_duration_billing.sql"):
            await conn.execute((_SQL_DIR / name).read_text())
    finally:
        await conn.close()
    p = BillingPgPool(DSN, logger=_LOG)
    yield p
    await p.close()


class _Pair:
    """Both store sets plus the dual wrappers, in the given direction."""

    def __init__(self, pool, *, pg_primary: bool) -> None:
        storage = dict(
            accounts=AccountStore(FakeBillingTable(), logger=_LOG),
            txns=WalletTxnStore(FakeBillingTable(), logger=_LOG),
            pricing=PricingStore(FakeBillingTable(), logger=_LOG),
            keys=ApiKeyStore(FakeBillingTable(), logger=_LOG,
                             track_last_used=False),
            index=AccountIndexStore(FakeBillingTable(), logger=_LOG),
        )
        pg = dict(
            accounts=PgAccountStore(pool, logger=_LOG),
            txns=PgWalletTxnStore(pool, logger=_LOG),
            pricing=PgPricingStore(pool, cache_ttl_s=0, logger=_LOG),
            keys=PgApiKeyStore(pool, logger=_LOG, track_last_used=False),
            index=PgAccountIndexStore(pool, logger=_LOG),
        )
        self.storage, self.pg = storage, pg
        first, second = (pg, storage) if pg_primary else (storage, pg)
        self.accounts = DualAccountStore(first["accounts"], second["accounts"],
                                         logger=_LOG)
        self.txns = DualWalletTxnStore(first["txns"], second["txns"], logger=_LOG)
        self.pricing = DualPricingStore(first["pricing"], second["pricing"],
                                        logger=_LOG)
        self.keys = DualApiKeyStore(first["keys"], second["keys"], logger=_LOG)
        self.index = DualAccountIndexStore(first["index"], second["index"],
                                           logger=_LOG)


@pytest_asyncio.fixture
async def dual(pool):
    return _Pair(pool, pg_primary=False)


# -- a day in the life, mirrored -----------------------------------------


async def test_an_account_opened_through_dual_write_exists_in_both(dual):
    await dual.accounts.create(account_id="acct-1", registered_name="中电达通",
                               phone="13800138000")
    storage = await dual.storage["accounts"].get("acct-1")
    postgres = await dual.pg["accounts"].get("acct-1")
    assert storage.registered_name == postgres.registered_name == "中电达通"
    assert storage.phone == postgres.phone == "13800138000"


async def test_a_recharge_lands_on_both_balances(dual):
    await dual.accounts.create(account_id="acct-1", registered_name="X")
    await dual.accounts.adjust_balance("acct-1", 100_000_000,
                                       recharge_micros=100_000_000)
    for store in (dual.storage["accounts"], dual.pg["accounts"]):
        record = await store.get("acct-1")
        assert (record.balance_micros, record.total_recharged_micros) == (
            100_000_000, 100_000_000)


async def test_the_same_key_works_against_either_store(dual):
    """The point of adopting rather than re-minting: one key, both stores."""
    await dual.accounts.create(account_id="acct-1", registered_name="X")
    plaintext, record = await dual.keys.mint(user_id="acct-1", note="生产")

    assert (await dual.storage["keys"].lookup(plaintext)).user_id == "acct-1"
    assert (await dual.pg["keys"].lookup(plaintext)).user_id == "acct-1"
    assert record.key_prefix == plaintext[:12]


async def test_revocation_reaches_both_stores(dual):
    await dual.accounts.create(account_id="acct-1", registered_name="X")
    plaintext, record = await dual.keys.mint(user_id="acct-1")
    assert await dual.keys.revoke(record.key_prefix) is True

    assert await dual.storage["keys"].lookup(plaintext) is None
    assert await dual.pg["keys"].lookup(plaintext) is None


async def test_a_contact_claim_binds_the_same_account_in_both(dual):
    await dual.accounts.create(account_id="acct-1", registered_name="X")
    assert await dual.index.claim(INDEX_KIND_PHONE, "13800138000", "acct-1")
    assert await dual.storage["index"].lookup(
        INDEX_KIND_PHONE, "13800138000") == "acct-1"
    # Postgres folds the kind to lowercase; the lookup folds it too, so callers
    # keep passing the uppercase constant.
    assert await dual.pg["index"].lookup(
        INDEX_KIND_PHONE, "13800138000") == "acct-1"


async def test_a_price_change_reaches_both(dual):
    await dual.pricing.upsert(price_key="video_music",
                              billing_mode="per_request",
                              unit_price_micros=128571,
                              teaser_unit_price_micros=64286)
    for store in (dual.storage["pricing"], dual.pg["pricing"]):
        record = await store.resolve("video_music", None)
        assert (record.unit_price_micros, record.teaser_unit_price_micros) == (
            128571, 64286)


async def test_a_billed_job_leaves_both_ledgers_agreeing(dual, pool):
    await dual.accounts.create(account_id="acct-1", registered_name="X")
    # Recharge exactly as the admin router does: move the balance, then record
    # the transaction. Reconciliation is only meaningful if both happen.
    after_recharge = await dual.accounts.adjust_balance(
        "acct-1", 100_000_000, recharge_micros=100_000_000)
    await dual.txns.write_manual("acct-1", 100_000_000, after_recharge, "首充")
    new_balance = await dual.accounts.adjust_balance("acct-1", -64_286)
    await dual.txns.write_debit("acct-1", "job-a", -64_286, new_balance)

    storage_rows, storage_total = await dual.storage["txns"].list_txns("acct-1")
    pg_rows, pg_total = await dual.pg["txns"].list_txns("acct-1")
    assert storage_total == pg_total == 2
    assert {r["job_id"] for r in storage_rows} == {r["job_id"] for r in pg_rows}

    # And the check Table Storage could never make.
    drift = await pool.fetchval(
        "SELECT drift_micros FROM balance_reconciliation WHERE account_id='acct-1'")
    assert int(drift) == 0


async def test_a_usage_row_is_mirrored_into_the_ledger(dual, pool):
    await dual.accounts.create(account_id="acct-1", registered_name="X")
    mirror = build_usage_mirror(PgUsageLedger(pool, logger=_LOG), _LOG)
    await mirror({
        "PartitionKey": "acct-1", "RowKey": "0009-job-a", "job_id": "job-a",
        "endpoint": "/api/v1/jobs/video", "status": "completed",
        "user_id": "acct-1", "key_prefix": "sk-abcdefghi",
        "model_spec": "edenn_enhanced", "total_tokens": 30,
        "total_cost_usd": 0.211, "billing_mode": "per_request",
        "billed_units": 1, "unit_price_usd": 0.064286,
        "billed_amount_micros": "64286", "price_track": "teaser",
        "timestamp_utc": "2026-07-03T00:00:00+00:00"})

    row = await pool.fetchrow("SELECT * FROM usage_ledger WHERE job_id='job-a'")
    assert row["price_source"] == "teaser"
    assert int(row["billed_amount_micros"]) == 64286
    assert row["source_row_key"] is None   # live rows carry no migration key
    # A per-request row records no metering, and NULL says so.
    assert (row["unit_seconds"], row["min_billable_seconds"]) == (None, None)


async def test_a_duration_billed_row_keeps_its_metering_snapshot(dual, pool):
    """What one unit meant travels with the charge, so a 90-second video
    billed as 3 units still explains itself after the block size changes."""
    await dual.accounts.create(account_id="acct-1", registered_name="X")
    mirror = build_usage_mirror(PgUsageLedger(pool, logger=_LOG), _LOG)
    await mirror({
        "PartitionKey": "acct-1", "job_id": "job-c",
        "endpoint": "/api/v2/jobs/video-music", "status": "completed",
        "user_id": "acct-1", "key_prefix": "sk-abcdefghi",
        "model_spec": "edenn_enhanced", "billing_mode": "per_second",
        "billed_units": 3, "unit_seconds": 30, "min_billable_seconds": 0,
        "video_duration_s": 90.0, "billed_amount_micros": "385713",
        "timestamp_utc": "2026-08-05T00:00:00+00:00"})

    row = await pool.fetchrow("SELECT * FROM usage_ledger WHERE job_id='job-c'")
    assert (row["billed_units"], row["unit_seconds"]) == (3, 30)
    assert row["min_billable_seconds"] == 0
    read_back = PgUsageLedger.to_storage_row(row)
    assert read_back["unit_seconds"] == 30


async def test_the_ledger_reads_back_in_the_shape_the_details_view_expects(
        dual, pool):
    """详单 rendering must not change on cutover day."""
    from EdennCode.Deployment.auth.usage_recorder import query_usage_window

    await dual.accounts.create(account_id="acct-1", registered_name="X")
    mirror = build_usage_mirror(PgUsageLedger(pool, logger=_LOG), _LOG)
    for job, billed in (("job-a", "64286"), ("job-b", "128571")):
        await mirror({
            "PartitionKey": "acct-1", "job_id": job,
            "endpoint": "/api/v1/jobs/video", "status": "completed",
            "user_id": "acct-1", "key_prefix": "sk-abcdefghi",
            "model_spec": "edenn_enhanced", "total_tokens": 30,
            "total_cost_usd": 0.211, "billing_mode": "per_request",
            "billed_units": 1, "billed_amount_micros": billed,
            "price_track": "teaser" if job == "job-a" else "",
            "timestamp_utc": f"2026-07-0{3 if job == 'job-a' else 4}T00:00:00+00:00"})

    rows, totals, page = await query_usage_window(
        PgUsageLedger(pool, logger=_LOG), user_id="acct-1")

    assert page["total_rows"] == 2
    assert totals["jobs"] == 2
    # micros_to_usd rounds to 4 decimals — the agreed USD display precision.
    assert totals["total_billed_usd"] == pytest.approx(0.1929, abs=1e-6)
    assert set(totals["by_key"]) == {"sk-abcdefghi"}
    assert set(totals["by_model"]) == {"edenn_enhanced"}
    assert set(totals["by_key_model"]) == {"sk-abcdefghi|edenn_enhanced"}
    assert rows[0]["price_track"] in {"teaser", "standard"}


# -- the rollback direction ----------------------------------------------


async def test_postgres_primary_mirrors_back_into_table_storage(pool):
    """After cutover, Table Storage keeps receiving everything — that is what
    makes flipping BILLING_PG_PRIMARY back a real rollback and not a wish."""
    pair = _Pair(pool, pg_primary=True)
    await pair.accounts.create(account_id="acct-1", registered_name="回滚测试")
    await pair.accounts.adjust_balance("acct-1", 50_000_000,
                                       recharge_micros=50_000_000)
    plaintext, record = await pair.keys.mint(user_id="acct-1")

    storage_account = await pair.storage["accounts"].get("acct-1")
    assert storage_account.registered_name == "回滚测试"
    assert storage_account.balance_micros == 50_000_000
    assert (await pair.storage["keys"].lookup(plaintext)).user_id == "acct-1"


async def test_a_down_secondary_does_not_stop_a_recharge(pool, monkeypatch):
    pair = _Pair(pool, pg_primary=False)
    await pair.accounts.create(account_id="acct-1", registered_name="X")

    async def _boom(*a, **k):
        raise RuntimeError("postgres unreachable")

    monkeypatch.setattr(pair.pg["accounts"], "adjust_balance", _boom)
    assert await pair.accounts.adjust_balance("acct-1", 1_000_000) == 1_000_000
    assert pair.accounts.mirror_failures == 1
    # The customer's balance moved. The copy is behind, which the verify script
    # reports — and the backfill repairs.
    assert (await pair.storage["accounts"].get("acct-1")).balance_micros == 1_000_000
