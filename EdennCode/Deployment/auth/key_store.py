"""API-key persistence: Azure Table Storage rows + in-process TTL cache.

Table ``apikeys{namespace}``: PartitionKey ``"APIKEY"``, RowKey = SHA-256 of the
full key. Plaintext keys are never stored; ``key_prefix`` (first 12 chars) is
kept for display and revocation addressing, and ``key_suffix`` (last 4) so a
console can render ``sk-XbgqH8R8r…RLYA`` — the shape every developer already
reads as "which key is this". All Table I/O runs through ``asyncio.to_thread``
so the sync SDK never blocks the event loop.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

API_KEY_PREFIX = "sk-"
KEY_PREFIX_DISPLAY_LEN = 12
# Last four characters, kept beside the prefix purely so the console can show a
# masked key that is still identifiable. A key carries 43 base64url characters
# (~256 bits) after "sk-"; showing 9 of them at the front and 4 at the back
# leaves ~180 bits unguessable, which is the same trade ModelGateway and ModelVendorAlt
# make. Raise either length and that stops being obviously true.
KEY_SUFFIX_DISPLAY_LEN = 4
KEYS_TABLE_BASE_NAME = "apikeys"
_PARTITION = "APIKEY"

# Floor between two ``last_used_at`` writes for one key. The lookup cache (60 s)
# already collapses a hot key's traffic into one table read per minute; this
# caps the resulting writes at ~288/day/key, which is plenty of resolution for
# "is anything still calling this key?" and far below anything worth batching.
LAST_USED_MIN_INTERVAL_S = 300.0


@dataclass(frozen=True)
class Principal:
    """Key-derived identity attached to a request (never client-supplied)."""

    user_id: str
    key_prefix: str


@dataclass(frozen=True)
class ApiKeyRecord:
    key_hash: str
    user_id: str
    key_prefix: str
    note: str
    created_at: str
    revoked_at: Optional[str]
    is_active: bool
    last_used_at: Optional[str] = None
    # Last, and defaulted, because keys minted before this field existed have no
    # suffix on file. "" is the honest answer for them, and the console renders
    # it as an unrevealed tail rather than inventing one.
    key_suffix: str = ""


class KeyStoreUnavailable(Exception):
    """Table Storage could not answer and no cache entry existed."""


def _merge_mode() -> Any:
    """The SDK's MERGE update mode, or the string it compares equal to.

    Imported lazily so this module keeps booting without the azure package,
    which is what lets the tests run against a duck-typed table double.
    """
    try:
        from azure.data.tables import UpdateMode

        return UpdateMode.MERGE
    except Exception:  # noqa: BLE001 - the literal is what UpdateMode.MERGE is
        return "merge"


def generate_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def display_prefix(key: str) -> str:
    return key[:KEY_PREFIX_DISPLAY_LEN]


def display_suffix(key: str) -> str:
    return key[-KEY_SUFFIX_DISPLAY_LEN:]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_not_found(exc: Exception) -> bool:
    """SDK-agnostic not-found check so fakes don't need the azure package."""
    return type(exc).__name__ in {"ResourceNotFoundError", "FakeResourceNotFound"}


def build_auth_table_client(
    settings: Any, table_name: str, logger: logging.Logger
) -> Optional[Any]:
    """Table client from deployment settings, or None when unconfigured/unreachable.

    Supports both storage auth shapes used across deployments: a full
    connection string, or the discrete account triad
    (AZURE_STORAGE_ACCOUNT_URL/NAME/KEY) that the Japan container apps use.
    Never raises — boot must not crash on storage.
    """
    conn = getattr(settings, "storage_connection_string", None)
    account_url = getattr(settings, "storage_account_url", None)
    account_name = getattr(settings, "storage_account_name", None)
    account_key = getattr(settings, "storage_account_key", None)
    if not conn and not (account_url and account_key and account_name):
        return None
    try:
        from azure.data.tables import TableServiceClient

        if conn:
            service = TableServiceClient.from_connection_string(conn)
        else:
            from azure.core.credentials import AzureNamedKeyCredential

            # Table endpoint differs from the blob URL in settings.
            endpoint = f"https://{account_name}.table.core.windows.net"
            service = TableServiceClient(
                endpoint=endpoint,
                credential=AzureNamedKeyCredential(account_name, account_key),
            )
        return service.create_table_if_not_exists(table_name)
    except Exception as exc:  # noqa: BLE001 - boot must not crash on storage
        logger.warning(
            "Auth table '%s' unavailable: %s", table_name, exc
        )
        return None


