"""Short-lived tickets for the two places a browser cannot send a header.

A WebSocket handshake and a `<video src=…>` both refuse to carry an
``Authorization`` header, so the studio put the caller's bearer token in the
query string instead. That token is long-lived and reusable, and a query string
is the least private part of a request: it lands in server access logs, in
browser history, in `Referer` headers, and in any proxy in between. Sharing a
console URL with a colleague shared the credential in it.

A ticket replaces it:

* minted only over a normally-authenticated request, so the real credential
  never leaves the header;
* **signed and stateless**, so any replica can verify one without shared state;
* good for ~60 seconds, because it is redeemed immediately after being minted;
* bound to a **purpose**, so a ticket for a media read cannot open a socket;
* optionally bound to one **session**, so it cannot be pointed at another;
* single-use within the process that issued it, as defence in depth.

The single-use record is per-process, which means a replay inside the TTL could
land on another replica. That is a real limit and it is why the TTL is a minute
rather than an hour: the fix is shared state, and the shape here does not change
when that arrives — only where ``_used`` lives.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

from .auth import AuthError

# Purposes are separate namespaces, not labels: the signature covers the
# purpose, so a ticket minted for one cannot be presented for another.
PURPOSE_WS = "ws"
PURPOSE_MEDIA = "media"
_PURPOSES = {PURPOSE_WS, PURPOSE_MEDIA}

TICKET_TTL_S = int(os.getenv("AGENTIC_AUDIO_TICKET_TTL_S", "60"))

_PROCESS_SECRET = os.urandom(32)

# Redeemed ticket ids, with the time they expire. Bounded by the TTL rather than
# by a count: entries are only useful until the ticket would expire anyway.
_used: dict[str, float] = {}


def _secret() -> bytes:
    """Signing key, distinct from the share-grant key.

    Deriving both from the same material with the same label would let a share
    grant be presented as a ticket, which is a different capability with a
    different lifetime.
    """

    explicit = os.getenv("AGENTIC_AUDIO_TICKET_SECRET", "").strip()
    if explicit:
        return explicit.encode()
    keys = os.getenv("AGENTIC_AUDIO_API_KEYS", "").strip()
    if keys:
        return hashlib.sha256(("connect-ticket:" + keys).encode()).digest()
    return _PROCESS_SECRET


def _sweep(now: float) -> None:
    for jti, expires in list(_used.items()):
        if expires <= now:
            _used.pop(jti, None)


def mint_ticket(
    principal: str,
    *,
    purpose: str = PURPOSE_WS,
    session_id: str = "",
    ttl_s: int = TICKET_TTL_S,
) -> dict[str, object]:
    """A one-shot credential for a request that cannot carry a header."""

    if purpose not in _PURPOSES:
        raise ValueError(f"unknown ticket purpose: {purpose}")
    body = {
        "sub": principal,
        "pur": purpose,
        "sid": session_id or "",
        "exp": int(time.time()) + int(ttl_s),
        "jti": base64.urlsafe_b64encode(os.urandom(9)).rstrip(b"=").decode(),
    }
    payload = base64.urlsafe_b64encode(
        json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    ).rstrip(b"=")
    sig = hmac.new(_secret(), payload, hashlib.sha256).hexdigest()[:32]
    return {
        "ticket": payload.decode() + "." + sig,
        "expires_in": int(ttl_s),
        "purpose": purpose,
    }


def redeem_ticket(
    ticket: str, *, purpose: str = PURPOSE_WS, session_id: str = ""
) -> str:
    """The principal a ticket proves, consuming it. ``AuthError`` otherwise."""

    now = time.time()
    _sweep(now)
    try:
        payload_s, sig = (ticket or "").split(".", 1)
        payload = payload_s.encode()
        expected = hmac.new(_secret(), payload, hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expected):
            raise ValueError("bad signature")
        body = json.loads(base64.urlsafe_b64decode(payload + b"=" * (-len(payload) % 4)))
    except Exception as exc:  # noqa: BLE001 - any malformed ticket is the same 401
        raise AuthError(401, "This connection ticket isn't valid.") from exc

    if str(body.get("pur") or "") != purpose:
        raise AuthError(401, "This connection ticket isn't valid for this request.")
    bound = str(body.get("sid") or "")
    if bound and session_id and bound != session_id:
        raise AuthError(401, "This connection ticket isn't valid for this session.")
    if int(body.get("exp") or 0) < now:
        raise AuthError(401, "This connection ticket has expired.")

    jti = str(body.get("jti") or "")
    if jti and jti in _used:
        raise AuthError(401, "This connection ticket has already been used.")
    if jti:
        _used[jti] = float(body.get("exp") or now)

    principal = str(body.get("sub") or "")
    if not principal:
        raise AuthError(401, "This connection ticket isn't valid.")
    return principal


def reset_for_tests() -> None:
    _used.clear()


__all__ = [
    "PURPOSE_MEDIA",
    "PURPOSE_WS",
    "TICKET_TTL_S",
    "mint_ticket",
    "redeem_ticket",
    "reset_for_tests",
]
