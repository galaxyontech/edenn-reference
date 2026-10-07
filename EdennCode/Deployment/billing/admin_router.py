"""Admin billing surface: account CRUD, recharge, transactions, pricing.

Guarded by the same ``x-admin-secret`` as the P1 key-management endpoints
(``admin_guard``). Amounts cross the wire as decimal USD and are stored as
integer micro-USD; validation rejects sub-micro precision. Error bodies are
flat ``{"detail", "code"}`` (family parity).
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
from typing import Any, Literal, Optional

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from EdennCode.Deployment.auth.admin_router import admin_guard
from EdennCode.Deployment.billing.stores import (
    AccountExists,
    AccountRecord,
    BillingStoreUnavailable,
    rmb_to_micros,
    usd_to_micros,
    micros_to_usd,
)

PRICE_KEY_RE = re.compile(r"^[a-z0-9_]+(:[a-z0-9_-]+)?$")


class CreateAccountRequest(BaseModel):
    # Optional: the server generates a canonical `acct_<hex>` id when omitted
    # (recommended). An explicit id stays supported for tenants whose external
    # identifier IS the account id (e.g. a tax number). The id becomes a Table
    # Storage PartitionKey and an OData filter literal, and — load-bearing —
    # MUST be used as the key's `user_id` when minting keys, since billing
    # finds the wallet by the key's user_id. Same charset rule as mint user_id.
    account_id: Optional[str] = Field(
        default=None, min_length=1, max_length=256, pattern=r"^[A-Za-z0-9._-]+$")
    registered_name: str = Field(min_length=1, max_length=512)
    entity_type: Literal["company", "individual"] = "individual"
    id_number: str = ""
    address: str = ""
    email: str = ""
    phone: str = ""
    note: str = ""


class PatchAccountRequest(BaseModel):
    registered_name: Optional[str] = Field(default=None, min_length=1,
                                           max_length=512)
    entity_type: Optional[Literal["company", "individual"]] = None
    id_number: Optional[str] = None
    address: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    note: Optional[str] = None
    is_active: Optional[bool] = None


class RechargeRequest(BaseModel):
    amount_usd: float
    note: str = ""


class PricingRequest(BaseModel):
    billing_mode: Literal["per_request", "per_second"]
    unit_price_usd: Optional[float] = Field(default=None, gt=0)
    unit_price_rmb: Optional[float] = Field(default=None, gt=0)
    teaser_unit_price_usd: Optional[float] = Field(default=None, gt=0)
    teaser_unit_price_rmb: Optional[float] = Field(default=None, gt=0)
    # Metering for per_second products: one unit price buys ``unit_seconds``
    # seconds (30 for 视频配乐), and a delivery shorter than
    # ``min_billable_seconds`` bills at that floor (15 for 多图配乐).
    unit_seconds: int = Field(default=1, ge=1, le=3600)
    min_billable_seconds: int = Field(default=0, ge=0, le=3600)
    note: str = ""


def _account_payload(record: AccountRecord) -> dict[str, Any]:
    return {
        "account_id": record.account_id,
        "entity_type": record.entity_type,
        "registered_name": record.registered_name,
        "id_number": record.id_number,
        "address": record.address,
        "email": record.email,
        "phone": record.phone,
        "note": record.note,
        "balance_usd": record.balance_usd,
        "balance_micros": str(record.balance_micros),
        "total_recharged_usd": micros_to_usd(record.total_recharged_micros),
        "total_recharged_micros": str(record.total_recharged_micros),
        "is_active": record.is_active,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _txn_payload(row: dict[str, Any]) -> dict[str, Any]:
    amount = int(row.get("amount_micros", "0") or 0)
    after = int(row.get("balance_after_micros", "0") or 0)
    return {
        "txn_id": str(row.get("RowKey", "")),
        "txn_type": str(row.get("txn_type", "")),
        "amount_usd": micros_to_usd(amount),
        "amount_micros": str(amount),
        "balance_after_usd": micros_to_usd(after),
        "balance_after_micros": str(after),
        "job_id": str(row.get("job_id", "")),
        "note": str(row.get("note", "")),
        "timestamp_utc": str(row.get("timestamp_utc", "")),
    }


def _error(status_code: int, detail: str, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code,
                        content={"detail": detail, "code": code})


def create_billing_admin_router(
    *,
    account_store: Any,
    txn_store: Any,
    pricing_store: Any,
    admin_secret: Optional[str],
    logger: logging.Logger,
    rmb_per_usd: float = 7.0,
    index_store: Any = None,
    usage_recorder: Any = None,
) -> APIRouter:
    router = APIRouter()

    def _guard(request: Request, provided: Optional[str],
               *stores: Any) -> Optional[JSONResponse]:
        from EdennCode.Deployment.auth.console_session import get_console_session

        err = admin_guard(provided, admin_secret,
                          session=get_console_session(request))
        if err is not None:
            return err
        if any(store is None for store in stores):
            return _error(503, "Billing storage is not configured.",
                          "billing_unavailable")
        return None

    # -- accounts ----------------------------------------------------------

    @router.post("/api/v1/admin/accounts")
    async def create_account(
        request: Request,
        body: CreateAccountRequest,
        x_admin_secret: Optional[str] = Header(default=None),
    ) -> Any:
        err = _guard(request, x_admin_secret, account_store)
        if err is not None:
            return err
        # Server-generated canonical id by default; explicit id honored for
        # externally-keyed tenants. Whichever it is, mint keys with
        # user_id == this account_id (billing finds the wallet by user_id).
        account_id = body.account_id or f"acct_{secrets.token_hex(12)}"
        try:
            record = await account_store.create(
                account_id=account_id,
                registered_name=body.registered_name,
                entity_type=body.entity_type,
                id_number=body.id_number,
                address=body.address,
                email=body.email,
                phone=body.phone,
                note=body.note,
            )
        except AccountExists:
            return _error(409, f"Account '{account_id}' already exists.",
                          "account_exists")
        logger.info("admin: created billing account %s", record.account_id)
        return _account_payload(record)

    @router.get("/api/v1/admin/accounts")
    async def list_accounts(
        request: Request,
        limit: int = Query(default=200, ge=1, le=1000),
        x_admin_secret: Optional[str] = Header(default=None),
    ) -> Any:
        err = _guard(request, x_admin_secret, account_store)
        if err is not None:
            return err
        records = await account_store.list_accounts(limit=limit)
        return {"accounts": [_account_payload(r) for r in records]}

    @router.get("/api/v1/admin/accounts/{account_id}")
    async def get_account(
        request: Request,
        account_id: str,
        x_admin_secret: Optional[str] = Header(default=None),
    ) -> Any:
        err = _guard(request, x_admin_secret, account_store)
        if err is not None:
            return err
        record = await account_store.get(account_id)
        if record is None:
            return _error(404, f"No account '{account_id}'.", "account_not_found")
        return _account_payload(record)

    @router.patch("/api/v1/admin/accounts/{account_id}")
    async def patch_account(
        request: Request,
        account_id: str,
        body: PatchAccountRequest,
        x_admin_secret: Optional[str] = Header(default=None),
    ) -> Any:
        err = _guard(request, x_admin_secret, account_store)
        if err is not None:
            return err
        record = await account_store.update_profile(
            account_id, body.model_dump(exclude_none=True)
        )
        if record is None:
            return _error(404, f"No account '{account_id}'.", "account_not_found")
        logger.info("admin: updated billing account %s", account_id)
        return _account_payload(record)

    @router.get("/api/v1/admin/account-lookup")
    async def account_lookup(
        request: Request,
        phone: Optional[str] = Query(
            default=None,
            description="Customer's phone as they wrote it; normalized server-side"),
        email: Optional[str] = Query(
            default=None,
            description="Customer's email as they wrote it; normalized server-side"),
        x_admin_secret: Optional[str] = Header(default=None),
    ) -> Any:
        # A flat path, not /accounts/lookup: the latter collides with the
        # /accounts/{account_id} pattern and would only work by declaration
        # order, so any future route reshuffle could silently break it.
        from EdennCode.Deployment.billing.account_index import (
            INDEX_KIND_EMAIL,
            INDEX_KIND_PHONE,
            normalize_email,
            normalize_phone,
        )

        err = _guard(request, x_admin_secret, account_store, index_store)
        if err is not None:
            return err
        # No `if phone else ""` needed: both normalizers coerce None via
        # str(raw or ""), and a blank/whitespace-only value normalizes to "" —
        # which the exactly-one check below reads as absent.
        normalized_phone = normalize_phone(phone)
        normalized_email = normalize_email(email)
        if bool(normalized_phone) == bool(normalized_email):
            return _error(
                400,
                "Provide exactly one of phone / email.",
                "invalid_lookup",
            )
        if normalized_phone:
            kind, value = INDEX_KIND_PHONE, normalized_phone
        else:
            kind, value = INDEX_KIND_EMAIL, normalized_email
        try:
            account_id = await index_store.lookup(kind, value)
            record = (await account_store.get(account_id)
                      if account_id is not None else None)
        except BillingStoreUnavailable as exc:
            # Same wire code as _guard's missing-store 503 — from the caller's
            # side billing storage is not answering either way — but logged
            # distinctly so an operator can tell an outage from a
            # misconfiguration. Spec: "索引或账户存储不可用 → 503".
            logger.warning("admin: account-lookup storage unavailable (%s)", exc)
            return _error(503, "Billing storage is unavailable.",
                          "billing_unavailable")
        if account_id is None:
            return _error(404, "No account for that contact.",
                          "account_not_found")
        if record is None:
            # Index entries are written after the account they point at, so this
            # only happens after manual surgery. Don't hand back an id that
            # resolves to nothing.
            logger.warning(
                "admin: dangling %s index entry -> missing account %s",
                kind, account_id,
            )
            return _error(404, "No account for that contact.",
                          "account_not_found")
        return _account_payload(record)

    # -- wallet ------------------------------------------------------------

    @router.post("/api/v1/admin/accounts/{account_id}/recharge")
    async def recharge(
        request: Request,
        account_id: str,
        body: RechargeRequest,
        x_admin_secret: Optional[str] = Header(default=None),
    ) -> Any:
        err = _guard(request, x_admin_secret, account_store, txn_store)
        if err is not None:
            return err
        try:
            amount_micros = usd_to_micros(body.amount_usd)
        except ValueError as exc:
            return _error(400, f"Invalid amount: {exc}", "invalid_amount")
        if amount_micros == 0:
            return _error(400, "Amount must be non-zero.", "invalid_amount")
        new_balance = await account_store.adjust_balance(
            account_id, amount_micros,
            recharge_micros=amount_micros if amount_micros > 0 else 0,
        )
        if new_balance is None:
            return _error(404, f"No account '{account_id}'.", "account_not_found")
        txn_id = await txn_store.write_manual(
            account_id, amount_micros, new_balance, body.note
        )
        txn_type = "recharge" if amount_micros > 0 else "adjustment"
        logger.info(
            "admin: %s account %s by %s micro-USD (balance %s)",
            txn_type, account_id, amount_micros, new_balance,
        )
        return {
            "account_id": account_id,
            "balance_usd": micros_to_usd(new_balance),
            "balance_micros": str(new_balance),
            "txn_id": txn_id,
            "txn_type": txn_type,
        }

    @router.get("/api/v1/admin/accounts/{account_id}/transactions")
    async def list_transactions(
        request: Request,
        account_id: str,
        limit: int = Query(default=200, ge=1, le=1000,
                           description="Page size (1-1000)"),
        offset: int = Query(default=0, ge=0,
                            description="Rows to skip for pagination"),
        from_ts: Optional[str] = Query(
            default=None, alias="from",
            description="Lower bound (inclusive), ISO-8601 or date (e.g. 2026-07-01)"),
        to_ts: Optional[str] = Query(
            default=None, alias="to",
            description="Upper bound (exclusive); use next day for a single-day query"),
        x_admin_secret: Optional[str] = Header(default=None),
    ) -> Any:
        err = _guard(request, x_admin_secret, txn_store)
        if err is not None:
            return err
        rows, total_rows = await txn_store.list_txns(
            account_id, limit=limit, offset=offset, from_ts=from_ts, to_ts=to_ts)
        return {
            "transactions": [_txn_payload(r) for r in rows],
            "page": {"limit": limit, "offset": offset, "returned": len(rows),
                     "total_rows": total_rows,
                     "has_more": offset + len(rows) < total_rows},
        }

    # -- pricing -----------------------------------------------------------

    @router.put("/api/v1/admin/pricing/{price_key}")
    async def put_pricing(
        request: Request,
        price_key: str,
        body: PricingRequest,
        x_admin_secret: Optional[str] = Header(default=None),
    ) -> Any:
        err = _guard(request, x_admin_secret, pricing_store)
        if err is not None:
            return err
        if not PRICE_KEY_RE.match(price_key):
            return _error(
                400,
                "price_key must match product[:modelspec] "
                "(lowercase letters, digits, underscores).",
                "invalid_price_key",
            )
        if (body.unit_price_usd is None) == (body.unit_price_rmb is None):
            return _error(400, "Provide exactly one of unit_price_usd / "
                               "unit_price_rmb.", "invalid_amount")
        if body.teaser_unit_price_usd is not None and \
                body.teaser_unit_price_rmb is not None:
            return _error(400, "Provide at most one of teaser_unit_price_usd / "
                               "teaser_unit_price_rmb.", "invalid_amount")
        # Rejected rather than ignored: silently dropping a metering rule the
        # caller believed they set is how a product ends up billed at a
        # granularity nobody chose.
        if body.billing_mode == "per_request" and (
                body.unit_seconds != 1 or body.min_billable_seconds != 0):
            return _error(400, "unit_seconds / min_billable_seconds apply to "
                               "per_second pricing only.", "invalid_metering")
        try:
            unit_price_micros = (
                usd_to_micros(body.unit_price_usd)
                if body.unit_price_usd is not None
                else rmb_to_micros(body.unit_price_rmb, rmb_per_usd))
            teaser_micros = None
            if body.teaser_unit_price_usd is not None:
                teaser_micros = usd_to_micros(body.teaser_unit_price_usd)
            elif body.teaser_unit_price_rmb is not None:
                teaser_micros = rmb_to_micros(body.teaser_unit_price_rmb,
                                              rmb_per_usd)
        except ValueError as exc:
            return _error(400, f"Invalid unit price: {exc}", "invalid_amount")
        record = await pricing_store.upsert(
            price_key=price_key,
            billing_mode=body.billing_mode,
            unit_price_micros=unit_price_micros,
            teaser_unit_price_micros=teaser_micros,
            unit_seconds=body.unit_seconds,
            min_billable_seconds=body.min_billable_seconds,
            note=body.note,
        )
        logger.info("admin: pricing %s = %s @ %s micro-USD "
                    "(unit %ss, floor %ss)",
                    price_key, record.billing_mode, unit_price_micros,
                    record.unit_seconds, record.min_billable_seconds)
        return {
            "price_key": record.price_key,
            "billing_mode": record.billing_mode,
            "unit_seconds": record.unit_seconds,
            "min_billable_seconds": record.min_billable_seconds,
            "unit_price_usd": record.unit_price_usd,
            "unit_price_micros": str(record.unit_price_micros),
            "teaser_unit_price_usd": record.teaser_unit_price_usd,
            "teaser_unit_price_micros": (
                str(record.teaser_unit_price_micros)
                if record.teaser_unit_price_micros is not None else None),
            "note": record.note,
            "updated_at": record.updated_at,
        }

    @router.get("/api/v1/admin/pricing")
    async def list_pricing(
        request: Request,
        x_admin_secret: Optional[str] = Header(default=None),
    ) -> Any:
        err = _guard(request, x_admin_secret, pricing_store)
        if err is not None:
            return err
        records = await pricing_store.get_all()
        return {
            "pricing": [
                {
                    "price_key": r.price_key,
                    "billing_mode": r.billing_mode,
                    "unit_seconds": r.unit_seconds,
                    "min_billable_seconds": r.min_billable_seconds,
                    "unit_price_usd": r.unit_price_usd,
                    "unit_price_micros": str(r.unit_price_micros),
                    "teaser_unit_price_usd": r.teaser_unit_price_usd,
                    "teaser_unit_price_micros": (
                        str(r.teaser_unit_price_micros)
                        if r.teaser_unit_price_micros is not None else None),
                    "note": r.note,
                    "updated_at": r.updated_at,
                }
                for r in records
            ]
        }

    @router.delete("/api/v1/admin/pricing/{price_key}")
    async def delete_pricing(
        request: Request,
        price_key: str,
        x_admin_secret: Optional[str] = Header(default=None),
    ) -> Any:
        err = _guard(request, x_admin_secret, pricing_store)
        if err is not None:
            return err
        if not await pricing_store.delete(price_key):
            return _error(404, f"No pricing row '{price_key}'.",
                          "price_key_not_found")
        logger.info("admin: deleted pricing %s", price_key)
        return {"deleted": True}

    # -- cross-account rollup ---------------------------------------------

    @router.get("/api/v1/admin/usage-summary")
    async def usage_summary(
        request: Request,
        from_ts: Optional[str] = Query(
            default=None, alias="from",
            description="Lower bound (inclusive), ISO-8601 or date"),
        to_ts: Optional[str] = Query(
            default=None, alias="to",
            description="Upper bound (exclusive)"),
        max_accounts: int = Query(
            default=100, ge=1, le=1000,
            description="Accounts to roll up; the response reports truncation"),
        x_admin_secret: Optional[str] = Header(default=None),
    ) -> Any:
        """Per-account spend plus a platform total, for the console landing page.

        Usage is partitioned by account, so this is O(accounts) queries — there
        is no cross-account index to ask instead. Hence the explicit cap, and
        hence ``truncated``: a silent top-N would read as "this is everything"
        precisely when it isn't. The real cross-account financial view is the
        billing-Postgres migration; this is the honest interim.
        """
        err = _guard(request, x_admin_secret, account_store)
        if err is not None:
            return err
        if usage_recorder is None:
            return _error(503, "Usage ledger is not configured.",
                          "usage_unavailable")
        from EdennCode.Deployment.auth.usage_recorder import query_usage_window
        from EdennCode.Deployment.billing.stores import (
            micros_to_usd,
            usd_to_micros,
        )

        # One extra row is enough to know whether we are looking at everything.
        records = await account_store.list_accounts(limit=max_accounts + 1)
        truncated = len(records) > max_accounts
        records = records[:max_accounts]

        # Bounded fan-out: the ledger is a shared table and an admin refresh
        # should not look like a burst of traffic to it.
        semaphore = asyncio.Semaphore(8)

        async def _one(record: Any) -> dict[str, Any]:
            async with semaphore:
                try:
                    _, totals, _ = await query_usage_window(
                        usage_recorder, user_id=record.account_id,
                        from_ts=from_ts, to_ts=to_ts, limit=1, offset=0)
                except Exception:  # noqa: BLE001 - one bad account, not a 500
                    logger.warning("admin: usage rollup failed for %s",
                                   record.account_id, exc_info=True)
                    totals = {"jobs": 0, "total_tokens": 0,
                              "total_cost_usd": 0.0, "total_billed_usd": 0.0}
            return {
                "account_id": record.account_id,
                "registered_name": record.registered_name,
                "is_active": record.is_active,
                "balance_usd": record.balance_usd,
                "jobs": totals["jobs"],
                "total_tokens": totals["total_tokens"],
                "total_cost_usd": totals["total_cost_usd"],
                "total_billed_usd": totals["total_billed_usd"],
            }

        rows = await asyncio.gather(*(_one(r) for r in records))
        rows.sort(key=lambda r: r["total_billed_usd"], reverse=True)
        return {
            "accounts": rows,
            "totals": {
                "accounts": len(rows),
                "jobs": sum(r["jobs"] for r in rows),
                "total_tokens": sum(r["total_tokens"] for r in rows),
                "total_cost_usd": round(
                    sum(r["total_cost_usd"] for r in rows), 6),
                # Back through micros so a hundred accounts' rounded dollars
                # cannot drift the platform figure.
                "total_billed_usd": micros_to_usd(
                    sum(usd_to_micros(r["total_billed_usd"]) for r in rows)),
                "balance_usd": round(sum(r["balance_usd"] for r in rows), 6),
            },
            "window": {"from": from_ts, "to": to_ts},
            "accounts_scanned": len(rows),
            "truncated": truncated,
        }

    return router


__all__ = ["create_billing_admin_router"]
