"""Dual-write behavior: reads hit the primary, secondary failures stay silent.

The wrappers are tested against fakes here because what matters is the
*policy* — which store answers, which one merely mirrors, and what happens
when the mirror is down. The end-to-end pass with a real Postgres secondary
lives in ``test_billing_pg_dual_write.py``.
"""
from __future__ import annotations

import logging

import pytest

from EdennCode.Deployment.auth.key_store import ApiKeyRecord
from EdennCode.Deployment.billing.dual_write import (
    DualAccountIndexStore,
    DualAccountStore,
    DualApiKeyStore,
    DualPricingStore,
    DualWalletTxnStore,
    build_usage_mirror,
)
from EdennCode.Deployment.billing.store_factory import resolve_store_mode

pytestmark = pytest.mark.asyncio
_LOG = logging.getLogger("test")


class _Recorder:
    """Records calls; optionally explodes, to stand in for a store that is down."""

    def __init__(self, *, boom: bool = False, result=None) -> None:
        self.calls: list[tuple] = []
        self._boom = boom
        self._result = result

    def _run(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self._boom:
            raise RuntimeError(f"{name}: store down")
        return self._result

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            return self._run(name, *args, **kwargs)

        return call

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


# -- reads never touch the secondary -------------------------------------


async def test_reads_go_to_the_primary_only():
    primary, secondary = _Recorder(result="answer"), _Recorder()
    store = DualAccountStore(primary, secondary, logger=_LOG)

    assert await store.get("acct-1") == "answer"
    await store.list_accounts(limit=5)
    await store.get_balance_cached("acct-1")

    assert secondary.calls == []


async def test_pricing_and_key_reads_also_skip_the_secondary():
    primary, secondary = _Recorder(result="x"), _Recorder()
    assert await DualPricingStore(primary, secondary, logger=_LOG).resolve(
        "video_music", None) == "x"
    assert await DualApiKeyStore(primary, secondary, logger=_LOG).lookup("sk-1") == "x"
    assert await DualWalletTxnStore(primary, secondary, logger=_LOG).list_txns(
        "acct-1") == "x"
    assert secondary.calls == []


# -- a broken secondary must not break the request ------------------------


async def test_a_failing_secondary_does_not_fail_the_write():
    primary = _Recorder(result=500)
    secondary = _Recorder(boom=True)
    store = DualAccountStore(primary, secondary, logger=_LOG)

    assert await store.adjust_balance("acct-1", -100) == 500
    assert store.mirror_failures == 1


async def test_mirror_failures_accumulate_for_monitoring():
    store = DualWalletTxnStore(_Recorder(result="t1"), _Recorder(boom=True),
                               logger=_LOG)
    await store.write_debit("acct-1", "job-a", -1, 0)
    await store.write_manual("acct-1", -1, 0, "note")
    assert store.mirror_failures == 2


async def test_no_secondary_configured_is_simply_single_write():
    store = DualAccountStore(_Recorder(result=1), None, logger=_LOG)
    assert await store.adjust_balance("acct-1", 5) == 1
    assert store.mirror_failures == 0


# -- what gets mirrored, and what deliberately does not -------------------


async def test_the_balance_delta_is_mirrored_not_the_result():
    """Mirroring the resulting balance would pin the copy to a stale number
    the first time a mirror write is lost. Deltas re-converge."""
    primary, secondary = _Recorder(result=999), _Recorder()
    await DualAccountStore(primary, secondary, logger=_LOG).adjust_balance(
        "acct-1", -64286, recharge_micros=0)

    name, args, kwargs = secondary.calls[0]
    assert (name, args) == ("adjust_balance", ("acct-1", -64286))
    assert kwargs == {"recharge_micros": 0}


async def test_a_missing_account_is_not_created_on_the_secondary():
    """adjust_balance returning None means no such account — mirroring it
    would create a divergence out of a no-op."""
    store = DualAccountStore(_Recorder(result=None), _Recorder(), logger=_LOG)
    assert await store.adjust_balance("nope", 5) is None
    assert store.secondary.calls == []


async def test_a_key_is_minted_once_and_adopted_by_the_secondary():
    record = ApiKeyRecord(key_hash="h", user_id="acct-1", key_prefix="sk-abc",
                          note="", created_at="2026-07-31T00:00:00+00:00",
                          revoked_at=None, is_active=True)
    primary = _Recorder(result=("sk-abcdef", record))
    secondary = _Recorder()

    plaintext, minted = await DualApiKeyStore(
        primary, secondary, logger=_LOG).mint(user_id="acct-1")

    assert plaintext == "sk-abcdef"
    # adopt, never mint: a second mint would produce a different key, and the
    # customer holds only one of them.
    assert secondary.names() == ["adopt"]
    assert secondary.calls[0][1] == (minted,)


async def test_a_rename_reaches_both_stores():
    """A label the customer edits has to survive the cutover.

    The wrapper is an explicit method list, not a proxy: a method missing here
    is an AttributeError at request time, not a fallback to the primary. This
    one was missing, so renaming a key 500'd on every deployment running
    dual-write.
    """
    primary, secondary = _Recorder(result=True), _Recorder()
    store = DualApiKeyStore(primary, secondary, logger=_LOG)

    assert await store.rename_for_user("sk-abc", "acct-1", "生产环境") is True
    assert secondary.names() == ["rename_for_user"]
    assert secondary.calls[0][1] == ("sk-abc", "acct-1", "生产环境")


async def test_the_manual_txn_id_is_reused_on_the_secondary():
    """It is the idempotency key — a different one lets the same adjustment
    apply twice after cutover."""
    primary, secondary = _Recorder(result="txn-abc123"), _Recorder()
    await DualWalletTxnStore(primary, secondary, logger=_LOG).write_manual(
        "acct-1", 100, 100, "充值")
    assert secondary.calls[0][1] == ("acct-1", 100, 100, "充值", "txn-abc123")


async def test_a_lost_contact_claim_is_not_mirrored():
    """Claiming independently would let the two stores disagree about who owns
    a phone number — and that decides whose wallet a signup merges into."""
    store = DualAccountIndexStore(_Recorder(result=False), _Recorder(),
                                  logger=_LOG)
    assert await store.claim("PHONE", "13800138000", "acct-2") is False
    assert store.secondary.calls == []


async def test_a_won_contact_claim_is_mirrored():
    store = DualAccountIndexStore(_Recorder(result=True), _Recorder(), logger=_LOG)
    assert await store.claim("PHONE", "13800138000", "acct-1") is True
    assert store.secondary.names() == ["claim"]


async def test_a_profile_update_on_a_missing_account_is_not_mirrored():
    store = DualAccountStore(_Recorder(result=None), _Recorder(), logger=_LOG)
    assert await store.update_profile("nope", {"phone": "1"}) is None
    assert store.secondary.calls == []


async def test_cache_invalidation_reaches_both_stores():
    class _Cache:
        def __init__(self):
            self.cleared = []

        def invalidate_balance_cache(self, account_id):
            self.cleared.append(account_id)

    primary, secondary = _Cache(), _Cache()
    DualAccountStore(primary, secondary, logger=_LOG).invalidate_balance_cache("a")
    assert (primary.cleared, secondary.cleared) == (["a"], ["a"])


# -- the usage mirror ----------------------------------------------------


async def test_usage_mirror_is_none_without_a_ledger():
    assert build_usage_mirror(None, _LOG) is None


async def test_a_rejected_usage_row_is_reported_not_swallowed(caplog):
    class _Ledger:
        async def insert_row(self, row):
            return False, "unknown status 'in_progress'"

    with caplog.at_level(logging.WARNING):
        await build_usage_mirror(_Ledger(), _LOG)({"job_id": "job-x"})
    assert "not mirrored" in caplog.text


async def test_a_broken_usage_mirror_never_raises(caplog):
    class _Ledger:
        async def insert_row(self, row):
            raise RuntimeError("pg down")

    with caplog.at_level(logging.WARNING):
        await build_usage_mirror(_Ledger(), _LOG)({"job_id": "job-x"})
    assert "usage mirror failed" in caplog.text


# -- mode selection ------------------------------------------------------


class _Settings:
    def __init__(self, dsn=None, pg_primary=False):
        self.billing_database_url = dsn
        self.billing_pg_primary = pg_primary


# async only to match the module-level asyncio mark; the function under
# test is synchronous.
async def test_store_mode_is_derived_from_two_settings():
    assert resolve_store_mode(_Settings()) == "storage"
    assert resolve_store_mode(_Settings(dsn="postgres://x")) == "dual"
    assert resolve_store_mode(
        _Settings(dsn="postgres://x", pg_primary=True)) == "pg"
    # The primary flag alone does nothing — without a DSN there is no Postgres
    # to read from, and honoring it would take billing offline.
    assert resolve_store_mode(_Settings(pg_primary=True)) == "storage"
