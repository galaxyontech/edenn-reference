"""Postgres store integration tests — real database, no doubles.

A fake pool would only prove the Python assembles a string; every property
worth testing here (atomic debit, idempotency by constraint, prefix
uniqueness, contact-claim races) lives in the database, not in the Python.

Runs only when ``BILLING_TEST_DATABASE_URL`` points at a **throwaway** database
— the fixture drops and recreates the ``public`` schema. Skipped otherwise, so
the default suite stays hermetic.

    EdennCode/Scripts/billing_pg_testdb.sh              # start a throwaway PG and print the DSN
    export BILLING_TEST_DATABASE_URL=...
    .venv/bin/python -m pytest EdennCode/Deployment/Testing/test_billing_pg_stores.py
"""
from __future__ import annotations

import logging
import os
import pathlib

import pytest
import pytest_asyncio

asyncpg = pytest.importorskip("asyncpg")

from EdennCode.Deployment.billing.account_index import (  # noqa: E402
    INDEX_KIND_EMAIL,
    INDEX_KIND_PHONE,
)
from EdennCode.Deployment.billing.pg_key_store import (  # noqa: E402
    PgAccountIndexStore,
    PgApiKeyStore,
)
from EdennCode.Deployment.billing.pg_stores import (  # noqa: E402
    BillingPgPool,
    PgAccountStore,
    PgContractPriceSource,
    PgDiscountTierSource,
    PgPricingStore,
    PgWallet,
    PgWalletTxnStore,
)
from EdennCode.Deployment.billing.pricing_resolver import PricingResolver  # noqa: E402
from EdennCode.Deployment.auth.key_store import KeyStoreUnavailable  # noqa: E402
from EdennCode.Deployment.billing.stores import AccountExists  # noqa: E402

DSN = os.getenv("BILLING_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.skipif(not DSN, reason="BILLING_TEST_DATABASE_URL not set"),
    pytest.mark.asyncio,
]

_SQL_DIR = pathlib.Path(__file__).resolve().parents[3] / "EdennCode" / "Database" / "billing"
_LOG = logging.getLogger("test")

# The roles 002 grants to. Created NOLOGIN here — this suite verifies store
# behavior; the role *boundaries* were verified directly against Azure.
_BOOTSTRAP_ROLES = """
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='billing_rw') THEN
      CREATE ROLE billing_rw NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='billing_readonly') THEN
      CREATE ROLE billing_readonly NOLOGIN;
  END IF;
END $$;
"""


@pytest_asyncio.fixture
async def pool():
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await conn.execute(_BOOTSTRAP_ROLES)
        for name in ("001_schema.sql",
                     "003_unpartition_usage_ledger.sql",
                     "004_seed_pricing.sql",
                     "006_key_suffix.sql",
                     "007_duration_billing.sql"):
            await conn.execute((_SQL_DIR / name).read_text())
    finally:
        await conn.close()
    p = BillingPgPool(DSN, logger=_LOG)
    yield p
    await p.close()


@pytest_asyncio.fixture
async def accounts(pool):
    return PgAccountStore(pool, logger=_LOG)


@pytest_asyncio.fixture
async def acct(accounts):
    return await accounts.create(account_id="acct-1", registered_name="测试公司")


# -- accounts ------------------------------------------------------------


async def test_create_and_read_back(accounts, acct):
    assert (acct.account_id, acct.registered_name) == ("acct-1", "测试公司")
    assert (acct.balance_micros, acct.total_recharged_micros) == (0, 0)
    assert acct.is_active is True
    assert acct.created_at  # ISO string, not a datetime — callers serialize it

    fetched = await accounts.get("acct-1")
    assert fetched == acct


async def test_missing_account_reads_as_none(accounts):
    assert await accounts.get("nope") is None


async def test_duplicate_account_id_is_rejected(accounts, acct):
    with pytest.raises(AccountExists):
        await accounts.create(account_id="acct-1", registered_name="别人")


async def test_update_profile_touches_only_named_fields(accounts, acct):
    updated = await accounts.update_profile(
        "acct-1", {"phone": "13800138000", "note": None, "is_active": False})
    assert updated.phone == "13800138000"
    assert updated.registered_name == "测试公司"   # untouched
    assert updated.is_active is False


