"""Billing persistence: accounts (USD wallet), wallet transactions, pricing.

Azure Table Storage, same account + ``AUTH_TABLE_NAMESPACE`` suffix as the P1
auth tables. Money is integer micro-USD ($1 = 1_000_000 micros) in
process and **string-encoded** in table entities — Table Storage caps plain
ints at 32 bits and the EdmType.INT64 wrapper round-trips inconsistently, so
strings are the durable representation and all arithmetic happens on ints.

Balance updates are optimistic-concurrency (ETag compare-and-swap with bounded
jittered retries): single-entity CAS is atomic in Table Storage and per-account
write rates are tiny. All Table I/O runs through ``asyncio.to_thread``.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional

ACCOUNTS_TABLE_BASE_NAME = "accounts"
WALLET_TABLE_BASE_NAME = "wallettxns"
PRICING_TABLE_BASE_NAME = "pricing"
_ACCOUNT_PARTITION = "ACCOUNT"
_PRICING_PARTITION = "PRICING"

MICROS_PER_USD = 1_000_000
# Keep |micros| comfortably inside int64: ~9.2e18 micros ≈ 9.2e12 USD.
_MAX_ABS_USD = Decimal(9_000_000_000_000)

_PROFILE_FIELDS = (
    "entity_type",
    "registered_name",
    "id_number",
    "address",
    "email",
    "phone",
    "note",
)


class AccountExists(Exception):
    """Account creation collided with an existing account_id."""


class BillingStoreUnavailable(Exception):
    """Table Storage could not answer (or CAS retries were exhausted)."""


def usd_to_micros(amount: Any) -> int:
    """Decimal USD -> integer micro-USD; rejects sub-micro precision."""
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Not a numeric USD amount: {amount!r}") from exc
    if not value.is_finite():
        raise ValueError(f"USD amount must be finite: {amount!r}")
    if abs(value) > _MAX_ABS_USD:
        raise ValueError(f"USD amount out of range: {amount!r}")
    micros = value * MICROS_PER_USD
    if micros != micros.to_integral_value():
        raise ValueError(
            f"USD amount finer than 1e-6 USD not representable: {amount!r}"
        )
    return int(micros)


def micros_to_usd(micros: int) -> float:
    """Integer micro-USD -> USD for DISPLAY only, rounded to 4 dp (half up).

    Ground truth stays the integer micros; every human-facing ``*_usd`` field
    goes through here so the API never shows more precision than 4 decimals.
    Never feed the result back into billing arithmetic.
    """
    usd = (Decimal(int(micros)) / MICROS_PER_USD).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return float(usd)


def rmb_to_micros(amount_rmb: Any, rmb_per_usd: float) -> int:
    """RMB price -> USD micros at the configured rate; rounds half up to 1 micro."""
    if not rmb_per_usd or float(rmb_per_usd) <= 0:
        raise ValueError(f"rmb_per_usd must be positive: {rmb_per_usd!r}")
    try:
        value = Decimal(str(amount_rmb))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Not a numeric RMB amount: {amount_rmb!r}") from exc
    if not value.is_finite() or value < 0:
        raise ValueError(f"RMB amount must be finite and >= 0: {amount_rmb!r}")
    micros = (value / Decimal(str(rmb_per_usd)) * MICROS_PER_USD).quantize(
        Decimal(1), rounding=ROUND_HALF_UP)
    return int(micros)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_not_found(exc: Exception) -> bool:
    return type(exc).__name__ in {"ResourceNotFoundError", "FakeResourceNotFound"}


def _is_already_exists(exc: Exception) -> bool:
    return type(exc).__name__ in {"ResourceExistsError", "FakeResourceExists"}


def _is_precondition_failed(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) == 412:
        return True
    return type(exc).__name__ in {"ResourceModifiedError", "FakePreconditionFailed"}


def _match_if_not_modified():
    """Real SDK enums when available; fakes ignore these kwargs entirely."""
    try:
        from azure.core import MatchConditions

        return MatchConditions.IfNotModified
    except ImportError:  # pragma: no cover - azure always present in prod
        return None


def _update_mode_replace():
    try:
        from azure.data.tables import UpdateMode

        return UpdateMode.REPLACE
    except ImportError:  # pragma: no cover
        return None


@dataclass(frozen=True)
class AccountRecord:
    account_id: str
    entity_type: str
    registered_name: str
    id_number: str
    address: str
    email: str
    phone: str
    note: str
    balance_micros: int
    total_recharged_micros: int      # lifetime inflows (recharges + positive adjustments)
    is_active: bool
    created_at: str
    updated_at: str

    @property
    def balance_usd(self) -> float:
        return micros_to_usd(self.balance_micros)


def _account_from_entity(entity: dict[str, Any]) -> AccountRecord:
    return AccountRecord(
        account_id=str(entity.get("RowKey", "")),
        entity_type=str(entity.get("entity_type", "individual")),
        registered_name=str(entity.get("registered_name", "")),
        id_number=str(entity.get("id_number", "")),
        address=str(entity.get("address", "")),
        email=str(entity.get("email", "")),
        phone=str(entity.get("phone", "")),
        note=str(entity.get("note", "")),
        balance_micros=int(entity.get("balance_micros", "0") or 0),
        total_recharged_micros=int(entity.get("total_recharged_micros", "0") or 0),
        is_active=bool(entity.get("is_active", False)),
        created_at=str(entity.get("created_at", "")),
        updated_at=str(entity.get("updated_at", "")),
    )


@dataclass(frozen=True)
class BalanceSnapshot:
    """Cached hot-path view of one account (gate + teaser + warning)."""
    balance_micros: int
    is_active: bool
    created_at: str
    total_recharged_micros: int


class AccountStore:
    def __init__(
        self,
        table_client: Any,
        *,
        cache_ttl_s: float = 15.0,
        clock=time.monotonic,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._table_client = table_client
        self._cache_ttl_s = cache_ttl_s
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        # account_id -> (expires_at, BalanceSnapshot | None)
        self._balance_cache: dict[str, tuple[float, Optional[BalanceSnapshot]]] = {}

    @classmethod
    def from_settings(
        cls, settings: Any, logger: logging.Logger
    ) -> Optional["AccountStore"]:
        from EdennCode.Deployment.auth.key_store import build_auth_table_client

        namespace = getattr(settings, "auth_table_namespace", "") or ""
        table_client = build_auth_table_client(
            settings, ACCOUNTS_TABLE_BASE_NAME + namespace, logger
        )
        if table_client is None:
            return None
        logger.info("AccountStore using table '%s'", ACCOUNTS_TABLE_BASE_NAME + namespace)
        return cls(table_client, logger=logger)

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
    ) -> AccountRecord:
        now = _utcnow_iso()
        entity = {
            "PartitionKey": _ACCOUNT_PARTITION,
            "RowKey": account_id,
            "entity_type": entity_type,
            "registered_name": registered_name,
            "id_number": id_number,
            "address": address,
            "email": email,
            "phone": phone,
            "note": note,
            "balance_micros": "0",
            "total_recharged_micros": "0",
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        }
        try:
            await asyncio.to_thread(self._table_client.create_entity, entity)
        except Exception as exc:  # noqa: BLE001
            if _is_already_exists(exc):
                raise AccountExists(account_id) from exc
            raise BillingStoreUnavailable(str(exc)) from exc
        return _account_from_entity(entity)

    async def get(self, account_id: str) -> Optional[AccountRecord]:
        entity = await self._get_entity(account_id)
        return _account_from_entity(entity) if entity is not None else None

    async def list_accounts(self, *, limit: int = 200) -> list[AccountRecord]:
        def _query() -> list[dict[str, Any]]:
            return list(
                self._table_client.query_entities(
                    f"PartitionKey eq '{_ACCOUNT_PARTITION}'"
                )
            )

        try:
            entities = await asyncio.to_thread(_query)
        except Exception as exc:  # noqa: BLE001
            raise BillingStoreUnavailable(str(exc)) from exc
        records = [_account_from_entity(e) for e in entities]
        records.sort(key=lambda r: r.account_id)
        return records[:limit]

    async def update_profile(
        self, account_id: str, updates: dict[str, Any]
    ) -> Optional[AccountRecord]:
        entity = await self._get_entity(account_id)
        if entity is None:
            return None
        for field in _PROFILE_FIELDS:
            if field in updates and updates[field] is not None:
                entity[field] = str(updates[field])
        if "is_active" in updates and updates["is_active"] is not None:
            entity["is_active"] = bool(updates["is_active"])
        entity["updated_at"] = _utcnow_iso()
        payload = {k: v for k, v in entity.items() if k != "metadata"}
        try:
            await asyncio.to_thread(self._table_client.upsert_entity, payload)
        except Exception as exc:  # noqa: BLE001
            raise BillingStoreUnavailable(str(exc)) from exc
        self.invalidate_balance_cache(account_id)
        return _account_from_entity(payload)

    # -- balance -----------------------------------------------------------

    async def get_balance_cached(self, account_id: str) -> Optional[BalanceSnapshot]:
        """Hot-path account snapshot, None when no such account. Cached."""
        now = self._clock()
        cached = self._balance_cache.get(account_id)
        if cached is not None and cached[0] > now:
            return cached[1]
        entity = await self._get_entity(account_id)
        value: Optional[BalanceSnapshot] = None
        if entity is not None:
            value = BalanceSnapshot(
                balance_micros=int(entity.get("balance_micros", "0") or 0),
                is_active=bool(entity.get("is_active", False)),
                created_at=str(entity.get("created_at", "")),
                total_recharged_micros=int(
                    entity.get("total_recharged_micros", "0") or 0),
            )
        self._balance_cache[account_id] = (now + self._cache_ttl_s, value)
        return value

    def invalidate_balance_cache(self, account_id: str) -> None:
        self._balance_cache.pop(account_id, None)

    async def adjust_balance(
        self, account_id: str, delta_micros: int, *,
        recharge_micros: int = 0, max_attempts: int = 5,
    ) -> Optional[int]:
        """CAS-add ``delta_micros``; new balance, or None when account missing."""
        last_exc: Optional[Exception] = None
        for attempt in range(max_attempts):
            entity = await self._get_entity(account_id)
            if entity is None:
                return None
            etag = getattr(entity, "metadata", {}).get("etag")
            new_balance = int(entity.get("balance_micros", "0") or 0) + int(delta_micros)
            entity["balance_micros"] = str(new_balance)
            entity["total_recharged_micros"] = str(
                int(entity.get("total_recharged_micros", "0") or 0)
                + int(recharge_micros)
            )
            entity["updated_at"] = _utcnow_iso()
            payload = {k: v for k, v in entity.items() if k != "metadata"}
            try:
                await asyncio.to_thread(
                    lambda: self._table_client.update_entity(
                        payload,
                        mode=_update_mode_replace(),
                        etag=etag,
                        match_condition=_match_if_not_modified(),
                    )
                )
                self.invalidate_balance_cache(account_id)
                return new_balance
            except Exception as exc:  # noqa: BLE001
                if _is_precondition_failed(exc):
                    last_exc = exc
                    await asyncio.sleep(random.uniform(0.005, 0.02) * (attempt + 1))
                    continue
                raise BillingStoreUnavailable(str(exc)) from exc
        raise BillingStoreUnavailable(
            f"balance CAS exhausted after {max_attempts} attempts for '{account_id}'"
        ) from last_exc

    # -- internals ---------------------------------------------------------

    async def _get_entity(self, account_id: str) -> Optional[Any]:
        try:
            return await asyncio.to_thread(
                self._table_client.get_entity, _ACCOUNT_PARTITION, account_id
            )
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                return None
            raise BillingStoreUnavailable(str(exc)) from exc


class WalletTxnStore:
    """Money-movement audit trail. Debit RowKeys are deterministic per job
    (``job-{job_id}``) so idempotency is a point read; manual rows are
    ``txn-{inverted_ts}-{uuid8}``. RowKey order is therefore NOT chronological —
    listings sort on ``timestamp_utc`` in memory."""

    def __init__(
        self, table_client: Any, *, logger: Optional[logging.Logger] = None
    ) -> None:
        self._table_client = table_client
        self._logger = logger or logging.getLogger(__name__)

    @classmethod
    def from_settings(
        cls, settings: Any, logger: logging.Logger
    ) -> Optional["WalletTxnStore"]:
        from EdennCode.Deployment.auth.key_store import build_auth_table_client

        namespace = getattr(settings, "auth_table_namespace", "") or ""
        table_client = build_auth_table_client(
            settings, WALLET_TABLE_BASE_NAME + namespace, logger
        )
        if table_client is None:
            return None
        logger.info(
            "WalletTxnStore using table '%s'", WALLET_TABLE_BASE_NAME + namespace
        )
        return cls(table_client, logger=logger)

    async def txn_exists(self, account_id: str, txn_id: str) -> bool:
        try:
            await asyncio.to_thread(
                self._table_client.get_entity, account_id, txn_id
            )
            return True
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                return False
            raise BillingStoreUnavailable(str(exc)) from exc

    async def debit_exists(self, account_id: str, job_id: str) -> bool:
        return await self.txn_exists(account_id, f"job-{job_id}")

    async def write_debit(
        self,
        account_id: str,
        job_id: str,
        amount_micros: int,
        balance_after_micros: int,
    ) -> None:
        entity = {
            "PartitionKey": account_id,
            "RowKey": f"job-{job_id}",
            "txn_type": "debit",
            "amount_micros": str(int(amount_micros)),
            "balance_after_micros": str(int(balance_after_micros)),
            "job_id": job_id,
            "note": "",
            "timestamp_utc": _utcnow_iso(),
        }
        try:
            await asyncio.to_thread(self._table_client.upsert_entity, entity)
        except Exception as exc:  # noqa: BLE001
            raise BillingStoreUnavailable(str(exc)) from exc

    async def write_manual(
        self, account_id: str, amount_micros: int,
        balance_after_micros: int, note: str,
        txn_id: Optional[str] = None,
    ) -> str:
        txn_id = txn_id or f"txn-{10**19 - time.time_ns():020d}-{uuid.uuid4().hex[:8]}"
        entity = {
            "PartitionKey": account_id,
            "RowKey": txn_id,
            "txn_type": "recharge" if int(amount_micros) > 0 else "adjustment",
            "amount_micros": str(int(amount_micros)),
            "balance_after_micros": str(int(balance_after_micros)),
            "job_id": "",
            "note": note,
            "timestamp_utc": _utcnow_iso(),
        }
        try:
            await asyncio.to_thread(self._table_client.upsert_entity, entity)
        except Exception as exc:  # noqa: BLE001
            raise BillingStoreUnavailable(str(exc)) from exc
        return txn_id

    async def list_txns(
        self,
        account_id: str,
        *,
        limit: int = 200,
        offset: int = 0,
        from_ts: Optional[str] = None,
        to_ts: Optional[str] = None,
    ) -> tuple[list[dict], int]:
        """One page of an account's transactions (newest first) + total count.

        ``from_ts``/``to_ts`` bound ``timestamp_utc`` (from inclusive, to
        exclusive); date-only strings select whole days. Returns
        ``(page_rows, total_rows)`` where ``total_rows`` is the full filtered
        count (for building a pagination envelope).
        """
        def _query() -> list[dict]:
            return [
                dict(r)
                for r in self._table_client.query_entities(
                    f"PartitionKey eq '{account_id}'"
                )
            ]

        try:
            rows = await asyncio.to_thread(_query)
        except Exception as exc:  # noqa: BLE001
            raise BillingStoreUnavailable(str(exc)) from exc
        if from_ts:
            rows = [r for r in rows if str(r.get("timestamp_utc", "")) >= from_ts]
        if to_ts:
            rows = [r for r in rows if str(r.get("timestamp_utc", "")) < to_ts]
        rows.sort(key=lambda r: str(r.get("timestamp_utc", "")), reverse=True)
        total_rows = len(rows)
        offset = max(0, offset)
        return rows[offset:offset + limit], total_rows


@dataclass(frozen=True)
class PricingRecord:
    price_key: str
    billing_mode: str
    unit_price_micros: int
    note: str
    updated_at: str
    teaser_unit_price_micros: Optional[int] = None
    # Metering for ``per_second`` products; ignored by ``per_request``.
    # ``unit_seconds`` is the block one unit price buys (30 for 视频配乐);
    # ``min_billable_seconds`` is the floor a shorter delivery bills at
    # (15 for 多图配乐). The defaults are plain per-second billing, so rows
    # written before these existed keep their exact meaning.
    unit_seconds: int = 1
    min_billable_seconds: int = 0

    @property
    def unit_price_usd(self) -> float:
        return micros_to_usd(self.unit_price_micros)

    @property
    def teaser_unit_price_usd(self) -> Optional[float]:
        if self.teaser_unit_price_micros is None:
            return None
        return micros_to_usd(self.teaser_unit_price_micros)


def _pricing_from_entity(entity: dict[str, Any]) -> PricingRecord:
    raw_teaser = str(entity.get("teaser_unit_price_micros", "") or "")
    return PricingRecord(
        price_key=str(entity.get("RowKey", "")),
        billing_mode=str(entity.get("billing_mode", "")),
        unit_price_micros=int(entity.get("unit_price_micros", "0") or 0),
        note=str(entity.get("note", "")),
        updated_at=str(entity.get("updated_at", "")),
        teaser_unit_price_micros=int(raw_teaser) if raw_teaser else None,
        # Absent on every row written before duration metering existed, and an
        # absent metering rule is per-second billing with no floor — which is
        # what those rows were charged under.
        unit_seconds=max(1, int(entity.get("unit_seconds", "1") or 1)),
        min_billable_seconds=max(
            0, int(entity.get("min_billable_seconds", "0") or 0)),
    )


class PricingStore:
    """Per-model billing strategy. ``resolve`` caches the whole (tiny) table."""

    def __init__(
        self,
        table_client: Any,
        *,
        cache_ttl_s: float = 30.0,
        clock=time.monotonic,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._table_client = table_client
        self._cache_ttl_s = cache_ttl_s
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self._cache: Optional[tuple[float, dict[str, PricingRecord]]] = None

    @classmethod
    def from_settings(
        cls, settings: Any, logger: logging.Logger
    ) -> Optional["PricingStore"]:
        from EdennCode.Deployment.auth.key_store import build_auth_table_client

        namespace = getattr(settings, "auth_table_namespace", "") or ""
        table_client = build_auth_table_client(
            settings, PRICING_TABLE_BASE_NAME + namespace, logger
        )
        if table_client is None:
            return None
        logger.info(
            "PricingStore using table '%s'", PRICING_TABLE_BASE_NAME + namespace
        )
        return cls(table_client, logger=logger)

    async def upsert(
        self,
        *,
        price_key: str,
        billing_mode: str,
        unit_price_micros: int,
        teaser_unit_price_micros: Optional[int] = None,
        unit_seconds: int = 1,
        min_billable_seconds: int = 0,
        note: str = "",
    ) -> PricingRecord:
        entity = {
            "PartitionKey": _PRICING_PARTITION,
            "RowKey": price_key,
            "billing_mode": billing_mode,
            "unit_price_micros": str(int(unit_price_micros)),
            "teaser_unit_price_micros": (
                str(int(teaser_unit_price_micros))
                if teaser_unit_price_micros is not None else ""),
            "unit_seconds": str(max(1, int(unit_seconds))),
            "min_billable_seconds": str(max(0, int(min_billable_seconds))),
            "note": note,
            "updated_at": _utcnow_iso(),
        }
        try:
            await asyncio.to_thread(self._table_client.upsert_entity, entity)
        except Exception as exc:  # noqa: BLE001
            raise BillingStoreUnavailable(str(exc)) from exc
        self._cache = None
        return _pricing_from_entity(entity)

    async def get_all(self) -> list[PricingRecord]:
        records = sorted(
            (await self._load_all()).values(), key=lambda r: r.price_key
        )
        return records

    async def delete(self, price_key: str) -> bool:
        try:
            await asyncio.to_thread(
                self._table_client.delete_entity, _PRICING_PARTITION, price_key
            )
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                return False
            raise BillingStoreUnavailable(str(exc)) from exc
        self._cache = None
        return True

    async def resolve(
        self, product: str, model_spec: Optional[str]
    ) -> Optional[PricingRecord]:
        table = await self._load_all(use_cache=True)
        if model_spec:
            specific = table.get(f"{product}:{model_spec}")
            if specific is not None:
                return specific
        return table.get(product)

    async def _load_all(self, *, use_cache: bool = False) -> dict[str, PricingRecord]:
        now = self._clock()
        if use_cache and self._cache is not None and self._cache[0] > now:
            return self._cache[1]

        def _query() -> list[dict[str, Any]]:
            return [
                dict(r)
                for r in self._table_client.query_entities(
                    f"PartitionKey eq '{_PRICING_PARTITION}'"
                )
            ]

        try:
            entities = await asyncio.to_thread(_query)
        except Exception as exc:  # noqa: BLE001
            raise BillingStoreUnavailable(str(exc)) from exc
        table = {
            str(e.get("RowKey", "")): _pricing_from_entity(e) for e in entities
        }
        self._cache = (now + self._cache_ttl_s, table)
        return table


__all__ = [
    "ACCOUNTS_TABLE_BASE_NAME",
    "AccountExists",
    "AccountRecord",
    "AccountStore",
    "BalanceSnapshot",
    "BillingStoreUnavailable",
    "MICROS_PER_USD",
    "PRICING_TABLE_BASE_NAME",
    "PricingRecord",
    "PricingStore",
    "WALLET_TABLE_BASE_NAME",
    "WalletTxnStore",
    "rmb_to_micros",
    "usd_to_micros",
    "micros_to_usd",
]
