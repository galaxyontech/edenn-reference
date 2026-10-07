"""Postgres API-key and contact-index stores.

Drop-in replacements for ``auth.key_store.ApiKeyStore`` and
``billing.account_index.AccountIndexStore``, keeping the same dataclasses, the
same caches, and the same TTLs — a revoked key still lingers for up to 60
seconds on other replicas, exactly as documented to customers today.

Two silent hazards the Table Storage versions carried, closed by the schema:

* Revocation scanned the whole table and matched on ``key_prefix`` alone. Two
  keys sharing a prefix (54 bits of entropy — unlikely, not impossible) would
  both be revoked, across account boundaries. ``api_keys_prefix_uniq`` makes
  the case unrepresentable.
* Contact claims raced on ``create_entity``. Here the primary key
  ``(kind, value)`` does it, and the readable value is stored as-is: Postgres
  has none of Table Storage's forbidden-character rules, so the SHA-256 row-key
  indirection is gone and an operator can read the table directly.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from EdennCode.Deployment.auth.key_store import (
    ApiKeyRecord,
    KeyStoreUnavailable,
    LAST_USED_MIN_INTERVAL_S,
    Principal,
    display_prefix,
    display_suffix,
    generate_api_key,
    hash_api_key,
)
from EdennCode.Deployment.billing.pg_stores import BillingPgPool, _iso
from EdennCode.Deployment.billing.stores import BillingStoreUnavailable

_LOGGER = logging.getLogger(__name__)

# How long past its normal 60 s expiry a positive cache entry may still admit a
# request while the database is unreachable. Long enough to ride out a failover
# or a short maintenance window; short enough that a revocation issued during
# one takes effect the same day. See PgApiKeyStore._serve_stale.
STALE_CACHE_GRACE_S = 900.0

_KEY_COLUMNS = """
    key_hash, account_id, key_prefix, key_suffix, note, created_at,
    revoked_at, is_active, last_used_at