async def test_update_profile_of_missing_account(accounts):
    assert await accounts.update_profile("nope", {"phone": "1"}) is None


# -- balance -------------------------------------------------------------


async def test_adjust_balance_returns_the_new_balance(accounts, acct):
    assert await accounts.adjust_balance("acct-1", 100_000_000,
                                         recharge_micros=100_000_000) == 100_000_000
    assert await accounts.adjust_balance("acct-1", -25_000_000) == 75_000_000
    record = await accounts.get("acct-1")
    # Lifetime recharge tracks inflows only — the debit must not reduce it.
    assert record.total_recharged_micros == 100_000_000


async def test_adjust_balance_of_missing_account_is_none(accounts):
    assert await accounts.adjust_balance("nope", 100) is None


async def test_balance_may_go_negative(accounts, acct):
    """Overdraft is by design: the 402 gate stops submission, not settlement."""
    assert await accounts.adjust_balance("acct-1", -500) == -500


async def test_balance_cache_is_invalidated_by_its_own_writes(accounts, acct):
    assert (await accounts.get_balance_cached("acct-1")).balance_micros == 0
    await accounts.adjust_balance("acct-1", 7_000_000)
    assert (await accounts.get_balance_cached("acct-1")).balance_micros == 7_000_000


# -- wallet: the atomicity the migration exists for ----------------------


async def test_debit_moves_balance_and_writes_one_ledger_row(pool, accounts, acct):
    await accounts.adjust_balance("acct-1", 1_000_000, recharge_micros=1_000_000)
    wallet = PgWallet(pool, account_store=accounts, logger=_LOG)

    assert await wallet.debit_for_job(
        account_id="acct-1", job_id="job-a", amount_micros=64286) == 935_714

    rows, total = await PgWalletTxnStore(pool).list_txns("acct-1")
    assert total == 1
    assert rows[0]["txn_type"] == "debit"
    assert rows[0]["amount_micros"] == "-64286"   # negative: reconciliation sums it


async def test_repeated_debit_is_a_no_op(pool, accounts, acct):
    await accounts.adjust_balance("acct-1", 1_000_000, recharge_micros=1_000_000)
    wallet = PgWallet(pool, account_store=accounts, logger=_LOG)

    first = await wallet.debit_for_job(account_id="acct-1", job_id="job-a",
                                       amount_micros=64286)
    second = await wallet.debit_for_job(account_id="acct-1", job_id="job-a",
                                        amount_micros=64286)
    assert (first, second) == (935_714, None)
    # The retry must not have moved the balance — that is the whole point of
    # doing the update and the ledger row in one transaction.
    assert (await accounts.get("acct-1")).balance_micros == 935_714
    assert (await PgWalletTxnStore(pool).list_txns("acct-1"))[1] == 1


async def test_debit_of_missing_account_is_none(pool, accounts):
    wallet = PgWallet(pool, account_store=accounts, logger=_LOG)
    assert await wallet.debit_for_job(account_id="nope", job_id="j",
                                      amount_micros=1) is None


async def test_reconciliation_view_sees_no_drift(pool, accounts, acct):
    wallet = PgWallet(pool, account_store=accounts, logger=_LOG)
    await wallet.apply(account_id="acct-1", delta_micros=100_000_000,
                       txn_type="recharge", idempotency_key="rc-1",
                       recharge_micros=100_000_000, created_via="admin")
    await wallet.debit_for_job(account_id="acct-1", job_id="job-a",
                               amount_micros=64286)
    await wallet.debit_for_job(account_id="acct-1", job_id="job-b",
                               amount_micros=100_000)

    drift = await pool.fetchval(
        "SELECT drift_micros FROM balance_reconciliation WHERE account_id = $1",
        "acct-1")
    assert int(drift) == 0


async def test_zero_or_negative_debit_is_ignored(pool, accounts, acct):
    wallet = PgWallet(pool, account_store=accounts, logger=_LOG)
    assert await wallet.debit_for_job(account_id="acct-1", job_id="j",
                                      amount_micros=0) is None
    assert (await PgWalletTxnStore(pool).list_txns("acct-1"))[1] == 0


