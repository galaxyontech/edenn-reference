"""Firebase ID token verification, without the firebase-admin SDK.

Google signs Firebase ID tokens with rotating RSA keys published as **x509
certificates** at ``…/x509/securetoken@system.gserviceaccount.com``. That
endpoint is deliberately *not* a JWKS document, so ``PyJWKClient`` cannot be
pointed at it: each value is a PEM certificate whose public key has to be
extracted with ``cryptography``.

Verification needs nothing but the project id. A service-account key would also
work, but it can read and write the entire project's user directory — holding a
credential that powerful to perform a signature check would be trading away the
main security benefit of federating identity in the first place.

Every rejection raises ``InvalidIdentityToken`` carrying one identical
client-facing message. The specific cause lives on ``.reason`` for logs only: a
caller probing the endpoint must not be able to tell "wrong audience" from
"expired" from "unknown key", which is how token forgery gets debugged.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

GOOGLE_X509_URL = (
    "https://www.googleapis.com/robot/v1/metadata/x509/"
    "securetoken@system.gserviceaccount.com"
)
ISSUER_PREFIX = "https://securetoken.google.com/"
PROVIDER_PHONE = "phone"

INVALID_TOKEN_CODE = "invalid_identity_token"
INVALID_TOKEN_MESSAGE = "Invalid or expired identity token."

# Firebase ID tokens live one hour; a minute of skew is generous for a client
# clock without meaningfully extending a stolen token's life.
CLOCK_LEEWAY_S = 60.0
_DEFAULT_CERT_TTL_S = 3600.0
_MIN_CERT_TTL_S = 60.0
_MAX_AGE_RE = re.compile(r"max-age\s*=\s*(\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class VerifiedIdentity:
    """What a valid ID token proves: a stable uid and a verified phone number."""

    uid: str
    phone_number: str
    provider: str


class InvalidIdentityToken(Exception):
    """Any verification failure. One message out, the real reason for logs."""

    code = INVALID_TOKEN_CODE

    def __init__(self, reason: str) -> None:
        super().__init__(INVALID_TOKEN_MESSAGE)
        self.reason = reason


def parse_max_age(cache_control: str) -> float:
    """Seconds from a ``Cache-Control`` header, falling back to one hour."""
    match = _MAX_AGE_RE.search(cache_control or "")
    if not match:
        return _DEFAULT_CERT_TTL_S
    return float(match.group(1))


def fetch_google_certs() -> tuple[dict[str, str], float]:
    """``({kid: PEM certificate}, ttl_seconds)`` straight from Google."""
    import httpx

    response = httpx.get(GOOGLE_X509_URL, timeout=10.0)
    response.raise_for_status()
    return response.json(), parse_max_age(response.headers.get("cache-control", ""))


class FirebaseTokenVerifier:
    """Verifies Firebase ID tokens for exactly one project.

    ``fetch_certs`` is injected so tests can sign with their own key pair; it is
    a *sync* callable run on a worker thread, matching the rest of this codebase's
    treatment of blocking SDK calls.
    """

    def __init__(
        self,
        project_id: str,
        *,
        fetch_certs: Callable[[], tuple[dict[str, str], float]] = fetch_google_certs,
        clock: Callable[[], float] = time.time,
        logger: Optional[logging.Logger] = None,
        allowed_providers: tuple[str, ...] = (PROVIDER_PHONE,),
    ) -> None:
        self.project_id = (project_id or "").strip()
        self._fetch_certs = fetch_certs
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self._allowed_providers = set(allowed_providers)
        self._keys: dict[str, Any] = {}
        self._keys_expire_at = 0.0
        self._refresh_lock = asyncio.Lock()

    @classmethod
    def from_settings(
        cls, settings: Any, logger: logging.Logger
    ) -> Optional["FirebaseTokenVerifier"]:
        """None when ``FIREBASE_PROJECT_ID`` is unset — verification is then off."""
        project_id = (getattr(settings, "firebase_project_id", "") or "").strip()
        if not project_id:
            return None
        logger.info("FirebaseTokenVerifier enabled for project '%s'", project_id)
        return cls(project_id, logger=logger)

    # -- signing keys -----------------------------------------------------

    async def _public_key(self, kid: str) -> Optional[Any]:
        now = self._clock()
        cached = self._keys.get(kid)
        if cached is not None and now < self._keys_expire_at:
            return cached
        async with self._refresh_lock:
            # Someone may have refreshed while we waited for the lock.
            cached = self._keys.get(kid)
            if cached is not None and self._clock() < self._keys_expire_at:
                return cached
            await self._refresh_keys()
        return self._keys.get(kid)

    async def _refresh_keys(self) -> None:
        """Replace the cache, or keep a stale copy when Google is unreachable.

        Serving a stale-but-still-valid certificate beats answering 503 to every
        login: Google publishes the next certificate well before it starts
        signing with it, so the window where stale keys are actually wrong is
        far smaller than the window where a transient fetch failure would
        otherwise take sign-in down.
        """
        try:
            certs, ttl_s = await asyncio.to_thread(self._fetch_certs)
        except Exception:  # noqa: BLE001 - any fetch failure keeps the old keys
            self._logger.warning(
                "firebase: signing certificate fetch failed; keeping %d cached "
                "key(s)", len(self._keys), exc_info=True,
            )
            return
        parsed: dict[str, Any] = {}
        for kid, pem in (certs or {}).items():
            try:
                from cryptography.x509 import load_pem_x509_certificate

                material = pem.encode("utf-8") if isinstance(pem, str) else pem
                parsed[str(kid)] = load_pem_x509_certificate(material).public_key()
            except Exception:  # noqa: BLE001 - one bad cert must not void the set
                self._logger.warning(
                    "firebase: unparseable certificate for kid %s", kid,
                    exc_info=True,
                )
        if not parsed:
            self._logger.warning(
                "firebase: certificate response held no usable keys; keeping "
                "%d cached key(s)", len(self._keys),
            )
            return
        self._keys = parsed
        self._keys_expire_at = self._clock() + max(ttl_s, _MIN_CERT_TTL_S)

    # -- verification -----------------------------------------------------

    async def verify(self, token: str) -> VerifiedIdentity:
        """The token's proven identity, or ``InvalidIdentityToken``."""
        import jwt

        if not self.project_id:
            raise InvalidIdentityToken("no project id configured")
        presented = (token or "").strip()
        if not presented:
            raise InvalidIdentityToken("empty token")

        try:
            header = jwt.get_unverified_header(presented)
        except Exception as exc:  # noqa: BLE001
            raise InvalidIdentityToken(f"unreadable header: {exc}") from exc

        # Pinning the algorithm here as well as in decode() is not redundant:
        # it is the check that stops "alg: none" and HMAC-with-the-public-key
        # from ever reaching the decoder, and it fails identically to every
        # other rejection.
        algorithm = header.get("alg")
        if algorithm != "RS256":
            raise InvalidIdentityToken(f"unexpected alg {algorithm!r}")
        kid = str(header.get("kid") or "")
        if not kid:
            raise InvalidIdentityToken("missing kid")
        public_key = await self._public_key(kid)
        if public_key is None:
            raise InvalidIdentityToken(f"unknown kid {kid}")

        try:
            claims = jwt.decode(
                presented,
                key=public_key,
                algorithms=["RS256"],
                audience=self.project_id,
                issuer=ISSUER_PREFIX + self.project_id,
                leeway=CLOCK_LEEWAY_S,
                options={"require": ["exp", "iat", "aud", "iss", "sub"]},
            )
        except Exception as exc:  # noqa: BLE001 - shape is checked below
            raise InvalidIdentityToken(f"decode rejected: {exc}") from exc

        return self._identity_from_claims(claims)

    def _identity_from_claims(self, claims: dict[str, Any]) -> VerifiedIdentity:
        now = self._clock()
        # PyJWT accepts a future `iat` (it only requires the claim to parse), so
        # a token minted for later has to be rejected here.
        for name in ("iat", "auth_time"):
            raw = claims.get(name)
            if raw is None:
                continue
            try:
                stamp = float(raw)
            except (TypeError, ValueError) as exc:
                raise InvalidIdentityToken(f"non-numeric {name}") from exc
            if stamp > now + CLOCK_LEEWAY_S:
                raise InvalidIdentityToken(f"{name} is in the future")

        uid = str(claims.get("sub") or "").strip()
        if not uid:
            raise InvalidIdentityToken("empty sub")

        firebase_claim = claims.get("firebase")
        if not isinstance(firebase_claim, dict):
            raise InvalidIdentityToken("missing firebase claim")
        provider = str(firebase_claim.get("sign_in_provider") or "")
        if provider not in self._allowed_providers:
            raise InvalidIdentityToken(f"provider {provider!r} not allowed")

        phone_number = str(claims.get("phone_number") or "").strip()
        if provider == PROVIDER_PHONE and not phone_number:
            raise InvalidIdentityToken("phone provider without phone_number")

        return VerifiedIdentity(
            uid=uid, phone_number=phone_number, provider=provider
        )


__all__ = [
    "CLOCK_LEEWAY_S",
    "GOOGLE_X509_URL",
    "INVALID_TOKEN_CODE",
    "INVALID_TOKEN_MESSAGE",
    "ISSUER_PREFIX",
    "PROVIDER_PHONE",
    "FirebaseTokenVerifier",
    "InvalidIdentityToken",
    "VerifiedIdentity",
    "fetch_google_certs",
    "parse_max_age",
]
