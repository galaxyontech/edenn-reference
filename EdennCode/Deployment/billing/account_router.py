"""User-facing account endpoints: balance, itemized bill (详单), own API keys.

Identity arrives one of two ways, and the difference decides what is allowed:

* an **API key** — the auth middleware attaches a ``Principal`` in every
  AUTH_MODE (off/log resolve without enforcing). Good enough to read your own
  balance and bill, which is what a script needs.
* a **console session** — a Firebase ID token, attached by the same middleware
  on console paths only.

Key management requires the session. An API key that can mint its own
successors and revoke its siblings turns a single leaked credential into
permanent, self-healing access to the account; requiring the human's login for
that is the same line ModelGateway's console draws.

Rows are a sanitized projection: infrastructure fields (partition/row keys,
auth_mode) never leave the service, and key *material* never leaves it at all —
only the 12-character display prefix.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from EdennCode.Deployment.auth.console_session import get_console_session
from EdennCode.Deployment.auth.middleware import get_principal

# A generous ceiling that still bounds the damage of a runaway script; the
# admin key endpoints are unaffected.
MAX_ACTIVE_KEYS_PER_ACCOUNT = 20

# 详单 row projection. Internal USD cost fields are included deliberately —
# they are already client-visible per-job via response cost_metadata, and
# key_prefix is the caller's own key identifier (a user may hold several keys
# against one shared wallet — this is how they split spend per key).
USER_USAGE_FIELDS = (
    "job_id",
    "endpoint",
    "status",
    "key_prefix",
    "model_spec",
    "music_provider",
    "timestamp_utc",
    "latency_ms",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "token_cost_usd",
    "generation_cost_usd",
    "total_cost_usd",
    "video_duration_s",
    "billing_mode",
    "billed_units",
    # Without these two, a 90-second video billed as 3 units and a 10-second
    # slideshow billed as 15 both look like arithmetic errors on the line.
    "unit_seconds",
    "min_billable_seconds",
    "unit_price_usd",
    "billed_amount_usd",
    "price_track",
)

_FIELD_DEFAULTS: dict[str, Any] = {
    "latency_ms": -1,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "token_cost_usd": 0.0,
    "generation_cost_usd": 0.0,
    "total_cost_usd": 0.0,
    "video_duration_s": None,
    "billed_units": 0,
    # None, not 1/0: rows predating duration metering did not record one.
    "unit_seconds": None,
    "min_billable_seconds": None,
    "unit_price_usd": 0.0,
    "billed_amount_usd": 0.0,
    "price_track": "",
}


def _billed_usd(row: dict[str, Any]) -> float:
    """Dollars for this row, derived from micros when the field is absent.

    The ledger writes ``billed_amount_micros`` (integer, as a string — Table
    Storage's Int32 ceiling) and never writes ``billed_amount_usd``. Projecting
    the declared field straight from the entity therefore reported $0.00 on
    every line while the grand total — which sums micros — came out right. A
    bill whose lines do not add up to its own total is worse than no bill.
    """
    from EdennCode.Deployment.billing.stores import micros_to_usd

    explicit = row.get("billed_amount_usd")
    if explicit not in (None, ""):
        try:
            return float(explicit)
        except (TypeError, ValueError):
            pass
    try:
        return micros_to_usd(int(row.get("billed_amount_micros", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def _project_row(row: dict[str, Any]) -> dict[str, Any]:
    projected = {
        field: row.get(field, _FIELD_DEFAULTS.get(field, ""))
        for field in USER_USAGE_FIELDS
    }
    projected["billed_amount_usd"] = _billed_usd(row)
    return projected


def _error(status_code: int, detail: str, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code,
                        content={"detail": detail, "code": code})


class CreateKeyRequest(BaseModel):
    name: str = Field(default="", max_length=512)


class RenameKeyRequest(BaseModel):
    name: str = Field(max_length=512)


def _signup_required() -> JSONResponse:
    return _error(
        404,
        "This phone number has no Edenn account yet. Complete signup first.",
        "signup_required",
    )


def create_account_router(
    *,
    account_store: Any,
    usage_recorder: Any,
    logger: logging.Logger,
    key_store: Any = None,
) -> APIRouter:
    router = APIRouter()

    def _require_principal(request: Request):
        principal = get_principal(request)
        if principal is not None:
            return principal, None
        if get_console_session(request) is not None:
            # Verified, just never signed up: telling them "bad credentials"
            # would send a real customer to debug the wrong thing.
            return None, _signup_required()
        return None, _error(
            401,
            "A valid API key is required for account endpoints. "
            "Provide an Authorization: Bearer sk-... header.",
            "api_key_required",
        )

    def _require_session(request: Request):
        """Key management is session-only — see the module docstring."""
        session = get_console_session(request)
        if session is None:
            if get_principal(request) is not None:
                return None, _error(
                    403,
                    "API keys cannot manage API keys. Sign in to the console "
                    "with your phone number to create or revoke keys.",
                    "session_required",
                )
            return None, _error(
                401,
                "Sign in to the console to manage API keys.",
                "session_required",
            )
        if not session.account_id:
            return None, _signup_required()
        if key_store is None:
            return None, _error(503, "Key store is not configured.",
                                "auth_unavailable")
        return session, None

    @router.get("/api/v1/account/balance")
    async def read_balance(request: Request) -> Any:
        principal, err = _require_principal(request)
        if err is not None:
            return err
        if account_store is None:
            return _error(503, "Billing storage is not configured.",
                          "billing_unavailable")
        record = await account_store.get(principal.user_id)
        if record is None:
            return _error(
                404,
                "No billing account for this API key yet. "
                "Ask your administrator to create one.",
                "account_not_found",
            )
        from EdennCode.Deployment.billing import get_billing
        from EdennCode.Deployment.billing.stores import micros_to_usd

        engine = get_billing()
        ratio = getattr(engine, "low_balance_ratio", 0.10) if engine else 0.10
        payload = {
            "account_id": record.account_id,
            "registered_name": record.registered_name,
            "balance_usd": record.balance_usd,
            "is_active": record.is_active,
            "updated_at": record.updated_at,
            "balance_warning": None,
        }
        total = record.total_recharged_micros
        if total > 0 and record.balance_micros < ratio * total:
            payload["balance_warning"] = {
                "balance_usd": record.balance_usd,
                "threshold_usd": micros_to_usd(int(ratio * total)),
                "message": ("Wallet balance is below {:.0%} of the total "
                            "recharged amount. Please recharge soon.").format(ratio),
            }
            return JSONResponse(content=payload,
                                headers={"X-Edenn-Balance-Warning": "low"})
        return payload

    @router.get("/api/v1/account/usage")
    async def read_usage(
        request: Request,
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
        key_prefix: Optional[str] = Query(
            default=None,
            description="Narrow to one of the account's API keys (first 12 chars, e.g. sk-XbgqH8R8r)"),
    ) -> Any:
        principal, err = _require_principal(request)
        if err is not None:
            return err
        if usage_recorder is None:
            return {"rows": [], "totals": {"jobs": 0, "total_tokens": 0,
                                           "total_cost_usd": 0.0,
                                           "total_billed_usd": 0.0,
                                           "by_key": {}, "by_model": {},
                                           "by_key_model": {}},
                    "page": {"limit": limit, "offset": offset, "returned": 0,
                             "total_rows": 0, "has_more": False}}
        from EdennCode.Deployment.auth.usage_recorder import query_usage_window

        rows, totals, page = await query_usage_window(
            usage_recorder, user_id=principal.user_id,
            from_ts=from_ts, to_ts=to_ts, limit=limit, offset=offset,
            key_prefix=key_prefix,
        )
        return {"rows": [_project_row(r) for r in rows], "totals": totals,
                "page": page}

    # -- self-serve key management (console session only) ------------------

    @router.get("/api/v1/account/keys")
    async def list_my_keys(
        request: Request,
        include_revoked: bool = Query(
            default=False,
            description="Include revoked keys (default: active only)"),
    ) -> Any:
        session, err = _require_session(request)
        if err is not None:
            return err
        records = await key_store.list_keys(user_id=session.account_id)
        return {
            "keys": [
                {
                    "key_prefix": r.key_prefix,
                    # Empty for keys minted before the suffix was recorded; the
                    # console renders that as an unrevealed tail.
                    "key_suffix": r.key_suffix,
                    "name": r.note,
                    "created_at": r.created_at,
                    "last_used_at": r.last_used_at,
                    "is_active": r.is_active,
                    "revoked_at": r.revoked_at,
                }
                for r in records
                if include_revoked or r.is_active
            ]
        }

    @router.post("/api/v1/account/keys")
    async def create_my_key(
        request: Request, body: Optional[CreateKeyRequest] = None,
    ) -> Any:
        # Optional body so an unauthorized caller gets the authorization answer
        # (403/404) rather than a 422 about a field they were never entitled
        # to submit.
        session, err = _require_session(request)
        if err is not None:
            return err
        body = body or CreateKeyRequest()
        active = await key_store.count_active(session.account_id)
        if active >= MAX_ACTIVE_KEYS_PER_ACCOUNT:
            return _error(
                409,
                f"This account already has {MAX_ACTIVE_KEYS_PER_ACCOUNT} "
                "active API keys. Revoke one before creating another.",
                "key_limit_reached",
            )
        plaintext, record = await key_store.mint(
            user_id=session.account_id, note=body.name.strip())
        logger.info("console: account %s created key %s",
                    session.account_id, record.key_prefix)
        return {
            "api_key": plaintext,
            "key_prefix": record.key_prefix,
            "key_suffix": record.key_suffix,
            "name": record.note,
            "created_at": record.created_at,
            "message": "Save this key now — it is shown only once.",
        }

    @router.patch("/api/v1/account/keys/{key_prefix}")
    async def rename_my_key(
        request: Request, key_prefix: str, body: RenameKeyRequest,
    ) -> Any:
        session, err = _require_session(request)
        if err is not None:
            return err
        name = body.name.strip()
        if not name:
            # An empty label is worse than the default one: the list would show
            # a blank row and there would be no way to tell which key it is.
            return _error(400, "Give the key a name.", "invalid_key_name")
        if not await key_store.rename_for_user(key_prefix, session.account_id,
                                               name):
            return _error(404, f"No active key with prefix '{key_prefix}'.",
                          "key_not_found")
        logger.info("console: account %s renamed key %s",
                    session.account_id, key_prefix)
        return {"key_prefix": key_prefix, "name": name}

    @router.delete("/api/v1/account/keys/{key_prefix}")
    async def revoke_my_key(request: Request, key_prefix: str) -> Any:
        session, err = _require_session(request)
        if err is not None:
            return err
        # account_id comes from the session, never from the request: a
        # key_prefix appears in 详单 rows and support tickets, so matching on
        # it alone would let anyone who has seen one revoke it (IDOR).
        if not await key_store.revoke_for_user(key_prefix, session.account_id):
            # Deliberately the same answer for "no such key" and "not yours",
            # so this endpoint cannot be used to probe other accounts.
            return _error(404, f"No active key with prefix '{key_prefix}'.",
                          "key_not_found")
        logger.info("console: account %s revoked key %s",
                    session.account_id, key_prefix)
        return {
            "revoked": True,
            "key_prefix": key_prefix,
            "message": ("Revoked. Servers that cached this key may accept it "
                        "for up to another minute."),
        }

    return router


__all__ = [
    "MAX_ACTIVE_KEYS_PER_ACCOUNT",
    "USER_USAGE_FIELDS",
    "CreateKeyRequest",
    "RenameKeyRequest",
    "create_account_router",
]
