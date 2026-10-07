"""Copy the billing tables from Azure Table Storage into the billing Postgres.

Idempotent by construction — every table has either a natural primary key or,
for the usage ledger, a ``source_row_key`` unique index (migration 005). Run it
as many times as you need: a re-run inserts only what is missing. That matters
because the last pass happens with dual-write already live, when truncating and
starting over is not an option.

Order is not arbitrary. ``accounts`` has to land before anything that
references it — api_keys, account_identities, wallet_txns and usage_ledger all
carry a foreign key to it.

Three translations happen on the way across, and each one is a place where a
silent mistake would be expensive:

* **Contact kinds fold to lowercase.** Table Storage used ``PHONE``/``EMAIL``
  partition keys; the Postgres CHECK constraint accepts only lowercase.
* **Debit amounts are forced negative.** ``balance_reconciliation`` sums this
  column against the balance, so one debit stored positive turns a whole
  account's reconciliation into a false alarm.
* **Keys and contacts whose account is missing get a placeholder account.**
  In Table Storage nothing enforced that link, so such rows exist and their
  keys authenticate today. Dropping them would silently lock a customer out at
  cutover; the placeholder keeps auth working and the summary names every one.

Usage:
    .venv/bin/python EdennCode/Scripts/billing_pg_backfill.py --dry-run
    .venv/bin/python EdennCode/Scripts/billing_pg_backfill.py

Needs the API's Table Storage env (AZURE_STORAGE_* / AUTH_TABLE_NAMESPACE) plus
BILLING_DATABASE_URL for the destination.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

# Table Storage listings fetch everything and then slice; a cap smaller than the
# row count would silently drop the tail. Ask for effectively everything.
LIST_LIMIT = 1_000_000

_ANONYMOUS = "__anonymous__"
_VALID_STATUS = {"completed", "failed", "canceled"}


def _parse_ts(raw: Any) -> Optional[datetime]:
    """ISO string -> aware datetime. Naive values are read as UTC."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _int(raw: Any, default: int = 0) -> int:
    try:
        return int(str(raw or default) or default)
    except (TypeError, ValueError):
        return default


