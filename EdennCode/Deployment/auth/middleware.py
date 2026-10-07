"""API-key validation middleware (AUTH_MODE = off | log | enforce).

Registered via ``app.middleware("http")`` textually BEFORE the CORS
``add_middleware`` call — Starlette builds the middleware stack LIFO, so this
ordering makes CORS the outer layer and guarantees 401/503 responses still
receive CORS headers for browser clients. OPTIONS is always exempt: when CORS
is disabled there is nothing to protect on a preflight, and when enabled the
CORS layer answers preflights before this middleware runs.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.responses import Response as StarletteResponse

from EdennCode.Deployment.auth.key_store import (
    API_KEY_PREFIX,
    KeyStoreUnavailable,
    Principal,
)

AUTH_MODES = {"off", "log", "enforce"}

# Where a Firebase ID token is accepted in place of an API key. Deliberately
# short: a console session must never be able to submit a billable job (it
# would spend money from a browser credential and produce usage no key_prefix
# can be attributed to). Everywhere else a JWT is simply an unknown API key.
_CONSOLE_PATH_PREFIXES = (
    "/api/v1/account/",
    "/api/v1/admin/",
)

_EXEMPT_EXACT = {
    "/healthz",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    "/openapi.json",
    "/api/v1/docs",
    "/provider_c/callback",
    # Self-serve signup: a caller with no key is exactly who needs one. The
    # endpoint only writes, and it refuses to serve unless billing enforces.
    # The unverified variant carries its own x-admin-secret guard; the verified
    # one carries a Firebase ID token, which this middleware's Bearer parser
    # would otherwise try (and fail) to look up as an API key.
    "/api/v1/signup",
    "/api/v1/signup/verified",
    # The console's own static files. Whoever is loading the sign-in page has
    # no API key by definition; requiring one to fetch the page would make the
    # page that hands out keys unreachable. Its runtime config is public for the
    # same reason, and carries only Firebase's public client identifiers.
    "/console",
    "/api/v1/console/config",
}
_EXEMPT_PREFIXES = (
    "/console/",
    "/docs/",
    "/redoc/",
    "/api/v1/docs/",
    "/api/v1/provider_callbacks/",
    # Admin router carries its own x-admin-secret guard (stronger credential).
    "/api/v1/admin/",
)


def resolve_auth_mode(raw: str) -> str:
    value = (raw or "").strip().lower()
    return value if value in AUTH_MODES else "off"


def is_exempt_path(path: str) -> bool:
    return path in _EXEMPT_EXACT or path.startswith(_EXEMPT_PREFIXES)


def is_console_path(path: str) -> bool:
    return path.startswith(_CONSOLE_PATH_PREFIXES)


def get_principal(request: Request) -> Optional[Principal]:
    return getattr(request.state, "principal", None)


def resolve_user_id(request: Request, provided_user_id: Optional[str]) -> Optional[str]:
    """Key-derived identity wins in enforce mode; passthrough otherwise."""
    principal = get_principal(request)
    if principal is not None and getattr(request.state, "auth_enforced", False):
        return principal.user_id
    return provided_user_id


def _error_response(status_code: int, message: str, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": message, "code": code})


def _parse_bearer(authorization: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Return ``(key, error_code)``; exactly one side is set (or absent header)."""
    if not authorization:
        return None, "missing_api_key"
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        return None, "invalid_authorization_header"
    return parts[1].strip(), None


# Public alias: the signup router parses the same header for a Firebase token.
parse_bearer = _parse_bearer


