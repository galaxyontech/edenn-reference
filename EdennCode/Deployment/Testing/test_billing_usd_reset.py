"""USD reset script: grants $10 exactly once per account."""
from __future__ import annotations

import asyncio
import logging

from EdennCode.Deployment.billing.stores import AccountStore, WalletTxnStore
from EdennCode.Deployment.Testing.test_billing_stores import FakeBillingTable

import EdennCode.Scripts.billing_usd_reset as usd_reset
from EdennCode.Scripts.billing_usd_reset import GRANT_MICROS, RESET_TXN_ID, reset_accounts

_LOG = logging.getLogger("t")


def _stores(txn_table=None):
    """AccountStore + WalletTxnStore over fresh fakes (txn table injectable)."""
    return (AccountStore(FakeBillingTable(), logger=_LOG),
            WalletTxnStore(txn_table or FakeBillingTable(), logger=_LOG))


def _zero_out_txns(txns, account_id: str) -> list[dict]:
    rows, _ = asyncio.run(txns.list_txns(account_id))
    return [r for r in rows if r["RowKey"] != RESET_TXN_ID]


def test_reset_zeroes_grants_and_is_idempotent():
    accounts, txns = _stores()
    asyncio.run(accounts.create(account_id="a1", registered_name="N"))
    asyncio.run(accounts.adjust_balance("a1", 123_000_000))     # old credits
    asyncio.run(accounts.create(account_id="a2", registered_name="N"))

    summary = asyncio.run(reset_accounts(
        accounts, txns, dry_run=False, logger=_LOG))
    assert summary == {"reset": 2, "skipped": 0, "failed": 0}
    for account_id in ("a1", "a2"):
        record = asyncio.run(accounts.get(account_id))
        assert record.balance_micros == GRANT_MICROS
        assert record.total_recharged_micros == GRANT_MICROS
        assert asyncio.run(txns.txn_exists(account_id, RESET_TXN_ID))

    zero_outs = _zero_out_txns(txns, "a1")
    assert len(zero_outs) == 1
    assert zero_outs[0]["note"] == "USD re-denomination reset 2026-07-28 (zero-out)"
    assert zero_outs[0]["amount_micros"] == str(-123_000_000)

    again = asyncio.run(reset_accounts(
        accounts, txns, dry_run=False, logger=_LOG))
    assert again == {"reset": 0, "skipped": 2, "failed": 0}
    record = asyncio.run(accounts.get("a1"))
    assert record.balance_micros == GRANT_MICROS       # not doubled
    assert record.total_recharged_micros == GRANT_MICROS


def test_reset_clears_prior_lifetime_recharge():
    """A real prior recharge must be replaced by the grant, not added to it."""
    accounts, txns = _stores()
    asyncio.run(accounts.create(account_id="a3", registered_name="N"))
    asyncio.run(accounts.adjust_balance("a3", 50_000_000,
                                        recharge_micros=50_000_000))

    summary = asyncio.run(reset_accounts(
        accounts, txns, dry_run=False, logger=_LOG))
    assert summary == {"reset": 1, "skipped": 0, "failed": 0}

    record = asyncio.run(accounts.get("a3"))
    assert record.balance_micros == GRANT_MICROS
    assert record.total_recharged_micros == GRANT_MICROS   # not 60_000_000


def test_reset_clears_lifetime_recharge_on_zero_balance_account():
    """Spent-to-zero account still needs its lifetime total re-denominated."""
    accounts, txns = _stores()
    asyncio.run(accounts.create(account_id="a4", registered_name="N"))
    asyncio.run(accounts.adjust_balance("a4", 50_000_000,
                                        recharge_micros=50_000_000))
    asyncio.run(accounts.adjust_balance("a4", -50_000_000))   # spent it all

    asyncio.run(reset_accounts(accounts, txns, dry_run=False, logger=_LOG))

    record = asyncio.run(accounts.get("a4"))
    assert record.balance_micros == GRANT_MICROS
    assert record.total_recharged_micros == GRANT_MICROS   # not 60_000_000