async def test_manual_txn_records_a_refund_as_an_adjustment(pool, accounts, acct):
    txns = PgWalletTxnStore(pool, logger=_LOG)
    new_balance = await accounts.adjust_balance("acct-1", -1_000_000)
    txn_id = await txns.write_manual("acct-1", -1_000_000, new_balance, "退费")
    assert await txns.txn_exists("acct-1", txn_id) is True
    rows, _ = await txns.list_txns("acct-1")
    assert rows[0]["txn_type"] == "adjustment"


# -- pricing -------------------------------------------------------------


async def test_seeded_prices_resolve(pool):
    """The seed plus 007: both products bill on delivered duration, and the
    prices themselves are untouched by the metering change."""
    store = PgPricingStore(pool, logger=_LOG)
    video = await store.resolve("video_music", None)
    assert (video.unit_price_micros, video.teaser_unit_price_micros) == (128571, 64286)
    assert video.billing_mode == "per_second"
    assert (video.unit_seconds, video.min_billable_seconds) == (30, 0)
    image = await store.resolve("image_music", None)
    assert (image.unit_price_micros, image.teaser_unit_price_micros) == (100000, 50000)
    assert (image.unit_seconds, image.min_billable_seconds) == (1, 15)


async def test_metering_survives_a_round_trip_and_is_checked(pool):
    store = PgPricingStore(pool, cache_ttl_s=0, logger=_LOG)
    await store.upsert(price_key="audio_edit", billing_mode="per_second",
                       unit_price_micros=1000, unit_seconds=15,
                       min_billable_seconds=5)
    stored = await store.resolve("audio_edit", None)
    assert (stored.unit_seconds, stored.min_billable_seconds) == (15, 5)
    # The database refuses metering on a per-request price even if a caller
    # bypasses the admin API's own check.
    with pytest.raises(Exception):
        await pool.execute(
            """INSERT INTO price_list (price_key, billing_mode,
                   unit_price_micros, unit_seconds)
               VALUES ('bad_key', 'per_request', 1000, 30)""")


async def test_model_specific_price_beats_the_product_price(pool):
    store = PgPricingStore(pool, cache_ttl_s=0, logger=_LOG)
    await store.upsert(price_key="video_music:edenn_studio",
                       billing_mode="per_request", unit_price_micros=999999)
    assert (await store.resolve("video_music", "edenn_studio")).unit_price_micros == 999999
    assert (await store.resolve("video_music", "edenn_basic")).unit_price_micros == 128571


async def test_upsert_overwrites_and_delete_removes(pool):
    store = PgPricingStore(pool, cache_ttl_s=0, logger=_LOG)
    await store.upsert(price_key="video_music", billing_mode="per_request",
                       unit_price_micros=1, teaser_unit_price_micros=None)
    refreshed = await store.resolve("video_music", None)
    assert (refreshed.unit_price_micros, refreshed.teaser_unit_price_micros) == (1, None)
    assert await store.delete("video_music") is True
    assert await store.delete("video_music") is False
    assert await store.resolve("video_music", None) is None


async def test_unpriced_product_resolves_to_none(pool):
    assert await PgPricingStore(pool, logger=_LOG).resolve("audio_edit", None) is None


# -- contract prices and tiers -------------------------------------------


async def test_only_the_in_window_contract_is_returned(pool, accounts, acct):
    await pool.execute(
        """INSERT INTO account_contract_prices
               (account_id, price_key, billing_mode, unit_price_micros,
                contract_ref, effective_from, effective_to)
           VALUES ($1,'video_music','per_request',30000,'CONTRACT-2026',
                   now() - interval '10 days', NULL),
                  ($1,'image_music','per_second',9000,'CONTRACT-2025',
                   now() - interval '400 days', now() - interval '30 days')""",
        "acct-1")
    source = PgContractPriceSource(pool)
    assert (await source.get_active("acct-1", "video_music")).unit_price_micros == 30000
    assert await source.get_active("acct-1", "image_music") is None   # expired
    assert await source.get_active("acct-other", "video_music") is None


