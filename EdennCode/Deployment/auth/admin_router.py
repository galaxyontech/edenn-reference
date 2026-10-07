"""Admin surface: mint/list/revoke API keys + read the usage ledger.

Guarded by the ``x-admin-secret`` header (env ``API_ADMIN_SECRET``). The auth
middleware skips ``/api/v1/admin/*`` Bearer validation — the admin secret is
the stronger credential. Plaintext keys appear exactly once, in the mint
response.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any, Optional

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class MintKeyRequest(BaseModel):
    # user_id becomes a Table Storage PartitionKey and an OData filter literal
    # (see read_usage below): `/ \ # ?` break table writes and `'` breaks the
    # query filter, so the mint-time charset is restricted to what's safe in
    # both contexts.
    user_id: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9._-]+$")
    note: str = ""


def admin_guard(
    provided: Optional[str],
    admin_secret: Optional[str],
    *,
    session: Any = None,
) -> Optional[JSONResponse]:
    """None when the request may proceed, else a flat {"detail","code"} response.

    Shared by every ``x-admin-secret``-guarded router (keys, usage, billing).

    Two credentials are accepted. ``x-admin-secret`` is the machine one. An
    allowlisted console ``session`` is the human one: the secret can read every
    customer's ledger and revoke anyone's key, so the browser is the last place
    it should live. Admin scope is decided server-side by the allowlist in
    ``ConsoleSessionResolver`` — never by a claim in the token.
    """
    if session is not None and getattr(session, "is_admin", False):
        return None
    if not admin_secret:
        return JSONResponse(
            status_code=503,
            content={"detail": "Admin API is disabled (API_ADMIN_SECRET unset).",
                     "code": "admin_disabled"},
        )
    if not provided or not secrets.compare_digest(provided, admin_secret):
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid admin secret.",
                     "code": "invalid_admin_secret"},
        )
    return None


class MintKeyResponse(BaseModel):
    api_key: str
    user_id: str
    key_prefix: str
    created_at: str


def create_admin_router(
    *,
    key_store: Any,
    usage_recorder: Any,
    admin_secret: Optional[str],
    logger: logging.Logger,
) -> APIRouter:
    router = APIRouter()

    # Both helpers return None when the request may proceed, or a flat
    # {"detail": ..., "code": ...} JSONResponse (matching the auth middleware's
    # error shape) when it must be rejected. Handlers call them and early-return
    # the response rather than raising HTTPException(detail=<dict>), which would
    # nest the dict a second time under "detail" on the wire.
    def _guard_response(
        request: Request, provided: Optional[str]
    ) -> Optional[JSONResponse]:
        from EdennCode.Deployment.auth.console_session import get_console_session

        return admin_guard(provided, admin_secret,
                           session=get_console_session(request))

    def _require_key_store_response() -> Optional[JSONResponse]:
        if key_store is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "Key store is not configured.",
                         "code": "auth_unavailable"},
            )
        return None

    @router.post("/api/v1/admin/keys", response_model=MintKeyResponse)
    async def mint_key(
        request: Request,
        body: MintKeyRequest,
        x_admin_secret: Optional[str] = Header(default=None),
    ) -> Any:
        err = _guard_response(request, x_admin_secret)
        if err is not None:
            return err
        err = _require_key_store_response()
        if err is not None:
            return err
        plaintext, record = await key_store.mint(user_id=body.user_id, note=body.note)
        logger.info(
            "admin: minted key %s for user %s", record.key_prefix, record.user_id
        )
        return MintKeyResponse(
            api_key=plaintext,
            user_id=record.user_id,
            key_prefix=record.key_prefix,
            created_at=record.created_at,
        )

    @router.get("/api/v1/admin/keys")
    async def list_keys(
        request: Request,
        user_id: Optional[str] = Query(default=None),
        x_admin_secret: Optional[str] = Header(default=None),
    ) -> Any:
        err = _guard_response(request, x_admin_secret)
        if err is not None:
            return err
        err = _require_key_store_response()
        if err is not None:
            return err
        records = await key_store.list_keys(user_id=user_id)
        return {
            "keys": [
                {
                    "user_id": r.user_id,
                    "key_prefix": r.key_prefix,
                    "note": r.note,
                    "created_at": r.created_at,
                    "last_used_at": r.last_used_at,
                    "revoked_at": r.revoked_at,
                    "is_active": r.is_active,
                }
                for r in records
            ]
        }

    @router.delete("/api/v1/admin/keys/{key_prefix}")
    async def revoke_key(
        request: Request,
        key_prefix: str,
        x_admin_secret: Optional[str] = Header(default=None),
    ) -> Any:
        err = _guard_response(request, x_admin_secret)
        if err is not None:
            return err
        err = _require_key_store_response()
        if err is not None:
            return err
        if not await key_store.revoke(key_prefix):
            return JSONResponse(
                status_code=404,
                content={"detail": f"No active key with prefix '{key_prefix}'.",
                         "code": "key_not_found"},
            )
        logger.info("admin: revoked key %s", key_prefix)
        return {"revoked": True}

    @router.get("/api/v1/admin/usage")
    async def read_usage(
        request: Request,
        user_id: str = Query(min_length=1),
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
            description="Narrow to one API key of the user (first 12 chars)"),
        x_admin_secret: Optional[str] = Header(default=None),
    ) -> Any:
        err = _guard_response(request, x_admin_secret)
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
            usage_recorder, user_id=user_id,
            from_ts=from_ts, to_ts=to_ts, limit=limit, offset=offset,
            key_prefix=key_prefix,
        )
        return {"rows": rows, "totals": totals, "page": page}

    return router


__all__ = ["admin_guard", "create_admin_router"]