def test_rerun_after_crash_between_grant_and_txn_does_not_double():
    """Crash window: grant applied, grant txn never written -> re-run heals."""
    txn_table = FakeBillingTable()
    accounts, txns = _stores(txn_table)
    asyncio.run(accounts.create(account_id="a5", registered_name="N"))
    asyncio.run(accounts.adjust_balance("a5", 7_000_000))

    asyncio.run(reset_accounts(accounts, txns, dry_run=False, logger=_LOG))
    # Simulate the crash: balance + lifetime total already moved, txn missing.
    txn_table.delete_entity("a5", RESET_TXN_ID)
    assert not asyncio.run(txns.txn_exists("a5", RESET_TXN_ID))

    summary = asyncio.run(reset_accounts(
        accounts, txns, dry_run=False, logger=_LOG))
    assert summary == {"reset": 1, "skipped": 0, "failed": 0}

    record = asyncio.run(accounts.get("a5"))
    assert record.balance_micros == GRANT_MICROS           # not 20_000_000
    assert record.total_recharged_micros == GRANT_MICROS   # not 20_000_000
    assert asyncio.run(txns.txn_exists("a5", RESET_TXN_ID))


def test_missing_account_counts_as_failed():
    """Account deleted mid-run: warn, count as failed, keep going."""
    accounts, txns = _stores()
    asyncio.run(accounts.create(account_id="a6", registered_name="N"))
    asyncio.run(accounts.adjust_balance("a6", 5_000_000))
    asyncio.run(accounts.create(account_id="a7", registered_name="N"))

    original_adjust = accounts.adjust_balance

    async def _adjust(account_id, delta_micros, **kwargs):
        if account_id == "a6":
            return None      # vanished between listing and write
        return await original_adjust(account_id, delta_micros, **kwargs)

    accounts.adjust_balance = _adjust
    summary = asyncio.run(reset_accounts(
        accounts, txns, dry_run=False, logger=_LOG))
    accounts.adjust_balance = original_adjust

    assert summary == {"reset": 1, "skipped": 0, "failed": 1}
    assert not asyncio.run(txns.txn_exists("a6", RESET_TXN_ID))
    assert asyncio.run(txns.txn_exists("a7", RESET_TXN_ID))


def test_dry_run_touches_nothing():
    accounts, txns = _stores()
    asyncio.run(accounts.create(account_id="a1", registered_name="N"))
    asyncio.run(accounts.adjust_balance("a1", 42_000_000,
                                        recharge_micros=42_000_000))
    summary = asyncio.run(reset_accounts(
        accounts, txns, dry_run=True, logger=_LOG))
    assert summary == {"reset": 1, "skipped": 0, "failed": 0}
    record = asyncio.run(accounts.get("a1"))
    assert record.balance_micros == 42_000_000
    assert record.total_recharged_micros == 42_000_000
    assert not asyncio.run(txns.txn_exists("a1", RESET_TXN_ID))
    assert _zero_out_txns(txns, "a1") == []


def test_listing_cap_warns(caplog, monkeypatch):
    """A full page means accounts may be missing — the run must say so.

    ``list_accounts`` truncates without a continuation token, so the limit must
    stay big enough to be a no-op on any real deployment; the warning is only a
    safety net telling the operator to raise it.
    """
    assert usd_reset.LIST_LIMIT >= 1_000_000
    accounts, txns = _stores()
    monkeypatch.setattr(usd_reset, "LIST_LIMIT", 3)
    seen_limits = []

    async def _list_accounts(*, limit):
        seen_limits.append(limit)
        return []

    accounts.list_accounts = _list_accounts
    with caplog.at_level(logging.WARNING, logger="t"):
        asyncio.run(reset_accounts(accounts, txns, dry_run=True, logger=_LOG))
    assert seen_limits == [3]           # the module constant is what's requested
    assert "cap" not in caplog.text

    class _Rec:
        account_id = "x"
        balance_micros = 0
        total_recharged_micros = 0

    async def _list_full(*, limit):
        return [_Rec() for _ in range(limit)]

    accounts.list_accounts = _list_full
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="t"):
        asyncio.run(reset_accounts(accounts, txns, dry_run=True, logger=_LOG))
    assert "hit the 3 cap" in caplog.text
    assert "raise the limit in reset_accounts and re-run" in caplog.text