async def test_tier_source_reads_only_active_tiers(pool):
    await pool.execute(
        """INSERT INTO discount_tiers (min_recharged_micros, discount_rate,
                                       effective_from, effective_to)
           VALUES (500000000, 0.0300, now() - interval '1 day', NULL),
                  (900000000, 0.5000, now() - interval '9 days',
                   now() - interval '1 day')""")
    tiers = await PgDiscountTierSource(pool, cache_ttl_s=0).list_active()
    assert [(t.min_recharged_micros, float(t.discount_rate)) for t in tiers] == [
        (500000000, 0.03)]


async def test_resolver_end_to_end_against_the_database(pool, accounts, acct):
    """The four sources wired to real tables, not stubs."""
    await pool.execute(
        """INSERT INTO discount_tiers (min_recharged_micros, discount_rate)
           VALUES (500000000, 0.0700)""")
    resolver = PricingResolver(
        pricing_store=PgPricingStore(pool, cache_ttl_s=0, logger=_LOG),
        logger=_LOG,
        contract_source=PgContractPriceSource(pool),
        tier_source=PgDiscountTierSource(pool, cache_ttl_s=0),
    )
    common = dict(product="video_music", model_spec=None, account_id="acct-1")

    aged = await accounts.get("acct-1")
    from datetime import datetime, timedelta, timezone
    old_enough = datetime.fromisoformat(aged.created_at) + timedelta(days=200)

    tiered = await resolver.resolve(**common, account_created_at=
                                    datetime.fromisoformat(aged.created_at),
                                    total_recharged_micros=500_000_000,
                                    now=old_enough)
    assert (tiered.price_source, tiered.unit_price_micros) == ("volume_tier", 119571)

    await pool.execute(
        """INSERT INTO account_contract_prices
               (account_id, price_key, billing_mode, unit_price_micros)
           VALUES ($1,'video_music','per_request',30000)""", "acct-1")
    contracted = await resolver.resolve(**common,
                                        total_recharged_micros=500_000_000,
                                        now=old_enough)
    assert (contracted.price_source, contracted.unit_price_micros) == ("contract", 30000)


# -- api keys ------------------------------------------------------------


async def test_mint_then_lookup(pool, acct):
    keys = PgApiKeyStore(pool, logger=_LOG, track_last_used=False)
    plaintext, record = await keys.mint(user_id="acct-1", note="生产")
    assert plaintext.startswith("sk-")
    assert record.key_prefix == plaintext[:12]
    # Both display fragments are stored at mint time — the tail can never be
    # recovered afterwards, because the plaintext only exists here.
    assert record.key_suffix == plaintext[-4:]

    principal = await keys.lookup(plaintext)
    assert (principal.user_id, principal.key_prefix) == ("acct-1", record.key_prefix)


async def test_a_pre_existing_row_lists_with_an_empty_suffix(pool, acct):
    """Rows written before the column existed default to '', not NULL.

    ``list_keys`` reads the field straight into a ``str``; a NULL there would
    put the string "None" in the console's key column.
    """
    keys = PgApiKeyStore(pool, logger=_LOG, track_last_used=False)
    await pool.execute(
        "INSERT INTO api_keys (key_hash, account_id, key_prefix) VALUES ($1,$2,$3)",
        "hash-from-before", "acct-1", "sk-XbgqH8R8r")
    assert (await keys.list_keys(user_id="acct-1"))[0].key_suffix == ""


async def test_renaming_is_scoped_to_the_owning_account(pool, accounts, acct):
    await accounts.create(account_id="acct-2", registered_name="别家")
    keys = PgApiKeyStore(pool, logger=_LOG, track_last_used=False)
    _, record = await keys.mint(user_id="acct-1", note="原名")

    # A key_prefix appears in 详单 rows and support tickets, so seeing one must
    # not be enough to relabel someone's production key.
    assert await keys.rename_for_user(record.key_prefix, "acct-2", "pwned") is False
    assert (await keys.list_keys(user_id="acct-1"))[0].note == "原名"

    assert await keys.rename_for_user(record.key_prefix, "acct-1", "生产环境") is True
    assert (await keys.list_keys(user_id="acct-1"))[0].note == "生产环境"


