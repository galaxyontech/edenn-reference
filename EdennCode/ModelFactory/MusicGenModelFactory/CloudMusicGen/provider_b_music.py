from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
import asyncio
import sys
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx

if __package__ in {None, ""}:
    # Allow running this file directly via `python .../provider_b_music.py`.
    sys.path.append(str(Path(__file__).resolve().parents[4]))

from EdennCode.exceptions import (
    EdennConfigurationError,
    EdennProviderAuthenticationError,
    EdennProviderError,
    EdennProviderRateLimitError,
    EdennProviderResponseError,
    EdennProviderTimeoutError,
    EdennValidationError,
)
from EdennCode.env import load_env
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.base import MusicProvider
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.lyrics_processor import WordTS
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_payload import (
    MAX_PROMPT_CHARS,
    ProviderBTimestampedLyrics,
    clamp_prompt,
    extract_audio_url,
    extract_audio_urls,
    extract_timestamped_lyrics,
    extract_timestamped_lyrics_for_choice,
    extract_timestamped_words,
    http_error_details,
)
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_pg_lock import (
    ProviderBKeyLock,
    NullKeyLock,
)
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_key_health import (
    InMemoryProviderBKeyHealthStore,
    ProviderBKeyHealthStore,
)
from EdennCode.Util.MediaUtils.pipeline_util import hash_str

load_env()

logger = logging.getLogger(__name__)
_NUMBERED_PROVIDER_B_API_KEY_RE = re.compile(r"^PROVIDER_B_API_KEY_(\d+)$")

# Upstream wording for an over-long prompt, e.g. "The prompt exceeds N
# characters". Used to tell an input rejection apart from a transient fault.
_PROMPT_LENGTH_REJECTION_RE = re.compile(
    r"prompt\s+(?:exceeds|is\s+too\s+long|too\s+long)",
    re.IGNORECASE,
)
_PROVIDER_B_EXTEND_TYPES = {"head", "tail"}
_PROVIDER_B_EXTEND_AT_MIN_MS = 8_000
_PROVIDER_B_EXTEND_AT_MAX_MS = 420_000


@dataclass(frozen=True)
class _ProviderBKey:
    label: str
    value: str


_current_cycle_key: ContextVar[Optional[_ProviderBKey]] = ContextVar(
    "provider_b_current_cycle_key",
    default=None,
)
_current_warning_collector: ContextVar[Optional[List[str]]] = ContextVar(
    "provider_b_warning_collector",
    default=None,
)


class _ProviderBKeyPool:
    """Set of ProviderB API keys.

    Reads every populated PROVIDER_B_API_KEY_N env var, sorted numerically, then
    falls back to PROVIDER_B_API_KEY. Gaps are allowed so a missing
    PROVIDER_B_API_KEY_2 does not silently hide PROVIDER_B_API_KEY_3+.
    """

    def __init__(self, keys: List[_ProviderBKey]) -> None:
        self._keys = keys
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, explicit_key: Optional[str] = None) -> "_ProviderBKeyPool":
        if explicit_key:
            return cls([_ProviderBKey("constructor_api_key", explicit_key.strip())])
        keys: List[_ProviderBKey] = []
        numbered_keys: List[Tuple[int, str, str]] = []
        for env_name, raw_value in os.environ.items():
            match = _NUMBERED_PROVIDER_B_API_KEY_RE.match(env_name)
            if match is None:
                continue
            key = raw_value.strip()
            if not key:
                continue
            numbered_keys.append((int(match.group(1)), env_name, key))
        for _index, env_name, key in sorted(numbered_keys):
            keys.append(_ProviderBKey(env_name, key))
        primary = os.getenv("PROVIDER_B_API_KEY", "").strip()
        if primary:
            keys.append(_ProviderBKey("PROVIDER_B_API_KEY", primary))
        return cls(keys)

    def choose_key(self) -> _ProviderBKey:
        with self._lock:
            return random.choice(self._keys)

    def all_keys(self) -> List[_ProviderBKey]:
        with self._lock:
            return list(self._keys)

    @property
    def count(self) -> int:
        return len(self._keys)


@dataclass
class ProviderBTask:
    task_id: str
    status: str
    trace_id: str = ""
    raw: Optional[Dict[str, Any]] = None
    api_key_label: str = ""


