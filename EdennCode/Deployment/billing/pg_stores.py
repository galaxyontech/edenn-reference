"""Postgres implementations of the billing stores (Table Storage replacements).

Every class here is a drop-in for its ``stores.py`` counterpart: same method
names, same dataclasses, same exceptions. That is what lets the cutover be a
constructor swap instead of a rewrite of the routers.

Three things get structurally better in the move, and they are the reason for
the migration rather than incidental to it:

* **Balance updates stop being a CAS loop.** ``UPDATE ... RETURNING`` is one
  atomic statement — no read-modify-write, no ETag, no retry budget to exhaust
  under contention.
* **Debit and its ledger row become one transaction.** In Table Storage they
  were two writes that could interleave or half-fail, and "balance == sum of
  transactions" was a hope. Here it is enforced by rollback.
* **Idempotency moves into the database.** ``UNIQUE (account_id,
  idempotency_key)`` replaces the deterministic-RowKey trick; a duplicate debit
  is now impossible rather than merely unlikely.

Timestamps cross the boundary as ISO-8601 strings because that is what the
existing dataclasses and JSON responses expect. Inside Postgres they are
``TIMESTAMPTZ``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from EdennCode.Deployment.billing.pricing_resolver import ContractPrice, DiscountTier
from EdennCode.Deployment.billing.stores import (
    AccountExists,
    AccountRecord,
    BalanceSnapshot,
    BillingStoreUnavailable,
    PricingRecord,
)

_LOGGER = logging.getLogger(__name__)


def _iso(value: Any) -> str:
    """TIMESTAMPTZ -> ISO string; '' for NULL. Naive values are read as UTC."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


class BillingPgPool:
    """Lazily-opened asyncpg pool for the billing database.

    Lazy because boot must not depend on the database being reachable — the
    same rule the Table Storage clients follow. The first query opens the pool;
    if that fails, the caller sees ``BillingStoreUnavailable`` like any other
    storage outage rather than a crashed process.
    """

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        command_timeout: float = 10.0,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._command_timeout = command_timeout
        self._logger = logger or _LOGGER
        self._pool: Any = None
        self._lock = asyncio.Lock()

    @classmethod
    def from_settings(
        cls, settings: Any, logger: logging.Logger
    ) -> Optional["BillingPgPool"]:
        """None when no billing DSN is configured — callers fall back to Table
        Storage. Deliberately does NOT fall back to ``DATABASE_URL``: that is
        the telemetry database, and silently writing the billing ledger into it
        would be far worse than not writing it at all."""
        dsn = (
            getattr(settings, "billing_database_url", None)
            or os.getenv("BILLING_DATABASE_URL", "")
        ).strip()
        if not dsn:
            return None
        logger.info("BillingPgPool configured (BILLING_DATABASE_URL present)")
        return cls(dsn, logger=logger)

    async def acquire(self):
        return (await self._ensure_pool()).acquire()

    async def _ensure_pool(self):
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is not None:
                return self._pool
            try:
                import asyncpg

                self._pool = await asyncpg.create_pool(
                    self._dsn,
                    min_size=self._min_size,
                    max_size=self._max_size,
                    command_timeout=self._command_timeout,
                )
            except Exception as exc:  # noqa: BLE001
                raise BillingStoreUnavailable(
                    f"billing database pool unavailable: {exc}"
                ) from exc
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # -- query helpers -----------------------------------------------------
    #
    # Every asyncpg error becomes BillingStoreUnavailable so callers keep the
    # one exception type they already handle. UniqueViolationError is the
    # exception to the exception — idempotency depends on telling it apart.

    async def fetch(self, sql: str, *args) -> list:
        pool = await self._ensure_pool()
        try:
            async with pool.acquire() as conn:
                return list(await conn.fetch(sql, *args))
        except Exception as exc:  # noqa: BLE001
            raise BillingStoreUnavailable(str(exc)) from exc

    async def fetchrow(self, sql: str, *args):
        pool = await self._ensure_pool()
        try:
            async with pool.acquire() as conn:
                return await conn.fetchrow(sql, *args)
        except Exception as exc:  # noqa: BLE001
            raise BillingStoreUnavailable(str(exc)) from exc

    async def fetchval(self, sql: str, *args):
        pool = await self._ensure_pool()
        try:
            async with pool.acquire() as conn:
                return await conn.fetchval(sql, *args)
        except Exception as exc:  # noqa: BLE001
            raise BillingStoreUnavailable(str(exc)) from exc

    async def execute(self, sql: str, *args) -> str:
        pool = await self._ensure_pool()
        try:
            async with pool.acquire() as conn:
                return await conn.execute(sql, *args)
        except Exception as exc:  # noqa: BLE001
            raise BillingStoreUnavailable(str(exc)) from exc