async def test_a_revoked_key_cannot_be_renamed(pool, acct):
    keys = PgApiKeyStore(pool, logger=_LOG, track_last_used=False)
    _, record = await keys.mint(user_id="acct-1", note="原名")
    await keys.revoke(record.key_prefix)
    assert await keys.rename_for_user(record.key_prefix, "acct-1", "x") is False


async def test_unknown_key_looks_up_to_none(pool, acct):
    assert await PgApiKeyStore(pool, logger=_LOG).lookup("sk-nonexistent") is None


async def test_revoked_key_stops_authenticating(pool, acct):
    keys = PgApiKeyStore(pool, logger=_LOG, track_last_used=False)
    plaintext, record = await keys.mint(user_id="acct-1")
    await keys.lookup(plaintext)                       # warm the cache
    assert await keys.revoke(record.key_prefix) is True
    assert await keys.lookup(plaintext) is None        # revoke cleared it
    assert await keys.revoke(record.key_prefix) is False


async def test_a_key_cannot_be_revoked_by_another_account(pool, accounts, acct):
    await accounts.create(account_id="acct-2", registered_name="别家")
    keys = PgApiKeyStore(pool, logger=_LOG, track_last_used=False)
    plaintext, record = await keys.mint(user_id="acct-1")

    assert await keys.revoke_for_user(record.key_prefix, "acct-2") is False
    assert await keys.lookup(plaintext) is not None     # still live
    assert await keys.revoke_for_user(record.key_prefix, "acct-1") is True


async def test_key_listing_and_active_count(pool, accounts, acct):
    await accounts.create(account_id="acct-2", registered_name="别家")
    keys = PgApiKeyStore(pool, logger=_LOG, track_last_used=False)
    _, first = await keys.mint(user_id="acct-1", note="a")
    await keys.mint(user_id="acct-1", note="b")
    await keys.mint(user_id="acct-2", note="c")

    assert await keys.count_active("acct-1") == 2
    assert {r.note for r in await keys.list_keys(user_id="acct-1")} == {"a", "b"}
    assert len(await keys.list_keys()) == 3

    await keys.revoke(first.key_prefix)
    assert await keys.count_active("acct-1") == 1
    revoked = [r for r in await keys.list_keys(user_id="acct-1") if not r.is_active]
    assert revoked[0].revoked_at   # stamped, so the console can show when


async def test_duplicate_key_prefix_is_impossible(pool, acct):
    """54-bit prefixes make this astronomically unlikely — and a collision would
    let one revocation hit two accounts, so the database refuses it outright."""
    keys = PgApiKeyStore(pool, logger=_LOG)
    plaintext, record = await keys.mint(user_id="acct-1")
    with pytest.raises(Exception) as excinfo:
        await pool.execute(
            "INSERT INTO api_keys (key_hash, account_id, key_prefix) VALUES ($1,$2,$3)",
            "some-other-hash", "acct-1", record.key_prefix)
    assert "api_keys_prefix_uniq" in str(excinfo.value)


async def test_key_requires_an_existing_account(pool):
    with pytest.raises(Exception) as excinfo:
        await pool.execute(
            "INSERT INTO api_keys (key_hash, account_id, key_prefix) VALUES ($1,$2,$3)",
            "h", "ghost-account", "sk-ghost0000")
    assert "foreign key" in str(excinfo.value).lower()


# -- contact index -------------------------------------------------------


async def test_first_claim_wins_and_the_second_loses(pool, accounts, acct):
    await accounts.create(account_id="acct-2", registered_name="别家")
    index = PgAccountIndexStore(pool, logger=_LOG)

    assert await index.claim(INDEX_KIND_PHONE, "13800138000", "acct-1") is True
    assert await index.claim(INDEX_KIND_PHONE, "13800138000", "acct-2") is False
    assert await index.lookup(INDEX_KIND_PHONE, "13800138000") == "acct-1"


async def test_kinds_are_folded_to_the_schemas_lowercase(pool, acct):
    index = PgAccountIndexStore(pool, logger=_LOG)
    await index.claim(INDEX_KIND_EMAIL, "a@b.com", "acct-1")
    assert await index.lookup("email", "a@b.com") == "acct-1"
    stored = await pool.fetchval("SELECT kind FROM account_identities LIMIT 1")
    assert stored == "email"


