"""Dual-write wrappers for the Table Storage → Postgres migration.

Every wrapper holds a **primary** and a **secondary** store. Reads go to the
primary alone. Writes go to the primary first and then, best effort, to the
secondary: a secondary failure is logged and counted, never raised. That
asymmetry is the whole design. During the migration the secondary is a copy
being warmed up — letting it break a customer's request would make the
migration more dangerous than the thing it is fixing.

The same wrapper serves both directions, which is what makes the cutover
reversible:

    dual   — primary = Table Storage, secondary = Postgres  (warming up)
    pg     — primary = Postgres,      secondary = Table Storage  (falling back)

Flipping ``BILLING_PG_PRIMARY`` swaps them. Both stores stay complete
throughout, so a rollback after cutover loses nothing.

These wrappers are deliberately explicit rather than a generic proxy: minting a
key, claiming a contact, and adjusting a balance each need different handling
to stay consistent across two stores, and a proxy that forwarded blindly would
get all three wrong.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)


class _Mirror:
    """Shared plumbing: run a secondary write, swallow and count its failures."""

    def __init__(self, primary: Any, secondary: Any, *,
                 label: str, logger: Optional[logging.Logger] = None) -> None:
        self.primary = primary
        self.secondary = secondary
        self._label = label
        self._log = logger or _LOGGER
        self.mirror_failures = 0

    async def _mirror(self, coro_factory, what: str) -> Any:
        if self.secondary is None:
            return None
        try:
            return await coro_factory()
        except Exception:  # noqa: BLE001 - the copy must never break the request
            self.mirror_failures += 1
            self._log.warning("dual-write: %s %s failed on the secondary store",
                              self._label, what, exc_info=True)
            return None


class DualAccountStore(_Mirror):
    """Balance deltas are mirrored as deltas, not as absolute values.

    Mirroring the resulting balance instead would make the copy diverge
    permanently the first time a mirror write is lost: the primary would keep
    counting from the true balance while the secondary was pinned to a stale
    one. Deltas re-converge.
    """

    def __init__(self, primary, secondary, *, logger=None) -> None:
        super().__init__(primary, secondary, label="account", logger=logger)

    async def create(self, **kwargs) -> Any:
        record = await self.primary.create(**kwargs)
        await self._mirror(lambda: self.secondary.create(**kwargs), "create")
        return record

    async def get(self, account_id: str) -> Any:
        return await self.primary.get(account_id)

    async def list_accounts(self, *, limit: int = 200) -> Any:
        return await self.primary.list_accounts(limit=limit)

    async def get_balance_cached(self, account_id: str) -> Any:
        return await self.primary.get_balance_cached(account_id)

    def invalidate_balance_cache(self, account_id: str) -> None:
        self.primary.invalidate_balance_cache(account_id)
        if self.secondary is not None:
            self.secondary.invalidate_balance_cache(account_id)

    async def update_profile(self, account_id: str, updates: dict) -> Any:
        record = await self.primary.update_profile(account_id, updates)
        if record is not None:
            await self._mirror(
                lambda: self.secondary.update_profile(account_id, updates),
                "update_profile")
        return record

    async def adjust_balance(self, account_id: str, delta_micros: int, *,
                             recharge_micros: int = 0, **kwargs) -> Any:
        new_balance = await self.primary.adjust_balance(
            account_id, delta_micros, recharge_micros=recharge_micros, **kwargs)
        if new_balance is not None:
            await self._mirror(
                lambda: self.secondary.adjust_balance(
                    account_id, delta_micros, recharge_micros=recharge_micros),
                "adjust_balance")
        return new_balance


class DualWalletTxnStore(_Mirror):
    def __init__(self, primary, secondary, *, logger=None) -> None:
        super().__init__(primary, secondary, label="wallet", logger=logger)

    async def txn_exists(self, account_id: str, txn_id: str) -> bool:
        return await self.primary.txn_exists(account_id, txn_id)

    async def debit_exists(self, account_id: str, job_id: str) -> bool:
        return await self.primary.debit_exists(account_id, job_id)

    async def write_debit(self, account_id: str, job_id: str,
                          amount_micros: int, balance_after_micros: int) -> None:
        await self.primary.write_debit(account_id, job_id, amount_micros,
                                       balance_after_micros)
        await self._mirror(
            lambda: self.secondary.write_debit(account_id, job_id, amount_micros,
                                               balance_after_micros),
            "write_debit")

    async def write_manual(self, account_id: str, amount_micros: int,
                           balance_after_micros: int, note: str,
                           txn_id: Optional[str] = None, **kwargs) -> str:
        txn_id = await self.primary.write_manual(
            account_id, amount_micros, balance_after_micros, note, txn_id)
        # Same txn_id on both sides — it is the idempotency key, so a differing
        # one would let the same adjustment be applied twice after cutover.
        await self._mirror(
            lambda: self.secondary.write_manual(
                account_id, amount_micros, balance_after_micros, note, txn_id),
            "write_manual")
        return txn_id

    async def list_txns(self, account_id: str, **kwargs) -> Any:
        return await self.primary.list_txns(account_id, **kwargs)


class DualPricingStore(_Mirror):
    def __init__(self, primary, secondary, *, logger=None) -> None:
        super().__init__(primary, secondary, label="pricing", logger=logger)

    async def resolve(self, product: str, model_spec: Optional[str]) -> Any:
        return await self.primary.resolve(product, model_spec)

    async def get_all(self) -> Any:
        return await self.primary.get_all()

    async def upsert(self, **kwargs) -> Any:
        record = await self.primary.upsert(**kwargs)
        await self._mirror(lambda: self.secondary.upsert(**kwargs), "upsert")
        return record

    async def delete(self, price_key: str) -> bool:
        deleted = await self.primary.delete(price_key)
        await self._mirror(lambda: self.secondary.delete(price_key), "delete")
        return deleted


class DualApiKeyStore(_Mirror):
    """Keys are minted once and *adopted* by the secondary.

    Calling ``mint`` on both would generate two different keys and hand the
    customer one that only half the system knows about — which, after the
    cutover flips, is a key that stops working.
    """

    def __init__(self, primary, secondary, *, logger=None) -> None:
        super().__init__(primary, secondary, label="apikey", logger=logger)

    async def lookup(self, presented_key: str) -> Any:
        return await self.primary.lookup(presented_key)

    async def mint(self, *, user_id: str, note: str = "", **kwargs) -> Any:
        plaintext, record = await self.primary.mint(user_id=user_id, note=note,
                                                    **kwargs)
        await self._mirror(lambda: self.secondary.adopt(record), "mint")
        return plaintext, record

    async def revoke(self, key_prefix: str) -> bool:
        revoked = await self.primary.revoke(key_prefix)
        await self._mirror(lambda: self.secondary.revoke(key_prefix), "revoke")
        return revoked

    async def revoke_for_user(self, key_prefix: str, user_id: str) -> bool:
        revoked = await self.primary.revoke_for_user(key_prefix, user_id)
        await self._mirror(
            lambda: self.secondary.revoke_for_user(key_prefix, user_id),
            "revoke_for_user")
        return revoked

    async def rename_for_user(self, key_prefix: str, user_id: str,
                              name: str) -> bool:
        renamed = await self.primary.rename_for_user(key_prefix, user_id, name)
        await self._mirror(
            lambda: self.secondary.rename_for_user(key_prefix, user_id, name),
            "rename_for_user")
        return renamed

    async def count_active(self, user_id: str) -> int:
        return await self.primary.count_active(user_id)

    async def list_keys(self, *, user_id: Optional[str] = None) -> Any:
        return await self.primary.list_keys(user_id=user_id)

    async def drain(self) -> None:
        await self.primary.drain()
        if self.secondary is not None and hasattr(self.secondary, "drain"):
            await self.secondary.drain()


class DualAccountIndexStore(_Mirror):
    """The secondary only mirrors claims the primary already won.

    Claiming independently would let the two stores disagree about who owns a
    phone number — and that disagreement decides whose wallet a signup merges
    into.
    """

    def __init__(self, primary, secondary, *, logger=None) -> None:
        super().__init__(primary, secondary, label="index", logger=logger)

    async def lookup(self, kind: str, normalized_value: str) -> Any:
        return await self.primary.lookup(kind, normalized_value)

    async def claim(self, kind: str, normalized_value: str,
                    account_id: str) -> bool:
        won = await self.primary.claim(kind, normalized_value, account_id)
        if won:
            await self._mirror(
                lambda: self.secondary.claim(kind, normalized_value, account_id),
                "claim")
        return won


def build_usage_mirror(ledger: Any, logger: logging.Logger):
    """A ``mirror`` callable for ``UsageRecorder``: copies each row into Postgres.

    Returns None when there is no ledger, which is how the recorder tells that
    dual-write is off. Rows the ledger refuses (a CHECK it would violate) are
    logged rather than dropped quietly — during dual-write that is the only
    signal that live data does not fit the new schema, and it is far better to
    learn that now than during the cutover.
    """
    if ledger is None:
        return None

    async def mirror(row: dict) -> None:
        try:
            _, skip = await ledger.insert_row(row)
            if skip:
                logger.warning(
                    "dual-write: usage row for job %s not mirrored (%s)",
                    row.get("job_id"), skip)
        except Exception:  # noqa: BLE001 - the copy must never break recording
            logger.warning("dual-write: usage mirror failed for job %s",
                           row.get("job_id"), exc_info=True)

    return mirror


__all__ = [
    "DualAccountIndexStore",
    "DualAccountStore",
    "DualApiKeyStore",
    "DualPricingStore",
    "DualWalletTxnStore",
    "build_usage_mirror",
]
