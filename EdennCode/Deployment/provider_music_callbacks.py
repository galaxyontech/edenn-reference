from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlencode, urlparse, urlunparse

from EdennCode.Deployment.postgres_wrapper import PostgresClient
from EdennCode.exceptions import (
    EdennConfigurationError,
    EdennProviderResponseError,
    EdennProviderTimeoutError,
)


logger = logging.getLogger(__name__)

_PROVIDER_CALLBACK_TABLE = "provider_music_callbacks"

# Provider keys whose wait-for-callback path, when enabled, requires a durable
# (cross-container) callback store. Used only for internal store routing.
_WAIT_CHECK_PROVIDERS: tuple[str, ...] = ("provider_c",)


@dataclass
class ProviderMusicCallbackEvent:
    provider: str
    task_id: str
    callback_type: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    received_at: int = field(default_factory=lambda: int(time.time()))


class InMemoryProviderMusicCallbackStore:
    def __init__(
        self,
        backing_store: Optional[dict[tuple[str, str], ProviderMusicCallbackEvent]] = None,
    ) -> None:
        self._events = backing_store if backing_store is not None else {}
        self._lock = threading.Lock()

    @property
    def events(self) -> dict[tuple[str, str], ProviderMusicCallbackEvent]:
        return self._events

    def upsert(self, event: ProviderMusicCallbackEvent) -> ProviderMusicCallbackEvent:
        with self._lock:
            self._events[(event.provider, event.task_id)] = event
        return event

    def get(self, provider: str, task_id: str) -> Optional[ProviderMusicCallbackEvent]:
        with self._lock:
            return self._events.get((_normalize_provider(provider), task_id.strip()))

    def cleanup_expired(self, *, ttl_seconds: int, now: Optional[int] = None) -> None:
        if ttl_seconds <= 0:
            return
        current_time = now if now is not None else int(time.time())
        with self._lock:
            for key, event in list(self._events.items()):
                if current_time - int(event.received_at) >= ttl_seconds:
                    self._events.pop(key, None)


class PostgresProviderMusicCallbackStore:
    def __init__(
        self,
        *,
        client_factory: Callable[[], PostgresClient] = PostgresClient.from_env,
    ) -> None:
        self._client_factory = client_factory
        self._ensure_lock = threading.Lock()
        self._schema_ready = False

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._ensure_lock:
            if self._schema_ready:
                return
            with self._client_factory() as client:
                client.run_sql(
                    f"""
                    CREATE TABLE IF NOT EXISTS {_PROVIDER_CALLBACK_TABLE} (
                        provider TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        callback_type TEXT NOT NULL DEFAULT '',
                        payload_json JSONB NOT NULL,
                        received_at BIGINT NOT NULL,
                        PRIMARY KEY (provider, task_id)
                    )
                    """
                )
                client.run_sql(
                    f"""
                    CREATE INDEX IF NOT EXISTS {_PROVIDER_CALLBACK_TABLE}_received_idx
                    ON {_PROVIDER_CALLBACK_TABLE} (received_at)
                    """
                )
            self._schema_ready = True

    @staticmethod
    def _event_from_row(row: dict[str, Any] | None) -> Optional[ProviderMusicCallbackEvent]:
        if row is None:
            return None
        payload = row.get("payload_json") or {}
        if not isinstance(payload, dict):
            payload = {"payload": payload}
        return ProviderMusicCallbackEvent(
            provider=str(row["provider"]),
            task_id=str(row["task_id"]),
            callback_type=str(row.get("callback_type") or ""),
            payload=payload,
            received_at=int(row["received_at"]),
        )

    def upsert(self, event: ProviderMusicCallbackEvent) -> ProviderMusicCallbackEvent:
        self._ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                f"""
                INSERT INTO {_PROVIDER_CALLBACK_TABLE} (
                    provider, task_id, callback_type, payload_json, received_at
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (provider, task_id) DO UPDATE SET
                    callback_type = EXCLUDED.callback_type,
                    payload_json = EXCLUDED.payload_json,
                    received_at = EXCLUDED.received_at
                RETURNING *
                """,
                params=[
                    event.provider,
                    event.task_id,
                    event.callback_type,
                    event.payload,
                    event.received_at,
                ],
            )
        stored = self._event_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if stored is None:
            raise KeyError(f"{event.provider}:{event.task_id}")
        return stored

    def get(self, provider: str, task_id: str) -> Optional[ProviderMusicCallbackEvent]:
        self._ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                f"""
                SELECT *
                FROM {_PROVIDER_CALLBACK_TABLE}
                WHERE provider = %s AND task_id = %s
                LIMIT 1
                """,
                params=[_normalize_provider(provider), task_id.strip()],
            )
        return self._event_from_row(rows[0] if isinstance(rows, list) and rows else None)

    def cleanup_expired(self, *, ttl_seconds: int, now: Optional[int] = None) -> None:
        if ttl_seconds <= 0:
            return
        self._ensure_schema()
        cutoff = (now if now is not None else int(time.time())) - ttl_seconds
        with self._client_factory() as client:
            client.run_sql(
                f"""
                DELETE FROM {_PROVIDER_CALLBACK_TABLE}
                WHERE received_at <= %s
                """,
                params=[cutoff],
            )