async def test_same_value_under_two_kinds_is_two_rows(pool, acct):
    index = PgAccountIndexStore(pool, logger=_LOG)
    assert await index.claim(INDEX_KIND_PHONE, "12345", "acct-1") is True
    assert await index.claim(INDEX_KIND_EMAIL, "12345", "acct-1") is True


async def test_empty_contact_touches_no_storage(pool, acct):
    index = PgAccountIndexStore(pool, logger=_LOG)
    assert await index.claim(INDEX_KIND_PHONE, "", "acct-1") is False
    assert await index.lookup(INDEX_KIND_PHONE, "") is None
    assert await pool.fetchval("SELECT count(*) FROM account_identities") == 0


async def test_unclaimed_contact_looks_up_to_none(pool):
    assert await PgAccountIndexStore(pool, logger=_LOG).lookup(
        INDEX_KIND_PHONE, "13900000000") is None


async def test_all_of_an_accounts_contacts(pool, acct):
    index = PgAccountIndexStore(pool, logger=_LOG)
    await index.claim(INDEX_KIND_PHONE, "13800138000", "acct-1")
    await index.claim(INDEX_KIND_EMAIL, "a@b.com", "acct-1")
    contacts = await index.list_for_account("acct-1")
    assert [(c["kind"], c["value"]) for c in contacts] == [
        ("email", "a@b.com"), ("phone", "13800138000")]


# -- surviving a database outage -----------------------------------------


class _Flaky:
    """Wraps the pool and can be told to start failing, like a failover would."""

    def __init__(self, pool):
        self._pool = pool
        self.down = False

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            if self.down:
                from EdennCode.Deployment.billing.stores import (
                    BillingStoreUnavailable,
                )
                raise BillingStoreUnavailable("connection refused")
            return await getattr(self._pool, name)(*args, **kwargs)

        return call


async def test_a_recently_valid_key_survives_a_database_outage(pool, acct):
    """Auth used to be independent of Postgres. Moving it there must not turn a
    maintenance window into 401s for paying customers."""
    clock = {"t": 1000.0}
    flaky = _Flaky(pool)
    keys = PgApiKeyStore(flaky, logger=_LOG, track_last_used=False,
                         clock=lambda: clock["t"], cache_ttl_s=60,
                         stale_grace_s=900)
    plaintext, _ = await keys.mint(user_id="acct-1")
    assert (await keys.lookup(plaintext)).user_id == "acct-1"

    flaky.down = True
    clock["t"] += 120          # past the 60 s cache, inside the 900 s grace
    assert (await keys.lookup(plaintext)).user_id == "acct-1"


async def test_the_grace_window_expires(pool, acct):
    clock = {"t": 1000.0}
    flaky = _Flaky(pool)
    keys = PgApiKeyStore(flaky, logger=_LOG, track_last_used=False,
                         clock=lambda: clock["t"], cache_ttl_s=60,
                         stale_grace_s=900)
    plaintext, _ = await keys.mint(user_id="acct-1")
    await keys.lookup(plaintext)

    flaky.down = True
    clock["t"] += 1200         # past 60 + 900
    with pytest.raises(KeyStoreUnavailable):
        await keys.lookup(plaintext)


async def test_an_unknown_key_is_never_admitted_by_the_grace_window(pool, acct):
    """A cached miss is not a licence to admit a key nobody has ever seen."""
    clock = {"t": 1000.0}
    flaky = _Flaky(pool)
    keys = PgApiKeyStore(flaky, logger=_LOG, track_last_used=False,
                         clock=lambda: clock["t"], stale_grace_s=900)
    assert await keys.lookup("sk-never-existed") is None   # caches the miss

    flaky.down = True
    clock["t"] += 30
    with pytest.raises(KeyStoreUnavailable):
        await keys.lookup("sk-never-existed")


async def test_a_key_never_seen_before_still_fails_closed(pool, acct):
    flaky = _Flaky(pool)
    keys = PgApiKeyStore(flaky, logger=_LOG, track_last_used=False)
    flaky.down = True
    with pytest.raises(KeyStoreUnavailable):
        await keys.lookup("sk-cold")
