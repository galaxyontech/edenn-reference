"""Authentication + session-ownership authorization for the agentic-audio router.

Gated by ``AGENTIC_AUDIO_REQUIRE_AUTH`` (default OFF, so existing deployments and
tests are byte-identical). When ON:

- the caller presents a token — ``Authorization: Bearer <token>`` on REST, or a
  ``?token=<token>`` query param on the WebSocket (browsers can't set WS headers);
- the token resolves to a user id: via ``AGENTIC_AUDIO_API_KEYS`` ("tok:user,..")
  when configured, otherwise the token value IS the user id (dev mode);
- a created session is owned by that principal, and every later read/mutation
  verifies the caller owns the session (403 otherwise).

There is no users table yet (MVP); the principal is a string user id that maps to
the :class:`~.domain.user.User` entity. This is the hook for a real identity
provider later.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)


def auth_enabled() -> bool:
    return os.getenv("AGENTIC_AUDIO_REQUIRE_AUTH", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class AuthError(Exception):
    """Auth failure carrying the HTTP status the router should surface."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return authorization.strip() or None  # tolerate a bare token


def _api_keys() -> dict[str, str]:
    """Parse ``AGENTIC_AUDIO_API_KEYS`` ("token:user,token2:user2") -> {token: user}."""

    raw = os.getenv("AGENTIC_AUDIO_API_KEYS", "").strip()
    mapping: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        token, _, user = pair.partition(":")
        token = token.strip()
        if token:
            mapping[token] = user.strip() or token
    return mapping


