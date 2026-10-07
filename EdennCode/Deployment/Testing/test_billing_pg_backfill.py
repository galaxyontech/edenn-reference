"""Backfill tests: fake Table Storage in, real Postgres out.

The source side is faked because Table Storage's behavior is already covered
elsewhere; the destination is real because every property that matters here is
a database one — foreign keys, CHECK constraints, and the ``source_row_key``
unique index that makes a re-run safe.

Same gate as ``test_billing_pg_stores``: needs ``BILLING_TEST_DATABASE_URL``
pointed at a throwaway database.
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
from EdennCode.Deployment.billing.pg_stores import BillingPgPool  # noqa: E402
from EdennCode.Deployment.billing.stores import (  # noqa: E402
    AccountStore,
    PricingStore,
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


def _backfill(pool, *, dry_run: bool = False):
    from EdennCode.Scripts.billing_pg_backfill import Backfill

    return Backfill(pool, dry_run=dry_run, logger=_LOG)


@pytest_asyncio.fixture
async def source():
    """A miniature Table Storage deployment: two accounts, keys, money, usage."""
    accounts = AccountStore(FakeBillingTable(), logger=_LOG)
    keys = ApiKeyStore(FakeBillingTable(), logger=_LOG, track_last_used=False)
    pricing = PricingStore(FakeBillingTable(), logger=_LOG)
    index_table, wallet_table, usage_table = (
        FakeBillingTable(), FakeBillingTable(), FakeBillingTable())

    await accounts.create(account_id="acct-1", registered_name="中电达通",
                          phone="13800138000")
    await accounts.create(account_id="acct-2", registered_name="示例传媒")
    # Recharge then debit, so the balance agrees with the two wallet rows
    # written below. A fixture where they disagree would make every
    # reconciliation assertion below vacuous.
    await accounts.adjust_balance("acct-1", 100_000_000,
                                  recharge_micros=100_000_000)
    await accounts.adjust_balance("acct-1", -64_286)
    plaintext, record = await keys.mint(user_id="acct-1", note="生产")
    await pricing.upsert(price_key="video_music", billing_mode="per_request",
                         unit_price_micros=128571,
                         teaser_unit_price_micros=64286)

    index_table.upsert_entity({
        "PartitionKey": "PHONE", "RowKey": "hash-of-phone",
        "value": "13800138000", "account_id": "acct-1",
        "created_at": "2026-07-01T00:00:00+00:00"})
    wallet_table.upsert_entity({
        "PartitionKey": "acct-1", "RowKey": "txn-recharge-1",
        "txn_type": "recharge", "amount_micros": "100000000",
        "balance_after_micros": "100000000", "job_id": "",
        "note": "首充", "timestamp_utc": "2026-07-02T00:00:00+00:00"})
    wallet_table.upsert_entity({
        "PartitionKey": "acct-1", "RowKey": "job-job-a",
        "txn_type": "debit", "amount_micros": "64286",   # positive: legacy sign
        "balance_after_micros": "99935714", "job_id": "job-a",
        "note": "", "timestamp_utc": "2026-07-03T00:00:00+00:00"})
    usage_table.upsert_entity({
        "PartitionKey": "acct-1", "RowKey": "0009-job-a", "job_id": "job-a",
        "endpoint": "/api/v1/jobs/video", "status": "completed",
        "auth_mode": "enforce", "user_id": "acct-1",
        "key_prefix": record.key_prefix, "prompt_tokens": 10,
        "completion_tokens": 20, "total_tokens": 30, "token_cost_usd": 0.001,
        "music_provider": "provider_b", "model_spec": "edenn_enhanced",
        "generation_call_count": 1, "generation_cost_usd": 0.21,
        "total_cost_usd": 0.211, "latency_ms": 45000,
        "billing_mode": "per_request", "billed_units": 1,
        "unit_price_usd": 0.064286, "billed_amount_usd": 0.064286,
        "billed_amount_micros": "64286", "price_track": "teaser",
        "timestamp_utc": "2026-07-03T00:00:00+00:00"})
    return dict(accounts=accounts, keys=keys, pricing=pricing,
                index_table=index_table, wallet_table=wallet_table,
                usage_table=usage_table, key_prefix=record.key_prefix)


async def _run_all(job, source):
    known = await job.accounts(source["accounts"])
    await job.api_keys(source["keys"], known)
    await job.identities(source["index_table"], known)
    await job.pricing(source["pricing"])
    await job.wallet_txns(source["wallet_table"], known)
    await job.usage(source["usage_table"], known)
    return known


# -- the happy path ------------------------------------------------------


async def test_every_table_lands(pool, source):
    await _run_all(_backfill(pool), source)

    assert await pool.fetchval("SELECT count(*) FROM accounts") == 2
    assert await pool.fetchval("SELECT count(*) FROM api_keys") == 1
    assert await pool.fetchval("SELECT count(*) FROM account_identities") == 1
    assert await pool.fetchval("SELECT count(*) FROM wallet_txns") == 2
    assert await pool.fetchval("SELECT count(*) FROM usage_ledger") == 1


async def test_balances_arrive_intact(pool, source):
    await _run_all(_backfill(pool), source)
    row = await pool.fetchrow(
        """SELECT balance_micros, total_recharged_micros, created_via
             FROM accounts WHERE account_id = 'acct-1'""")
    assert int(row["balance_micros"]) == 99_935_714
    assert int(row["total_recharged_micros"]) == 100_000_000
    assert row["created_via"] == "migration"


async def test_debits_are_stored_negative(pool, source):
    """Legacy rows carried a positive debit; reconciliation needs it negative."""
    await _run_all(_backfill(pool), source)
    amount = await pool.fetchval(
        "SELECT amount_micros FROM wallet_txns WHERE job_id = 'job-a'")
    assert int(amount) == -64286


async def test_reconciliation_is_clean_after_backfill(pool, source):
    """100000000 recharged − 64286 debited must equal the copied balance."""
    await _run_all(_backfill(pool), source)
    drift = await pool.fetchval(
        "SELECT drift_micros FROM balance_reconciliation WHERE account_id='acct-1'")
    assert int(drift) == 0


async def test_contact_kind_is_folded_to_lowercase(pool, source):
    """The CHECK constraint accepts only lowercase; Table Storage used PHONE."""
    await _run_all(_backfill(pool), source)
    row = await pool.fetchrow("SELECT kind, value FROM account_identities")
    assert (row["kind"], row["value"]) == ("phone", "13800138000")


async def test_usage_row_gets_a_price_snapshot(pool, source):
    await _run_all(_backfill(pool), source)
    row = await pool.fetchrow(
        "SELECT * FROM usage_ledger WHERE job_id = 'job-a'")
    assert row["price_source"] == "teaser"        # derived from price_track
    assert int(row["unit_price_micros"]) == 64286
    assert int(row["billed_amount_micros"]) == 64286
    assert int(row["token_cost_micros"]) == 1000  # 0.001 USD
    assert row["list_unit_price_micros"] is None  # unknowable for history
    assert row["discount_rate"] is None           # so no rate can be claimed


# -- idempotency ---------------------------------------------------------


async def test_second_run_inserts_nothing(pool, source):
    await _run_all(_backfill(pool), source)
    job = _backfill(pool)
    await _run_all(job, source)

    assert job.summary["accounts"].get("inserted") is None
    assert job.summary["usage_ledger"]["already_present"] == 1
    assert await pool.fetchval("SELECT count(*) FROM usage_ledger") == 1
    assert await pool.fetchval("SELECT count(*) FROM wallet_txns") == 2


async def test_a_live_dual_write_row_is_not_disturbed_by_a_rerun(pool, source):
    """The final pass runs with dual-write live: its rows have no source key."""
    await _run_all(_backfill(pool), source)
    await pool.execute(
        """INSERT INTO usage_ledger (job_id, account_id, endpoint, status,
                                     occurred_at)
           VALUES ('job-live','acct-1','/api/v1/jobs/video','completed', now())""")
    await _run_all(_backfill(pool), source)
    assert await pool.fetchval("SELECT count(*) FROM usage_ledger") == 2


async def test_dry_run_writes_nothing(pool, source):
    job = _backfill(pool, dry_run=True)
    await _run_all(job, source)
    assert job.summary["accounts"]["inserted"] == 2   # reports intent
    assert await pool.fetchval("SELECT count(*) FROM accounts") == 0


# -- the awkward rows ----------------------------------------------------


async def test_a_key_without_an_account_gets_a_placeholder(pool, source):
    """Dropping it would lock a paying customer out at cutover."""
    _, orphan = await source["keys"].mint(user_id="ghost-acct", note="孤儿")
    job = _backfill(pool)
    await _run_all(job, source)

    assert await pool.fetchval(
        "SELECT count(*) FROM api_keys WHERE account_id = 'ghost-acct'") == 1
    row = await pool.fetchrow(
        "SELECT created_via, balance_micros, note FROM accounts WHERE account_id='ghost-acct'")
    assert (row["created_via"], int(row["balance_micros"])) == ("migration", 0)
    assert any("ghost-acct" in line for line in job.placeholders)


async def test_a_row_with_an_unknown_status_is_reported_not_coerced(pool, source):
    source["usage_table"].upsert_entity({
        "PartitionKey": "acct-1", "RowKey": "0008-job-weird", "job_id": "job-weird",
        "endpoint": "/api/v1/jobs/video", "status": "in_progress",
        "user_id": "acct-1", "timestamp_utc": "2026-07-04T00:00:00+00:00"})
    job = _backfill(pool)
    await _run_all(job, source)

    assert job.summary["usage_ledger"]["skipped"] == 1
    assert any("unknown status" in line for line in job.skipped)
    assert await pool.fetchval("SELECT count(*) FROM usage_ledger") == 1


async def test_a_billed_row_on_a_failed_job_is_reported(pool, source):
    """The CHECK would reject it — and it means a real accounting bug upstream."""
    source["usage_table"].upsert_entity({
        "PartitionKey": "acct-1", "RowKey": "0007-job-bad", "job_id": "job-bad",
        "endpoint": "/api/v1/jobs/video", "status": "failed", "user_id": "acct-1",
        "billed_amount_micros": "5000", "billed_units": 1,
        "unit_price_usd": 0.005, "timestamp_utc": "2026-07-05T00:00:00+00:00"})
    job = _backfill(pool)
    await _run_all(job, source)
    assert any("billed 5000 on status 'failed'" in line for line in job.skipped)


async def test_an_anonymous_usage_row_keeps_a_null_account(pool, source):
    source["usage_table"].upsert_entity({
        "PartitionKey": "__anonymous__", "RowKey": "0006-job-anon",
        "job_id": "job-anon", "endpoint": "/api/v1/jobs/video",
        "status": "completed", "user_id": "",
        "timestamp_utc": "2026-07-06T00:00:00+00:00"})
    await _run_all(_backfill(pool), source)
    assert await pool.fetchval(
        "SELECT account_id FROM usage_ledger WHERE job_id='job-anon'") is None
    # And no placeholder account was invented for the anonymous partition.
    assert await pool.fetchval(
        "SELECT count(*) FROM accounts WHERE account_id = '__anonymous__'") == 0


async def test_prices_overwrite_because_storage_stays_authoritative(pool, source):
    """The seed may be stale by cutover day; a wrong price bills everyone wrong."""
    await source["pricing"].upsert(price_key="video_music",
                                   billing_mode="per_request",
                                   unit_price_micros=200000,
                                   teaser_unit_price_micros=None)
    await _run_all(_backfill(pool), source)
    row = await pool.fetchrow(
        "SELECT unit_price_micros, teaser_unit_price_micros FROM price_list "
        "WHERE price_key='video_music'")
    assert int(row["unit_price_micros"]) == 200000
    assert row["teaser_unit_price_micros"] is None