_provider_callback_events: dict[tuple[str, str], ProviderMusicCallbackEvent] = {}
_memory_provider_music_callback_store = InMemoryProviderMusicCallbackStore(
    _provider_callback_events
)
_provider_music_callback_store: Any | None = None
_provider_music_callback_store_override: Any | None = None


def _normalize_provider(provider: str) -> str:
    return (provider or "").strip().lower()


def _env_truthy(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _has_postgres_env() -> bool:
    return bool(
        os.getenv("DATABASE_URL")
        or (os.getenv("PGHOST") and os.getenv("PGDATABASE") and os.getenv("PGUSER"))
    )


def build_provider_music_callback_store_from_env(
    *,
    memory_store: Optional[InMemoryProviderMusicCallbackStore] = None,
    client_factory: Optional[Callable[[], PostgresClient]] = None,
) -> InMemoryProviderMusicCallbackStore | PostgresProviderMusicCallbackStore:
    # NOTE: ASYNC_VIDEO_MUSIC_JOB_STORE is honored as a secondary selector for
    # backward compatibility; setting it to 'memory' also forces the callback
    # store to memory (the fail-closed guard below catches that when the wait
    # path is enabled). Prefer PROVIDER_MUSIC_CALLBACK_STORE.
    raw_mode = (
        os.getenv("PROVIDER_MUSIC_CALLBACK_STORE")
        or os.getenv("ASYNC_VIDEO_MUSIC_JOB_STORE")
        or "auto"
    ).strip().lower()
    mode = "postgres" if raw_mode == "auto" and _has_postgres_env() else raw_mode
    if mode == "postgres":
        # client_factory (a pooled factory) is optional; default None keeps the
        # store's own PostgresClient.from_env behavior for existing callers.
        if client_factory is not None:
            return PostgresProviderMusicCallbackStore(client_factory=client_factory)
        return PostgresProviderMusicCallbackStore()
    if mode in {"auto", "memory", "inmemory", "in-memory"}:
        return memory_store or InMemoryProviderMusicCallbackStore()
    raise ValueError(
        "PROVIDER_MUSIC_CALLBACK_STORE must be 'auto', 'postgres', or 'memory'."
    )


def _providers_awaiting_callback(
    providers: Sequence[str] = _WAIT_CHECK_PROVIDERS,
) -> list[str]:
    """Providers whose wait-for-callback path is currently enabled."""
    return [
        provider
        for provider in providers
        if should_wait_for_provider_callback(provider, provider_callback_url(provider))
    ]


def get_provider_music_callback_store() -> Any:
    if _provider_music_callback_store_override is not None:
        return _provider_music_callback_store_override
    global _provider_music_callback_store
    if _provider_music_callback_store is None:
        store = build_provider_music_callback_store_from_env(
            memory_store=_memory_provider_music_callback_store,
        )
        # Defense in depth: if a code path reaches the store lazily (bypassing the
        # startup resolver) with an in-memory store while the wait path is on,
        # warn rather than raise so no-DB imports and the test override still work.
        if isinstance(store, InMemoryProviderMusicCallbackStore) and _providers_awaiting_callback():
            logger.warning(
                "Provider callback store resolved to in-memory while the callback "
                "wait path is enabled; callbacks are per-process and invisible across "
                "containers. Configure a durable store "
                "(PROVIDER_MUSIC_CALLBACK_STORE=postgres)."
            )
        _provider_music_callback_store = store
    return _provider_music_callback_store


def set_provider_music_callback_store(store: Any) -> None:
    """Install the process-wide callback store (production startup)."""
    global _provider_music_callback_store
    _provider_music_callback_store = store
    logger.info(
        "Provider callback store backend: %s",
        type(store).__name__,
    )


def resolve_provider_music_callback_store(
    *,
    client_factory: Optional[Callable[[], PostgresClient]] = None,
    providers: Sequence[str] = _WAIT_CHECK_PROVIDERS,
    logger: Optional[logging.Logger] = None,
) -> Any:
    """Build, validate (fail-closed), and install the process callback store.

    Fail-closed: when any provider's wait-for-callback path is enabled but the
    resolved store is in-memory (per-process, invisible across containers), raise
    so the process refuses to boot instead of silently timing out every awaited
    job. When a test override is already installed, this is a no-op that returns
    the override.
    """
    log = logger or globals()["logger"]
    if _provider_music_callback_store_override is not None:
        return _provider_music_callback_store_override

    store = build_provider_music_callback_store_from_env(
        memory_store=_memory_provider_music_callback_store,
        client_factory=client_factory,
    )
    waiting = _providers_awaiting_callback(providers)
    if waiting and isinstance(store, InMemoryProviderMusicCallbackStore):
        raise EdennConfigurationError(
            "The callback wait path is enabled but the callback store resolved to "
            "in-memory, which is per-process and invisible across containers, so "
            "every awaited job would time out. Set PROVIDER_MUSIC_CALLBACK_STORE="
            "postgres and provide Postgres connection env (PGHOST/PGDATABASE/PGUSER "
            "or DATABASE_URL).",
            component="provider_music_callbacks",
            operation="resolve_store",
        )
    set_provider_music_callback_store(store)
    return store


def set_provider_music_callback_store_for_testing(store: Any | None) -> None:
    global _provider_music_callback_store_override
    _provider_music_callback_store_override = store


def _first_str(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _payload_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def extract_provider_task_id(provider: str, payload: dict[str, Any]) -> str:
    normalized = _normalize_provider(provider)
    data = _payload_data(payload)
    response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    if normalized == "provider_c":
        return _first_str(
            payload.get("taskId"),
            payload.get("task_id"),
            data.get("taskId"),
            data.get("task_id"),
            response.get("taskId"),
            response.get("task_id"),
        )
    return _first_str(
        payload.get("taskId"),
        payload.get("task_id"),
        payload.get("id"),
        data.get("taskId"),
        data.get("task_id"),
        data.get("id"),
        response.get("taskId"),
        response.get("task_id"),
        response.get("id"),
    )


def extract_provider_callback_type(provider: str, payload: dict[str, Any]) -> str:
    normalized = _normalize_provider(provider)
    data = _payload_data(payload)
    if normalized == "provider_c":
        return _first_str(
            payload.get("callbackType"),
            payload.get("callback_type"),
            payload.get("type"),
            data.get("callbackType"),
            data.get("callback_type"),
            data.get("type"),
        )
    return _first_str(
        payload.get("callbackType"),
        payload.get("callback_type"),
        payload.get("type"),
        payload.get("status"),
        data.get("callbackType"),
        data.get("callback_type"),
        data.get("type"),
        data.get("status"),
    )


def record_provider_music_callback(
    *,
    provider: str,
    payload: dict[str, Any],
    store: Any | None = None,
) -> ProviderMusicCallbackEvent:
    normalized_provider = _normalize_provider(provider)
    task_id = extract_provider_task_id(normalized_provider, payload)
    if not task_id:
        raise ValueError(f"{normalized_provider} callback payload missing task id")
    event = ProviderMusicCallbackEvent(
        provider=normalized_provider,
        task_id=task_id,
        callback_type=extract_provider_callback_type(normalized_provider, payload),
        payload=payload,
    )
    return (store or get_provider_music_callback_store()).upsert(event)


def provider_callback_url(provider: str) -> Optional[str]:
    normalized = _normalize_provider(provider)
    env_prefix = normalized.upper()
    explicit = (os.getenv(f"{env_prefix}_CALLBACK_URL") or "").strip()
    if explicit:
        return explicit

    base = (
        os.getenv("EDENN_PUBLIC_API_BASE_URL")
        or os.getenv("API_PUBLIC_BASE_URL")
        or os.getenv("PUBLIC_API_BASE_URL")
        or ""
    ).strip()
    if not base:
        return None

    base = base.rstrip("/")
    url = f"{base}/api/v1/provider_callbacks/{normalized}"
    secret = (
        os.getenv(f"{env_prefix}_WEBHOOK_SECRET")
        or os.getenv("PROVIDER_WEBHOOK_SECRET")
        or ""
    ).strip()
    if not secret:
        return url
    parsed = urlparse(url)
    query = urlencode({"secret": secret})
    return urlunparse(parsed._replace(query=query))


def _callback_url_is_local_receiver(provider: str, callback_url: Optional[str]) -> bool:
    if not callback_url:
        return False
    parsed = urlparse(callback_url)
    path = parsed.path.rstrip("/")
    normalized = _normalize_provider(provider)
    return path in {
        f"/api/v1/provider_callbacks/{normalized}",
        f"/{normalized}/callback",
    }


def should_wait_for_provider_callback(
    provider: str,
    callback_url: Optional[str],
) -> bool:
    env_name = f"{_normalize_provider(provider).upper()}_CALLBACK_WAIT_ENABLED"
    if os.getenv(env_name) is not None:
        return _env_truthy(env_name)
    return _callback_url_is_local_receiver(provider, callback_url)


def _provider_c_callback_is_terminal(event: ProviderMusicCallbackEvent) -> bool:
    payload = event.payload
    callback_type = event.callback_type.strip().lower()
    code = payload.get("code")
    try:
        numeric_code = int(code)
    except (TypeError, ValueError):
        numeric_code = None
    return callback_type in {"complete", "error"} or (
        numeric_code is not None and numeric_code != 200
    )


def _extract_provider_c_tracks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    candidates: list[Any] = []
    if isinstance(data, dict):
        candidates.extend(
            [
                data.get("data"),
                data.get("tracks"),
                data.get("provider_cData"),
                (data.get("response") or {}).get("provider_cData")
                if isinstance(data.get("response"), dict)
                else None,
            ]
        )
    candidates.extend(
        [
            payload.get("tracks"),
            (payload.get("response") or {}).get("provider_cData")
            if isinstance(payload.get("response"), dict)
            else None,
            (payload.get("response") or {}).get("data")
            if isinstance(payload.get("response"), dict)
            else None,
        ]
    )
    tracks: list[dict[str, Any]] = []
    for candidate in candidates:
        if isinstance(candidate, dict):
            candidate = (
                candidate.get("tracks")
                or candidate.get("data")
                or candidate.get("provider_cData")
            )
        if not isinstance(candidate, list):
            continue
        for item in candidate:
            if isinstance(item, dict):
                tracks.append(item)
        if tracks:
            break
    return tracks


def provider_c_generation_result_from_callback(
    event: ProviderMusicCallbackEvent,
) -> Any:
    from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_c import (
        GenerationResult,
        ProviderCTrack,
    )

    payload = event.payload
    code = payload.get("code")
    try:
        numeric_code = int(code)
    except (TypeError, ValueError):
        numeric_code = 200
    if event.callback_type.strip().lower() == "error" or numeric_code != 200:
        raise EdennProviderResponseError(
            "ProviderC callback reported a failed generation task",
            provider_name="provider_c",
            operation="wait_provider_c_generation_callback",
            context={
                "task_id": event.task_id,
                "callback_type": event.callback_type,
                "code": code,
                "message": payload.get("msg") or payload.get("message"),
            },
        )

    tracks = [
        ProviderCTrack(
            audio_id=_first_str(
                item.get("id"),
                item.get("audioId"),
                item.get("audio_id"),
                item.get("trackId"),
            ),
            audio_url=_first_str(
                item.get("audio_url"),
                item.get("audioUrl"),
                item.get("source_audio_url"),
                item.get("sourceAudioUrl"),
                item.get("url"),
            ),
            prompt=_first_str(item.get("prompt")),
            title=_first_str(item.get("title")),
            image_url=_first_str(item.get("image_url"), item.get("imageUrl")) or None,
        )
        for item in _extract_provider_c_tracks(payload)
    ]
    tracks = [track for track in tracks if track.audio_id and track.audio_url]
    if not tracks:
        raise EdennProviderResponseError(
            "ProviderC callback completed without audio tracks",
            provider_name="provider_c",
            operation="wait_provider_c_generation_callback",
            context={"task_id": event.task_id, "response": payload},
        )
    return GenerationResult(
        task_id=event.task_id,
        status="SUCCESS",
        tracks=tracks,
    )


async def wait_for_provider_c_generation_callback(
    task_id: str,
    *,
    timeout_s: float,
    poll_s: float = 1.0,
    store: Any | None = None,
) -> Any:
    store = store or get_provider_music_callback_store()
    normalized_task_id = task_id.strip()
    deadline = time.monotonic() + max(0.1, float(timeout_s))
    sleep_s = max(0.1, float(poll_s))
    while time.monotonic() < deadline:
        event = store.get("provider_c", normalized_task_id)
        if event is not None and _provider_c_callback_is_terminal(event):
            return provider_c_generation_result_from_callback(event)
        await asyncio.sleep(min(sleep_s, max(0.0, deadline - time.monotonic())))
    raise EdennProviderTimeoutError(
        f"Timed out waiting for ProviderC callback for task {normalized_task_id}",
        provider_name="provider_c",
        operation="wait_provider_c_generation_callback",
        retryable=True,
        context={"task_id": normalized_task_id},
    )


__all__ = [
    "InMemoryProviderMusicCallbackStore",
    "PostgresProviderMusicCallbackStore",
    "ProviderMusicCallbackEvent",
    "build_provider_music_callback_store_from_env",
    "extract_provider_callback_type",
    "extract_provider_task_id",
    "get_provider_music_callback_store",
    "provider_callback_url",
    "record_provider_music_callback",
    "resolve_provider_music_callback_store",
    "set_provider_music_callback_store",
    "set_provider_music_callback_store_for_testing",
    "should_wait_for_provider_callback",
    "provider_c_generation_result_from_callback",
    "wait_for_provider_c_generation_callback",
]