def _is_unique_violation(exc: Exception) -> bool:
    """Driver-agnostic so tests can use a double without asyncpg installed."""
    if getattr(exc, "sqlstate", None) == "23505":
        return True
    return type(exc).__name__ == "UniqueViolationError"


_ACCOUNT_COLUMNS = """
    account_id, entity_type, registered_name, id_number, address,
    email, phone, note, balance_micros, total_recharged_micros,
    is_active, created_at, updated_at
"""

_PROFILE_FIELDS = (
    "entity_type", "registered_name", "id_number", "address",
    "email", "phone", "note",
)


def _account_from_row(row: Any) -> AccountRecord:
    return AccountRecord(
        account_id=str(row["account_id"]),
        entity_type=str(row["entity_type"]),
        registered_name=str(row["registered_name"]),
        id_number=str(row["id_number"]),
        address=str(row["address"]),
        email=str(row["email"]),
        phone=str(row["phone"]),
        note=str(row["note"]),
        balance_micros=int(row["balance_micros"]),
        total_recharged_micros=int(row["total_recharged_micros"]),
        is_active=bool(row["is_active"]),
        created_at=_iso(row["created_at"]),
        updated_at=_iso(row["updated_at"]),
    )


class PgAccountStore:
    """Accounts and wallet balances. Same surface as ``stores.AccountStore``."""

    def __init__(
        self,
        pool: BillingPgPool,
        *,
        cache_ttl_s: float = 15.0,
        clock=time.monotonic,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._pool = pool
        self._cache_ttl_s = cache_ttl_s
        self._clock = clock
        self._logger = logger or _LOGGER
        self._balance_cache: dict[str, tuple[float, Optional[BalanceSnapshot]]] = {}

    # -- profile CRUD ------------------------------------------------------

    async def create(
        self,
        *,
        account_id: str,
        registered_name: str,
        entity_type: str = "individual",
        id_number: str = "",
        address: str = "",
        email: str = "",
        phone: str = "",
        note: str = "",
        created_via: str = "admin",
        created_by: str = "",
    ) -> AccountRecord:
        try:
            row = await self._pool.fetchrow(
                f"""
                INSERT INTO accounts (account_id, entity_type, registered_name,
                                      id_number, address, email, phone, note,
                                      created_via, created_by)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                RETURNING {_ACCOUNT_COLUMNS}
                """,
                account_id, entity_type, registered_name, id_number, address,
                email, phone, note, created_via, created_by,
            )
        except BillingStoreUnavailable as exc:
            if _is_unique_violation(exc.__cause__ or exc):
                raise AccountExists(account_id) from exc
            raise
        return _account_from_row(row)

    async def get(self, account_id: str) -> Optional[AccountRecord]:
        row = await self._pool.fetchrow(
            f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE account_id = $1",
            account_id,
        )
        return _account_from_row(row) if row is not None else None

    async def list_accounts(self, *, limit: int = 200) -> list[AccountRecord]:
        rows = await self._pool.fetch(
            f"SELECT {_ACCOUNT_COLUMNS} FROM accounts ORDER BY account_id LIMIT $1",
            int(limit),
        )
        return [_account_from_row(r) for r in rows]

    async def update_profile(
        self, account_id: str, updates: dict[str, Any]
    ) -> Optional[AccountRecord]:
        sets, args = [], []
        for field in _PROFILE_FIELDS:
            if updates.get(field) is not None:
                args.append(str(updates[field]))
                sets.append(f"{field} = ${len(args) + 1}")
        if updates.get("is_active") is not None:
            args.append(bool(updates["is_active"]))
            sets.append(f"is_active = ${len(args) + 1}")
        if not sets:
            return await self.get(account_id)
        row = await self._pool.fetchrow(
            f"""UPDATE accounts SET {', '.join(sets)}, updated_at = now()
                 WHERE account_id = $1 RETURNING {_ACCOUNT_COLUMNS}""",
            account_id, *args,
        )
        self.invalidate_balance_cache(account_id)
        return _account_from_row(row) if row is not None else None

    # -- balance -----------------------------------------------------------

    async def get_balance_cached(self, account_id: str) -> Optional[BalanceSnapshot]:
        now = self._clock()
        cached = self._balance_cache.get(account_id)
        if cached is not None and cached[0] > now:
            return cached[1]
        row = await self._pool.fetchrow(
            """SELECT balance_micros, is_active, created_at, total_recharged_micros
                 FROM accounts WHERE account_id = $1""",
            account_id,
        )
        value = None if row is None else BalanceSnapshot(
            balance_micros=int(row["balance_micros"]),
            is_active=bool(row["is_active"]),
            created_at=_iso(row["created_at"]),
            total_recharged_micros=int(row["total_recharged_micros"]),
        )
        self._balance_cache[account_id] = (now + self._cache_ttl_s, value)
        return value

    def invalidate_balance_cache(self, account_id: str) -> None:
        self._balance_cache.pop(account_id, None)

    async def adjust_balance(
        self, account_id: str, delta_micros: int, *,
        recharge_micros: int = 0, max_attempts: int = 5,
    ) -> Optional[int]:
        """New balance, or None when the account does not exist.

        ``max_attempts`` is accepted and ignored: it was the CAS retry budget,
        and one atomic statement has nothing to retry. Kept in the signature so
        callers written against the Table Storage store need no edit.

        No existence check first — ``balance_micros`` is NOT NULL, so a NULL
        from RETURNING can only mean the WHERE matched nothing. Asking first
        would add a round trip and a race window for no information.
        """
        new_balance = await self._pool.fetchval(
            """UPDATE accounts
                  SET balance_micros = balance_micros + $2,
                      total_recharged_micros = total_recharged_micros + $3,
                      updated_at = now()
                WHERE account_id = $1
            RETURNING balance_micros""",
            account_id, int(delta_micros), int(recharge_micros),
        )
        self.invalidate_balance_cache(account_id)
        return None if new_balance is None else int(new_balance)


class PgWalletTxnStore:
    """Append-only money movement. Debits are idempotent by unique constraint."""

    def __init__(
        self, pool: BillingPgPool, *, logger: Optional[logging.Logger] = None
    ) -> None:
        self._pool = pool
        self._logger = logger or _LOGGER

    async def txn_exists(self, account_id: str, txn_id: str) -> bool:
        return bool(await self._pool.fetchval(
            "SELECT 1 FROM wallet_txns WHERE account_id = $1 AND idempotency_key = $2",
            account_id, txn_id,
        ))

    async def debit_exists(self, account_id: str, job_id: str) -> bool:
        return await self.txn_exists(account_id, f"job-{job_id}")

    async def write_debit(
        self, account_id: str, job_id: str,
        amount_micros: int, balance_after_micros: int,
    ) -> None:
        """Ledger row only. Prefer :class:`PgWallet` — see its docstring.

        The sign is normalized to negative here rather than trusted from the
        caller: ``balance_reconciliation`` sums this column against the balance,
        so one debit written positive turns the whole account's reconciliation
        into a false alarm — or worse, masks a real one.
        """
        await self._insert(
            account_id=account_id, txn_type="debit",
            amount_micros=-abs(int(amount_micros)),
            balance_after_micros=int(balance_after_micros),
            job_id=job_id, idempotency_key=f"job-{job_id}",
            note="", created_via="system",
        )

    async def write_manual(
        self, account_id: str, amount_micros: int,
        balance_after_micros: int, note: str,
        txn_id: Optional[str] = None,
        created_by: str = "", created_via: str = "admin",
    ) -> str:
        import uuid

        txn_id = txn_id or f"txn-{uuid.uuid4().hex}"
        await self._insert(
            account_id=account_id,
            txn_type="recharge" if int(amount_micros) > 0 else "adjustment",
            amount_micros=int(amount_micros),
            balance_after_micros=int(balance_after_micros),
            job_id=None, idempotency_key=txn_id, note=note,
            created_by=created_by, created_via=created_via,
        )
        return txn_id

    async def _insert(
        self, *, account_id: str, txn_type: str, amount_micros: int,
        balance_after_micros: int, job_id: Optional[str],
        idempotency_key: str, note: str,
        created_by: str = "", created_via: str = "system",
    ) -> None:
        await self._pool.execute(
            """INSERT INTO wallet_txns
                   (account_id, txn_type, amount_micros, balance_after_micros,
                    job_id, idempotency_key, note, created_by, created_via,
                    occurred_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9, now())""",
            account_id, txn_type, amount_micros, balance_after_micros,
            job_id, idempotency_key, note, created_by, created_via,
        )

    async def list_txns(
        self, account_id: str, *, limit: int = 200, offset: int = 0,
        from_ts: Optional[str] = None, to_ts: Optional[str] = None,
    ) -> tuple[list[dict], int]:
        where = ["account_id = $1"]
        args: list[Any] = [account_id]
        if from_ts:
            args.append(from_ts)
            where.append(f"occurred_at >= ${len(args)}::timestamptz")
        if to_ts:
            args.append(to_ts)
            where.append(f"occurred_at < ${len(args)}::timestamptz")
        clause = " AND ".join(where)
        total = int(await self._pool.fetchval(
            f"SELECT count(*) FROM wallet_txns WHERE {clause}", *args) or 0)
        rows = await self._pool.fetch(
            f"""SELECT txn_id, account_id, txn_type, amount_micros,
                       balance_after_micros, job_id, idempotency_key, note,
                       created_by, created_via, occurred_at, recorded_at
                  FROM wallet_txns WHERE {clause}
                 ORDER BY occurred_at DESC, txn_id DESC
                 LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}""",
            *args, int(limit), max(0, int(offset)),
        )
        return [
            {
                "txn_id": str(r["txn_id"]),
                "account_id": str(r["account_id"]),
                "txn_type": str(r["txn_type"]),
                "amount_micros": str(int(r["amount_micros"])),
                "balance_after_micros": str(int(r["balance_after_micros"])),
                "job_id": str(r["job_id"] or ""),
                "note": str(r["note"]),
                "timestamp_utc": _iso(r["occurred_at"]),
            }
            for r in rows
        ], total


class PgWallet:
    """Balance change and its ledger row, in one transaction.

    This is the class the migration exists for. In Table Storage the debit and
    its audit row were two independent writes: a crash between them left the
    balance and the ledger permanently disagreeing, and nothing in the system
    could tell which one was right. Here either both land or neither does, and
    a duplicate ``idempotency_key`` aborts the transaction — so a retried job
    cannot double-charge even if it retries at the worst possible moment.
    """

    def __init__(
        self, pool: BillingPgPool, *,
        account_store: Optional[PgAccountStore] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._pool = pool
        self._account_store = account_store
        self._logger = logger or _LOGGER

    async def apply(
        self, *, account_id: str, delta_micros: int, txn_type: str,
        idempotency_key: str, job_id: Optional[str] = None,
        note: str = "", recharge_micros: int = 0,
        created_by: str = "", created_via: str = "system",
    ) -> Optional[int]:
        """New balance; None if the account is missing or already applied.

        None for "already applied" is deliberate: every caller of a wallet
        operation must treat a repeat as a no-op, and returning a balance would
        invite callers to record a second ledger row from it.
        """
        try:
            async with await self._pool.acquire() as conn:
                async with conn.transaction():
                    new_balance = await conn.fetchval(
                        """UPDATE accounts
                              SET balance_micros = balance_micros + $2,
                                  total_recharged_micros =
                                      total_recharged_micros + $3,
                                  updated_at = now()
                            WHERE account_id = $1
                        RETURNING balance_micros""",
                        account_id, int(delta_micros), int(recharge_micros),
                    )
                    if new_balance is None:
                        return None
                    try:
                        await conn.execute(
                            """INSERT INTO wallet_txns
                                   (account_id, txn_type, amount_micros,
                                    balance_after_micros, job_id,
                                    idempotency_key, note, created_by,
                                    created_via, occurred_at)
                               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9, now())""",
                            account_id, txn_type, int(delta_micros),
                            int(new_balance), job_id, idempotency_key, note,
                            created_by, created_via,
                        )
                    except Exception as exc:  # noqa: BLE001
                        if _is_unique_violation(exc):
                            # Rolls back the balance change with it — that is
                            # the whole point of doing this in one transaction.
                            raise _AlreadyApplied from exc
                        raise
                    return int(new_balance)
        except _AlreadyApplied:
            self._logger.info(
                "billing: wallet op '%s' for %s already applied; no-op",
                idempotency_key, account_id)
            return None
        except BillingStoreUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BillingStoreUnavailable(str(exc)) from exc
        finally:
            if self._account_store is not None:
                self._account_store.invalidate_balance_cache(account_id)

    async def debit_for_job(
        self, *, account_id: str, job_id: str, amount_micros: int
    ) -> Optional[int]:
        if int(amount_micros) <= 0:
            return None
        return await self.apply(
            account_id=account_id, delta_micros=-int(amount_micros),
            txn_type="debit", idempotency_key=f"job-{job_id}", job_id=job_id,
        )


class _AlreadyApplied(Exception):
    """Internal: unwinds the transaction on a duplicate idempotency key."""


def _pricing_from_row(row: Any) -> PricingRecord:
    teaser = row["teaser_unit_price_micros"]
    return PricingRecord(
        price_key=str(row["price_key"]),
        billing_mode=str(row["billing_mode"]),
        unit_price_micros=int(row["unit_price_micros"]),
        note=str(row["note"]),
        updated_at=_iso(row["updated_at"]),
        teaser_unit_price_micros=None if teaser is None else int(teaser),
        unit_seconds=max(1, int(row["unit_seconds"] or 1)),
        min_billable_seconds=max(0, int(row["min_billable_seconds"] or 0)),
    )


class PgPricingStore:
    """List prices. Tiny table, so the whole thing is cached like today."""

    def __init__(
        self, pool: BillingPgPool, *, cache_ttl_s: float = 30.0,
        clock=time.monotonic, logger: Optional[logging.Logger] = None,
    ) -> None:
        self._pool = pool
        self._cache_ttl_s = cache_ttl_s
        self._clock = clock
        self._logger = logger or _LOGGER
        self._cache: Optional[tuple[float, dict[str, PricingRecord]]] = None

    async def resolve(
        self, product: str, model_spec: Optional[str]
    ) -> Optional[PricingRecord]:
        table = await self._load_all(use_cache=True)
        if model_spec:
            specific = table.get(f"{product}:{model_spec}")
            if specific is not None:
                return specific
        return table.get(product)

    async def get_all(self) -> list[PricingRecord]:
        return sorted((await self._load_all()).values(), key=lambda r: r.price_key)

    async def upsert(
        self, *, price_key: str, billing_mode: str, unit_price_micros: int,
        teaser_unit_price_micros: Optional[int] = None,
        unit_seconds: int = 1, min_billable_seconds: int = 0, note: str = "",
    ) -> PricingRecord:
        row = await self._pool.fetchrow(
            """INSERT INTO price_list (price_key, billing_mode, unit_price_micros,
                                       teaser_unit_price_micros, unit_seconds,
                                       min_billable_seconds, note)
               VALUES ($1,$2,$3,$4,$5,$6,$7)
               ON CONFLICT (price_key) DO UPDATE
                   SET billing_mode = EXCLUDED.billing_mode,
                       unit_price_micros = EXCLUDED.unit_price_micros,
                       teaser_unit_price_micros = EXCLUDED.teaser_unit_price_micros,
                       unit_seconds = EXCLUDED.unit_seconds,
                       min_billable_seconds = EXCLUDED.min_billable_seconds,
                       note = EXCLUDED.note,
                       updated_at = now()
               RETURNING price_key, billing_mode, unit_price_micros,
                         teaser_unit_price_micros, unit_seconds,
                         min_billable_seconds, note, updated_at""",
            price_key, billing_mode, int(unit_price_micros),
            None if teaser_unit_price_micros is None else int(teaser_unit_price_micros),
            max(1, int(unit_seconds)), max(0, int(min_billable_seconds)),
            note,
        )
        self._cache = None
        return _pricing_from_row(row)

    async def delete(self, price_key: str) -> bool:
        result = await self._pool.execute(
            "DELETE FROM price_list WHERE price_key = $1", price_key)
        self._cache = None
        return not str(result).endswith(" 0")

    async def _load_all(self, *, use_cache: bool = False) -> dict[str, PricingRecord]:
        now = self._clock()
        if use_cache and self._cache is not None and self._cache[0] > now:
            return self._cache[1]
        rows = await self._pool.fetch(
            """SELECT price_key, billing_mode, unit_price_micros,
                      teaser_unit_price_micros, unit_seconds,
                      min_billable_seconds, note, updated_at
                 FROM price_list""")
        table = {str(r["price_key"]): _pricing_from_row(r) for r in rows}
        self._cache = (now + self._cache_ttl_s, table)
        return table


VALID_USAGE_STATUS = frozenset({"completed", "failed", "canceled"})
ANONYMOUS_PARTITION = "__anonymous__"


def _as_int(raw: Any, default: int = 0) -> int:
    try:
        return int(str(raw if raw is not None else default) or default)
    except (TypeError, ValueError):
        return default


def _usd_to_micros(raw: Any) -> Optional[int]:
    from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not value.is_finite():
        return None
    return int((value * 1_000_000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _parse_ts(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class PgUsageLedger:
    """Usage rows, in the Table Storage row shape the recorder already emits.

    One conversion function, used by both the backfill and the live dual-write.
    Two copies of this mapping would drift, and the direction it drifts in is
    "the historical bill and the live bill disagree" — which is the one kind of
    disagreement a billing system cannot explain away.

    ``insert_row`` returns ``(landed, skip_reason)``. A skip is never silent:
    the row violates a CHECK the database is right to enforce, and the caller
    reports it rather than coercing the data into something that passes.
    """

    def __init__(self, pool: BillingPgPool, *,
                 logger: Optional[logging.Logger] = None) -> None:
        self._pool = pool
        self._logger = logger or _LOGGER

    @staticmethod
    def to_params(
        row: dict[str, Any], *, source_row_key: Optional[str] = None
    ) -> tuple[Optional[list], Optional[str]]:
        partition = str(row.get("PartitionKey", "")).strip()
        account_id = str(row.get("user_id", "")).strip() or (
            "" if partition == ANONYMOUS_PARTITION else partition)

        status = str(row.get("status", "")).strip()
        if status not in VALID_USAGE_STATUS:
            return None, f"unknown status '{status}'"

        billed = _as_int(row.get("billed_amount_micros"))
        if billed and status != "completed":
            # The CHECK would reject it, and rightly: a charge on a job that
            # did not complete is an accounting bug upstream, not a row to
            # quietly zero out on the way in.
            return None, f"billed {billed} on status '{status}'"

        unit_price = _usd_to_micros(row.get("unit_price_usd"))
        price_source = str(row.get("price_source", "")).strip() or None
        price_ref = row.get("price_ref")
        list_price = _as_int(row.get("list_unit_price_micros"), 0) or None
        if billed and price_source is None:
            # Rows written before price_source existed carry only price_track;
            # anything that was not the teaser was the list price.
            price_source = ("teaser" if str(row.get("price_track", "")).strip()
                            == "teaser" else "list")
        if billed and unit_price is None:
            unit_price = billed // (_as_int(row.get("billed_units")) or 1)

        return [
            str(row.get("job_id", "")),
            account_id or None,
            str(row.get("key_prefix", "")) or None,
            str(row.get("endpoint", "")),
            status,
            str(row.get("auth_mode", "")),
            _as_int(row.get("prompt_tokens")),
            _as_int(row.get("completion_tokens")),
            _as_int(row.get("total_tokens")),
            _usd_to_micros(row.get("token_cost_usd")) or 0,
            str(row.get("music_provider", "")),
            str(row.get("model_spec", "")),
            _as_int(row.get("generation_call_count")),
            _usd_to_micros(row.get("generation_cost_usd")) or 0,
            _usd_to_micros(row.get("total_cost_usd")) or 0,
            str(row.get("billing_mode", "")) or None,
            _as_int(row.get("billed_units")),
            # NULL, not a default, when the row carries no metering: rows
            # written before duration metering existed should read as "not
            # recorded", not as a rule that was applied.
            None if row.get("unit_seconds") in (None, "") else _as_int(
                row.get("unit_seconds"), 1),
            None if row.get("min_billable_seconds") in (None, "") else _as_int(
                row.get("min_billable_seconds"), 0),
            list_price,
            unit_price,
            billed,
            price_source,
            None if price_ref is None else _as_int(price_ref),
            row.get("video_duration_s"),
            _as_int(row.get("latency_ms"), -1),
            _parse_ts(row.get("timestamp_utc")) or datetime.now(timezone.utc),
            source_row_key,
        ], None

    INSERT_SQL = """
        INSERT INTO usage_ledger (job_id, account_id, key_prefix, endpoint,
            status, auth_mode, prompt_tokens, completion_tokens, total_tokens,
            token_cost_micros, music_provider, model_spec,
            generation_call_count, generation_cost_micros, total_cost_micros,
            billing_mode, billed_units, unit_seconds, min_billable_seconds,
            list_unit_price_micros,
            unit_price_micros, billed_amount_micros, price_source, price_ref,
            video_duration_s, latency_ms, occurred_at, source_row_key)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,
                $18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28)
        ON CONFLICT (source_row_key) WHERE source_row_key IS NOT NULL
            DO NOTHING
    """

    async def insert_row(
        self, row: dict[str, Any], *, source_row_key: Optional[str] = None
    ) -> tuple[bool, Optional[str]]:
        params, skip = self.to_params(row, source_row_key=source_row_key)
        if skip is not None:
            return False, skip
        result = await self._pool.execute(self.INSERT_SQL, *params)
        return not str(result).endswith(" 0"), None

    async def query_usage(
        self, *, user_id: str, limit: Optional[int] = 200
    ) -> list[dict[str, Any]]:
        """One account's rows, newest first, in the Table Storage row shape.

        Returning the legacy shape rather than a cleaner one is deliberate:
        ``query_usage_window`` — which builds the 详单 totals and all three
        breakdowns — then works against Postgres unchanged. Re-implementing
        that aggregation in SQL would be faster and would eventually disagree
        with the Table Storage version by some rounding detail, at which point
        a customer's bill depends on which store answered.

        Time and key filtering still happen in the caller, as they do today.
        Pushing them into SQL is the obvious next optimization; it is not a
        correctness matter, and doing it here would mean two filter
        implementations to keep in step during the migration.
        """
        rows = await self._pool.fetch(
            """SELECT * FROM usage_ledger
                WHERE account_id = $1
                ORDER BY occurred_at DESC, usage_id DESC""" +
            ("" if limit is None else " LIMIT $2"),
            *( (user_id,) if limit is None else (user_id, int(limit)) ),
        )
        return [self.to_storage_row(r) for r in rows]

    @staticmethod
    def to_storage_row(row: Any) -> dict[str, Any]:
        from EdennCode.Deployment.billing.stores import micros_to_usd

        source = row["price_source"]
        billed = int(row["billed_amount_micros"] or 0)
        unit_price = row["unit_price_micros"]
        return {
            "job_id": str(row["job_id"] or ""),
            "endpoint": str(row["endpoint"] or ""),
            "status": str(row["status"] or ""),
            "auth_mode": str(row["auth_mode"] or ""),
            "user_id": str(row["account_id"] or ""),
            "key_prefix": str(row["key_prefix"] or ""),
            "prompt_tokens": int(row["prompt_tokens"] or 0),
            "completion_tokens": int(row["completion_tokens"] or 0),
            "total_tokens": int(row["total_tokens"] or 0),
            "token_cost_usd": micros_to_usd(int(row["token_cost_micros"] or 0)),
            "music_provider": str(row["music_provider"] or ""),
            "model_spec": str(row["model_spec"] or ""),
            "generation_call_count": int(row["generation_call_count"] or 0),
            "generation_cost_usd": micros_to_usd(
                int(row["generation_cost_micros"] or 0)),
            "total_cost_usd": micros_to_usd(int(row["total_cost_micros"] or 0)),
            "video_duration_s": (None if row["video_duration_s"] is None
                                 else float(row["video_duration_s"])),
            "latency_ms": int(row["latency_ms"] if row["latency_ms"] is not None
                              else -1),
            "billing_mode": str(row["billing_mode"] or ""),
            "billed_units": int(row["billed_units"] or 0),
            **({} if row["unit_seconds"] is None
               else {"unit_seconds": int(row["unit_seconds"])}),
            **({} if row["min_billable_seconds"] is None
               else {"min_billable_seconds": int(row["min_billable_seconds"])}),
            "unit_price_usd": (0.0 if unit_price is None
                               else micros_to_usd(int(unit_price))),
            "billed_amount_usd": micros_to_usd(billed),
            # String-encoded like every persisted micros value, so the
            # aggregator's int() call behaves identically on both stores.
            "billed_amount_micros": str(billed),
            # price_source is the finer four-way truth; price_track is the
            # two-track label the 详单 has always shown.
            "price_track": ("" if source is None
                            else ("teaser" if source == "teaser" else "standard")),
            "timestamp_utc": _iso(row["occurred_at"]),
        }

    async def account_id_for(self, row: dict[str, Any]) -> Optional[str]:
        """Which account this row belongs to, or None when anonymous."""
        partition = str(row.get("PartitionKey", "")).strip()
        account_id = str(row.get("user_id", "")).strip() or (
            "" if partition == ANONYMOUS_PARTITION else partition)
        return account_id or None


class PgContractPriceSource:
    """Active contract price for one account × product, or None.

    Not cached: contract prices are per-account and read once per billed job,
    so a cache would add staleness for a query that costs an indexed point read.
    """

    def __init__(self, pool: BillingPgPool) -> None:
        self._pool = pool

    async def get_active(
        self, account_id: str, price_key: str
    ) -> Optional[ContractPrice]:
        row = await self._pool.fetchrow(
            """SELECT contract_id, price_key, billing_mode, unit_price_micros
                 FROM account_contract_prices
                WHERE account_id = $1 AND price_key = $2
                  AND effective_from <= now()
                  AND (effective_to IS NULL OR effective_to > now())
                ORDER BY effective_from DESC
                LIMIT 1""",
            account_id, price_key,
        )
        if row is None:
            return None
        return ContractPrice(
            contract_id=int(row["contract_id"]),
            price_key=str(row["price_key"]),
            billing_mode=str(row["billing_mode"]),
            unit_price_micros=int(row["unit_price_micros"]),
        )


class PgDiscountTierSource:
    """Global volume tiers. Cached — one small table shared by every account."""

    def __init__(
        self, pool: BillingPgPool, *, cache_ttl_s: float = 30.0,
        clock=time.monotonic,
    ) -> None:
        self._pool = pool
        self._cache_ttl_s = cache_ttl_s
        self._clock = clock
        self._cache: Optional[tuple[float, list[DiscountTier]]] = None

    async def list_active(self) -> list[DiscountTier]:
        now = self._clock()
        if self._cache is not None and self._cache[0] > now:
            return self._cache[1]
        rows = await self._pool.fetch(
            """SELECT tier_id, min_recharged_micros, discount_rate
                 FROM discount_tiers
                WHERE effective_to IS NULL AND effective_from <= now()
                ORDER BY min_recharged_micros""")
        tiers = [
            DiscountTier(
                tier_id=int(r["tier_id"]),
                min_recharged_micros=int(r["min_recharged_micros"]),
                discount_rate=Decimal(str(r["discount_rate"])),
            )
            for r in rows
        ]
        self._cache = (now + self._cache_ttl_s, tiers)
        return tiers

    def invalidate(self) -> None:
        self._cache = None


__all__ = [
    "BillingPgPool",
    "PgAccountStore",
    "PgContractPriceSource",
    "PgDiscountTierSource",
    "PgPricingStore",
    "PgUsageLedger",
    "PgWallet",
    "PgWalletTxnStore",
    "VALID_USAGE_STATUS",
]