class ProviderBMusicProvider(MusicProvider):
    """
    Async wrapper for ProviderB music APIs.

    Main endpoints:
    - POST /v1/lyrics/generate
    - POST /v1/song/generate
    - GET  /v1/song/query/{task_id}
    - POST /v1/instrumental/generate
    - GET  /v1/instrumental/query/{task_id}
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: str = "https://api.provider-b.example.invalid",
        timeout: int = 120,
        default_output_dir: Path | str = Path("outputs/audio/provider_b"),
        default_model: str = "auto",
        max_retries: int = 5,
        retry_backoff_s: float = 1.0,
        key_lock: Optional[ProviderBKeyLock] = None,
        key_health_store: Optional[ProviderBKeyHealthStore] = None,
        key_rotation_attempts: Optional[int] = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self._key_pool = _ProviderBKeyPool.from_env(api_key)
        if not self._key_pool.count:
            raise EdennConfigurationError(
                "PROVIDER_B_API_KEY must be set",
                component="provider_b",
                operation="initialize",
            )
        if self._key_pool.count > 1:
            logger.info(
                "ProviderB key pool initialised with %d keys",
                self._key_pool.count,
            )
        self._key_binding_lock = threading.Lock()
        self._task_api_keys: Dict[str, _ProviderBKey] = {}
        self._file_api_keys: Dict[str, _ProviderBKey] = {}
        self._credit_cache: Dict[str, Tuple[int, float]] = {}
        self._credit_cache_ttl_s = 30.0
        self._credit_cache_lock = threading.Lock()
        self._credit_threshold_fen = 500
        self._credit_low_warning_threshold_fen = 5000
        self._key_lock: ProviderBKeyLock = key_lock or NullKeyLock()
        self._key_health_store: ProviderBKeyHealthStore = (
            key_health_store or InMemoryProviderBKeyHealthStore()
        )
        self._key_rotation_attempts = (
            max(1, int(key_rotation_attempts))
            if key_rotation_attempts is not None
            else None
        )

        self.base_url = base_url.rstrip("/")
        self.default_output_dir = Path(default_output_dir)
        self.default_model = default_model
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_s = max(0.2, float(retry_backoff_s))

    def _jittered_backoff(self, attempt: int) -> float:
        """Exponential backoff with up to +30% jitter to break retry-storm sync
        when concurrent jobs hit the same ProviderB rate-limit window."""
        base = self.retry_backoff_s * (2 ** attempt)
        return base + random.uniform(0, base * 0.3)

    @staticmethod
    def _headers(api_key: _ProviderBKey, *, content_type: bool = True) -> Dict[str, str]:
        headers = {"Authorization": f"Bearer {api_key.value}"}
        if content_type:
            headers["Content-Type"] = "application/json"
        return headers

    def _bind_task_key(self, task: ProviderBTask, api_key: _ProviderBKey) -> ProviderBTask:
        if task.task_id:
            task.api_key_label = api_key.label
            with self._key_binding_lock:
                self._task_api_keys[task.task_id] = api_key
        return task

    def _bind_file_key(self, file_id: str, api_key: _ProviderBKey) -> None:
        if file_id.strip():
            with self._key_binding_lock:
                self._file_api_keys[file_id.strip()] = api_key

    def _task_key(self, task_id: str) -> Optional[_ProviderBKey]:
        with self._key_binding_lock:
            return self._task_api_keys.get(task_id.strip())

    def _file_key(self, file_id: Optional[str]) -> Optional[_ProviderBKey]:
        if not file_id:
            return None
        with self._key_binding_lock:
            return self._file_api_keys.get(file_id.strip())

    def _key_for_references(self, *ids: Optional[str]) -> Optional[_ProviderBKey]:
        keys = [key for key in (self._file_key(value)
                                for value in ids) if key is not None]
        if not keys:
            return None
        first = keys[0]
        labels = {key.label for key in keys}
        if len(labels) > 1:
            logger.warning(
                "ProviderB request references IDs from multiple API keys; using %s for this provider call",
                first.label,
            )
        return first

    @staticmethod
    def _format_balance_yuan(balance_fen: int) -> str:
        return f"{balance_fen / 100:.2f}"

    async def _fetch_balance_fen(self, api_key: _ProviderBKey) -> int:
        resp = await self._request_json(
            "GET",
            "/v1/account/billing",
            api_key=api_key,
        )
        raw_balance = resp.get("balance")
        try:
            return int(raw_balance)
        except (TypeError, ValueError) as exc:
            raise EdennProviderResponseError(
                "ProviderB billing response missing integer balance",
                provider_name="provider_b",
                operation="GET /v1/account/billing",
                context={"api_key_label": api_key.label, "response": resp},
                cause=exc,
            ) from exc

    async def _get_cached_balance_fen(self, api_key: _ProviderBKey) -> int:
        now = time.time()
        with self._credit_cache_lock:
            cached = self._credit_cache.get(api_key.label)
            if cached is not None:
                balance_fen, fetched_at = cached
                if now - fetched_at <= self._credit_cache_ttl_s:
                    return balance_fen

        balance_fen = await self._fetch_balance_fen(api_key)
        with self._credit_cache_lock:
            self._credit_cache[api_key.label] = (balance_fen, time.time())
        return balance_fen

    def _append_warning(self, message: str) -> None:
        collector = _current_warning_collector.get()
        if collector is not None and message not in collector:
            collector.append(message)
        logger.warning(message)

    def _append_low_credit_warning(self, api_key: _ProviderBKey, balance_fen: int) -> None:
        if balance_fen > self._credit_low_warning_threshold_fen:
            return
        message = (
            "edenn_enhanced: upstream provider_b key "
            f"{api_key.label} balance ¥{self._format_balance_yuan(balance_fen)} "
            "is below safe operating threshold; request succeeded, please top up"
        )
        self._append_warning(message)

    async def _cooldown_remaining_s(self, label: str) -> float:
        try:
            return await self._key_health_store.cooldown_remaining_s(label)
        except Exception as exc:
            logger.warning(
                "ProviderB key health check failed for key=%s; treating key as usable for this cycle: %s",
                label,
                exc,
            )
            return 0.0

    async def _mark_key_rate_limited(
        self,
        label: str,
        exc: Optional[BaseException] = None,
    ) -> None:
        reason = type(exc).__name__ if exc is not None else "rate_limited"
        message = str(exc or "")
        if message:
            reason = f"{reason}: {message}"
        try:
            cooldown_s = await self._key_health_store.mark_rate_limited(
                label,
                reason=reason,
            )
        except Exception as mark_exc:
            logger.warning(
                "ProviderB key health update failed while cooling down key=%s after rate limit: %s",
                label,
                mark_exc,
            )
            return
        logger.warning(
            "ProviderB key=%s cooled down for %.1fs after rate limit",
            label,
            cooldown_s,
        )

    async def _mark_key_success(self, label: str) -> None:
        try:
            await self._key_health_store.mark_success(label)
        except Exception as exc:
            logger.warning(
                "ProviderB key health success update failed for key=%s: %s",
                label,
                exc,
            )

    def begin_warning_collection(self) -> Token:
        return _current_warning_collector.set([])

    def finish_warning_collection(self, token: Token) -> Optional[str]:
        messages = list(_current_warning_collector.get() or [])
        _current_warning_collector.reset(token)
        if not messages:
            return None
        return " | ".join(messages)

    async def _verify_required_key_credit(self, required_key: _ProviderBKey) -> None:
        """Verify a specific required key passes the credit gate.

        Raises if the key fails the threshold. Warns when low-but-acceptable
        — appropriate here because the required key is always the selected key.
        """
        try:
            balance_fen = await self._get_cached_balance_fen(required_key)
        except EdennProviderError as exc:
            raise EdennProviderError(
                "provider_b required key credit check failed",
                provider_name="provider_b",
                operation="pick_key_for_cycle",
                retryable=True,
                context={"api_key_label": required_key.label},
                cause=exc,
            ) from exc
        if balance_fen > self._credit_threshold_fen:
            self._append_low_credit_warning(required_key, balance_fen)
            return
        raise EdennProviderError(
            f"provider_b required key {required_key.label} below credit threshold "
            f"(¥{self._format_balance_yuan(balance_fen)} <= "
            f"¥{self._format_balance_yuan(self._credit_threshold_fen)})",
            provider_name="provider_b",
            operation="pick_key_for_cycle",
            retryable=True,
            context={
                "api_key_label": required_key.label,
                "balance_fen": balance_fen,
                "threshold_fen": self._credit_threshold_fen,
            },
        )

    async def _credit_qualified_keys_shuffled(
        self,
    ) -> List[Tuple[_ProviderBKey, int]]:
        """Shuffle the pool, credit-check each, return (key, balance_fen) tuples.

        Returning the balance alongside the key lets the caller emit the
        low-credit warning for the actually-selected key without a second
        billing roundtrip — even if the credit cache TTL is zero.

        Raises EdennProviderError("all provider_b keys exhausted") when nothing
        qualifies — matches the prior _pick_key_for_cycle contract so callers
        don't need to handle empty lists separately.

        Does NOT emit low-credit warnings here. Warnings are deferred until
        after lock acquisition narrows the cycle down to one key.
        """
        keys = self._key_pool.all_keys()
        random.shuffle(keys)
        qualifying: List[Tuple[_ProviderBKey, int]] = []
        skipped: List[Dict[str, Any]] = []
        last_error: Optional[EdennProviderError] = None
        for api_key in keys:
            cooldown_remaining_s = await self._cooldown_remaining_s(api_key.label)
            if cooldown_remaining_s > 0:
                skipped.append(
                    {
                        "api_key_label": api_key.label,
                        "reason": "rate_limited_cooldown",
                        "cooldown_remaining_s": round(cooldown_remaining_s, 3),
                    }
                )
                logger.info(
                    "ProviderB key=%s skipped for %.1fs rate-limit cooldown",
                    api_key.label,
                    cooldown_remaining_s,
                )
                continue
            try:
                balance_fen = await self._get_cached_balance_fen(api_key)
            except EdennProviderError as exc:
                last_error = exc
                skipped.append(
                    {"api_key_label": api_key.label, "reason": "billing_check_failed"}
                )
                logger.warning(
                    "ProviderB billing check failed for key=%s; skipping key for this cycle: %s",
                    api_key.label,
                    exc,
                )
                continue
            if balance_fen > self._credit_threshold_fen:
                qualifying.append((api_key, balance_fen))
            else:
                skipped.append(
                    {
                        "api_key_label": api_key.label,
                        "reason": "low_balance",
                        "balance_fen": balance_fen,
                    }
                )

        if qualifying:
            return qualifying

        raise EdennProviderError(
            "all provider_b keys exhausted",
            provider_name="provider_b",
            operation="pick_key_for_cycle",
            retryable=last_error is not None,
            context={
                "threshold_fen": self._credit_threshold_fen,
                "skipped": skipped,
            },
            cause=last_error,
        )

    async def _pick_key_for_cycle(
        self,
        *,
        required_key: Optional[_ProviderBKey] = None,
    ) -> _ProviderBKey:
        """Thin shim preserving the prior single-key contract for unit tests.

        Live cycles go through _credit_qualified_keys_shuffled + key_lock
        directly inside _cycle; this method is no longer on the production
        request path.
        """
        if required_key is not None:
            await self._verify_required_key_credit(required_key)
            return required_key
        candidates = await self._credit_qualified_keys_shuffled()
        selected, balance_fen = candidates[0]
        self._append_low_credit_warning(selected, balance_fen)
        return selected

    def _current_key_or_none(self) -> Optional[_ProviderBKey]:
        return _current_cycle_key.get()

    def _require_current_key(self, operation: str) -> _ProviderBKey:
        api_key = self._current_key_or_none()
        if api_key is not None:
            return api_key
        raise EdennProviderError(
            "no provider_b key available for this call — cycle key was never set",
            provider_name="provider_b",
            operation=operation,
            retryable=False,
        )

    def _key_for_task_path(self, path: str) -> Optional[_ProviderBKey]:
        for prefix in ("/v1/song/query/", "/v1/instrumental/query/"):
            if path.startswith(prefix):
                task_id = path[len(prefix):].split("?", 1)[0].strip("/")
                return self._task_key(task_id)
        return None

    def _key_for_payload_references(
        self,
        payload: Optional[Dict[str, Any]],
    ) -> Optional[_ProviderBKey]:
        if not payload:
            return None
        reference_ids = [
            str(payload.get(name)).strip()
            for name in (
                "vocal_id",
                "melody_id",
                "reference_id",
                "instrumental_id",
                "upload_audio_id",
            )
            if payload.get(name)
        ]
        return self._key_for_references(*reference_ids)

    def _resolve_request_key(
        self,
        *,
        path: str,
        payload: Optional[Dict[str, Any]],
        api_key: Optional[_ProviderBKey],
    ) -> _ProviderBKey:
        if api_key is not None:
            return api_key
        path_key = self._key_for_task_path(path)
        if path_key is not None:
            return path_key
        payload_key = self._key_for_payload_references(payload)
        if payload_key is not None:
            return payload_key
        current_key = self._current_key_or_none()
        if current_key is not None:
            return current_key
        raise EdennProviderError(
            "no provider_b key available for this call — cycle key was never set",
            provider_name="provider_b",
            operation=f"resolve_key {path}",
            retryable=False,
            context={"path": path},
        )

    @asynccontextmanager
    async def _cycle(
        self,
        *,
        required_key: Optional[_ProviderBKey] = None,
    ) -> AsyncIterator[_ProviderBKey]:
        existing_key = self._current_key_or_none()
        if existing_key is not None:
            # Reentrance: the outer cycle already holds the lock; reuse silently.
            if required_key is not None and required_key.label != existing_key.label:
                raise EdennProviderError(
                    "provider_b cycle key conflicts with bound reference key",
                    provider_name="provider_b",
                    operation="cycle_key_binding",
                    retryable=False,
                    context={
                        "cycle_key_label": existing_key.label,
                        "required_key_label": required_key.label,
                    },
                )
            yield existing_key
            return

        if required_key is not None:
            await self._verify_required_key_credit(required_key)
            lock_handle = await self._key_lock.acquire(required_key.label)
            try:
                token = _current_cycle_key.set(required_key)
                try:
                    yield required_key
                finally:
                    _current_cycle_key.reset(token)
            finally:
                await lock_handle.release()
            return

        while True:
            candidates = await self._credit_qualified_keys_shuffled()
            selected_label, lock_handle = await self._key_lock.acquire_one_of(
                [key.label for key, _ in candidates],
            )
            try:
                selected_key, selected_balance_fen = next(
                    (key, balance)
                    for key, balance in candidates
                    if key.label == selected_label
                )
                cooldown_remaining_s = await self._cooldown_remaining_s(
                    selected_label,
                )
                if cooldown_remaining_s > 0:
                    logger.info(
                        "ProviderB key=%s acquired after it entered %.1fs cooldown; releasing and reselecting",
                        selected_label,
                        cooldown_remaining_s,
                    )
                    continue
                self._append_low_credit_warning(selected_key, selected_balance_fen)
                token = _current_cycle_key.set(selected_key)
                try:
                    yield selected_key
                    return
                finally:
                    _current_cycle_key.reset(token)
            finally:
                await lock_handle.release()

    async def _run_with_cycle_failover(
        self,
        *,
        operation: str,
        required_key: Optional[_ProviderBKey],
        body: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Run a top-level provider cycle and rotate keys after a final 429.

        ``_request_json`` still retries transient 429s against the current key.
        If those retries are exhausted, this wrapper cools down that key,
        releases its lock, and starts a fresh cycle so another healthy key can
        be tried. Bound/reference-key calls cannot rotate because ProviderB IDs are
        account scoped, so they only mark the key unhealthy and re-raise.
        """
        existing_key = self._current_key_or_none()
        if existing_key is not None:
            try:
                return await body()
            except EdennProviderRateLimitError as exc:
                await self._mark_key_rate_limited(existing_key.label, exc)
                raise

        if required_key is not None:
            async with self._cycle(required_key=required_key) as key:
                try:
                    result = await body()
                except EdennProviderRateLimitError as exc:
                    # Mark the key while its advisory lock is still held so
                    # waiters cannot acquire a just-failed key before the
                    # shared cooldown row exists.
                    await self._mark_key_rate_limited(key.label, exc)
                    raise
                await self._mark_key_success(key.label)
                return result

        max_attempts = self._key_rotation_attempts or self._key_pool.count
        max_attempts = max(1, min(max_attempts, self._key_pool.count))
        last_rate_limit: Optional[EdennProviderRateLimitError] = None
        attempted_labels: List[str] = []
        for attempt in range(max_attempts):
            selected_label: Optional[str] = None
            marked_label: Optional[str] = None
            try:
                async with self._cycle() as key:
                    selected_label = key.label
                    try:
                        result = await body()
                    except EdennProviderRateLimitError as exc:
                        marked_label = selected_label or str(
                            getattr(exc, "context", {}).get("api_key_label", "")
                        ).strip()
                        if marked_label:
                            attempted_labels.append(marked_label)
                            # Mark while the lock is still held. Otherwise a
                            # queued waiter can acquire the same label in the
                            # small gap between lock release and cooldown write.
                            await self._mark_key_rate_limited(marked_label, exc)
                        raise
                    await self._mark_key_success(selected_label)
                    return result
            except EdennProviderRateLimitError as exc:
                last_rate_limit = exc
                label = marked_label or selected_label or str(
                    getattr(exc, "context", {}).get("api_key_label", "")
                ).strip()
                if label and marked_label is None:
                    attempted_labels.append(label)
                    await self._mark_key_rate_limited(label, exc)
                if attempt + 1 >= max_attempts:
                    break
                logger.warning(
                    "ProviderB %s hit rate limit with key=%s; retrying with another key (%d/%d)",
                    operation,
                    label or "<unknown>",
                    attempt + 1,
                    max_attempts,
                )
                continue

        if last_rate_limit is not None:
            last_rate_limit.context.setdefault(
                "rate_limited_key_labels",
                attempted_labels,
            )
            raise last_rate_limit
        raise EdennProviderError(
            "provider_b cycle failed without a result",
            provider_name="provider_b",
            operation=operation,
            retryable=True,
        )

    def _prompt_for_payload(self, prompt: str, *, operation: str) -> str:
        """Clamp an outbound prompt to the provider's hard character limit.

        The prompt reaching this layer is assembled downstream of the direction
        the user approved — scene enrichment and per-take variation are appended
        to it — so it can overshoot the limit even when that direction is well
        inside it. An over-limit prompt is rejected outright and the generation
        never starts, so the cap belongs here, where the payload is built,
        rather than on the user-facing direction.
        """
        clamped = clamp_prompt(prompt)
        original = (prompt or "").strip()
        if len(clamped) < len(original):
            logger.warning(
                "Music prompt clamped for %s: %d -> %d characters (limit %d); "
                "trailing enrichment dropped so the musical direction survives.",
                operation,
                len(original),
                len(clamped),
                MAX_PROMPT_CHARS,
            )
        return clamped

    @staticmethod
    def _context_for(path: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        context: Dict[str, Any] = {"path": path}
        if extra:
            context.update(extra)
        return context

    def _map_httpx_exception(
        self,
        exc: Exception,
        *,
        operation: str,
        path: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> EdennProviderError:
        details = self._context_for(path, context)
        if isinstance(exc, httpx.TimeoutException):
            return EdennProviderTimeoutError(
                "ProviderB request timed out",
                provider_name="provider_b",
                operation=operation,
                retryable=True,
                context=details,
                cause=exc,
            )
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            message = f"ProviderB request failed with HTTP {status_code}"
            kwargs = {
                "provider_name": "provider_b",
                "operation": operation,
                "status_code": status_code,
                "retryable": status_code in {429, 500, 502, 503, 504},
                "context": details,
                "cause": exc,
            }
            if status_code in {401, 403}:
                return EdennProviderAuthenticationError(message, **kwargs)
            if status_code == 429:
                return EdennProviderRateLimitError(message, **kwargs)
            return EdennProviderResponseError(message, **kwargs)
        if isinstance(exc, httpx.RequestError):
            return EdennProviderError(
                "ProviderB request failed before a response was returned",
                provider_name="provider_b",
                operation=operation,
                retryable=True,
                context=details,
                cause=exc,
            )
        return EdennProviderError.wrap(
            exc,
            message="ProviderB provider request failed",
            provider_name="provider_b",
            operation=operation,
            context=details,
        )

    def _raise_payload_error(
        self,
        message: str,
        *,
        operation: str,
        path: str,
        context: Optional[Dict[str, Any]] = None,
        status_code: Optional[int] = None,
        retryable: bool = False,
    ) -> None:
        details = self._context_for(path, context)
        kwargs = {
            "provider_name": "provider_b",
            "operation": operation,
            "status_code": status_code,
            "retryable": retryable,
            "context": details,
        }
        if status_code in {401, 403}:
            raise EdennProviderAuthenticationError(message, **kwargs)
        if status_code == 429:
            raise EdennProviderRateLimitError(message, **kwargs)
        raise EdennProviderResponseError(message, **kwargs)

    def _unwrap_payload(
        self,
        body: Dict[str, Any],
        *,
        operation: str,
        path: str,
    ) -> Dict[str, Any]:
        if not isinstance(body, dict):
            raise EdennProviderResponseError(
                f"Unexpected ProviderB response type: {type(body)}",
                provider_name="provider_b",
                operation=operation,
                context=self._context_for(path),
            )

        if isinstance(body.get("error"), dict):
            self._raise_payload_error(
                f"ProviderB API error: {body['error']}",
                operation=operation,
                path=path,
            )

        code = body.get("code")
        if code is not None and code not in {0, 200}:
            self._raise_payload_error(
                f"ProviderB API error: {body}",
                operation=operation,
                path=path,
                status_code=code if isinstance(code, int) else None,
                # Marks status_code as a provider BODY code, not an HTTP
                # status — the public-payload 4xx rule must not read it.
                context={"response_code": code},
            )

        if isinstance(body.get("resp_data"), dict):
            return body["resp_data"]
        if isinstance(body.get("data"), dict):
            return body["data"]
        return body

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        api_key: Optional[_ProviderBKey] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        start = time.time()
        selected_key = self._resolve_request_key(
            path=path,
            payload=payload,
            api_key=api_key,
        )
        request_kwargs: Dict[str, Any] = {}
        if payload is not None:
            request_kwargs["json"] = payload

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                    resp = await client.request(
                        method,
                        url,
                        headers=self._headers(
                            selected_key,
                            content_type=payload is not None,
                        ),
                        **request_kwargs,
                    )
            except Exception as exc:
                mapped_error = self._map_httpx_exception(
                    exc,
                    operation=f"{method.upper()} {path}",
                    path=path,
                    context={"api_key_label": selected_key.label},
                )
                last_error = mapped_error
                if isinstance(mapped_error, EdennProviderTimeoutError) and attempt < self.max_retries:
                    wait_s = self._jittered_backoff(attempt)
                    await asyncio.sleep(wait_s)
                    continue
                raise mapped_error from exc

            if resp.status_code == 429 and attempt < self.max_retries:
                retry_after = self._parse_retry_after_s(
                    resp.headers.get("Retry-After"))
                wait_s = retry_after if retry_after is not None else self._jittered_backoff(
                    attempt)
                logger.warning(
                    "ProviderB rate limited (429) on %s %s with key=%s, retrying same key in %.2fs (%d/%d)",
                    method.upper(),
                    path,
                    selected_key.label,
                    wait_s,
                    attempt + 1,
                    self.max_retries,
                )
                await asyncio.sleep(wait_s)
                continue

            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                details = http_error_details(resp)
                logger.warning(
                    "ProviderB HTTP %s on %s %s: trace_id=%s provider_code=%s "
                    "error_message=%s api_key_label=%s body_preview=%s",
                    resp.status_code,
                    method.upper(),
                    path,
                    details["trace_id"] or "<none>",
                    details["provider_code"] or "<none>",
                    details["error_message"] or "<none>",
                    selected_key.label,
                    details["body_preview"] or "<empty>",
                )
                if resp.status_code == 400 and _PROMPT_LENGTH_REJECTION_RE.search(
                    details["error_message"] or ""
                ):
                    # An over-long prompt is an INPUT rejection, not a transient
                    # provider fault. The identical payload fails on every key,
                    # so retrying here — or rotating the numbered key pool —
                    # cannot help and only burns keys. Fail fast, non-retryable.
                    raise EdennProviderResponseError(
                        "Music generation request rejected: the prompt exceeded "
                        "the provider's character limit",
                        provider_name="provider_b",
                        operation=f"{method.upper()} {path}",
                        status_code=resp.status_code,
                        retryable=False,
                        error_code="prompt_too_long",
                        public_message=(
                            "This generation could not be started because the "
                            "request was too long."
                        ),
                        context=self._context_for(
                            path,
                            {
                                "api_key_label": selected_key.label,
                                "prompt_chars": len(
                                    str((payload or {}).get("prompt") or "")
                                ),
                                "prompt_char_limit": MAX_PROMPT_CHARS,
                            },
                        ),
                        cause=exc,
                    ) from exc
                mapped_error = self._map_httpx_exception(
                    exc,
                    operation=f"{method.upper()} {path}",
                    path=path,
                    context={"api_key_label": selected_key.label},
                )
                last_error = mapped_error
                if resp.status_code in {500, 502, 503, 504} and attempt < self.max_retries:
                    wait_s = self._jittered_backoff(attempt)
                    logger.warning(
                        "ProviderB transient HTTP %s on %s %s with key=%s, retrying same key in %.2fs (%d/%d)",
                        resp.status_code,
                        method.upper(),
                        path,
                        selected_key.label,
                        wait_s,
                        attempt + 1,
                        self.max_retries,
                    )
                    await asyncio.sleep(wait_s)
                    continue
                raise mapped_error from exc

            try:
                body = resp.json()
            except ValueError as exc:
                raise EdennProviderResponseError(
                    "ProviderB returned a non-JSON response",
                    provider_name="provider_b",
                    operation=f"{method.upper()} {path}",
                    status_code=resp.status_code,
                    context=self._context_for(path),
                    cause=exc,
                ) from exc
            logger.info("ProviderB %s %s took %.2fs with key=%s",
                        method.upper(), path, time.time() - start, selected_key.label)
            return self._unwrap_payload(
                body,
                operation=f"{method.upper()} {path}",
                path=path,
            )

        if last_error:
            raise last_error
        raise EdennProviderError(
            f"ProviderB request failed after retries: {method} {path}",
            provider_name="provider_b",
            operation=f"{method.upper()} {path}",
            context={"path": path},
        )

    @staticmethod
    def _parse_retry_after_s(value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        try:
            seconds = float(value.strip())
            if seconds >= 0:
                return seconds
        except (TypeError, ValueError):
            return None
        return None

    @staticmethod
    def _task_from_payload(
        payload: Dict[str, Any],
        *,
        api_key: Optional[_ProviderBKey] = None,
    ) -> ProviderBTask:
        task_id = str(
            payload.get("id")
            or payload.get("task_id")
            or payload.get("taskId")
            or ""
        ).strip()
        status = str(payload.get("status") or "").strip().lower()
        trace_id = str(payload.get("trace_id")
                       or payload.get("traceId") or "").strip()
        return ProviderBTask(
            task_id=task_id,
            status=status,
            trace_id=trace_id,
            raw=payload,
            api_key_label=api_key.label if api_key is not None else "",
        )

    @staticmethod
    def _is_terminal_status(status: str) -> bool:
        s = (status or "").strip().lower()
        return s in {
            "succeeded",
            "success",
            "completed",
            "done",
            "finished",
            "failed",
            "error",
            "canceled",
            "cancelled",
        }

    @staticmethod
    def _is_success_status(status: str) -> bool:
        s = (status or "").strip().lower()
        return s in {"succeeded", "success", "completed", "done", "finished"}

    def _build_output_path(self, stem_prefix: str = "song", suffix: str = ".mp3") -> Path:
        self.default_output_dir.mkdir(parents=True, exist_ok=True)
        return self.default_output_dir / f"{stem_prefix}_{hash_str()}{suffix}"

    @staticmethod
    def _normalize_extend_type(extend_type: str) -> str:
        normalized = (extend_type or "").strip().lower()
        if normalized not in _PROVIDER_B_EXTEND_TYPES:
            raise EdennValidationError(
                "extend_type must be one of: head, tail",
                component="provider_b",
                operation="extend_song_task",
                context={"extend_type": extend_type},
            )
        return normalized

    @staticmethod
    def _normalize_extend_at_ms(extend_at_ms: int) -> int:
        normalized = int(round(float(extend_at_ms)))
        if normalized <= 0:
            raise EdennValidationError(
                "extend_at_ms must be positive",
                component="provider_b",
                operation="extend_song_task",
                context={"extend_at_ms": extend_at_ms},
            )
        return max(
            _PROVIDER_B_EXTEND_AT_MIN_MS,
            min(normalized, _PROVIDER_B_EXTEND_AT_MAX_MS),
        )

    async def _create_task(
        self,
        *,
        path: str,
        payload: Dict[str, Any],
        reference_ids: Tuple[Optional[str], ...],
        operation: str,
        missing_task_id_message: str,
    ) -> ProviderBTask:
        required_key = self._key_for_references(*reference_ids)
        async with self._cycle(required_key=required_key):
            selected_key = required_key or self._require_current_key(operation)
            resp = await self._request_json(
                "POST",
                path,
                payload=payload,
                api_key=selected_key,
            )
            task = self._task_from_payload(resp, api_key=selected_key)
            if not task.task_id:
                raise EdennProviderResponseError(
                    missing_task_id_message,
                    provider_name="provider_b",
                    operation=operation,
                    context={"response": resp},
                )
            return self._bind_task_key(task, selected_key)

    async def _query_task(
        self,
        task_id: str,
        *,
        path_template: str,
        operation: str,
    ) -> ProviderBTask:
        if not task_id.strip():
            raise EdennValidationError(
                "task_id is required",
                component="provider_b",
                operation=operation,
            )
        normalized_task_id = task_id.strip()
        required_key = self._task_key(normalized_task_id)
        async with self._cycle(required_key=required_key):
            selected_key = required_key or self._require_current_key(operation)
            resp = await self._request_json(
                "GET",
                path_template.format(task_id=normalized_task_id),
                api_key=selected_key,
            )
            task = self._task_from_payload(resp, api_key=selected_key)
            if not task.task_id:
                task.task_id = normalized_task_id
            return self._bind_task_key(task, selected_key)

    async def _wait_task(
        self,
        task_id: str,
        *,
        timeout_s: float,
        poll_s: float,
        max_query_errors: int,
        query_fn: Callable[[str], Awaitable[ProviderBTask]],
        task_kind: str,
        operation: str,
    ) -> ProviderBTask:
        start = time.time()
        consecutive_query_errors = 0
        while True:
            try:
                task = await query_fn(task_id)
                consecutive_query_errors = 0
            except Exception as exc:
                consecutive_query_errors += 1
                if consecutive_query_errors >= max_query_errors:
                    raise EdennProviderResponseError(
                        f"ProviderB {task_kind} task query failed {consecutive_query_errors} times in a row: {task_id}",
                        provider_name="provider_b",
                        operation=operation,
                        context={"task_id": task_id, "last_error": str(exc)},
                    ) from exc
                logger.warning(
                    "ProviderB query_%s_task transient error (%d/%d): %s",
                    task_kind, consecutive_query_errors, max_query_errors, exc,
                )
                if time.time() - start > timeout_s:
                    raise EdennProviderTimeoutError(
                        f"ProviderB {task_kind} task timed out: {task_id}",
                        provider_name="provider_b",
                        operation=operation,
                        retryable=True,
                        context={"task_id": task_id},
                    )
                await asyncio.sleep(poll_s)
                continue
            if self._is_terminal_status(task.status):
                if not self._is_success_status(task.status):
                    raise EdennProviderResponseError(
                        f"ProviderB {task_kind} task failed ({task.status})",
                        provider_name="provider_b",
                        operation=operation,
                        context={"task_id": task_id, "response": task.raw},
                    )
                return task
            if time.time() - start > timeout_s:
                raise EdennProviderTimeoutError(
                    f"ProviderB {task_kind} task timed out: {task_id}",
                    provider_name="provider_b",
                    operation=operation,
                    retryable=True,
                    context={"task_id": task_id},
                )
            await asyncio.sleep(poll_s)

    @staticmethod
    def _write_sidecar(
        final_path: Path,
        *,
        metadata: Dict[str, Any],
        lyrics: Optional[str] = None,
    ) -> None:
        if lyrics is not None:
            final_path.with_suffix(".lyrics.txt").write_text(
                lyrics, encoding="utf-8")
        final_path.with_suffix(".json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def generate_lyrics(self, *, prompt: str) -> Dict[str, str]:
        if not prompt.strip():
            raise EdennValidationError(
                "prompt is required for /v1/lyrics/generate",
                component="provider_b",
                operation="generate_lyrics",
            )

        async with self._cycle():
            payload = {"prompt": prompt}
            resp = await self._request_json("POST", "/v1/lyrics/generate", payload=payload)
            title = str(resp.get("title") or resp.get("song_title") or "").strip()
            lyrics = str(resp.get("lyrics") or "").strip()
            if not lyrics:
                raise EdennProviderResponseError(
                    "ProviderB lyrics generation returned empty lyrics",
                    provider_name="provider_b",
                    operation="generate_lyrics",
                    context={"response": resp},
                )
            return {"title": title, "lyrics": lyrics}

    async def extend_lyrics(self, *, lyrics: str) -> Dict[str, str]:
        if not lyrics.strip():
            raise EdennValidationError(
                "lyrics is required for /v1/lyrics/extend",
                component="provider_b",
                operation="extend_lyrics",
            )

        async with self._cycle():
            resp = await self._request_json("POST", "/v1/lyrics/extend", payload={"lyrics": lyrics})
            title = str(resp.get("title") or resp.get("song_title") or "").strip()
            extended_lyrics = str(resp.get("lyrics") or "").strip()
            if not extended_lyrics:
                raise EdennProviderResponseError(
                    "ProviderB lyrics extension returned empty lyrics",
                    provider_name="provider_b",
                    operation="extend_lyrics",
                    context={"response": resp},
                )
            return {"title": title, "lyrics": extended_lyrics}

    async def upload_audio_file(self, audio_path: Path, *, purpose: str = "audio") -> str:
        path = Path(audio_path)
        if not path.exists():
            raise EdennValidationError(
                f"Audio file not found: {path}",
                component="provider_b",
                operation="upload_audio_file",
            )

        async with self._cycle():
            return await self._upload_audio_file_in_cycle(path, purpose=purpose)

    async def _upload_audio_file_in_cycle(self, path: Path, *, purpose: str) -> str:
        url = f"{self.base_url}/v1/files/upload"
        start = time.time()
        selected_key = self._require_current_key("upload_audio_file")
        try:
            with path.open("rb") as fh:
                files = {
                    "file": (path.name, fh, "application/octet-stream"),
                    "purpose": (None, purpose),
                }
                async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                    resp = await client.post(
                        url,
                        headers=self._headers(selected_key, content_type=False),
                        files=files,
                    )
                    resp.raise_for_status()
        except Exception as exc:
            raise self._map_httpx_exception(
                exc,
                operation="upload_audio_file",
                path="/v1/files/upload",
                context={
                    "audio_path": str(path),
                    "purpose": purpose,
                    "api_key_label": selected_key.label,
                },
            ) from exc

        try:
            body = resp.json()
        except ValueError as exc:
            raise EdennProviderResponseError(
                "ProviderB returned a non-JSON response for file upload",
                provider_name="provider_b",
                operation="upload_audio_file",
                status_code=resp.status_code,
                context=self._context_for(
                    "/v1/files/upload", {"audio_path": str(path), "purpose": purpose}),
                cause=exc,
            ) from exc

        logger.info("ProviderB POST /v1/files/upload took %.2fs",
                    time.time() - start)
        payload = self._unwrap_payload(
            body,
            operation="POST /v1/files/upload",
            path="/v1/files/upload",
        )
        file_id = str(
            payload.get("id")
            or payload.get("file_id")
            or payload.get("fileId")
            or ""
        ).strip()
        if not file_id:
            raise EdennProviderResponseError(
                "ProviderB file upload response missing file id",
                provider_name="provider_b",
                operation="upload_audio_file",
                context={"response": payload, "audio_path": str(
                    path), "purpose": purpose},
            )
        self._bind_file_key(file_id, selected_key)
        return file_id

    async def clone_vocal(self, audio_path: Path) -> str:
        path = Path(audio_path)
        if not path.exists():
            raise EdennValidationError(
                f"Audio file not found: {path}",
                component="provider_b",
                operation="clone_vocal",
            )
        # On the CN API, vocal clone creation is exposed through file upload with
        # purpose="vocal"; the returned file id is then passed back as vocal_id.
        return await self.upload_audio_file(path, purpose="vocal")

    async def generate_song_task(
        self,
        *,
        lyrics: str,
        prompt: str = "",
        model: Optional[str] = None,
        n: int = 1,
        stream: Optional[bool] = None,
        reference_id: Optional[str] = None,
        vocal_id: Optional[str] = None,
        melody_id: Optional[str] = None,
        instrumental_id: Optional[str] = None,
    ) -> ProviderBTask:
        if not lyrics.strip():
            raise EdennValidationError(
                "lyrics is required for /v1/song/generate",
                component="provider_b",
                operation="generate_song_task",
            )

        payload: Dict[str, Any] = {
            "lyrics": lyrics,
            "model": (model or self.default_model),
            "n": max(1, min(int(n), 3)),
        }
        if prompt.strip():
            payload["prompt"] = self._prompt_for_payload(
                prompt, operation="generate_song_task")
        if stream is not None:
            payload["stream"] = bool(stream)
        if reference_id:
            payload["reference_id"] = reference_id
        if vocal_id:
            payload["vocal_id"] = vocal_id
        if melody_id:
            payload["melody_id"] = melody_id
        if instrumental_id:
            payload["instrumental_id"] = instrumental_id

        return await self._create_task(
            path="/v1/song/generate",
            payload=payload,
            reference_ids=(reference_id, vocal_id, melody_id, instrumental_id),
            operation="generate_song_task",
            missing_task_id_message="ProviderB song/generate response missing task id",
        )

    async def extend_song_task(
        self,
        *,
        upload_audio_id: str,
        lyrics: str,
        prompt: str = "",
        model: Optional[str] = None,
        n: int = 1,
        extend_type: str = "tail",
        extend_at_ms: Optional[int] = None,
    ) -> ProviderBTask:
        if not upload_audio_id.strip():
            raise EdennValidationError(
                "upload_audio_id is required for /v1/song/extend",
                component="provider_b",
                operation="extend_song_task",
            )
        if not lyrics.strip():
            raise EdennValidationError(
                "lyrics is required for /v1/song/extend",
                component="provider_b",
                operation="extend_song_task",
            )

        payload: Dict[str, Any] = {
            "upload_audio_id": upload_audio_id,
            "lyrics": lyrics,
            "model": (model or self.default_model),
            "n": max(1, min(int(n), 3)),
            "extend_type": self._normalize_extend_type(extend_type),
        }
        if extend_at_ms is not None:
            payload["extend_at"] = self._normalize_extend_at_ms(extend_at_ms)
        if prompt.strip():
            payload["prompt"] = self._prompt_for_payload(
                prompt, operation="extend_song_task")

        return await self._create_task(
            path="/v1/song/extend",
            payload=payload,
            reference_ids=(upload_audio_id,),
            operation="extend_song_task",
            missing_task_id_message="ProviderB song/extend response missing task id",
        )

    async def query_song_task(self, task_id: str) -> ProviderBTask:
        return await self._query_task(
            task_id,
            path_template="/v1/song/query/{task_id}",
            operation="query_song_task",
        )

    async def wait_song_task(
        self,
        task_id: str,
        *,
        timeout_s: float = 600.0,
        poll_s: float = 4.0,
        max_query_errors: int = 5,
    ) -> ProviderBTask:
        return await self._wait_task(
            task_id,
            timeout_s=timeout_s,
            poll_s=poll_s,
            max_query_errors=max_query_errors,
            query_fn=self.query_song_task,
            task_kind="song",
            operation="wait_song_task",
        )

    async def download_audio(self, url: str, dest_path: Path) -> Path:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        start = time.time()
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                dest_path.write_bytes(resp.content)
        except Exception as exc:
            raise self._map_httpx_exception(
                exc,
                operation="download_audio",
                path=url,
                context={"destination": str(dest_path)},
            ) from exc
        logger.info("ProviderB download took %.2fs -> %s",
                    time.time() - start, dest_path)
        return dest_path

    async def generate(
        self,
        prompt: str,
        *,
        lyrics: Optional[str] = None,
        lyrics_prompt: Optional[str] = None,
        model: Optional[str] = None,
        n: int = 1,
        timeout_s: float = 600.0,
        poll_s: float = 5.0,
        output_path: Optional[Path] = None,
        save_sidecar: bool = True,
    ) -> Tuple[Path, List[WordTS]]:
        """
        Full song generation helper:
        1) lyrics generation (if lyrics not provided)
        2) song generation
        3) poll query task
        4) download first available audio URL
        """

        final_path, _, ts_words = await self.generate_with_variants(
            prompt,
            lyrics=lyrics,
            lyrics_prompt=lyrics_prompt,
            model=model,
            n=n,
            timeout_s=timeout_s,
            poll_s=poll_s,
            output_path=output_path,
            save_sidecar=save_sidecar,
        )
        return final_path, ts_words

    async def generate_with_variants(
        self,
        prompt: str,
        *,
        lyrics: Optional[str] = None,
        lyrics_prompt: Optional[str] = None,
        model: Optional[str] = None,
        n: int = 2,
        timeout_s: float = 600.0,
        poll_s: float = 5.0,
        output_path: Optional[Path] = None,
        save_sidecar: bool = True,
    ) -> Tuple[Path, Optional[Path], List[WordTS]]:
        """
        Generate one or more song candidates and download the first two full tracks.
        Returns the primary path used downstream, an optional secondary full-song path,
        and timestamped lyrics for the primary track.
        """
        final_path, secondary_path, primary_timestamps, _secondary_timestamps = await self.generate_with_variants_detailed(
            prompt,
            lyrics=lyrics,
            lyrics_prompt=lyrics_prompt,
            model=model,
            n=n,
            timeout_s=timeout_s,
            poll_s=poll_s,
            output_path=output_path,
            save_sidecar=save_sidecar,
        )
        return final_path, secondary_path, primary_timestamps.word_level or primary_timestamps.line_level

    async def generate_with_variants_detailed(
        self,
        prompt: str,
        *,
        lyrics: Optional[str] = None,
        lyrics_prompt: Optional[str] = None,
        vocal_id: Optional[str] = None,
        model: Optional[str] = None,
        n: int = 2,
        timeout_s: float = 600.0,
        poll_s: float = 5.0,
        output_path: Optional[Path] = None,
        save_sidecar: bool = True,
        resume_task_id: Optional[str] = None,
        on_task_created: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Tuple[Path, Optional[Path], ProviderBTimestampedLyrics, Optional[ProviderBTimestampedLyrics]]:
        required_key = self._key_for_references(vocal_id)
        async def _body() -> Tuple[Path, Optional[Path], ProviderBTimestampedLyrics, Optional[ProviderBTimestampedLyrics]]:
            return await self._generate_with_variants_detailed_in_cycle(
                prompt,
                lyrics=lyrics,
                lyrics_prompt=lyrics_prompt,
                vocal_id=vocal_id,
                model=model,
                n=n,
                timeout_s=timeout_s,
                poll_s=poll_s,
                output_path=output_path,
                save_sidecar=save_sidecar,
                resume_task_id=resume_task_id,
                on_task_created=on_task_created,
            )
        return await self._run_with_cycle_failover(
            operation="generate_with_variants_detailed",
            required_key=required_key,
            body=_body,
        )

    async def _generate_with_variants_detailed_in_cycle(
        self,
        prompt: str,
        *,
        lyrics: Optional[str] = None,
        lyrics_prompt: Optional[str] = None,
        vocal_id: Optional[str] = None,
        model: Optional[str] = None,
        n: int = 2,
        timeout_s: float = 600.0,
        poll_s: float = 5.0,
        output_path: Optional[Path] = None,
        save_sidecar: bool = True,
        resume_task_id: Optional[str] = None,
        on_task_created: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Tuple[Path, Optional[Path], ProviderBTimestampedLyrics, Optional[ProviderBTimestampedLyrics]]:
        """
        Generate one or more song candidates and download the first two full tracks.
        Returns the primary path used downstream, an optional secondary full-song path,
        plus both line-level and word-level lyric timestamps when available.
        """

        _api_start = time.time()
        # Defined for BOTH paths. On the resume path the provider task already
        # exists, so lyrics are never (re)generated here; fall back to the
        # caller-supplied lyrics. Without these defaults the sidecar write below
        # references undefined names on the resume path (guaranteed NameError
        # after the paid wait/download).
        lyrics_meta: Dict[str, str] = {}
        resolved_lyrics = lyrics or ""
        if resume_task_id:
            finished = await self.wait_song_task(
                resume_task_id,
                timeout_s=timeout_s,
                poll_s=poll_s,
            )
        else:
            lyrics_meta = await self.generate_lyrics(prompt=lyrics_prompt)
            resolved_lyrics = lyrics_meta["lyrics"]
            task = await self.generate_song_task(
                lyrics=resolved_lyrics,
                prompt=prompt,
                vocal_id=vocal_id,
                model='provider_b-8',
                n=n,
            )
            if on_task_created is not None:
                await on_task_created(task.task_id)
            finished = await self.wait_song_task(task.task_id, timeout_s=timeout_s, poll_s=poll_s)
        _elapsed = time.time() - _api_start
        logging.info(json.dumps({
            "event": "pipeline_timing",
            "stage": "provider_b_api_call",
            "duration_s": round(_elapsed, 3),
            "provider": "provider_b",
            "task_id": finished.task_id,
        }))
        audio_urls = extract_audio_urls(
            finished.raw or {}, limit=max(1, n))
        if not audio_urls:
            raise EdennProviderResponseError(
                "ProviderB song task completed but no audio URL was found",
                provider_name="provider_b",
                operation="generate_with_variants",
                context={"task_id": finished.task_id,
                         "response": finished.raw},
            )

        final_path = output_path or self._build_output_path("song")
        await self.download_audio(audio_urls[0], final_path)
        secondary_path: Optional[Path] = None
        if len(audio_urls) > 1:
            secondary_path = final_path.with_name(
                f"{final_path.stem}_alt2{final_path.suffix}"
            )
            await self.download_audio(audio_urls[1], secondary_path)
        primary_timestamps = extract_timestamped_lyrics_for_choice(
            finished.raw or {},
            0,
        )
        if not primary_timestamps.word_level and not primary_timestamps.line_level:
            primary_timestamps = extract_timestamped_lyrics(
                finished.raw or {})
        if not primary_timestamps.word_level and not primary_timestamps.line_level:
            raise EdennProviderResponseError(
                "ProviderB song task returned no timestamped lyrics",
                provider_name="provider_b",
                operation="generate_with_variants_detailed",
                context={"task_id": finished.task_id,
                         "response": finished.raw},
            )
        secondary_timestamps: Optional[ProviderBTimestampedLyrics] = None
        if len(audio_urls) > 1:
            secondary_timestamps = extract_timestamped_lyrics_for_choice(
                finished.raw or {},
                1,
            )
            if (
                not secondary_timestamps.word_level
                and not secondary_timestamps.line_level
                and (finished.raw or {}).get("lyrics_sections")
            ):
                secondary_timestamps = extract_timestamped_lyrics(
                    finished.raw or {})

        if save_sidecar:
            self._write_sidecar(
                final_path,
                lyrics=resolved_lyrics,
                metadata={
                    "task_id": finished.task_id,
                    "status": finished.status,
                    "trace_id": finished.trace_id,
                    "prompt": prompt,
                    "lyrics_title": lyrics_meta.get("title", ""),
                    "lyrics": resolved_lyrics,
                    "vocal_id": vocal_id,
                    "audio_url": audio_urls[0],
                    "audio_urls": audio_urls,
                    "model": model or self.default_model,
                    "source": "provider_b_song_api",
                },
            )
        return final_path, secondary_path, primary_timestamps, secondary_timestamps

    async def generate_with_melody_variants(
        self,
        *,
        prompt: str,
        lyrics_prompt: str,
        melody_audio_path: Path,
        vocal_id: Optional[str] = None,
        model: Optional[str] = None,
        n: int = 2,
        timeout_s: float = 600.0,
        poll_s: float = 5.0,
        output_path: Optional[Path] = None,
        save_sidecar: bool = True,
    ) -> Tuple[Path, Optional[Path], List[WordTS]]:
        required_key = self._key_for_references(vocal_id)
        async def _body() -> Tuple[Path, Optional[Path], List[WordTS]]:
            return await self._generate_with_melody_variants_in_cycle(
                prompt=prompt,
                lyrics_prompt=lyrics_prompt,
                melody_audio_path=melody_audio_path,
                vocal_id=vocal_id,
                model=model,
                n=n,
                timeout_s=timeout_s,
                poll_s=poll_s,
                output_path=output_path,
                save_sidecar=save_sidecar,
            )
        return await self._run_with_cycle_failover(
            operation="generate_with_melody_variants",
            required_key=required_key,
            body=_body,
        )

    async def _generate_with_melody_variants_in_cycle(
        self,
        *,
        prompt: str,
        lyrics_prompt: str,
        melody_audio_path: Path,
        vocal_id: Optional[str] = None,
        model: Optional[str] = None,
        n: int = 2,
        timeout_s: float = 600.0,
        poll_s: float = 5.0,
        output_path: Optional[Path] = None,
        save_sidecar: bool = True,
    ) -> Tuple[Path, Optional[Path], List[WordTS]]:
        lyrics_meta = await self.generate_lyrics(prompt=lyrics_prompt)
        resolved_lyrics = lyrics_meta["lyrics"]
        melody_id = await self.upload_audio_file(melody_audio_path, purpose="melody")
        # ProviderB rejects requests that combine multiple control parameters:
        # `prompt` is suppressed when `melody_id` is present (see comment above),
        # and `melody_id` must be suppressed when `vocal_id` is present — the
        # provider treats them as mutually exclusive generation controls.
        task = await self.generate_song_task(
            lyrics=resolved_lyrics,
            prompt="",
            model=model or "provider_b-8",
            n=n,
            vocal_id=vocal_id,
            melody_id=melody_id if not vocal_id else None,
        )
        finished = await self.wait_song_task(task.task_id, timeout_s=timeout_s, poll_s=poll_s)
        audio_urls = extract_audio_urls(
            finished.raw or {}, limit=max(1, n))
        if not audio_urls:
            raise EdennProviderResponseError(
                "ProviderB melody-guided song task completed but no audio URL was found",
                provider_name="provider_b",
                operation="generate_with_melody_variants",
                context={"task_id": finished.task_id,
                         "response": finished.raw},
            )

        final_path = output_path or self._build_output_path("song_melody")
        await self.download_audio(audio_urls[0], final_path)
        secondary_path: Optional[Path] = None
        if len(audio_urls) > 1:
            secondary_path = final_path.with_name(
                f"{final_path.stem}_alt2{final_path.suffix}")
            await self.download_audio(audio_urls[1], secondary_path)

        ts_words = extract_timestamped_words(finished.raw or {})
        if not ts_words:
            raise EdennProviderResponseError(
                "ProviderB melody-guided song task returned no timestamped lyrics",
                provider_name="provider_b",
                operation="generate_with_melody_variants",
                context={"task_id": finished.task_id,
                         "response": finished.raw},
            )

        if save_sidecar:
            self._write_sidecar(
                final_path,
                lyrics=resolved_lyrics,
                metadata={
                    "task_id": finished.task_id,
                    "status": finished.status,
                    "trace_id": finished.trace_id,
                    "prompt": prompt,
                    "lyrics_title": lyrics_meta.get("title", ""),
                    "lyrics": resolved_lyrics,
                    "melody_id": melody_id,
                    "vocal_id": vocal_id,
                    "audio_url": audio_urls[0],
                    "audio_urls": audio_urls,
                    "model": model or self.default_model,
                    "source": "provider_b_song_melody_api",
                },
            )
        return final_path, secondary_path, ts_words

    async def generate_instrumental_with_melody_variants(
        self,
        *,
        prompt: str,
        melody_audio_path: Path,
        model: Optional[str] = None,
        n: int = 2,
        timeout_s: float = 600.0,
        poll_s: float = 4.0,
        output_path: Optional[Path] = None,
        save_sidecar: bool = True,
    ) -> Tuple[Path, Optional[Path]]:
        async def _body() -> Tuple[Path, Optional[Path]]:
            return await self._generate_instrumental_with_melody_variants_in_cycle(
                prompt=prompt,
                melody_audio_path=melody_audio_path,
                model=model,
                n=n,
                timeout_s=timeout_s,
                poll_s=poll_s,
                output_path=output_path,
                save_sidecar=save_sidecar,
            )
        return await self._run_with_cycle_failover(
            operation="generate_instrumental_with_melody_variants",
            required_key=None,
            body=_body,
        )

    async def _generate_instrumental_with_melody_variants_in_cycle(
        self,
        *,
        prompt: str,
        melody_audio_path: Path,
        model: Optional[str] = None,
        n: int = 2,
        timeout_s: float = 600.0,
        poll_s: float = 4.0,
        output_path: Optional[Path] = None,
        save_sidecar: bool = True,
    ) -> Tuple[Path, Optional[Path]]:
        melody_id = await self.upload_audio_file(melody_audio_path, purpose="melody")
        task = await self.generate_instrumental_task(
            prompt=prompt,
            model=model or "provider_b-8",
            n=n,
            melody_id=melody_id,
        )
        finished = await self.wait_instrumental_task(task.task_id, timeout_s=timeout_s, poll_s=poll_s)
        audio_urls = extract_audio_urls(
            finished.raw or {}, limit=max(1, n))
        if not audio_urls:
            raise EdennProviderResponseError(
                "ProviderB melody-guided instrumental task completed but no audio URL was found",
                provider_name="provider_b",
                operation="generate_instrumental_with_melody_variants",
                context={"task_id": finished.task_id,
                         "response": finished.raw},
            )

        final_path = output_path or self._build_output_path(
            "instrumental_melody")
        await self.download_audio(audio_urls[0], final_path)
        secondary_path: Optional[Path] = None
        if len(audio_urls) > 1:
            secondary_path = final_path.with_name(
                f"{final_path.stem}_alt2{final_path.suffix}")
            await self.download_audio(audio_urls[1], secondary_path)

        if save_sidecar:
            self._write_sidecar(
                final_path,
                metadata={
                    "task_id": finished.task_id,
                    "status": finished.status,
                    "trace_id": finished.trace_id,
                    "prompt": prompt,
                    "melody_id": melody_id,
                    "audio_url": audio_urls[0],
                    "audio_urls": audio_urls,
                    "model": model or self.default_model,
                    "source": "provider_b_instrumental_melody_api",
                },
            )
        return final_path, secondary_path

    async def extend_song_from_audio(
        self,
        *,
        audio_path: Path,
        prompt: str,
        lyrics: str,
        model: Optional[str] = None,
        timeout_s: float = 600.0,
        poll_s: float = 5.0,
        output_path: Optional[Path] = None,
        save_sidecar: bool = True,
        extend_type: str = "tail",
        extend_at_ms: Optional[int] = None,
    ) -> Tuple[Path, List[WordTS], str]:
        final_path, timestamps, extended_lyrics = await self.extend_song_from_audio_detailed(
            audio_path=audio_path,
            prompt=prompt,
            lyrics=lyrics,
            model=model,
            timeout_s=timeout_s,
            poll_s=poll_s,
            output_path=output_path,
            save_sidecar=save_sidecar,
            extend_type=extend_type,
            extend_at_ms=extend_at_ms,
        )
        return final_path, timestamps.word_level or timestamps.line_level, extended_lyrics

    async def extend_song_from_audio_detailed(
        self,
        *,
        audio_path: Path,
        prompt: str,
        lyrics: str,
        model: Optional[str] = None,
        timeout_s: float = 600.0,
        poll_s: float = 5.0,
        output_path: Optional[Path] = None,
        save_sidecar: bool = True,
        extend_type: str = "tail",
        extend_at_ms: Optional[int] = None,
    ) -> Tuple[Path, ProviderBTimestampedLyrics, str]:
        async def _body() -> Tuple[Path, ProviderBTimestampedLyrics, str]:
            return await self._extend_song_from_audio_detailed_in_cycle(
                audio_path=audio_path,
                prompt=prompt,
                lyrics=lyrics,
                model=model,
                timeout_s=timeout_s,
                poll_s=poll_s,
                output_path=output_path,
                save_sidecar=save_sidecar,
                extend_type=extend_type,
                extend_at_ms=extend_at_ms,
            )
        return await self._run_with_cycle_failover(
            operation="extend_song_from_audio_detailed",
            required_key=None,
            body=_body,
        )

    async def _extend_song_from_audio_detailed_in_cycle(
        self,
        *,
        audio_path: Path,
        prompt: str,
        lyrics: str,
        model: Optional[str] = None,
        timeout_s: float = 600.0,
        poll_s: float = 5.0,
        output_path: Optional[Path] = None,
        save_sidecar: bool = True,
        extend_type: str = "tail",
        extend_at_ms: Optional[int] = None,
    ) -> Tuple[Path, ProviderBTimestampedLyrics, str]:
        extended_lyrics_meta = await self.extend_lyrics(lyrics=lyrics)
        extended_lyrics = extended_lyrics_meta["lyrics"]
        uploaded_audio_id = await self.upload_audio_file(audio_path, purpose="audio")
        normalized_extend_type = self._normalize_extend_type(extend_type)
        normalized_extend_at_ms = (
            self._normalize_extend_at_ms(extend_at_ms)
            if extend_at_ms is not None
            else None
        )
        task = await self.extend_song_task(
            upload_audio_id=uploaded_audio_id,
            lyrics=extended_lyrics,
            prompt=prompt,
            model=model or "provider_b-8",
            n=1,
            extend_type=normalized_extend_type,
            extend_at_ms=normalized_extend_at_ms,
        )
        finished = await self.wait_song_task(task.task_id, timeout_s=timeout_s, poll_s=poll_s)
        audio_url = extract_audio_url(finished.raw or {})
        if not audio_url:
            raise EdennProviderResponseError(
                "ProviderB song extension completed but no audio URL was found",
                provider_name="provider_b",
                operation="extend_song_from_audio",
                context={"task_id": finished.task_id,
                         "response": finished.raw},
            )

        final_path = output_path or self._build_output_path("song_extend")
        await self.download_audio(audio_url, final_path)
        timestamps = extract_timestamped_lyrics(finished.raw or {})
        if not timestamps.word_level and not timestamps.line_level:
            raise EdennProviderResponseError(
                "ProviderB song extension returned no timestamped lyrics",
                provider_name="provider_b",
                operation="extend_song_from_audio_detailed",
                context={"task_id": finished.task_id,
                         "response": finished.raw},
            )

        if save_sidecar:
            self._write_sidecar(
                final_path,
                lyrics=extended_lyrics,
                metadata={
                    "task_id": finished.task_id,
                    "status": finished.status,
                    "trace_id": finished.trace_id,
                    "prompt": prompt,
                    "lyrics": extended_lyrics,
                    "audio_url": audio_url,
                    "model": model or self.default_model,
                    "extend_type": normalized_extend_type,
                    "extend_at_ms": normalized_extend_at_ms,
                    "source": "provider_b_song_extend_api",
                },
            )
        return final_path, timestamps, extended_lyrics

    async def generate_instrumental_task(
        self,
        *,
        prompt: str,
        model: Optional[str] = None,
        n: int = 1,
        stream: Optional[bool] = None,
        reference_id: Optional[str] = None,
        melody_id: Optional[str] = None,
        instrumental_id: Optional[str] = None,
    ) -> ProviderBTask:
        if not prompt.strip():
            raise EdennValidationError(
                "prompt is required for /v1/instrumental/generate",
                component="provider_b",
                operation="generate_instrumental_task",
            )

        payload: Dict[str, Any] = {
            "prompt": self._prompt_for_payload(
                prompt, operation="generate_instrumental_task"),
            "model": (model or self.default_model),
            "n": max(1, min(int(n), 3)),
        }
        if stream is not None:
            payload["stream"] = bool(stream)
        if reference_id:
            payload["reference_id"] = reference_id
        if melody_id:
            payload["melody_id"] = melody_id
        if instrumental_id:
            payload["instrumental_id"] = instrumental_id

        return await self._create_task(
            path="/v1/instrumental/generate",
            payload=payload,
            reference_ids=(reference_id, melody_id, instrumental_id),
            operation="generate_instrumental_task",
            missing_task_id_message="ProviderB instrumental/generate response missing task id",
        )

    async def query_instrumental_task(self, task_id: str) -> ProviderBTask:
        return await self._query_task(
            task_id,
            path_template="/v1/instrumental/query/{task_id}",
            operation="query_instrumental_task",
        )

    async def wait_instrumental_task(
        self,
        task_id: str,
        *,
        timeout_s: float = 600.0,
        poll_s: float = 4.0,
        max_query_errors: int = 5,
    ) -> ProviderBTask:
        return await self._wait_task(
            task_id,
            timeout_s=timeout_s,
            poll_s=poll_s,
            max_query_errors=max_query_errors,
            query_fn=self.query_instrumental_task,
            task_kind="instrumental",
            operation="wait_instrumental_task",
        )

    async def generate_instrumental(
        self,
        *,
        prompt: str,
        model: Optional[str] = None,
        n: int = 1,
        timeout_s: float = 600.0,
        poll_s: float = 4.0,
        output_path: Optional[Path] = None,
    ) -> Path:
        async def _body() -> Path:
            return await self._generate_instrumental_in_cycle(
                prompt=prompt,
                model=model,
                n=n,
                timeout_s=timeout_s,
                poll_s=poll_s,
                output_path=output_path,
            )
        return await self._run_with_cycle_failover(
            operation="generate_instrumental",
            required_key=None,
            body=_body,
        )

    async def _generate_instrumental_in_cycle(
        self,
        *,
        prompt: str,
        model: Optional[str] = None,
        n: int = 1,
        timeout_s: float = 600.0,
        poll_s: float = 4.0,
        output_path: Optional[Path] = None,
    ) -> Path:
        task = await self.generate_instrumental_task(prompt=prompt, model=model, n=n)
        finished = await self.wait_instrumental_task(task.task_id, timeout_s=timeout_s, poll_s=poll_s)
        audio_url = extract_audio_url(finished.raw or {})
        if not audio_url:
            raise EdennProviderResponseError(
                "ProviderB instrumental task completed but no audio URL was found",
                provider_name="provider_b",
                operation="generate_instrumental",
                context={"task_id": finished.task_id,
                         "response": finished.raw},
            )

        final_path = output_path or self._build_output_path(
            "instrumental")
        await self.download_audio(audio_url, final_path)
        return final_path


__all__ = ["ProviderBMusicProvider", "ProviderBTask", "ProviderBTimestampedLyrics"]


if __name__ == "__main__":

    provider = ProviderBMusicProvider()  # needs PROVIDER_B_API_KEY
    audio_path, _ = asyncio.run(provider.generate(
        prompt="upbeat coastal holiday pop",
        lyrics_prompt="中文",
    ))
    print(audio_path)
