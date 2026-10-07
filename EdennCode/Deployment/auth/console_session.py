"""Console sessions: what a Firebase ID token entitles its bearer to.

A session is *not* an API key. It authenticates a person in the console; it
never authorizes spending. The auth middleware honors sessions on console
paths only — a token presented to a billable endpoint is just an unknown API
key, and is rejected as one. Two reasons that separation is load-bearing:

* spending should require the credential the customer chose to put in their
  server, not the one their browser happens to be holding;
* usage is attributed by ``key_prefix``, so a keyless submission would produce
  a charge nobody can trace back to a key.

Admin scope comes from a **server-side allowlist**, never from a claim. Firebase
custom claims would work too, but they are set through the same project console
that issues the tokens; keeping the allowlist in deployment config means
promoting an admin is a deploy, not an API call.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class ConsoleSession:
    """A signed-in person. ``account_id`` is None until they have signed up."""

    uid: str
    phone_number: str
    account_id: Optional[str]
    is_admin: bool


class ConsoleSessionUnavailable(Exception):
    """Identity storage could not answer; the caller must fail closed (503)."""


def parse_allowlist(raw: Any) -> tuple[str, ...]:
    """Comma-separated env var → tuple, blanks dropped."""
    return tuple(
        part.strip() for part in str(raw or "").split(",") if part.strip()
    )


def get_console_session(request: Any) -> Optional[ConsoleSession]:
    return getattr(getattr(request, "state", None), "console_session", None)


class ConsoleSessionResolver:
    def __init__(
        self,
        *,
        verifier: Any,
        index_store: Any,
        admin_uids: tuple[str, ...] = (),
        admin_phones: tuple[str, ...] = (),
        logger: Optional[logging.Logger] = None,
    ) -> None:
        from EdennCode.Deployment.billing.account_index import normalize_phone

        self._verifier = verifier
        self._index_store = index_store
        # UIDs are compared verbatim: Firebase UIDs are case-sensitive, and a
        # case-folding comparison would grant admin to a *different* user whom
        # an attacker could register.
        self._admin_uids = frozenset(admin_uids)
        # Phone numbers are normalized on both sides — the allowlist is typed
        # by a human, the token carries E.164.
        self._admin_phones = frozenset(
            normalize_phone(p) for p in admin_phones if normalize_phone(p)
        )
        self._logger = logger or logging.getLogger(__name__)

    @classmethod
    def from_settings(
        cls, settings: Any, *, verifier: Any, index_store: Any,
        logger: logging.Logger,
    ) -> Optional["ConsoleSessionResolver"]:
        """None when either half is missing — no verifier, no sessions."""
        if verifier is None or index_store is None:
            return None
        admin_uids = parse_allowlist(getattr(settings, "admin_firebase_uids", ""))
        admin_phones = parse_allowlist(getattr(settings, "admin_phone_numbers", ""))
        if not admin_uids and not admin_phones:
            logger.info(
                "Console sessions enabled with no session admins; "
                "/api/v1/admin/* stays x-admin-secret only.",
            )
        else:
            logger.info(
                "Console sessions enabled (%d admin uid(s), %d admin phone(s))",
                len(admin_uids), len(admin_phones),
            )
        return cls(verifier=verifier, index_store=index_store,
                   admin_uids=admin_uids, admin_phones=admin_phones,
                   logger=logger)

    async def resolve(self, presented: str) -> Optional[ConsoleSession]:
        """The session a token proves, or None when it proves nothing.

        Raises ``ConsoleSessionUnavailable`` when the identity index cannot be
        read: answering "you have no account" while storage is down would tell
        a paying customer to sign up a second time.
        """
        from EdennCode.Deployment.billing.account_index import (
            INDEX_KIND_FIREBASE,
            normalize_phone,
            normalize_uid,
        )
        from EdennCode.Deployment.billing.stores import BillingStoreUnavailable

        if self._verifier is None or not (presented or "").strip():
            return None
        try:
            identity = await self._verifier.verify(presented)
        except Exception as exc:  # noqa: BLE001 - InvalidIdentityToken and kin
            self._logger.debug("console session: token rejected (%s)",
                               getattr(exc, "reason", exc))
            return None

        uid = normalize_uid(identity.uid)
        if not uid:
            return None
        try:
            account_id = await self._index_store.lookup(INDEX_KIND_FIREBASE, uid)
        except BillingStoreUnavailable as exc:
            raise ConsoleSessionUnavailable(str(exc)) from exc

        phone = identity.phone_number or ""
        is_admin = (
            uid in self._admin_uids
            or (bool(self._admin_phones)
                and normalize_phone(phone) in self._admin_phones)
        )
        return ConsoleSession(uid=uid, phone_number=phone,
                              account_id=account_id or None, is_admin=is_admin)


__all__ = [
    "ConsoleSession",
    "ConsoleSessionResolver",
    "ConsoleSessionUnavailable",
    "get_console_session",
    "parse_allowlist",
]