def create_auth_middleware(
    *,
    mode: str,
    key_store: Any,
    logger: logging.Logger,
    billing_gate: Optional[Callable] = None,
    console_session_resolver: Optional[Any] = None,
) -> Callable:
    resolved_mode = resolve_auth_mode(mode)
    enforced = resolved_mode == "enforce"

    async def _resolve_console_session(request: Request, presented: Optional[str]):
        """A console session for this request, or None. Never raises past 503."""
        from EdennCode.Deployment.auth.console_session import (
            ConsoleSessionUnavailable,
        )

        if console_session_resolver is None or not presented:
            return None, None
        if not is_console_path(request.url.path):
            return None, None
        if presented.startswith(API_KEY_PREFIX):
            return None, None  # an API key is an API key everywhere
        try:
            return await console_session_resolver.resolve(presented), None
        except ConsoleSessionUnavailable:
            return None, _error_response(
                503,
                "Authentication service temporarily unavailable.",
                "auth_unavailable",
            )

    async def dispatch(request: Request, call_next):
        request.state.principal = None
        request.state.console_session = None
        request.state.auth_enforced = enforced
        if request.method == "OPTIONS":
            return await call_next(request)

        presented, parse_error = _parse_bearer(request.headers.get("Authorization"))
        session, session_error = await _resolve_console_session(request, presented)
        if session_error is not None:
            return session_error
        request.state.console_session = session

        if is_exempt_path(request.url.path):
            # Admin paths land here: the session (if any) is attached above so
            # the admin routers can accept it in place of x-admin-secret.
            return await call_next(request)

        principal: Optional[Principal] = None
        if session is not None:
            # Signed in but not signed up yet is a real state; the route
            # answers it (404 signup_required), the middleware does not.
            if session.account_id:
                principal = Principal(user_id=session.account_id, key_prefix="")
        elif presented is not None:
            if key_store is None:
                if enforced:
                    logger.error(
                        "AUTH_MODE=enforce but no key store is configured; failing closed."
                    )
                    return _error_response(
                        503,
                        "Authentication service temporarily unavailable.",
                        "auth_unavailable",
                    )
            else:
                try:
                    principal = await key_store.lookup(presented)
                except KeyStoreUnavailable:
                    if enforced:
                        return _error_response(
                            503,
                            "Authentication service temporarily unavailable.",
                            "auth_unavailable",
                        )
                    # Off mode stays quiet — identity there is best-effort only.
                    if resolved_mode == "log":
                        logger.warning(
                            "auth: key store unavailable in %s mode; continuing without principal (path=%s)",
                            resolved_mode,
                            request.url.path,
                        )

        if principal is None and enforced and session is None:
            if parse_error == "missing_api_key":
                return _error_response(
                    401,
                    "Missing API key. Provide an Authorization: Bearer sk-... header.",
                    "missing_api_key",
                )
            if parse_error == "invalid_authorization_header":
                return _error_response(
                    401,
                    "Invalid Authorization header. Expected: Bearer YOUR_API_KEY.",
                    "invalid_authorization_header",
                )
            return _error_response(401, "Invalid API key.", "invalid_api_key")

        if principal is None and session is None and resolved_mode == "log":
            logger.info(
                "auth[log]: unauthenticated request would be rejected under enforce "
                "(path=%s reason=%s)",
                request.url.path,
                parse_error or "invalid_api_key",
            )

        request.state.principal = principal
        if (
            billing_gate is not None
            and request.method == "POST"
            and _is_billable_path(request.url.path)
        ):
            try:
                blocked = await billing_gate(request, principal)
            except Exception:  # noqa: BLE001 - the gate must never break requests
                logger.warning(
                    "auth: billing gate errored; proceeding (path=%s)",
                    request.url.path,
                    exc_info=True,
                )
                blocked = None
            if blocked is not None:
                return blocked
        response = await call_next(request)
        warning = getattr(request.state, "balance_warning", None)
        if warning is not None:
            response = await _apply_balance_warning(response, warning, logger)
        return response

    return dispatch


async def _apply_balance_warning(response, warning, logger):
    """Header always; body block only for 2xx JSON objects. Best effort."""
    response.headers["X-Edenn-Balance-Warning"] = "low"
    content_type = response.headers.get("content-type", "")
    if response.status_code >= 300 or "application/json" not in content_type:
        return response
    body = None
    chunks: list = []
    try:
        body_iterator = getattr(response, "body_iterator", None)
        if body_iterator is not None:
            # Accumulate incrementally: an iterator that dies mid-stream leaves
            # the original response partially drained, so the except path below
            # needs whatever was buffered to rebuild an intact reply.
            async for chunk in body_iterator:
                chunks.append(chunk)
            body = b"".join(chunks)
        else:
            body = getattr(response, "body", b"")
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("non-object JSON body")
        data["balance_warning"] = warning
        new_body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return StarletteResponse(content=new_body,
                                 status_code=response.status_code,
                                 headers=headers)
    except Exception:  # noqa: BLE001 - warning must never break a response
        if body is None and chunks:
            body = b"".join(chunks)
        logger.warning("auth: balance-warning body injection failed",
                       exc_info=True)
        if body is not None:
            headers = dict(response.headers)
            headers.pop("content-length", None)
            return StarletteResponse(content=body,
                                     status_code=response.status_code,
                                     headers=headers)
        return response


def _is_billable_path(path: str) -> bool:
    try:
        from EdennCode.Deployment.billing.engine import BILLABLE_PATHS

        return path in BILLABLE_PATHS
    except Exception:  # noqa: BLE001 - billing package absence must not break auth
        return False


__all__ = [
    "AUTH_MODES",
    "create_auth_middleware",
    "get_principal",
    "is_console_path",
    "is_exempt_path",
    "parse_bearer",
    "resolve_auth_mode",
    "resolve_user_id",
]