def token_is_user_mode() -> bool:
    """Explicit opt-in to "any token authenticates as its own user id".

    This is a LOCAL convenience — it means every string is a valid credential
    for the user it names, so a caller can be anyone. It must never be reachable
    by forgetting to set something, which is why it is its own flag rather than
    a consequence of an empty key map.
    """

    return os.getenv("AGENTIC_AUDIO_TOKEN_IS_USER", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def resolve_principal(
    *, authorization: Optional[str] = None, token: Optional[str] = None
) -> Optional[str]:
    """Legacy, synchronous credential check — static tokens only.

    Kept for callers that genuinely cannot await. It CANNOT see a signed-in
    identity (verification is async), so it refuses when identity is the only
    configured source rather than silently rejecting real users as unknown.
    Prefer :func:`resolve_caller`.
    """

    from .identity import identity_configured

    if auth_enabled() and identity_configured() and not _api_keys():
        raise AuthError(
            401,
            "This endpoint cannot verify a signed-in session; use the "
            "asynchronous resolver.",
        )

    if not auth_enabled():
        return None
    presented = _bearer(authorization) or (token.strip() if token else None)
    if not presented:
        raise AuthError(401, "Authentication required.")
    keys = _api_keys()
    if keys:
        user = keys.get(presented)
        if not user:
            raise AuthError(401, "Invalid credentials.")
        return user
    # No key map. Auth was asked for and cannot be performed, so REFUSE — the
    # previous behaviour returned the presented string as the user id, which
    # meant one unset env var silently turned "authentication required" into
    # "anyone may be anyone". A deployment that genuinely wants that has to say
    # so with AGENTIC_AUDIO_TOKEN_IS_USER.
    if token_is_user_mode():
        return presented
    logger.error(
        "AGENTIC_AUDIO_REQUIRE_AUTH is on but AGENTIC_AUDIO_API_KEYS is empty: "
        "refusing every request. Set the key map, or set "
        "AGENTIC_AUDIO_TOKEN_IS_USER=1 for a local run where any token may "
        "authenticate as the user it names."
    )
    raise AuthError(401, "Invalid credentials.")


async def resolve_caller(
    *, authorization: Optional[str] = None, token: Optional[str] = None
) -> Optional["Caller"]:
    """The authenticated caller, or ``None`` when auth is disabled.

    Two sources, in order, and the order is the whole design:

    1. **A signed-in identity.** The presented credential is verified as an ID
       token and the uid becomes the principal. This is the real door.
    2. **A static token from the env var.** Deprecated, kept so existing
       deployments and local runs keep working through the transition, and
       consulted only when the credential is not a valid identity.

    Raises :class:`AuthError` (401) when a credential is required and missing or
    unrecognised, and (503) when identity storage cannot answer — failing closed
    there rather than telling a signed-in person they are a stranger.
    """

    from .identity import (
        Caller,
        IdentityUnavailable,
        identity_configured,
        legacy_tokens_enabled,
        resolver,
        warn_legacy_once,
    )

    if not auth_enabled():
        return None
    presented = _bearer(authorization) or (token.strip() if token else None)
    if not presented:
        raise AuthError(401, "Authentication required.")

    if identity_configured():
        try:
            caller = await resolver().resolve(presented)
        except IdentityUnavailable as exc:
            logger.error("identity storage unavailable: %s", exc)
            raise AuthError(
                503, "Sign-in is temporarily unavailable. Please try again."
            ) from exc
        if caller is not None:
            return caller

    if legacy_tokens_enabled():
        keys = _api_keys()
        if keys:
            user = keys.get(presented)
            if user:
                warn_legacy_once()
                return Caller(principal=user, source="legacy_token")
        elif token_is_user_mode():
            return Caller(principal=presented, source="legacy_token")

    if not identity_configured() and not _api_keys() and not token_is_user_mode():
        logger.error(
            "Authentication is required but no identity source is configured: "
            "no verifier, and AGENTIC_AUDIO_API_KEYS is empty. Refusing every "
            "request."
        )
    raise AuthError(401, "Invalid credentials.")


def authorize_owner(session_creator_id: Optional[str], principal: Optional[str]) -> None:
    """No-op when auth is off; otherwise 403 unless the caller owns the session."""

    if not auth_enabled():
        return
    if principal is None:
        raise AuthError(401, "Authentication required.")
    if (session_creator_id or "") != principal:
        raise AuthError(403, "You do not have access to this session.")


# ---------------------------------------------------------------------------
# Share-link grants — how an invite link works when auth is ON.
#
# The owner mints a grant: an HMAC-signed {session, role, expiry} blob that
# rides the share URL. The recipient authenticates as THEMSELVES (their own
# bearer token) and redeems the grant, which registers them as a participant
# at the granted role. The link carries capability, never identity — a leaked
# link can't impersonate anyone, and revoking a participant still works.
# ---------------------------------------------------------------------------

# Dev fallback secret: random per process, so unsigned deployments still get
# working (if restart-fragile) links instead of a crash.
_PROCESS_SECRET = os.urandom(32)

GRANT_TTL_S = 7 * 24 * 3600
_GRANT_ROLES = {"view", "comment", "iterate"}


def _share_secret() -> bytes:
    explicit = os.getenv("AGENTIC_AUDIO_SHARE_SECRET", "").strip()
    if explicit:
        return explicit.encode()
    keys = os.getenv("AGENTIC_AUDIO_API_KEYS", "").strip()
    if keys:
        # Derived from the key set: deterministic across replicas/restarts
        # configured identically, and rotates when the keys rotate.
        return hashlib.sha256(("share-grant:" + keys).encode()).digest()
    return _PROCESS_SECRET


def mint_share_grant(
    session_id: str, role: str, *, ttl_s: int = GRANT_TTL_S, epoch: int = 0
) -> str:
    """A URL-safe `payload.sig` grant for this session at this role.

    ``epoch`` is the session's grant generation at mint time. Bumping the
    session's epoch invalidates every link minted before it, which is what
    "revoke the link I shared" actually means — a signed capability cannot be
    taken back any other way, since the holder already has the bytes.

    ``jti`` is a per-grant id. Nothing revokes an individual grant yet, but it
    is what makes that possible later, and it means two links minted in the same
    second are distinguishable in an audit trail.
    """

    clean_role = role if role in _GRANT_ROLES else "comment"
    body = {
        "sid": session_id,
        "role": clean_role,
        "exp": int(time.time()) + int(ttl_s),
        "epoch": int(epoch),
        "jti": base64.urlsafe_b64encode(os.urandom(9)).rstrip(b"=").decode(),
    }
    payload = base64.urlsafe_b64encode(
        json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    ).rstrip(b"=")
    sig = hmac.new(_share_secret(), payload, hashlib.sha256).hexdigest()[:32]
    return payload.decode() + "." + sig


def verify_share_grant(grant: str, session_id: str, *, epoch: int = 0) -> str:
    """The role a valid grant confers on this session; AuthError(403) otherwise.

    A grant minted before the session's current ``epoch`` is refused: that is
    how a shared link is revoked.
    """

    try:
        payload_s, sig = (grant or "").split(".", 1)
        payload = payload_s.encode()
        expected = hmac.new(_share_secret(), payload, hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expected):
            raise ValueError("bad signature")
        body = json.loads(base64.urlsafe_b64decode(payload + b"=" * (-len(payload) % 4)))
        if str(body.get("sid") or "") != session_id:
            raise ValueError("wrong session")
        expires = int(body.get("exp") or 0)
    except AuthError:
        raise
    except Exception as exc:  # noqa: BLE001 - any malformed grant is the same 403
        raise AuthError(403, "This invite link isn't valid for this session.") from exc
    if expires < time.time():
        raise AuthError(403, "This invite link has expired — ask for a fresh one.")
    # A link minted before the session's current generation has been revoked.
    # Grants issued before epochs existed carry no field and read as 0, so they
    # keep working until the owner revokes for the first time.
    if int(body.get("epoch") or 0) < int(epoch or 0):
        raise AuthError(403, "This invite link was revoked — ask for a fresh one.")
    role = str(body.get("role") or "comment")
    return role if role in _GRANT_ROLES else "comment"


__all__ = [
    "AuthError",
    "auth_enabled",
    "authorize_owner",
    "mint_share_grant",
    "resolve_caller",
    "resolve_principal",
    "verify_share_grant",
]
