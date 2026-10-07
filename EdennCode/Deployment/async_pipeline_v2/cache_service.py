from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional
from uuid import uuid4

from EdennCode.Deployment.async_pipeline_v2.repositories import AsyncPipelineV2Repository
from EdennCode.Deployment.postgres_wrapper import PostgresClient


# Bump a key version whenever the algorithm/policy/model that produces the cached
# value changes, so stale entries are ignored instead of returned. The compression
# version tracks the ffmpeg compression policy; the understanding version encodes the
# video-understanding model identity so a model swap auto-invalidates.
KV_COMPRESS = "c1"
KV_UNDERSTANDING = "u1"


def compression_cache_key(*, source_sha: str, max_height: int) -> str:
    """Content-addressed key for a compressed source video.

    Keyed on the source video bytes (sha256) plus the only compression input that
    changes the output (max_height) and the policy version. Independent of job, user,
    or URL: two different URLs with identical bytes resolve to the same key.
    """

    return f"compress:{KV_COMPRESS}:{source_sha}:{int(max_height)}"


def analysis_inputs_digest(inputs: dict[str, Any]) -> str:
    """Stable short digest of the exact inputs passed to the cached analysis stages.

    The understanding cache must be correct-by-construction: rather than hardcoding
    "language", key on a digest of every input that actually flows into scene
    segmentation + video understanding. Today that is the resolved language(s); if a
    future change wires the user prompt (or vocals, or scene-detection params) into
    those stages, including it here automatically changes the key and prevents serving
    a stale understanding for different inputs.
    """

    canonical = json.dumps(inputs, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def understanding_cache_key(
    *, source_sha: str, max_height: Optional[int], language: str
) -> str:
    """Content-addressed key for scene-segmentation + video-understanding output.

    Keyed on the SOURCE sha (not the prepared/compressed video) so ffmpeg
    non-determinism cannot cause false misses, plus max_height (which determines the
    prepared video) and `language` — which callers should set to
    `analysis_inputs_digest(...)` of the actual cached-stage inputs (it accepts a plain
    language string too, for simple callers/tests).
    """

    height = "none" if max_height is None else str(int(max_height))
    return f"understanding:{KV_UNDERSTANDING}:{source_sha}:{height}:{language}"


@dataclass(frozen=True)
class CacheEntry:
    cache_key: str
    kind: str
    payload_json: dict[str, Any]
    content_sha: Optional[str] = None
    key_version: Optional[str] = None
    status: str = "ready"
    size_bytes: Optional[int] = None
    hit_count: int = 0
    created_at: Optional[datetime] = None
    last_accessed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class CacheService:
    """Postgres-backed content-addressed cache for async v2 media work.

    Stores small JSON payloads that point at already-durable blobs (e.g. a compressed
    source video's container/blob_name), so a second job on the same content reuses the
    prior result instead of recompressing or re-analyzing. The cache is a pure
    optimization: callers must behave identically on a miss, and every lookup is safe
    to skip. `get_or_lease` provides single-flight so a burst of identical jobs computes
    once rather than N times.
    """

    def __init__(
        self,
        *,
        client_factory: Callable[[], PostgresClient] = PostgresClient.from_env,
        default_ttl_days: int = 14,
        ensure_schema: bool = True,
    ) -> None:
        self._client_factory = client_factory
        self._default_ttl_days = default_ttl_days
        self._ensure_schema_enabled = ensure_schema
        self._ensure_lock = threading.Lock()
        self._schema_ready = False

    def ensure_schema(self) -> None:
        if not self._ensure_schema_enabled or self._schema_ready:
            return
        with self._ensure_lock:
            if self._schema_ready:
                return
            # The cache table lives in the shared async v2 migration.
            AsyncPipelineV2Repository(client_factory=self._client_factory).ensure_schema()
            self._schema_ready = True

    @staticmethod
    def _entry_from_row(row: dict[str, Any] | None) -> Optional[CacheEntry]:
        if row is None:
            return None
        return CacheEntry(
            cache_key=str(row["cache_key"]),
            kind=str(row["kind"]),
            payload_json=dict(row.get("payload_json") or {}),
            content_sha=row.get("content_sha"),
            key_version=row.get("key_version"),
            status=str(row.get("status") or "ready"),
            size_bytes=row.get("size_bytes"),
            hit_count=int(row.get("hit_count") or 0),
            created_at=row.get("created_at"),
            last_accessed_at=row.get("last_accessed_at"),
            expires_at=row.get("expires_at"),
        )

    def get(
        self, cache_key: str, expected_kind: Optional[str] = None
    ) -> Optional[CacheEntry]:
        """Return a ready, unexpired entry and bump its access stats, else None.

        When ``expected_kind`` is provided, only an entry whose ``kind`` matches is
        returned. This is a structural guard for the shared ``async_v2_cache``
        table: one functionality can never read back another functionality's row
        even if their ``cache_key`` strings ever coincide.
        """

        self.ensure_schema()
        # ``kind_clause`` is a fixed literal (never user input), so interpolating it
        # is injection-safe; the value is bound as a parameter.
        kind_clause = "AND kind = %s\n" if expected_kind is not None else ""
        params: list[Any] = [cache_key]
        if expected_kind is not None:
            params.append(expected_kind)
        with self._client_factory() as client:
            rows = client.run_sql(
                f"""
                UPDATE async_v2_cache
                SET hit_count = hit_count + 1,
                    last_accessed_at = now()
                WHERE cache_key = %s
                  {kind_clause}  AND status = 'ready'
                  AND (expires_at IS NULL OR expires_at > now())
                RETURNING *
                """,
                params=params,
            )
        return self._entry_from_row(rows[0] if isinstance(rows, list) and rows else None)

    def put(
        self,
        cache_key: str,
        *,
        kind: str,
        payload_json: dict[str, Any],
        content_sha: Optional[str] = None,
        key_version: Optional[str] = None,
        size_bytes: Optional[int] = None,
        ttl_days: Optional[int] = None,
    ) -> CacheEntry:
        """Insert or replace a ready cache entry (idempotent upsert)."""

        self.ensure_schema()
        ttl = self._default_ttl_days if ttl_days is None else ttl_days
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                INSERT INTO async_v2_cache (
                    cache_key, kind, content_sha, key_version, status,
                    payload_json, size_bytes, hit_count, lease_owner, lease_until,
                    expires_at
                )
                VALUES (%s, %s, %s, %s, 'ready', %s, %s, 0, NULL, NULL,
                        now() + (%s || ' days')::interval)
                ON CONFLICT (cache_key) DO UPDATE SET
                    kind = EXCLUDED.kind,
                    content_sha = EXCLUDED.content_sha,
                    key_version = EXCLUDED.key_version,
                    status = 'ready',
                    payload_json = EXCLUDED.payload_json,
                    size_bytes = EXCLUDED.size_bytes,
                    lease_owner = NULL,
                    lease_until = NULL,
                    last_accessed_at = now(),
                    expires_at = EXCLUDED.expires_at
                RETURNING *
                """,
                params=[
                    cache_key,
                    kind,
                    content_sha,
                    key_version,
                    payload_json,
                    size_bytes,
                    int(ttl),
                ],
            )
        entry = self._entry_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if entry is None:
            raise RuntimeError(f"Failed to write cache entry {cache_key}.")
        return entry

    def get_or_lease(
        self,
        cache_key: str,
        *,
        kind: str,
        content_sha: Optional[str] = None,
        key_version: Optional[str] = None,
        lease_seconds: int = 300,
    ) -> tuple[Optional[CacheEntry], Optional[str]]:
        """Single-flight lookup.

        Returns one of:
          - (entry, None)        ready cache hit, use it
          - (None, lease_token)  we won the compute lease; produce the value then call
                                 complete_lease(cache_key, lease_token, ...)
          - (None, None)         someone else is computing (a live pending lease) or
                                 the value is not ready; the caller should compute
                                 without caching this round (no duplicate result, just
                                 a missed cache write)
        """

        self.ensure_schema()
        ready = self.get(cache_key, expected_kind=kind)
        if ready is not None:
            return ready, None

        lease_token = uuid4().hex
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                INSERT INTO async_v2_cache (
                    cache_key, kind, content_sha, key_version, status,
                    lease_owner, lease_until
                )
                VALUES (%s, %s, %s, %s, 'pending', %s, now() + (%s || ' seconds')::interval)
                ON CONFLICT (cache_key) DO UPDATE SET
                    lease_owner = EXCLUDED.lease_owner,
                    lease_until = EXCLUDED.lease_until,
                    status = 'pending'
                WHERE async_v2_cache.status = 'pending'
                  AND (async_v2_cache.lease_until IS NULL
                       OR async_v2_cache.lease_until < now())
                RETURNING cache_key
                """,
                params=[
                    cache_key,
                    kind,
                    content_sha,
                    key_version,
                    lease_token,
                    int(lease_seconds),
                ],
            )
        if isinstance(rows, list) and rows:
            return None, lease_token
        # Either a ready entry appeared concurrently, or a live pending lease is held.
        ready = self.get(cache_key, expected_kind=kind)
        return (ready, None) if ready is not None else (None, None)

    def complete_lease(
        self,
        cache_key: str,
        lease_token: str,
        *,
        kind: str,
        payload_json: dict[str, Any],
        content_sha: Optional[str] = None,
        key_version: Optional[str] = None,
        size_bytes: Optional[int] = None,
        ttl_days: Optional[int] = None,
    ) -> Optional[CacheEntry]:
        """Publish a computed value for a lease we own. No-op if the lease was lost."""

        self.ensure_schema()
        ttl = self._default_ttl_days if ttl_days is None else ttl_days
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                UPDATE async_v2_cache
                SET status = 'ready',
                    kind = %s,
                    content_sha = %s,
                    key_version = %s,
                    payload_json = %s,
                    size_bytes = %s,
                    lease_owner = NULL,
                    lease_until = NULL,
                    created_at = now(),
                    last_accessed_at = now(),
                    expires_at = now() + (%s || ' days')::interval
                WHERE cache_key = %s
                  AND lease_owner = %s
                RETURNING *
                """,
                params=[
                    kind,
                    content_sha,
                    key_version,
                    payload_json,
                    size_bytes,
                    int(ttl),
                    cache_key,
                    lease_token,
                ],
            )
        return self._entry_from_row(rows[0] if isinstance(rows, list) and rows else None)

    def release_lease(self, cache_key: str, lease_token: str) -> None:
        """Drop a pending row we own (e.g. the compute failed), freeing the key."""

        self.ensure_schema()
        with self._client_factory() as client:
            client.run_sql(
                """
                DELETE FROM async_v2_cache
                WHERE cache_key = %s AND lease_owner = %s AND status = 'pending'
                """,
                params=[cache_key, lease_token],
            )

    def evict_expired(self, *, limit: int = 500) -> int:
        """Delete expired entries; returns the number removed. Blob cleanup is separate."""

        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                WITH expired AS (
                    SELECT cache_key FROM async_v2_cache
                    WHERE expires_at IS NOT NULL AND expires_at < now()
                    ORDER BY expires_at
                    LIMIT %s
                )
                DELETE FROM async_v2_cache c USING expired e
                WHERE c.cache_key = e.cache_key
                RETURNING c.cache_key
                """,
                params=[int(limit)],
            )
        return len(rows) if isinstance(rows, list) else 0


__all__ = [
    "CacheService",
    "CacheEntry",
    "compression_cache_key",
    "understanding_cache_key",
    "analysis_inputs_digest",
    "KV_COMPRESS",
    "KV_UNDERSTANDING",
]