"""


def _key_from_row(row: Any) -> ApiKeyRecord:
    return ApiKeyRecord(
        key_hash=str(row["key_hash"]),
        user_id=str(row["account_id"]),
        key_prefix=str(row["key_prefix"]),
        key_suffix=str(row["key_suffix"] or ""),
        note=str(row["note"]),
        created_at=_iso(row["created_at"]),
        revoked_at=_iso(row["revoked_at"]) or None,
        is_active=bool(row["is_active"]),
        last_used_at=_iso(row["last_used_at"]) or None,
    )


class PgApiKeyStore:
    """API keys in Postgres. ``user_id`` in the API is ``account_id`` in the DB.

    The rename is not cosmetic: in Table Storage the two were separate strings
    that happened to match, and nothing enforced it. Here ``account_id`` is a
    foreign key, so a key can no longer point at an account that does not exist.
    """

    def __init__(
        self,
        pool: BillingPgPool,
        *,
        cache_ttl_s: float = 60.0,
        negative_cache_ttl_s: float = 15.0,
        clock=time.monotonic,
        logger: Optional[logging.Logger] = None,
        track_last_used: bool = True,
        last_used_min_interval_s: float = LAST_USED_MIN_INTERVAL_S,
        wall_clock=None,
        stale_grace_s: float = STALE_CACHE_GRACE_S,
    ) -> None:
        self._pool = pool
        self._cache_ttl_s = cache_ttl_s
        self._negative_cache_ttl_s = negative_cache_ttl_s
        self._clock = clock
        self._logger = logger or _LOGGER
        self._track_last_used = track_last_used
        self._last_used_min_interval_s = last_used_min_interval_s
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._stale_grace_s = stale_grace_s
        self._cache: dict[str, tuple[float, Optional[Principal]]] = {}
        self._pending_writes: set = set()

    # -- lookup (hot path) -------------------------------------------------

    async def lookup(self, presented_key: str) -> Optional[Principal]:
        key_hash = hash_api_key(presented_key)
        now = self._clock()
        cached = self._cache.get(key_hash)
        if cached is not None and cached[0] > now:
            return cached[1]
        try:
            row = await self._pool.fetchrow(
                """SELECT key_hash, account_id, key_prefix, is_active, last_used_at
                     FROM api_keys WHERE key_hash = $1""",
                key_hash,
            )
        except BillingStoreUnavailable as exc:
            stale = self._serve_stale(key_hash, cached, now, exc)
            if stale is not None:
                return stale
            raise KeyStoreUnavailable(str(exc)) from exc
        principal: Optional[Principal] = None
        if row is not None and row["is_active"]:
            principal = Principal(
                user_id=str(row["account_id"]),
                key_prefix=str(row["key_prefix"] or ""),
            )
            self._note_use(key_hash, row["last_used_at"])
        ttl = self._cache_ttl_s if principal else self._negative_cache_ttl_s
        self._cache[key_hash] = (now + ttl, principal)
        return principal

    def _serve_stale(self, key_hash, cached, now, exc) -> Optional[Principal]:
        """Keep a recently-valid key working through a database outage.

        Moving authentication onto Postgres changes its availability story: a
        maintenance window used to be invisible to auth, and after the
        migration a cache miss during one would 401 a paying customer in
        enforce mode. Inheriting fail-closed here would make the migration a
        downgrade in availability for no security gain — the key was valid
        sixty seconds ago, and the grace window is bounded.

        Only positive entries are extended. A cached *miss* is not a licence
        to admit an unknown key, and a revocation issued during the outage is
        the case the bound exists for: worst case a revoked key keeps working
        for the grace period, which is the same promise already documented for
        the 60-second cache, just longer.
        """
        if cached is None or cached[1] is None:
            return None
        if now >= cached[0] + self._stale_grace_s:
            return None
        self._logger.warning(
            "auth: billing database unreachable (%s); serving key %s from a "
            "stale cache entry for up to %.0fs",
            exc, key_hash[:12], self._stale_grace_s,
        )
        return cached[1]

    # -- last-used bookkeeping --------------------------------------------

    def _note_use(self, key_hash: str, previous: Any) -> None:
        """Fire-and-forget ``last_used_at`` stamp, throttled.

        Only runs on a cache miss, so the write rate is already bounded by the
        60 s lookup cache; the interval floor trims what remains. A key's usage
        timestamp is never worth a millisecond of a customer's request.
        """
        if not self._track_last_used:
            return
        now = self._wall_clock()
        if isinstance(previous, datetime):
            prev = previous if previous.tzinfo else previous.replace(
                tzinfo=timezone.utc)
            if (now - prev).total_seconds() < self._last_used_min_interval_s:
                return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop (sync caller): nothing to schedule onto
            return
        task = loop.create_task(self._write_last_used(key_hash, now))
        self._pending_writes.add(task)
        task.add_done_callback(self._pending_writes.discard)

    async def _write_last_used(self, key_hash: str, when: datetime) -> None:
        """One column only — a wider write could undo a concurrent revoke."""
        try:
            await self._pool.execute(
                "UPDATE api_keys SET last_used_at = $2 WHERE key_hash = $1",
                key_hash, when,
            )
        except Exception:  # noqa: BLE001 - bookkeeping must never surface
            self._logger.debug("auth: last_used_at write failed for %s",
                               key_hash[:12], exc_info=True)

    async def drain(self) -> None:
        while self._pending_writes:
            await asyncio.gather(*list(self._pending_writes),
                                 return_exceptions=True)

    # -- management --------------------------------------------------------

    async def mint(self, *, user_id: str, note: str = "",
                   created_via: str = "admin", created_by: str = "") -> tuple[str, ApiKeyRecord]:
        plaintext = generate_api_key()
        row = await self._pool.fetchrow(
            f"""INSERT INTO api_keys (key_hash, account_id, key_prefix,
                                      key_suffix, note, created_via, created_by)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                RETURNING {_KEY_COLUMNS}""",
            hash_api_key(plaintext), user_id, display_prefix(plaintext),
            display_suffix(plaintext), note, created_via, created_by,
        )
        return plaintext, _key_from_row(row)

    async def adopt(self, record: ApiKeyRecord) -> None:
        """Store a key minted elsewhere, verbatim. Used by dual-write.

        The plaintext is generated exactly once, by whichever store is primary;
        this copies the resulting record. Minting again here would produce a
        second, different key — and the customer holds only one of them.
        """
        from EdennCode.Deployment.billing.pg_stores import _parse_ts

        await self._pool.execute(
            """INSERT INTO api_keys (key_hash, account_id, key_prefix,
                    key_suffix, note, created_at, revoked_at, last_used_at,
                    is_active)
               VALUES ($1,$2,$3,$4,$5,COALESCE($6, now()),$7,$8,$9)
               ON CONFLICT (key_hash) DO NOTHING""",
            record.key_hash, record.user_id, record.key_prefix,
            record.key_suffix, record.note,
            _parse_ts(record.created_at), _parse_ts(record.revoked_at),
            _parse_ts(record.last_used_at), record.is_active,
        )

    async def revoke(self, key_prefix: str) -> bool:
        """Admin revocation by prefix. At most one row — the index guarantees it."""
        rows = await self._pool.fetch(
            """UPDATE api_keys SET is_active = false, revoked_at = now()
                WHERE key_prefix = $1 AND is_active
            RETURNING key_hash""",
            key_prefix,
        )
        for row in rows:
            self._cache.pop(str(row["key_hash"]), None)
        return bool(rows)

    async def revoke_for_user(self, key_prefix: str, user_id: str) -> bool:
        """Revoke one of ``user_id``'s own keys. Never anyone else's.

        The ``account_id`` filter is authorization, not disambiguation: a
        ``key_prefix`` appears in 详单 rows and support tickets, so matching on
        it alone would let anyone who has seen one disable it. ``user_id`` must
        come from the caller's session, never from the request body.
        """
        if not key_prefix or not user_id:
            return False
        rows = await self._pool.fetch(
            """UPDATE api_keys SET is_active = false, revoked_at = now()
                WHERE key_prefix = $1 AND account_id = $2 AND is_active
            RETURNING key_hash""",
            key_prefix, user_id,
        )
        for row in rows:
            self._cache.pop(str(row["key_hash"]), None)
        return bool(rows)

    async def rename_for_user(
        self, key_prefix: str, user_id: str, name: str
    ) -> bool:
        """Relabel one of ``user_id``'s own active keys.

        Same account filter as ``revoke_for_user`` and for the same reason: a
        ``key_prefix`` is public enough that matching on it alone would let a
        stranger relabel someone's production key into something misleading.
        Touches ``note`` only, so a concurrent revocation survives.
        """
        if not key_prefix or not user_id:
            return False
        rows = await self._pool.fetch(
            """UPDATE api_keys SET note = $3
                WHERE key_prefix = $1 AND account_id = $2 AND is_active
            RETURNING key_hash""",
            key_prefix, user_id, name,
        )
        return bool(rows)

    async def count_active(self, user_id: str) -> int:
        if not user_id:
            return 0
        return int(await self._pool.fetchval(
            "SELECT count(*) FROM api_keys WHERE account_id = $1 AND is_active",
            user_id,
        ) or 0)

    async def list_keys(self, *, user_id: Optional[str] = None) -> list[ApiKeyRecord]:
        if user_id is None:
            rows = await self._pool.fetch(
                f"SELECT {_KEY_COLUMNS} FROM api_keys ORDER BY created_at DESC")
        else:
            rows = await self._pool.fetch(
                f"""SELECT {_KEY_COLUMNS} FROM api_keys WHERE account_id = $1
                     ORDER BY created_at DESC""",
                user_id,
            )
        return [_key_from_row(r) for r in rows]


class PgAccountIndexStore:
    """Contact (phone/email/firebase) -> account.

    Kinds are lowercase here (``phone``/``email``/``firebase``) because that is
    what the table's CHECK constraint allows; the Table Storage store used
    uppercase partition keys. Callers keep passing the uppercase constants and
    this store folds them, so the switch needs no caller changes — and the
    backfill must fold them too.
    """

    def __init__(
        self, pool: BillingPgPool, *, logger: Optional[logging.Logger] = None
    ) -> None:
        self._pool = pool
        self._logger = logger or _LOGGER

    @staticmethod
    def _kind(kind: str) -> str:
        return str(kind or "").strip().lower()

    async def lookup(self, kind: str, normalized_value: str) -> Optional[str]:
        if not normalized_value:
            # An empty contact matches nothing. Without this guard a caller
            # whose input normalized to "" (normalize_phone("---")) would be
            # handed whichever account claimed the empty row first — and its
            # wallet.
            return None
        account_id = await self._pool.fetchval(
            "SELECT account_id FROM account_identities WHERE kind = $1 AND value = $2",
            self._kind(kind), normalized_value,
        )
        return str(account_id) if account_id else None

    async def claim(self, kind: str, normalized_value: str, account_id: str) -> bool:
        """Bind this contact to an account. False when someone already owns it.

        ``ON CONFLICT DO NOTHING`` is the atomic claim: two concurrent signups
        with the same phone number cannot both win, without a lock or a retry.
        """
        if not normalized_value:
            return False
        result = await self._pool.execute(
            """INSERT INTO account_identities (kind, value, account_id)
               VALUES ($1,$2,$3) ON CONFLICT (kind, value) DO NOTHING""",
            self._kind(kind), normalized_value, account_id,
        )
        claimed = not str(result).endswith(" 0")
        if not claimed:
            self._logger.debug(
                "account index: %s row already claimed; '%s' did not take it",
                self._kind(kind), account_id)
        return claimed

    async def list_for_account(self, account_id: str) -> list[dict[str, str]]:
        """Every contact bound to one account — the admin lookup's reverse view.

        Table Storage could not answer this without scanning the whole index;
        here it is one index hit.
        """
        rows = await self._pool.fetch(
            """SELECT kind, value, created_at FROM account_identities
                WHERE account_id = $1 ORDER BY kind, value""",
            account_id,
        )
        return [
            {"kind": str(r["kind"]), "value": str(r["value"]),
             "created_at": _iso(r["created_at"])}
            for r in rows
        ]


__all__ = ["PgAccountIndexStore", "PgApiKeyStore"]
