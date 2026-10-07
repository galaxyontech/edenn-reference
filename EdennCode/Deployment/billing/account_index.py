"""Contact-to-account index: how a repeat signup finds its existing account.

One row per (kind, normalized contact). Claims go through ``create_entity``,
which fails when the row already exists — that is what makes "first signup with
this email owns the account" atomic rather than a read-then-write race two
concurrent requests would both win.

RowKey is the SHA-256 of the normalized value, not the value itself: Table
Storage forbids ``/ \\ # ?`` and control characters in row keys, and a normalized
email may legally contain ``#``. The readable value is kept in a column so an
operator can still see what a row means.

Subproject 4 (Firebase sign-in) adds a ``FIREBASE`` kind to the same table.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from EdennCode.Deployment.billing.stores import (
    BillingStoreUnavailable,
    _is_already_exists,
    _is_not_found,
)

ACCOUNT_INDEX_TABLE_BASE_NAME = "acctindex"
INDEX_KIND_PHONE = "PHONE"
INDEX_KIND_EMAIL = "EMAIL"
INDEX_KIND_FIREBASE = "FIREBASE"

_CN_MOBILE_WITH_CC_LEN = 13  # 86 + 11-digit mainland mobile


def normalize_email(raw: Any) -> str:
    """Case- and whitespace-insensitive; plus-suffixes stay distinct."""
    return str(raw or "").strip().lower()


def normalize_uid(raw: Any) -> str:
    """Whitespace only. Firebase UIDs are case-sensitive — never lowercase one.

    ``aB3`` and ``Ab3`` are two different Firebase users; folding case here
    would let one of them claim the other's account.
    """
    return str(raw or "").strip()


def normalize_phone(raw: Any) -> str:
    """Digits only, with a leading mainland country code unwrapped.

    Other countries' country codes are left in place, so `+1 415…` and
    `415…` do not merge. Documented limitation, not an oversight: unwrapping
    arbitrary country codes needs a real phone-number library.
    """
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == _CN_MOBILE_WITH_CC_LEN and digits.startswith("86"):
        digits = digits[2:]
    return digits


def _row_key(normalized_value: str) -> str:
    return hashlib.sha256(normalized_value.encode("utf-8")).hexdigest()


class AccountIndexStore:
    def __init__(
        self, table_client: Any, *, logger: Optional[logging.Logger] = None
    ) -> None:
        self._table_client = table_client
        self._logger = logger or logging.getLogger(__name__)

    @classmethod
    def from_settings(
        cls, settings: Any, logger: logging.Logger
    ) -> Optional["AccountIndexStore"]:
        from EdennCode.Deployment.auth.key_store import build_auth_table_client

        namespace = getattr(settings, "auth_table_namespace", "") or ""
        table_name = ACCOUNT_INDEX_TABLE_BASE_NAME + namespace
        table_client = build_auth_table_client(settings, table_name, logger)
        if table_client is None:
            return None
        logger.info("AccountIndexStore using table '%s'", table_name)
        return cls(table_client, logger=logger)

    async def lookup(self, kind: str, normalized_value: str) -> Optional[str]:
        """The account owning this contact, or None when unclaimed."""
        if not normalized_value:
            # An empty contact matches nothing. Do NOT "simplify" this away:
            # sha256("") is a valid row key, so without the guard a caller whose
            # input normalized to "" (normalize_phone("---")) would be handed
            # whichever account claimed that row first — and its wallet.
            return None
        try:
            entity = await asyncio.to_thread(
                self._table_client.get_entity, kind, _row_key(normalized_value)
            )
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                return None
            raise BillingStoreUnavailable(str(exc)) from exc
        return str(entity.get("account_id", "")) or None

    async def claim(
        self, kind: str, normalized_value: str, account_id: str
    ) -> bool:
        """Bind this contact to an account. False when someone already owns it."""
        if not normalized_value:
            # Nothing to own, and False already means "you don't own this".
            # The guard lives here rather than in each caller so no future
            # caller can let two unrelated contact-less signups both claim the
            # sha256("") row and merge onto one wallet. Touches no storage.
            return False
        entity = {
            "PartitionKey": kind,
            "RowKey": _row_key(normalized_value),
            "value": normalized_value,
            "account_id": account_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await asyncio.to_thread(self._table_client.create_entity, entity)
        except Exception as exc:  # noqa: BLE001
            if _is_already_exists(exc):
                # The interesting half of a merge: who lost the race, and for
                # which row. Hash prefix only — logs are not the place to
                # accumulate contact details.
                self._logger.debug(
                    "account index: %s row %s already claimed; '%s' did not "
                    "take it", kind, entity["RowKey"][:12], account_id,
                )
                return False
            raise BillingStoreUnavailable(str(exc)) from exc
        return True


__all__ = [
    "ACCOUNT_INDEX_TABLE_BASE_NAME",
    "AccountIndexStore",
    "INDEX_KIND_EMAIL",
    "INDEX_KIND_FIREBASE",
    "INDEX_KIND_PHONE",
    "normalize_email",
    "normalize_phone",
    "normalize_uid",
]