def _micros_from_usd(raw: Any) -> Optional[int]:
    from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not value.is_finite():
        return None
    return int((value * 1_000_000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _query_all(table_client: Any, filter_: str = "") -> list[dict]:
    return [dict(r) for r in table_client.query_entities(filter_ or "")]


class Backfill:
    def __init__(self, pool, *, dry_run: bool, logger: logging.Logger) -> None:
        self._pool = pool
        self._dry_run = dry_run
        self._log = logger
        self.summary: dict[str, dict[str, int]] = {}
        self.placeholders: list[str] = []
        self.skipped: list[str] = []

    def _count(self, table: str, key: str, n: int = 1) -> None:
        self.summary.setdefault(table, {}).setdefault(key, 0)
        self.summary[table][key] += n

    async def _execute(self, sql: str, *args) -> bool:
        """True when a row landed. Dry-run reports intent without writing."""
        if self._dry_run:
            return True
        result = await self._pool.execute(sql, *args)
        return not str(result).endswith(" 0")

    # -- accounts ---------------------------------------------------------

    async def accounts(self, store) -> set[str]:
        records = await store.list_accounts(limit=LIST_LIMIT)
        if len(records) >= LIST_LIMIT:
            raise SystemExit(
                f"account listing hit the {LIST_LIMIT} cap — raise LIST_LIMIT")
        known: set[str] = set()
        for r in records:
            known.add(r.account_id)
            created = _parse_ts(r.created_at) or datetime.now(timezone.utc)
            landed = await self._execute(
                """INSERT INTO accounts (account_id, entity_type, registered_name,
                        id_number, address, email, phone, note, balance_micros,
                        total_recharged_micros, is_active, created_at,
                        created_via, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,'migration',$13)
                   ON CONFLICT (account_id) DO NOTHING""",
                r.account_id, r.entity_type, r.registered_name, r.id_number,
                r.address, r.email, r.phone, r.note, r.balance_micros,
                r.total_recharged_micros, r.is_active, created,
                _parse_ts(r.updated_at) or created,
            )
            self._count("accounts", "inserted" if landed else "already_present")
        return known

    async def _ensure_account(self, account_id: str, known: set[str], why: str) -> bool:
        """Placeholder for an account Table Storage never had. See module docstring."""
        if account_id in known:
            return True
        self._log.warning(
            "placeholder account '%s' created — referenced by %s but no account "
            "row existed in Table Storage", account_id, why)
        await self._execute(
            """INSERT INTO accounts (account_id, registered_name, note,
                                     created_via, is_active)
               VALUES ($1,$1,$2,'migration',true)
               ON CONFLICT (account_id) DO NOTHING""",
            account_id,
            f"迁移时自动补建:表存储里有{why}但没有账户行。余额 0,需人工核对。",
        )
        known.add(account_id)
        self.placeholders.append(f"{account_id} ({why})")
        self._count("accounts", "placeholder")
        return True

    # -- api keys ---------------------------------------------------------

    async def api_keys(self, store, known: set[str]) -> None:
        for record in await store.list_keys():
            if not record.user_id:
                self.skipped.append(f"api_key {record.key_prefix}: no user_id")
                self._count("api_keys", "skipped")
                continue
            await self._ensure_account(record.user_id, known,
                                       f"API 密钥 {record.key_prefix}")
            created = _parse_ts(record.created_at) or datetime.now(timezone.utc)
            landed = await self._execute(
                """INSERT INTO api_keys (key_hash, account_id, key_prefix,
                        key_suffix, note, created_at, created_via, revoked_at,
                        last_used_at, is_active)
                   VALUES ($1,$2,$3,$4,$5,$6,'migration',$7,$8,$9)
                   ON CONFLICT (key_hash) DO NOTHING""",
                record.key_hash, record.user_id, record.key_prefix,
                # Empty for anything minted before the column existed; the
                # plaintext is long gone, so there is nothing to recover.
                record.key_suffix, record.note,
                created, _parse_ts(record.revoked_at),
                _parse_ts(record.last_used_at), record.is_active,
            )
            self._count("api_keys", "inserted" if landed else "already_present")

    # -- contact index ----------------------------------------------------

    async def identities(self, index_table: Any, known: set[str]) -> None:
        if index_table is None:
            self._log.info("no account index table — skipping identities")
            return
        for entity in await asyncio.to_thread(_query_all, index_table):
            # Lowercase: the Postgres CHECK accepts only phone/email/firebase.
            kind = str(entity.get("PartitionKey", "")).strip().lower()
            value = str(entity.get("value", "")).strip()
            account_id = str(entity.get("account_id", "")).strip()
            if not (kind and value and account_id):
                self.skipped.append(f"identity {kind}/{value or '<empty>'}: incomplete")
                self._count("account_identities", "skipped")
                continue
            await self._ensure_account(account_id, known, f"身份索引 {kind}")
            landed = await self._execute(
                """INSERT INTO account_identities (kind, value, account_id, created_at)
                   VALUES ($1,$2,$3,$4) ON CONFLICT (kind, value) DO NOTHING""",
                kind, value, account_id,
                _parse_ts(entity.get("created_at")) or datetime.now(timezone.utc),
            )
            self._count("account_identities",
                        "inserted" if landed else "already_present")

    # -- pricing ----------------------------------------------------------

    async def pricing(self, store) -> None:
        for record in await store.get_all():
            landed = await self._execute(
                """INSERT INTO price_list (price_key, billing_mode,
                        unit_price_micros, teaser_unit_price_micros,
                        unit_seconds, min_billable_seconds, note, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                   ON CONFLICT (price_key) DO UPDATE
                       SET billing_mode = EXCLUDED.billing_mode,
                           unit_price_micros = EXCLUDED.unit_price_micros,
                           teaser_unit_price_micros = EXCLUDED.teaser_unit_price_micros,
                           unit_seconds = EXCLUDED.unit_seconds,
                           min_billable_seconds = EXCLUDED.min_billable_seconds,
                           note = EXCLUDED.note,
                           updated_at = EXCLUDED.updated_at""",
                record.price_key, record.billing_mode, record.unit_price_micros,
                record.teaser_unit_price_micros,
                # Metering copied like the price, and for the same reason: a
                # row left at 007's seeded 30-second block while Table Storage
                # says otherwise bills a granularity nobody chose. Rows that
                # predate the columns read as 1/0 — plain per-second billing.
                record.unit_seconds, record.min_billable_seconds, record.note,
                _parse_ts(record.updated_at) or datetime.now(timezone.utc),
            )
            # Prices are the one table that DOES overwrite: Table Storage is
            # authoritative until cutover, and a stale seeded price here would
            # quietly bill everyone wrong.
            self._count("price_list", "upserted" if landed else "unchanged")

    # -- wallet transactions ----------------------------------------------

    async def wallet_txns(self, wallet_table: Any, known: set[str]) -> None:
        for entity in await asyncio.to_thread(_query_all, wallet_table):
            account_id = str(entity.get("PartitionKey", "")).strip()
            idem = str(entity.get("RowKey", "")).strip()
            if not (account_id and idem):
                self._count("wallet_txns", "skipped")
                continue
            await self._ensure_account(account_id, known, "钱包流水")
            txn_type = str(entity.get("txn_type", "")) or "adjustment"
            amount = _int(entity.get("amount_micros"))
            if txn_type == "debit":
                # Reconciliation sums this column — a positive debit breaks it.
                amount = -abs(amount)
            occurred = (_parse_ts(entity.get("timestamp_utc"))
                        or datetime.now(timezone.utc))
            landed = await self._execute(
                """INSERT INTO wallet_txns (account_id, txn_type, amount_micros,
                        balance_after_micros, job_id, idempotency_key, note,
                        created_via, occurred_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,'migration',$8)
                   ON CONFLICT (account_id, idempotency_key) DO NOTHING""",
                account_id, txn_type, amount,
                _int(entity.get("balance_after_micros")),
                str(entity.get("job_id", "")) or None, idem,
                str(entity.get("note", "")), occurred,
            )
            self._count("wallet_txns", "inserted" if landed else "already_present")

    # -- usage ledger -----------------------------------------------------

    async def usage(self, usage_table: Any, known: set[str]) -> None:
        from EdennCode.Deployment.billing.pg_stores import PgUsageLedger

        ledger = PgUsageLedger(self._pool, logger=self._log)
        for entity in await asyncio.to_thread(_query_all, usage_table):
            row_key = str(entity.get("RowKey", "")).strip()
            if not row_key:
                self._count("usage_ledger", "skipped")
                continue
            account_id = await ledger.account_id_for(entity)
            if account_id:
                await self._ensure_account(account_id, known, "用量台账")

            # Same conversion the live dual-write uses — see PgUsageLedger.
            params, skip = ledger.to_params(entity, source_row_key=row_key)
            if skip is not None:
                self.skipped.append(f"usage {row_key}: {skip}")
                self._count("usage_ledger", "skipped")
                continue
            landed = await self._execute(ledger.INSERT_SQL, *params)
            self._count("usage_ledger", "inserted" if landed else "already_present")


async def run(*, dry_run: bool, logger: logging.Logger) -> int:
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
        logger.error("BILLING_DATABASE_URL is not set — nothing to back fill into.")
        return 2

    namespace = getattr(settings, "auth_table_namespace", "") or ""
    accounts = AccountStore.from_settings(settings, logger)
    keys = ApiKeyStore.from_settings(settings, logger)
    pricing = PricingStore.from_settings(settings, logger)
    index_table = build_auth_table_client(
        settings, ACCOUNT_INDEX_TABLE_BASE_NAME + namespace, logger)
    wallet_table = build_auth_table_client(
        settings, WALLET_TABLE_BASE_NAME + namespace, logger)
    usage_table = build_auth_table_client(
        settings, USAGE_TABLE_BASE_NAME + namespace, logger)
    if accounts is None or keys is None:
        logger.error("Table Storage is not configured — nothing to read.")
        return 2

    job = Backfill(pool, dry_run=dry_run, logger=logger)
    try:
        known = await job.accounts(accounts)
        await job.api_keys(keys, known)
        await job.identities(index_table, known)
        await job.pricing(pricing)
        if wallet_table is not None:
            await job.wallet_txns(wallet_table, known)
        if usage_table is not None:
            await job.usage(usage_table, known)
    finally:
        await pool.close()

    logger.info("---- backfill summary%s ----", " (dry run)" if dry_run else "")
    for table in sorted(job.summary):
        counts = ", ".join(f"{k}={v}" for k, v in sorted(job.summary[table].items()))
        logger.info("  %-20s %s", table, counts)
    for line in job.placeholders:
        logger.warning("  placeholder account: %s", line)
    for line in job.skipped:
        logger.warning("  SKIPPED: %s", line)
    if job.skipped:
        logger.warning("%d row(s) skipped — review them before cutover.",
                       len(job.skipped))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be written without writing it")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s")
    return asyncio.run(run(dry_run=args.dry_run,
                           logger=logging.getLogger("backfill")))


if __name__ == "__main__":
    raise SystemExit(main())