class ApiKeyStore:
    def __init__(
        self,
        table_client: Any,
        *,
        cache_ttl_s: float = 60.0,
        negative_cache_ttl_s: float = 15.0,
        clock=time.monotonic,
        logger: Optional[logging.Logger] = None,
        track_last_used: bool = True,
        last_used_min_interval_s: float = LAST_USED_MIN_INTERVAL_S,
        wall_clock=None,
    ) -> None:
        self._table_client = table_client
        self._cache_ttl_s = cache_ttl_s
        self._negative_cache_ttl_s = negative_cache_ttl_s
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self._track_last_used = track_last_used
        self._last_used_min_interval_s = last_used_min_interval_s
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        # key_hash -> (expires_at_monotonic, Principal | None)
        self._cache: dict[str, tuple[float, Optional[Principal]]] = {}
        self._pending_writes: set = set()

    # -- construction -----------------------------------------------------

    @classmethod
    def from_settings(cls, settings: Any, logger: logging.Logger) -> Optional["ApiKeyStore"]:
        """Real store from deployment settings; None when storage is unconfigured."""
        namespace = getattr(settings, "auth_table_namespace", "") or ""
        table_name = KEYS_TABLE_BASE_NAME + namespace
        table_client = build_auth_table_client(settings, table_name, logger)
        if table_client is None:
            return None
        logger.info("ApiKeyStore using table '%s'", table_name)
        return cls(table_client, logger=logger)

    # -- lookup (hot path) ------------------------------------------------

    async def lookup(self, presented_key: str) -> Optional[Principal]:
        key_hash = hash_api_key(presented_key)
        now = self._clock()
        cached = self._cache.get(key_hash)
        if cached is not None and cached[0] > now:
            return cached[1]
        try:
            entity = await asyncio.to_thread(
                self._table_client.get_entity, _PARTITION, key_hash
            )
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                self._cache[key_hash] = (now + self._negative_cache_ttl_s, None)
                return None
            raise KeyStoreUnavailable(str(exc)) from exc
        principal: Optional[Principal] = None
        if entity.get("is_active", False):
            principal = Principal(
                user_id=str(entity["user_id"]),
                key_prefix=str(entity.get("key_prefix", "")),
            )
            self._note_use(entity)
        ttl = self._cache_ttl_s if principal else self._negative_cache_ttl_s
        self._cache[key_hash] = (now + ttl, principal)
        return principal

    # -- last-used bookkeeping --------------------------------------------

    def _note_use(self, entity: dict[str, Any]) -> None:
        """Schedule a ``last_used_at`` stamp, unless one is recent enough.

        Runs only on a cache miss, so the write rate is already bounded by the
        cache TTL; the floor below trims what's left. Fire-and-forget on
        purpose — this is the authentication hot path, and a key's usage
        timestamp is never worth a millisecond of a customer's request.
        """
        if not self._track_last_used:
            return
        now = self._wall_clock()
        previous = str(entity.get("last_used_at") or "")
        if previous:
            try:
                elapsed = (now - datetime.fromisoformat(previous)).total_seconds()
            except (TypeError, ValueError):
                elapsed = None  # unreadable stamp: overwrite it
            if elapsed is not None and elapsed < self._last_used_min_interval_s:
                return
        patch = {
            "PartitionKey": _PARTITION,
            "RowKey": str(entity["RowKey"]),
            "last_used_at": now.isoformat(),
        }
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop (sync caller): nothing to schedule onto
            return
        task = loop.create_task(self._write_last_used(patch))
        self._pending_writes.add(task)
        task.add_done_callback(self._pending_writes.discard)

    async def _write_last_used(self, patch: dict[str, Any]) -> None:
        """MERGE, never replace: a full write-back would undo a concurrent revoke."""
        try:
            await asyncio.to_thread(
                self._table_client.upsert_entity, patch, mode=_merge_mode()
            )
        except Exception:  # noqa: BLE001 - bookkeeping must never surface
            self._logger.debug(
                "auth: last_used_at write failed for %s", patch["RowKey"][:12],
                exc_info=True,
            )

    async def drain(self) -> None:
        """Await the scheduled last-used writes (tests and shutdown)."""
        while self._pending_writes:
            await asyncio.gather(*list(self._pending_writes),
                                 return_exceptions=True)

    # -- management -------------------------------------------------------

    async def mint(self, *, user_id: str, note: str = "") -> tuple[str, ApiKeyRecord]:
        plaintext = generate_api_key()
        record = ApiKeyRecord(
            key_hash=hash_api_key(plaintext),
            user_id=user_id,
            key_prefix=display_prefix(plaintext),
            note=note,
            created_at=_utcnow_iso(),
            revoked_at=None,
            is_active=True,
            key_suffix=display_suffix(plaintext),
        )
        entity = {
            "PartitionKey": _PARTITION,
            "RowKey": record.key_hash,
            "user_id": record.user_id,
            "key_prefix": record.key_prefix,
            "key_suffix": record.key_suffix,
            "note": record.note,
            "created_at": record.created_at,
            "revoked_at": "",
            "is_active": True,
            "last_used_at": "",
        }
        await asyncio.to_thread(self._table_client.upsert_entity, entity)
        return plaintext, record

    async def adopt(self, record: ApiKeyRecord) -> None:
        """Store a key minted elsewhere, verbatim. Used by dual-write.

        The plaintext is generated exactly once, by whichever store is primary;
        this copies the resulting record. Minting again here would produce a
        second, different key — and the customer holds only one of them.
        """
        await asyncio.to_thread(self._table_client.upsert_entity, {
            "PartitionKey": _PARTITION,
            "RowKey": record.key_hash,
            "user_id": record.user_id,
            "key_prefix": record.key_prefix,
            "key_suffix": record.key_suffix,
            "note": record.note,
            "created_at": record.created_at,
            "revoked_at": record.revoked_at or "",
            "is_active": record.is_active,
            "last_used_at": record.last_used_at or "",
        })

    async def revoke(self, key_prefix: str) -> bool:
        revoked_any = False
        for entity in await asyncio.to_thread(self._query_all):
            if entity.get("key_prefix") == key_prefix and entity.get("is_active"):
                entity["is_active"] = False
                entity["revoked_at"] = _utcnow_iso()
                await asyncio.to_thread(self._table_client.upsert_entity, entity)
                self._cache.pop(str(entity["RowKey"]), None)
                revoked_any = True
        return revoked_any

    async def revoke_for_user(self, key_prefix: str, user_id: str) -> bool:
        """Revoke one of ``user_id``'s own keys. Never anyone else's.

        The ``user_id`` filter is authorization, not disambiguation: a
        ``key_prefix`` is printed in 详单 rows and routinely pasted into support
        tickets, so matching on it alone would let anyone who has seen one
        disable it. ``user_id`` must come from the caller's session — never
        from the request body.

        Other replicas keep serving this key until their own lookup cache
        expires (60 s). Documented in the customer guide; revocation is prompt,
        not instantaneous.
        """
        if not key_prefix or not user_id:
            return False
        revoked_any = False
        for entity in await asyncio.to_thread(self._query_all):
            if entity.get("key_prefix") != key_prefix:
                continue
            if entity.get("user_id") != user_id:
                continue
            if not entity.get("is_active"):
                continue
            patch = {
                "PartitionKey": _PARTITION,
                "RowKey": str(entity["RowKey"]),
                "is_active": False,
                "revoked_at": _utcnow_iso(),
            }
            await asyncio.to_thread(
                self._table_client.upsert_entity, patch, mode=_merge_mode()
            )
            self._cache.pop(str(entity["RowKey"]), None)
            revoked_any = True
        return revoked_any

    async def rename_for_user(
        self, key_prefix: str, user_id: str, name: str
    ) -> bool:
        """Relabel one of ``user_id``'s own active keys.

        Same account filter as ``revoke_for_user`` and for the same reason: a
        ``key_prefix`` is public enough that matching on it alone would let a
        stranger relabel someone's production key into something misleading.
        Writes only the note, so a concurrent revocation survives.
        """
        if not key_prefix or not user_id:
            return False
        for entity in await asyncio.to_thread(self._query_all):
            if entity.get("key_prefix") != key_prefix:
                continue
            if entity.get("user_id") != user_id:
                continue
            if not entity.get("is_active"):
                continue
            patch = {
                "PartitionKey": _PARTITION,
                "RowKey": str(entity["RowKey"]),
                "note": name,
            }
            await asyncio.to_thread(
                self._table_client.upsert_entity, patch, mode=_merge_mode()
            )
            return True
        return False

    async def count_active(self, user_id: str) -> int:
        if not user_id:
            return 0
        return sum(
            1
            for entity in await asyncio.to_thread(self._query_all)
            if entity.get("user_id") == user_id and entity.get("is_active")
        )

    async def list_keys(self, *, user_id: Optional[str] = None) -> list[ApiKeyRecord]:
        records = []
        for entity in await asyncio.to_thread(self._query_all):
            if user_id is not None and entity.get("user_id") != user_id:
                continue
            records.append(
                ApiKeyRecord(
                    key_hash=str(entity["RowKey"]),
                    user_id=str(entity.get("user_id", "")),
                    key_prefix=str(entity.get("key_prefix", "")),
                    key_suffix=str(entity.get("key_suffix", "")),
                    note=str(entity.get("note", "")),
                    created_at=str(entity.get("created_at", "")),
                    revoked_at=str(entity.get("revoked_at") or "") or None,
                    is_active=bool(entity.get("is_active", False)),
                    last_used_at=str(entity.get("last_used_at") or "") or None,
                )
            )
        return records

    def _query_all(self) -> list[dict[str, Any]]:
        return list(
            self._table_client.query_entities(f"PartitionKey eq '{_PARTITION}'")
        )


__all__ = [
    "API_KEY_PREFIX",
    "ApiKeyRecord",
    "ApiKeyStore",
    "KEY_PREFIX_DISPLAY_LEN",
    "KEY_SUFFIX_DISPLAY_LEN",
    "KeyStoreUnavailable",
    "Principal",
    "build_auth_table_client",
    "display_prefix",
    "display_suffix",
    "generate_api_key",
    "hash_api_key",
]
