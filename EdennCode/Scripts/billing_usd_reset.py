"""One-time dev-test wallet reset for the USD re-denomination.

Per spec docs/superpowers/specs/2026-07-28-teaser-pricing-usd-wallet-design.md
§3.5: zero every account's (old credit-denominated) balance and grant $10 in
the new USD denomination. Idempotent — the grant txn has a deterministic
RowKey per account; accounts that already carry it are skipped.

Run with API traffic paused (the zero-out reads a listing snapshot; a debit
racing between zero-out and grant would skew that account by the debit).

Usage:
    .venv/bin/python EdennCode/Scripts/billing_usd_reset.py [--dry-run]

Requires the same env as the API: AZURE_STORAGE_* (or connection string) and
AUTH_TABLE_NAMESPACE for the target deployment.
"""
from __future__ import annotations

import argparse
import asyncio
import logging

RESET_TXN_ID = "txn-usdreset-20260728"
RESET_NOTE = "USD re-denomination reset 2026-07-28"
GRANT_MICROS = 100_000_000  # $100 — keeps existing test accounts usable
                            # (~778 video jobs at the standard ¥0.9/job rate).
# list_accounts fetches every entity and then truncates to `limit`, with no
# continuation token — a limit smaller than the account count silently drops
# the tail (those accounts keep a credit-era balance). So ask for effectively
# everything; the check below is a safety net, not a pagination cursor.
LIST_LIMIT = 1_000_000


async def reset_accounts(accounts, txns, *, dry_run: bool,
                         logger: logging.Logger) -> dict:
    summary = {"reset": 0, "skipped": 0, "failed": 0}
    records = await accounts.list_accounts(limit=LIST_LIMIT)
    if len(records) >= LIST_LIMIT:
        logger.warning("account listing hit the %s cap — raise the limit in "
                       "reset_accounts and re-run (already-reset accounts are "
                       "skipped)", LIST_LIMIT)
    for record in records:
        if await txns.txn_exists(record.account_id, RESET_TXN_ID):
            logger.info("skip %s (already reset)", record.account_id)
            summary["skipped"] += 1
            continue
        logger.info("reset %s: balance %s -> %s micros%s",
                    record.account_id, record.balance_micros, GRANT_MICROS,
                    " [dry-run]" if dry_run else "")
        if dry_run:
            summary["reset"] += 1
            continue
        # Zero the balance AND the old-denomination lifetime total together, so
        # the grant below leaves total_recharged at exactly GRANT_MICROS (spec
        # §3.5). Guarding on both fields also self-heals a crash that lands the
        # grant but not its txn: the re-run zeroes what the grant already added
        # instead of stacking a second grant on top.
        if record.balance_micros != 0 or record.total_recharged_micros != 0:
            after_zero = await accounts.adjust_balance(
                record.account_id, -record.balance_micros,
                recharge_micros=-record.total_recharged_micros)
            if after_zero is None:
                logger.warning("failed %s: account disappeared before zero-out",
                               record.account_id)
                summary["failed"] += 1
                continue
            await txns.write_manual(
                record.account_id, -record.balance_micros, after_zero,
                RESET_NOTE + " (zero-out)")
        new_balance = await accounts.adjust_balance(
            record.account_id, GRANT_MICROS, recharge_micros=GRANT_MICROS)
        if new_balance is None:
            logger.warning("failed %s: account disappeared before grant",
                           record.account_id)
            summary["failed"] += 1
            continue
        await txns.write_manual(
            record.account_id, GRANT_MICROS, new_balance, RESET_NOTE,
            txn_id=RESET_TXN_ID)
        summary["reset"] += 1
    return summary


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logger = logging.getLogger("billing_usd_reset")

    from EdennCode.Deployment.billing.stores import AccountStore, WalletTxnStore
    from EdennCode.Deployment.settings import DeploymentSettings

    settings = DeploymentSettings.from_env()
    accounts = AccountStore.from_settings(settings, logger)
    txns = WalletTxnStore.from_settings(settings, logger)
    if accounts is None or txns is None:
        raise SystemExit("Billing storage unavailable — check AZURE_STORAGE_* env.")
    summary = await reset_accounts(accounts, txns, dry_run=args.dry_run,
                                   logger=logger)
    logger.info("done: %s", summary)


if __name__ == "__main__":
    asyncio.run(_main())
