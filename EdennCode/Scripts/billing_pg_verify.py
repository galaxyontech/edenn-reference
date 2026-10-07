"""Compare Table Storage against the billing Postgres, row by row where it counts.

Run after every backfill pass and once more immediately before the read
cutover. Exits non-zero on any mismatch so it can gate a deployment step.

Counts alone are not enough for money. A balance that copied across as the
wrong number would pass a row count and fail a customer, so accounts are
compared field by field on the two figures that decide what anyone owes:
``balance_micros`` and ``total_recharged_micros``.

The last check has no Table Storage counterpart at all: ``balance_reconciliation``
asserts every account's balance equals the sum of its own transactions. Table
Storage could never check this — the balance and the ledger were separate
writes with nothing tying them together. Non-zero drift here means the two
disagree, and it is the single most important number in this report.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

LIST_LIMIT = 1_000_000
_ANONYMOUS = "__anonymous__"


class Report:
    def __init__(self, logger: logging.Logger) -> None:
        self._log = logger
        self.failures: list[str] = []

    def check(self, label: str, storage: Any, postgres: Any, *,
              exact: bool = True) -> None:
        """``exact`` off means Postgres may hold more (dual-write is running)."""
        ok = storage == postgres if exact else postgres >= storage
        mark = "OK  " if ok else "FAIL"
        comparison = "==" if exact else ">="
        self._log.info("  %s %-28s storage=%-10s pg=%-10s (%s)",
                       mark, label, storage, postgres, comparison)
        if not ok:
            self.failures.append(
                f"{label}: storage={storage} pg={postgres} ({comparison})")

    def fail(self, message: str) -> None:
        self._log.error("  FAIL %s", message)
        self.failures.append(message)


async def verify(*, allow_extra: bool, logger: logging.Logger) -> int:
    from EdennCode.Deployment.auth.key_store import (
        ApiKeyStore, build_auth_table_client,
    )
    from EdennCode.Deployment.auth.usage_recorder import USAGE_TABLE_BASE_NAME
    from EdennCode.Deployment.billing.account_index import (
        ACCOUNT_INDEX_TABLE_BASE_NAME,
    )
    from EdennCode.Deployment.billing.pg_stores import BillingPgPool
    from EdennCode.Deployment.billing.stores import (
        AccountStore, PricingStore, WALLET_TABLE_BASE_NAME,
    )
    from EdennCode.Deployment.settings import DeploymentSettings

    settings = DeploymentSettings.from_env()
    pool = BillingPgPool.from_settings(settings, logger)
    if pool is None:
        logger.error("BILLING_DATABASE_URL is not set.")
        return 2
    namespace = getattr(settings, "auth_table_namespace", "") or ""
    report = Report(logger)

    try:
        # -- accounts: counts, then the two numbers that decide what is owed --
        storage_accounts = await AccountStore.from_settings(
            settings, logger).list_accounts(limit=LIST_LIMIT)
        logger.info("accounts")
        report.check("count", len(storage_accounts),
                     int(await pool.fetchval("SELECT count(*) FROM accounts")),
                     exact=not allow_extra)

        pg_rows = {
            r["account_id"]: r
            for r in await pool.fetch(
                "SELECT account_id, balance_micros, total_recharged_micros FROM accounts")
        }
        drifted = 0
        for record in storage_accounts:
            pg = pg_rows.get(record.account_id)
            if pg is None:
                report.fail(f"account {record.account_id} missing from Postgres")
                continue
            if int(pg["balance_micros"]) != record.balance_micros:
                report.fail(
                    f"account {record.account_id} balance: "
                    f"storage={record.balance_micros} pg={pg['balance_micros']}")
                drifted += 1
            if int(pg["total_recharged_micros"]) != record.total_recharged_micros:
                report.fail(
                    f"account {record.account_id} total_recharged: "
                    f"storage={record.total_recharged_micros} "
                    f"pg={pg['total_recharged_micros']}")
                drifted += 1
        logger.info("  %s balances compared field by field, %d mismatch(es)",
                    len(storage_accounts), drifted)

        # -- api keys ---------------------------------------------------------
        storage_keys = await ApiKeyStore.from_settings(settings, logger).list_keys()
        logger.info("api_keys")
        report.check("count", len(storage_keys),
                     int(await pool.fetchval("SELECT count(*) FROM api_keys")),
                     exact=not allow_extra)
        report.check(
            "active count",
            sum(1 for k in storage_keys if k.is_active),
            int(await pool.fetchval(
                "SELECT count(*) FROM api_keys WHERE is_active")),
            exact=not allow_extra)

        # -- the rest: counts, and sums where the number is money -------------
        for label, table_name, sql_count, sql_sum, storage_sum_field in (
            ("account_identities", ACCOUNT_INDEX_TABLE_BASE_NAME + namespace,
             "SELECT count(*) FROM account_identities", None, None),
            ("price_list", None, "SELECT count(*) FROM price_list", None, None),
            ("wallet_txns", WALLET_TABLE_BASE_NAME + namespace,
             "SELECT count(*) FROM wallet_txns",
             "SELECT COALESCE(sum(abs(amount_micros)),0) FROM wallet_txns",
             "amount_micros"),
            ("usage_ledger", USAGE_TABLE_BASE_NAME + namespace,
             "SELECT count(*) FROM usage_ledger",
             "SELECT COALESCE(sum(billed_amount_micros),0) FROM usage_ledger",
             "billed_amount_micros"),
        ):
            logger.info("%s", label)
            if table_name is None:   # price_list has no separate table client
                storage_rows = [
                    r.__dict__ for r in
                    await PricingStore.from_settings(settings, logger).get_all()
                ]
            else:
                client = build_auth_table_client(settings, table_name, logger)
                storage_rows = [] if client is None else [
                    dict(r) for r in await asyncio.to_thread(
                        lambda c=client: list(c.query_entities("")))
                ]
            report.check("count", len(storage_rows),
                         int(await pool.fetchval(sql_count)),
                         exact=not allow_extra)
            if sql_sum and storage_sum_field:
                storage_total = sum(
                    abs(int(str(r.get(storage_sum_field, "0") or 0)))
                    for r in storage_rows)
                report.check(f"sum({storage_sum_field})", storage_total,
                             int(await pool.fetchval(sql_sum) or 0),
                             exact=not allow_extra)

        # -- the check Table Storage could never do --------------------------
        logger.info("reconciliation (Postgres only)")
        drift = await pool.fetch(
            """SELECT account_id, balance_micros, ledger_sum_micros, drift_micros
                 FROM balance_reconciliation WHERE drift_micros <> 0
                ORDER BY abs(drift_micros) DESC""")
        if drift:
            for row in drift:
                report.fail(
                    f"balance/ledger drift on {row['account_id']}: "
                    f"balance={row['balance_micros']} "
                    f"ledger_sum={row['ledger_sum_micros']} "
                    f"drift={row['drift_micros']}")
        else:
            logger.info("  OK   every balance equals the sum of its transactions")
    finally:
        await pool.close()

    if report.failures:
        logger.error("---- %d mismatch(es) ----", len(report.failures))
        for line in report.failures:
            logger.error("  %s", line)
        return 1
    logger.info("---- all checks passed ----")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-extra", action="store_true",
        help="Postgres may hold MORE rows than Table Storage. Use while "
             "dual-write is live — new rows land in Postgres first.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return asyncio.run(verify(allow_extra=args.allow_extra,
                              logger=logging.getLogger("verify")))


if __name__ == "__main__":
    raise SystemExit(main())
