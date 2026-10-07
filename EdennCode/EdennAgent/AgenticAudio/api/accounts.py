"""Which account a signed-in person belongs to.

The studio knows a **principal** — a verified uid, the thing ownership,
membership and quotas are already keyed on. Money lives on a platform
**account**. Nothing joined the two, so "whose balance does this render come
out of" had no answer at all; not a wrong one, an absent one.

This module is the seam, and deliberately only the seam. The directory itself
is injected, exactly as the identity verifier is, because *how* the studio
reads the platform's account index is an open decision with two real answers:

* read the platform's account index directly, which means lifting the
  config-level refusal of the billing database host (a deliberate isolation
  rule, so lifting it is a deliberate change with a reason written down); or
* ask the platform over an admin-guarded internal endpoint, which keeps the
  isolation and adds a network dependency to authentication.

Until one is chosen and wired, no directory is configured, `account_for`
answers ``None``, and that is recorded as "not established" — never as "free".
Nothing here refuses anything: enforcement arrives with billing, and enforcing
before the numbers are instrumented is how a customer gets told they are out of
credit by a system that has never counted any.

Answers are cached briefly. Authentication happens on every request, and the
account of a uid changes about once in its life, so a lookup per request would
be a network round trip added to every call to buy information that did not
change. A negative answer is cached for much less: it is the one that flips,
the moment somebody finishes signing up.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)

#: How long a known account is trusted. Long: this does not change.
HIT_TTL_S = 900.0
#: How long "no account" is trusted. Short: this is the answer that flips.
MISS_TTL_S = 30.0


class AccountDirectory(Protocol):
    """The half of the platform's account index this module needs."""

    async def account_for(self, uid: str) -> Optional[str]:
        ...


class AccountsUnavailable(Exception):
    """The index could not answer.

    Distinct from "this uid has no account", and the distinction is the whole
    point: one is a fact about the person, the other is a fact about our
    infrastructure, and only the first may ever be held against them.
    """


class _Directory:
    def __init__(self, directory: Optional[AccountDirectory] = None) -> None:
        self._directory = directory
        self._cache: dict[str, tuple[Optional[str], float]] = {}

    @property
    def configured(self) -> bool:
        return self._directory is not None

    def set_directory(self, directory: Optional[AccountDirectory]) -> None:
        self._directory = directory
        self._cache.clear()

    def forget(self, uid: str) -> None:
        self._cache.pop(uid, None)

    async def account_for(self, uid: str) -> Optional[str]:
        key = (uid or "").strip()
        if not key or self._directory is None:
            return None
        now = time.time()
        cached = self._cache.get(key)
        if cached is not None and now < cached[1]:
            return cached[0]
        account_id = await self._directory.account_for(key)
        account_id = str(account_id).strip() if account_id else None
        self._cache[key] = (
            account_id,
            now + (HIT_TTL_S if account_id else MISS_TTL_S),
        )
        return account_id


_directory = _Directory()


def set_directory(directory: Optional[AccountDirectory]) -> None:
    _directory.set_directory(directory)


def directory_configured() -> bool:
    return _directory.configured


async def account_for(uid: str) -> Optional[str]:
    """The account behind a uid, or ``None`` when there is none to be had.

    Raises :class:`AccountsUnavailable` only if a configured directory chooses
    to; a directory that answers "I don't know" by raising is telling the truth,
    and the caller decides what to do about it.
    """

    return await _directory.account_for(uid)


def forget(uid: str) -> None:
    """Drop a cached answer — after a signup, or in a test."""

    _directory.forget(uid)


def reset_for_tests() -> None:
    _directory.set_directory(None)


__all__ = [
    "AccountDirectory",
    "AccountsUnavailable",
    "account_for",
    "directory_configured",
    "forget",
    "reset_for_tests",
    "set_directory",
]
