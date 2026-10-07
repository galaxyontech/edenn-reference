from __future__ import annotations

import json
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional, Dict, Any, Iterable, Iterator, Sequence, Tuple
import contextvars
import os
import random
import re
import threading
import time
import asyncio
import logging
from pathlib import Path

import httpx

from EdennCode.exceptions import (
    EdennConfigurationError,
    EdennProviderAuthenticationError,
    EdennProviderError,
    EdennProviderRateLimitError,
    EdennProviderResponseError,
    EdennProviderTimeoutError,
)
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.lyrics_processor import (
    WordTS,
    LyricsProcessor,
)

# Terminal task states from the provider's record-info status enum. "FAILED" is
# NOT one of them — polling for it meant every failed task ran the poll budget to
# exhaustion and then reported a timeout, hiding the real cause. A live extension
# that died on SENSITIVE_WORD_ERROR at ~30s was still being polled 10 minutes
# later, and the job it belonged to would have been billed and lost with a
# misleading error.
PROVIDER_C_TERMINAL_FAILURE_STATUSES = frozenset({
    "CREATE_TASK_FAILED",
    "GENERATE_AUDIO_FAILED",
    "CALLBACK_EXCEPTION",
    "SENSITIVE_WORD_ERROR",
})

BASE_URL = "https://api.provider-c.example.invalid/api/v1"
# 520-524 are Cloudflare edge codes for "the origin didn't answer" — the same
# transient class as 502/504 (the gateway fronts the provider through
# Cloudflare). 408 is a plain request timeout.
RETRYABLE_HTTP_STATUSES = frozenset(
    {408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524})
# Subset safe to auto-retry on a NON-idempotent submit POST: statuses where the
# origin cannot have processed the request (edge never reached it / it refused).
# 408/520/524 are ambiguous — the origin may have accepted the paid submission
# before the edge gave up — so they stay client-retryable but are never
# auto-resubmitted by _json_request.
SAFE_AUTO_RETRY_HTTP_STATUSES = frozenset(
    {429, 500, 502, 503, 504, 521, 522, 523})
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# ----------------------------
# API-key pool (rotate-only)
# ----------------------------
#
# Mirrors the ProviderB numbered-env pattern (PROVIDER_B_API_KEY_N) but deliberately
# WITHOUT the per-key advisory-lock lease: ProviderC keys are not serialized, so a
# pool of N keys keeps today's unlimited per-key concurrency and only spreads
# new generations across keys, cooling a key down and failing over when it
# rate-limits. Task/track ids are account-scoped, so every follow-up call
# (polling, record-info, timestamped lyrics, extend) must reuse the key that
# created the task — hence the task/track -> key binding below.

_NUMBERED_PROVIDER_C_API_KEY_RE = re.compile(r"^PROVIDER_C_API_KEY_(\d+)$")


def provider_c_api_key_candidates_from_env() -> List[Tuple[str, str]]:
    """(label, value) pairs: numbered PROVIDER_C_API_KEY_N sorted first, legacy last."""
    numbered: List[Tuple[int, str, str]] = []
    for name, raw in os.environ.items():
        match = _NUMBERED_PROVIDER_C_API_KEY_RE.match(name)
        if match and raw.strip():
            numbered.append((int(match.group(1)), name, raw.strip()))
    pairs = [(name, value) for _, name, value in sorted(numbered, key=lambda item: item[0])]
    legacy = os.getenv("PROVIDER_C_API_KEY", "").strip()
    if legacy:
        pairs.append(("PROVIDER_C_API_KEY", legacy))
    return pairs


def has_provider_c_api_key_configured() -> bool:
    """True when any ProviderC key (legacy or numbered) is present in the env."""
    return bool(provider_c_api_key_candidates_from_env())


@dataclass(frozen=True)
class _ProviderCKey:
    label: str
    value: str


class _ProviderCKeyPool:
    def __init__(self, keys: Sequence[_ProviderCKey]) -> None:
        self._keys: Tuple[_ProviderCKey, ...] = tuple(keys)

    @classmethod
    def from_env(cls, explicit_api_key: Optional[str] = None) -> "_ProviderCKeyPool":
        if explicit_api_key and explicit_api_key.strip():
            return cls([_ProviderCKey("constructor_api_key", explicit_api_key.strip())])
        return cls([_ProviderCKey(label, value) for label, value in provider_c_api_key_candidates_from_env()])

    @property
    def keys(self) -> Tuple[_ProviderCKey, ...]:
        return self._keys

    @property
    def count(self) -> int:
        return len(self._keys)

    @property
    def default(self) -> _ProviderCKey:
        return self._keys[0]


class _ProviderCKeyCooldowns:
    """In-memory, process-wide rate-limit cooldowns keyed by env-var label.

    ProviderCApi instances are short-lived (constructed per workflow run), so the
    store is module-global. Exponential backoff like the ProviderB health store;
    cross-replica sharing is intentionally omitted (rotate-only policy — a
    stale replica just earns its own 429 and cools locally).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: Dict[str, Tuple[float, int]] = {}

    @staticmethod
    def _base_s() -> float:
        return max(1.0, float(os.getenv("PROVIDER_C_KEY_COOLDOWN_BASE_S", "60")))

    @staticmethod
    def _max_s() -> float:
        return max(1.0, float(os.getenv("PROVIDER_C_KEY_COOLDOWN_MAX_S", "600")))

    def remaining_s(self, label: str) -> float:
        with self._lock:
            entry = self._state.get(label)
        if not entry:
            return 0.0
        return max(0.0, entry[0] - time.monotonic())

    def mark_rate_limited(self, label: str) -> float:
        with self._lock:
            failures = self._state.get(label, (0.0, 0))[1] + 1
            cooldown_s = min(self._max_s(), self._base_s() * (2 ** (failures - 1)))
            self._state[label] = (time.monotonic() + cooldown_s, failures)
        return cooldown_s

    def mark_success(self, label: str) -> None:
        with self._lock:
            self._state.pop(label, None)


_KEY_COOLDOWNS = _ProviderCKeyCooldowns()

_ACTIVE_PROVIDER_C_KEY: contextvars.ContextVar[Optional[_ProviderCKey]] = contextvars.ContextVar(
    "edenn_provider_c_active_key", default=None
)

_TASK_KEY_MAP_MAX = 4096


# ----------------------------
# Data classes
# ----------------------------


@dataclass
class ProviderCTrack:
    audio_id: str
    audio_url: str
    prompt: str = ""
    title: str = ""
    image_url: Optional[str] = None


@dataclass
class GenerationResult:
    task_id: str
    status: str
    tracks: List[ProviderCTrack]


@dataclass
class LyricsVariant:
    text: str
    title: str
    status: str
    error_message: str = ""


@dataclass
class LyricsResponse:
    task_id: str
    status: str
    variants: List[LyricsVariant]
    raw: Dict[str, Any]

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "LyricsResponse":
        if isinstance(payload, str):
            return cls(
                task_id="",
                status="SUCCESS",
                variants=[
                    LyricsVariant(
                        text=payload,
                        title="",
                        status="SUCCESS",
                        error_message="",
                    )
                ],
                raw={"text": payload},
            )
        response = payload.get("response") or {}
        data = response.get("data") or []
        variants = [
            LyricsVariant(
                text=item.get("text") or "",
                title=item.get("title") or "",
                status=item.get("status") or "",
                error_message=item.get("errorMessage") or "",
            )
            for item in data
        ]
        return cls(
            task_id=payload.get("taskId") or "",
            status=payload.get("status") or payload.get("taskStatus") or "",
            variants=variants,
            raw=payload,
        )

    @property
    def primary(self) -> LyricsVariant:
        if not self.variants:
            raise ValueError("No lyrics variants available")
        return self.variants[0]


@dataclass
class GenerateParams:
    """
    Encapsulates ProviderC generation parameters so payload building stays tidy.
    Only non-None fields are included in the API request.
    """
    prompt: str
    model: str = "V5"
    custom_mode: bool = False
    instrumental: bool = False
    callback_url: Optional[str] = None
    title: Optional[str] = None
    style: Optional[str] = None
    negative_tags: Optional[str] = None
    style_weight: Optional[float] = None
    weirdness_constraint: Optional[float] = None
    audio_weight: Optional[float] = None
    extra: Optional[Dict[str, Any]] = None

    def to_payload(self) -> Dict[str, Any]:
        # ProviderC gateway enforces callBackUrl; default to env or localhost placeholder.
        effective_callback = (
            self.callback_url
            or os.getenv("PROVIDER_C_CALLBACK_URL")
            or "http://localhost:3000/provider_c-callback"
        )
        payload: Dict[str, Any] = {
            "prompt": self.prompt,
            "model": self.model,
            "customMode": self.custom_mode,
            "instrumental": self.instrumental,
            "callBackUrl": effective_callback
        }
        if self.title:
            payload["title"] = self.title
        if self.style:
            payload["style"] = self.style
        if self.negative_tags:
            payload["negativeTags"] = self.negative_tags
        if self.style_weight is not None:
            payload["styleWeight"] = self.style_weight
        if self.weirdness_constraint is not None:
            payload["weirdnessConstraint"] = self.weirdness_constraint
        if self.audio_weight is not None:
            payload["audioWeight"] = self.audio_weight
        if self.extra:
            payload.update(self.extra)
        return payload


@dataclass
class UploadCoverParams:
    prompt: str
    upload_url: str
    model: str = "V5"
    custom_mode: bool = False
    instrumental: bool = False
    callback_url: Optional[str] = None
    title: Optional[str] = None
    style: Optional[str] = None
    negative_tags: Optional[str] = None
    style_weight: Optional[float] = None
    weirdness_constraint: Optional[float] = None
    audio_weight: Optional[float] = None
    extra: Optional[Dict[str, Any]] = None

    def to_payload(self) -> Dict[str, Any]:
        effective_callback = (
            self.callback_url
            or os.getenv("PROVIDER_C_CALLBACK_URL")
            or "http://localhost:3000/provider_c-callback"
        )
        payload: Dict[str, Any] = {
            "uploadUrl": self.upload_url,
            "model": self.model,
            "customMode": self.custom_mode,
            "instrumental": self.instrumental,
            "callBackUrl": effective_callback,
        }
        if self.prompt.strip() or not (self.custom_mode and self.instrumental):
            payload["prompt"] = self.prompt
        if self.title:
            payload["title"] = self.title
        if self.style:
            payload["style"] = self.style
        if self.negative_tags:
            payload["negativeTags"] = self.negative_tags
        if self.style_weight is not None:
            payload["styleWeight"] = self.style_weight
        if self.weirdness_constraint is not None:
            payload["weirdnessConstraint"] = self.weirdness_constraint
        if self.audio_weight is not None:
            payload["audioWeight"] = self.audio_weight
        if self.extra:
            payload.update(self.extra)
        return payload


# ----------------------------
# Client
# ----------------------------


class ProviderCApi:
    def __init__(
            self,
            api_key: Optional[str] = None,
            *,
            base_url: Optional[str] = None,
            client: Optional[httpx.AsyncClient] = None,
            timeout: int = 60,
            max_retries: int = 2,
            retry_backoff_s: float = 1.0,
    ) -> None:
        """Lightweight async client for the ProviderC music generation API."""
        primary_base = base_url or os.getenv("PROVIDER_C_BASE_URL") or BASE_URL
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_s = max(0.2, float(retry_backoff_s))

        # Allow comma-separated fallbacks via env.
        env_fallbacks = os.getenv("PROVIDER_C_FALLBACK_BASE_URLS", "")
        fallback_list = [b.strip()
                         for b in env_fallbacks.split(",") if b.strip()]

        # Build candidate list with sensible defaults; preserve order, remove dups.
        # Do not auto-fallback to KIE. Use it only if explicitly configured.
        candidates = [primary_base] + fallback_list + [BASE_URL]
        seen = set()
        self.base_candidates = []
        for b in candidates:
            cleaned = b.rstrip("/")
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                self.base_candidates.append(cleaned)

        self.base = self.base_candidates[0]
        self._key_pool = _ProviderCKeyPool.from_env(api_key)
        if self._key_pool.count == 0:
            raise EdennConfigurationError(
                "PROVIDER_C_API_KEY (or numbered PROVIDER_C_API_KEY_1..N) is required",
                component="provider_c",
                operation="initialize",
            )
        # Back-compat: callers that read .api_key get the pool's default key.
        self.api_key = self._key_pool.default.value
        # task/track id -> creating key. ProviderC ids are account-scoped, so all
        # follow-up calls must reuse the creating key. Bounded FIFO.
        self._id_keys: "OrderedDict[str, _ProviderCKey]" = OrderedDict()
        self._id_keys_lock = threading.Lock()

        self._external_client = client is not None
        self.client = client or httpx.AsyncClient(
            timeout=timeout, follow_redirects=True)

    async def aclose(self) -> None:
        if not self._external_client:
            await self.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.aclose()

    # ---- core helpers ----

    def _headers(self) -> Dict[str, str]:
        return self._headers_for(None)

    def _headers_for(self, task_or_audio_id: Optional[str]) -> Dict[str, str]:
        """Auth headers for a call, preferring the key that created the id."""
        key = (
            self._key_for_id(task_or_audio_id)
            or _ACTIVE_PROVIDER_C_KEY.get()
            or self._key_pool.default
        )
        return {
            "Authorization": f"Bearer {key.value}",
            "Content-Type": "application/json",
        }

    # ---- key-pool helpers (rotate-only; see module docstring above) ----

    @contextmanager
    def _use_key(self, key: Optional[_ProviderCKey]):
        if key is None:
            yield
            return
        token = _ACTIVE_PROVIDER_C_KEY.set(key)
        try:
            yield
        finally:
            _ACTIVE_PROVIDER_C_KEY.reset(token)

    def _bind_id_key(self, task_or_audio_id: Optional[str], key: _ProviderCKey) -> None:
        if not task_or_audio_id:
            return
        with self._id_keys_lock:
            self._id_keys[task_or_audio_id] = key
            while len(self._id_keys) > _TASK_KEY_MAP_MAX:
                self._id_keys.popitem(last=False)

    def _bind_result_keys(self, result: "GenerationResult", key: _ProviderCKey) -> None:
        for track in result.tracks:
            self._bind_id_key(track.audio_id, key)

    def rebind_task_key(self, task_or_audio_id: str, key_label: Optional[str]) -> bool:
        """Re-establish a task->key binding from a durably recorded key label.

        The in-memory binding does not survive a process change, but resumed
        polling must reuse the account that created the task (a non-owner key
        gets a silent-empty 200). Returns False (and degrades to the default
        key) when the label is no longer configured.
        """
        label = (key_label or "").strip()
        if not label:
            return False
        for key in self._key_pool.keys:
            if key.label == label:
                self._bind_id_key(task_or_audio_id, key)
                return True
        logger.warning(
            "[ProviderC] recorded key label %s for task %s is not configured; "
            "resume will poll with the default key and may not see the task",
            label, task_or_audio_id,
        )
        return False

    def _key_for_id(self, task_or_audio_id: Optional[str]) -> Optional[_ProviderCKey]:
        if not task_or_audio_id:
            return None
        with self._id_keys_lock:
            return self._id_keys.get(task_or_audio_id)

    def _task_key_context(self, task_or_audio_id: Optional[str]):
        """Pin the creating key for a follow-up call when the binding is known."""
        return self._use_key(self._key_for_id(task_or_audio_id))

    def _attempt_keys(self, required_key: Optional[_ProviderCKey] = None) -> List[_ProviderCKey]:
        """Candidate keys in attempt order.

        Healthy keys first (shuffled to spread load), cooling keys appended by
        soonest expiry — every key is always eventually tried, so a pool of
        one behaves exactly like the pre-pool client.
        """
        if required_key is not None:
            return [required_key]
        healthy = [k for k in self._key_pool.keys if _KEY_COOLDOWNS.remaining_s(k.label) <= 0]
        cooling = [k for k in self._key_pool.keys if _KEY_COOLDOWNS.remaining_s(k.label) > 0]
        random.shuffle(healthy)
        cooling.sort(key=lambda k: _KEY_COOLDOWNS.remaining_s(k.label))
        return healthy + cooling

    def _iter_attempt_pairs(
        self, required_key: Optional[_ProviderCKey] = None
    ) -> Iterator[Tuple[_ProviderCKey, str]]:
        for key in self._attempt_keys(required_key):
            for base in self.base_candidates:
                yield key, base

    @staticmethod
    def _note_attempt_failure(key: _ProviderCKey, exc: Exception) -> None:
        if isinstance(exc, EdennProviderRateLimitError):
            cooldown_s = _KEY_COOLDOWNS.mark_rate_limited(key.label)
            logger.warning(
                "[ProviderC] key %s rate-limited; cooling down for %.0fs", key.label, cooldown_s
            )

    @staticmethod
    def _note_attempt_success(key: _ProviderCKey) -> None:
        _KEY_COOLDOWNS.mark_success(key.label)

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
                "ProviderC request timed out",
                provider_name="provider_c",
                operation=operation,
                retryable=True,
                context=details,
                cause=exc,
            )
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            message = f"ProviderC request failed with HTTP {status_code}"
            kwargs = {
                "provider_name": "provider_c",
                "operation": operation,
                "status_code": status_code,
                "retryable": status_code in RETRYABLE_HTTP_STATUSES,
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
                "ProviderC request failed before a response was returned",
                provider_name="provider_c",
                operation=operation,
                retryable=True,
                context=details,
                cause=exc,
            )
        return EdennProviderError.wrap(
            exc,
            message="ProviderC provider request failed",
            provider_name="provider_c",
            operation=operation,
            context=details,
        )

    async def _tolerate_transient_poll_error(
        self,
        exc: Exception,
        *,
        operation: str,
        path: str,
        task_id: Optional[str],
        deadline: float,
        poll_s: float,
    ) -> None:
        """Swallow one transient status-poll failure and wait ``poll_s``.

        By the time we are polling, the task is already submitted (and paid
        for), so a single transport blip or retryable gateway status must not
        abandon it — the deadline alone bounds the total wait. Non-retryable
        errors (auth, terminal task status, malformed body) and deadline
        expiry re-raise.
        """
        if isinstance(exc, EdennProviderError):
            mapped = exc
        else:
            mapped = self._map_httpx_exception(
                exc,
                operation=operation,
                path=path,
                context={"task_id": task_id},
            )
        if not mapped.retryable or time.monotonic() + poll_s >= deadline:
            if mapped is exc:
                raise mapped
            raise mapped from exc
        logger.warning(
            "[ProviderC] transient error polling %s for task %s (%s); continuing until deadline",
            path, task_id, mapped,
        )
        await asyncio.sleep(poll_s)

    def _raise_api_body_error(
        self,
        body: Dict[str, Any],
        *,
        operation: str,
        path: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        code = body.get("code")
        message = str(
            body.get("msg")
            or body.get("message")
            or body.get("error")
            or "ProviderC API error"
        )
        details = self._context_for(path, context)
        details["response_code"] = code
        kwargs = {
            "provider_name": "provider_c",
            "operation": operation,
            "status_code": code if isinstance(code, int) else None,
            "context": details,
        }
        if code in {401, 403}:
            raise EdennProviderAuthenticationError(message, **kwargs)
        if code == 429:
            raise EdennProviderRateLimitError(message, retryable=True, **kwargs)
        raise EdennProviderResponseError(message, **kwargs)

    def _decode_json_body(
        self,
        resp: httpx.Response,
        *,
        operation: str,
        path: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        try:
            body = resp.json()
        except ValueError as exc:
            raise EdennProviderResponseError(
                "ProviderC returned a non-JSON response",
                provider_name="provider_c",
                operation=operation,
                status_code=resp.status_code,
                context=self._context_for(path, context),
                cause=exc,
            ) from exc
        if not isinstance(body, dict):
            raise EdennProviderResponseError(
                f"Unexpected ProviderC response type: {type(body)}",
                provider_name="provider_c",
                operation=operation,
                status_code=resp.status_code,
                context=self._context_for(path, context),
            )
        return body

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

    async def _json_request(
            self,
            method: str,
            path: str,
            payload: Optional[Dict[str, Any]] = None,
            *,
            timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base}{path}"
        request_kwargs: Dict[str, Any] = {
            "json": payload,
            "headers": self._headers()
        }
        if timeout_s is not None:
            request_kwargs["timeout"] = timeout_s
        operation = f"{method.upper()} {path}"

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self.client.request(method, url, **request_kwargs)
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                mapped = self._map_httpx_exception(exc, operation=operation, path=path)
                last_error = mapped
                if attempt < self.max_retries:
                    wait_s = self.retry_backoff_s * (2 ** attempt)
                    logger.warning(
                        "Music provider transport error on %s, retrying in %.2fs (%d/%d): %s",
                        operation, wait_s, attempt + 1, self.max_retries, exc,
                    )
                    await asyncio.sleep(wait_s)
                    continue
                raise mapped from exc

            status_code = resp.status_code
            if status_code in SAFE_AUTO_RETRY_HTTP_STATUSES and attempt < self.max_retries:
                retry_after = self._parse_retry_after_s(resp.headers.get("Retry-After"))
                wait_s = retry_after if retry_after is not None else self.retry_backoff_s * (2 ** attempt)
                logger.warning(
                    "Music provider HTTP %d on %s, retrying in %.2fs (%d/%d)",
                    status_code, operation, wait_s, attempt + 1, self.max_retries,
                )
                await asyncio.sleep(wait_s)
                continue

            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise self._map_httpx_exception(exc, operation=operation, path=path) from exc

            body = self._decode_json_body(resp, operation=operation, path=path)
            if body.get("code") != 200:
                self._raise_api_body_error(body, operation=operation, path=path)
            return body["data"]

        if last_error:
            raise last_error
        raise EdennProviderError(
            f"ProviderC request failed after retries: {operation}",
            provider_name="provider_c",
            operation=operation,
            context={"path": path},
        )

    # ---- generation ----

    async def generate(self, params: GenerateParams) -> str:
        """
        Create a new generation job.

        Args:
            params: GenerateParams bundle containing prompt and optional tuning fields.
        Returns:
            task_id string for polling.
        """
        data = await self._json_request("POST", "/generate", params.to_payload())
        task_id = data.get("taskId")
        if not task_id:
            raise EdennProviderResponseError(
                "ProviderC generate response missing taskId",
                provider_name="provider_c",
                operation="generate",
                context={"path": "/generate"},
            )
        # Bind here too so bare callers (outside the rotation wrappers) still
        # get account-consistent follow-up calls.
        self._bind_id_key(task_id, _ACTIVE_PROVIDER_C_KEY.get() or self._key_pool.default)
        return task_id

    async def upload_cover(self, params: UploadCoverParams) -> str:
        if not params.upload_url.strip():
            raise EdennProviderResponseError(
                "ProviderC upload-cover requires upload_url",
                provider_name="provider_c",
                operation="upload_cover",
                context={"path": "/generate/upload-cover"},
            )
        if params.custom_mode:
            if not (params.style or "").strip():
                raise EdennProviderResponseError(
                    "ProviderC upload-cover custom mode requires a non-empty style",
                    provider_name="provider_c",
                    operation="upload_cover",
                    context={"path": "/generate/upload-cover"},
                )
            if not (params.title or "").strip():
                raise EdennProviderResponseError(
                    "ProviderC upload-cover custom mode requires a non-empty title",
                    provider_name="provider_c",
                    operation="upload_cover",
                    context={"path": "/generate/upload-cover"},
                )
            if not params.instrumental and not params.prompt.strip():
                raise EdennProviderResponseError(
                    "ProviderC upload-cover custom mode requires non-empty lyrics prompt for vocal runs",
                    provider_name="provider_c",
                    operation="upload_cover",
                    context={"path": "/generate/upload-cover"},
                )
        elif not params.prompt.strip():
            raise EdennProviderResponseError(
                "ProviderC upload-cover requires a non-empty prompt",
                provider_name="provider_c",
                operation="upload_cover",
                context={"path": "/generate/upload-cover"},
            )
        data = await self._json_request("POST", "/generate/upload-cover", params.to_payload())
        task_id = data.get("taskId")
        if not task_id:
            raise EdennProviderResponseError(
                "ProviderC upload-cover response missing taskId",
                provider_name="provider_c",
                operation="upload_cover",
                context={"path": "/generate/upload-cover", "response": data},
            )
        self._bind_id_key(task_id, _ACTIVE_PROVIDER_C_KEY.get() or self._key_pool.default)
        return task_id

    async def extend(
        self,
        *,
        audio_id: str,
        model: str = "V5",
        callback_url: Optional[str] = None,
        instrumental: Optional[bool] = None,
        style: Optional[str] = None,
        title: Optional[str] = None,
        continue_at: Optional[float] = None,
    ) -> str:
        """
        Create an extension job for an existing ProviderC audio track.

        Two modes, per the provider's parameter guide:

        ``defaultParamFlag=False`` needs only ``audioId`` and reuses the source
        track's parameters — but NOT ``instrumental``: a live extension of an
        instrumental track came back recorded as ``instrumental: false``, so a
        successful extension would have sung over an instrumental deliverable.

        Supplying ``style``/``title``/``continue_at`` switches to
        ``defaultParamFlag=True``, where we state the extension's parameters
        ourselves instead of inheriting the source's stored text. That matters
        for an instrumental: inheriting text we cannot see means the provider
        re-screens it, and two live extensions of a clean instrumental were
        refused with SENSITIVE_WORD_ERROR while the generation of that very
        track passed. In this mode the provider requires style and title, and
        forbids ``prompt``/``vocalGender`` when ``instrumental`` is true.

        ``model`` must match the source track's model either way.
        """
        if not audio_id.strip():
            raise EdennProviderResponseError(
                "ProviderC extend requires audio_id",
                provider_name="provider_c",
                operation="extend",
                context={"path": "/generate/extend"},
            )

        effective_callback = (
            callback_url
            or os.getenv("PROVIDER_C_CALLBACK_URL")
            or "http://localhost:3000/provider_c-callback"
        )
        custom_params = any(v is not None for v in (style, title, continue_at))
        payload = {
            "audioId": audio_id.strip(),
            "defaultParamFlag": custom_params,
            "model": model,
            "callBackUrl": effective_callback,
        }
        if instrumental is not None:
            payload["instrumental"] = bool(instrumental)
        if custom_params:
            missing = [n for n, v in (("style", style), ("title", title),
                                      ("continue_at", continue_at)) if v is None]
            if missing:
                raise EdennProviderResponseError(
                    "ProviderC extend with custom parameters requires style, title and "
                    f"continue_at; missing: {', '.join(missing)}",
                    provider_name="provider_c",
                    operation="extend",
                    context={"path": "/generate/extend", "missing": missing},
                )
            payload["style"] = str(style)
            payload["title"] = str(title)
            payload["continueAt"] = float(continue_at)
        # Bare calls honor the source track's creating key when it is known.
        with self._task_key_context(audio_id.strip()):
            data = await self._json_request("POST", "/generate/extend", payload)
        task_id = data.get("taskId")
        if not task_id:
            raise EdennProviderResponseError(
                "ProviderC extend response missing taskId",
                provider_name="provider_c",
                operation="extend",
                context={"path": "/generate/extend", "response": data},
            )
        self._bind_id_key(
            task_id,
            self._key_for_id(audio_id.strip()) or _ACTIVE_PROVIDER_C_KEY.get() or self._key_pool.default,
        )
        return task_id

    async def generate_and_poll_first_track(
            self,
            params: GenerateParams,
            *,
            timeout_s: float = 180.0,
            poll_s: Optional[float] = None,
    ) -> Tuple[str, ProviderCTrack, str]:
        """
        Generate a task and poll for first track with failover across base URLs.
        Returns (task_id, first_track, base_used).
        Tries each candidate base in order; on timeout/failure moves to next.
        """
        last_err: Optional[Exception] = None
        for key, base in self._iter_attempt_pairs():
            self.base = base
            submitted_task_id: Optional[str] = None
            try:
                with self._use_key(key):
                    task_id = await self.generate(params)
                    submitted_task_id = task_id
                    self._bind_id_key(task_id, key)
                    track = await self.poll_generation_first_track(
                        task_id,
                        timeout_s=timeout_s,
                        poll_s=poll_s,
                    )
                self._bind_id_key(track.audio_id, key)
                self._note_attempt_success(key)
                return task_id, track, base
            except Exception as exc:
                self._note_attempt_failure(key, exc)
                last_err = exc
                if submitted_task_id is not None:
                    # The provider accepted (and billed) this task; submitting
                    # again on another key/base would pay twice, and no other
                    # key can even see the created task. Surface the error so
                    # the recorded task id can be resumed instead.
                    raise
                logging.warning(
                    "[ProviderC] key %s base %s failed (%s), trying next candidate", key.label, base, exc)
                continue
        raise last_err if last_err else EdennProviderError(
            "ProviderC generation failed on all configured bases",
            provider_name="provider_c",
            operation="generate_and_poll_first_track",
            context={"bases": list(self.base_candidates)},
        )

    async def generate_and_poll_tracks(
            self,
            params: GenerateParams,
            *,
            timeout_s: float = 180.0,
            poll_s: Optional[float] = None,
            on_task_created: Optional[Callable[[str, str, str], Awaitable[None]]] = None,
    ) -> Tuple[str, GenerationResult, str]:
        """
        Generate a task and poll until the full set of tracks is ready.
        Returns (task_id, generation_result, base_used).
        Tries each candidate base in order; on timeout/failure moves to next.
        """
        last_err: Optional[Exception] = None
        _api_start = time.time()
        for key, base in self._iter_attempt_pairs():
            self.base = base
            submitted_task_id: Optional[str] = None
            try:
                with self._use_key(key):
                    task_id = await self.generate(params)
                    submitted_task_id = task_id
                    self._bind_id_key(task_id, key)
                    if on_task_created is not None:
                        await on_task_created(task_id, base, key.label)
                    result = await self.poll_generation(
                        task_id,
                        timeout_s=timeout_s,
                        poll_s=poll_s,
                    )
                self._bind_result_keys(result, key)
                self._note_attempt_success(key)
                _elapsed = time.time() - _api_start
                logging.info(json.dumps({
                    "event": "pipeline_timing",
                    "stage": "provider_c_api_call",
                    "duration_s": round(_elapsed, 3),
                    "provider": "provider_c",
                    "base_used": base,
                }))
                return task_id, result, base
            except Exception as exc:
                self._note_attempt_failure(key, exc)
                last_err = exc
                if submitted_task_id is not None:
                    # The provider accepted (and billed) this task; submitting
                    # again on another key/base would pay twice, and no other
                    # key can even see the created task. Surface the error so
                    # the recorded task id can be resumed instead.
                    raise
                logging.warning(
                    "[ProviderC] key %s base %s failed (%s), trying next candidate", key.label, base, exc)
                continue
        raise last_err if last_err else EdennProviderError(
            "ProviderC generation failed on all configured bases",
            provider_name="provider_c",
            operation="generate_and_poll_tracks",
            context={"bases": list(self.base_candidates)},
        )

    async def generate_and_wait_tracks(
            self,
            params: GenerateParams,
            *,
            timeout_s: float = 180.0,
            poll_s: Optional[float] = None,
            callback_waiter: Optional[
                Callable[[str, float], Awaitable[GenerationResult]]
            ] = None,
            on_task_created: Optional[Callable[[str, str, str], Awaitable[None]]] = None,
    ) -> Tuple[str, GenerationResult, str]:
        """
        Generate a task and wait for tracks via provider callback when supplied.

        The callback waiter is responsible for receiving and resolving the task
        payload. If no waiter is supplied, this preserves the existing polling
        behavior.
        """
        last_err: Optional[Exception] = None
        _api_start = time.time()
        for key, base in self._iter_attempt_pairs():
            self.base = base
            submitted_task_id: Optional[str] = None
            try:
                with self._use_key(key):
                    task_id = await self.generate(params)
                    submitted_task_id = task_id
                    self._bind_id_key(task_id, key)
                    if on_task_created is not None:
                        await on_task_created(task_id, base, key.label)
                    if callback_waiter is not None:
                        try:
                            result = await callback_waiter(task_id, timeout_s)
                        except EdennProviderTimeoutError as exc:
                            fallback_enabled = (
                                os.getenv(
                                    "PROVIDER_C_CALLBACK_FALLBACK_POLL_ON_TIMEOUT",
                                    "true",
                                ).strip().lower()
                                in {"1", "true", "yes", "on"}
                            )
                            if not fallback_enabled:
                                raise
                            fallback_timeout_s = float(
                                os.getenv("PROVIDER_C_CALLBACK_FALLBACK_POLL_TIMEOUT_S", "60.0")
                            )
                            logging.warning(
                                "[ProviderC] callback wait timed out for task %s; falling back to provider polling for %.1fs: %s",
                                task_id,
                                fallback_timeout_s,
                                exc,
                            )
                            result = await self.poll_generation(
                                task_id,
                                timeout_s=fallback_timeout_s,
                                poll_s=poll_s,
                            )
                    else:
                        result = await self.poll_generation(
                            task_id,
                            timeout_s=timeout_s,
                            poll_s=poll_s,
                        )
                self._bind_result_keys(result, key)
                self._note_attempt_success(key)
                _elapsed = time.time() - _api_start
                logging.info(json.dumps({
                    "event": "pipeline_timing",
                    "stage": "provider_c_api_call",
                    "duration_s": round(_elapsed, 3),
                    "provider": "provider_c",
                    "base_used": base,
                    "wait_mode": "callback" if callback_waiter is not None else "poll",
                }))
                return task_id, result, base
            except Exception as exc:
                self._note_attempt_failure(key, exc)
                last_err = exc
                if submitted_task_id is not None:
                    # The provider accepted (and billed) this task; submitting
                    # again on another key/base would pay twice, and no other
                    # key can even see the created task. Surface the error so
                    # the recorded task id can be resumed instead.
                    raise
                logging.warning(
                    "[ProviderC] key %s base %s failed (%s), trying next candidate", key.label, base, exc)
                continue
        raise last_err if last_err else EdennProviderError(
            "ProviderC generation failed on all configured bases",
            provider_name="provider_c",
            operation="generate_and_wait_tracks",
            context={"bases": list(self.base_candidates)},
        )

    async def upload_cover_and_poll_tracks(
            self,
            params: UploadCoverParams,
            *,
            timeout_s: float = 180.0,
            poll_s: Optional[float] = None,
    ) -> Tuple[str, GenerationResult, str]:
        last_err: Optional[Exception] = None
        for key, base in self._iter_attempt_pairs():
            self.base = base
            submitted_task_id: Optional[str] = None
            try:
                with self._use_key(key):
                    task_id = await self.upload_cover(params)
                    submitted_task_id = task_id
                    self._bind_id_key(task_id, key)
                    result = await self.poll_generation(
                        task_id,
                        timeout_s=timeout_s,
                        poll_s=poll_s,
                    )
                self._bind_result_keys(result, key)
                self._note_attempt_success(key)
                return task_id, result, base
            except Exception as exc:
                self._note_attempt_failure(key, exc)
                last_err = exc
                if submitted_task_id is not None:
                    # The provider accepted (and billed) this task; submitting
                    # again on another key/base would pay twice, and no other
                    # key can even see the created task.
                    raise
                logging.warning(
                    "[ProviderC] upload-cover key %s base %s failed (%s), trying next candidate", key.label, base, exc)
                continue
        raise last_err if last_err else EdennProviderError(
            "ProviderC upload-cover failed on all configured bases",
            provider_name="provider_c",
            operation="upload_cover_and_poll_tracks",
            context={"bases": list(self.base_candidates)},
        )

    async def extend_and_poll_track(
            self,
            audio_id: str,
            *,
            model: str = "V5",
            instrumental: Optional[bool] = None,
            timeout_s: float = 180.0,
            poll_s: Optional[float] = None,
            on_task_created: Optional[Callable[[str, str, str], Awaitable[None]]] = None,
    ) -> Tuple[str, ProviderCTrack, str]:
        """
        Extend an existing track and wait until the first extended track is ready.
        Returns (task_id, first_track, base_used).
        """
        last_err: Optional[Exception] = None
        # audio_id is account-scoped: pin the creating key when we know it.
        for key, base in self._iter_attempt_pairs(required_key=self._key_for_id(audio_id)):
            self.base = base
            submitted_task_id: Optional[str] = None
            try:
                with self._use_key(key):
                    task_id = await self.extend(
                        audio_id=audio_id, model=model, instrumental=instrumental,
                    )
                    submitted_task_id = task_id
                    self._bind_id_key(task_id, key)
                    if on_task_created is not None:
                        await on_task_created(task_id, base, key.label)
                    track = await self.poll_generation_first_track(
                        task_id,
                        timeout_s=timeout_s,
                        poll_s=poll_s,
                    )
                self._bind_id_key(track.audio_id, key)
                self._note_attempt_success(key)
                return task_id, track, base
            except Exception as exc:
                self._note_attempt_failure(key, exc)
                last_err = exc
                if submitted_task_id is not None:
                    # The provider accepted (and billed) this extend task;
                    # submitting again would pay twice.
                    raise
                logging.warning(
                    "[ProviderC] extend key %s base %s failed (%s), trying next candidate", key.label, base, exc)
                continue
        raise last_err if last_err else EdennProviderError(
            "ProviderC extend failed on all configured bases",
            provider_name="provider_c",
            operation="extend_and_poll_track",
            context={"bases": list(self.base_candidates), "audio_id": audio_id},
        )

    async def extend_and_wait_track(
            self,
            audio_id: str,
            *,
            model: str = "V5",
            instrumental: Optional[bool] = None,
            timeout_s: float = 180.0,
            poll_s: Optional[float] = None,
            callback_url: Optional[str] = None,
            callback_waiter: Optional[
                Callable[[str, float], Awaitable[GenerationResult]]
            ] = None,
            on_task_created: Optional[Callable[[str, str, str], Awaitable[None]]] = None,
    ) -> Tuple[str, ProviderCTrack, str]:
        last_err: Optional[Exception] = None
        # audio_id is account-scoped: pin the creating key when we know it.
        for key, base in self._iter_attempt_pairs(required_key=self._key_for_id(audio_id)):
            self.base = base
            submitted_task_id: Optional[str] = None
            try:
                with self._use_key(key):
                    task_id = await self.extend(
                        audio_id=audio_id,
                        model=model,
                        callback_url=callback_url,
                        instrumental=instrumental,
                    )
                    submitted_task_id = task_id
                    self._bind_id_key(task_id, key)
                    if on_task_created is not None:
                        await on_task_created(task_id, base, key.label)
                    if callback_waiter is not None:
                        try:
                            result = await callback_waiter(task_id, timeout_s)
                            if not result.tracks:
                                raise EdennProviderResponseError(
                                    "ProviderC extend callback completed without tracks",
                                    provider_name="provider_c",
                                    operation="extend_and_wait_track",
                                    context={"task_id": task_id},
                                )
                            track = result.tracks[0]
                        except EdennProviderTimeoutError as exc:
                            fallback_enabled = (
                                os.getenv(
                                    "PROVIDER_C_CALLBACK_FALLBACK_POLL_ON_TIMEOUT",
                                    "true",
                                ).strip().lower()
                                in {"1", "true", "yes", "on"}
                            )
                            if not fallback_enabled:
                                raise
                            fallback_timeout_s = float(
                                os.getenv("PROVIDER_C_CALLBACK_FALLBACK_POLL_TIMEOUT_S", "60.0")
                            )
                            logging.warning(
                                "[ProviderC] extend callback wait timed out for task %s; falling back to provider polling for %.1fs: %s",
                                task_id,
                                fallback_timeout_s,
                                exc,
                            )
                            track = await self.poll_generation_first_track(
                                task_id,
                                timeout_s=fallback_timeout_s,
                                poll_s=poll_s,
                            )
                    else:
                        track = await self.poll_generation_first_track(
                            task_id,
                            timeout_s=timeout_s,
                            poll_s=poll_s,
                        )
                self._bind_id_key(track.audio_id, key)
                self._note_attempt_success(key)
                return task_id, track, base
            except Exception as exc:
                self._note_attempt_failure(key, exc)
                last_err = exc
                if submitted_task_id is not None:
                    # The provider accepted (and billed) this extend task;
                    # submitting again would pay twice.
                    raise
                logging.warning(
                    "[ProviderC] extend key %s base %s failed (%s), trying next candidate", key.label, base, exc)
                continue
        raise last_err if last_err else EdennProviderError(
            "ProviderC extend failed on all configured bases",
            provider_name="provider_c",
            operation="extend_and_wait_track",
            context={"bases": list(self.base_candidates), "audio_id": audio_id},
        )

    async def poll_generation(
            self,
            task_id: str,
            *,
            timeout_s: float = 180.0,
            poll_s: Optional[float] = None,
            max_poll_s: Optional[float] = None,
    ) -> GenerationResult:
        """
        Poll a generation job until success/failure.

        Args:
            task_id: Job identifier from generate().
            timeout_s: Max seconds to wait.
            poll_s: Interval between polls (uniform).
            max_poll_s: Unused (kept for compatibility).
        Returns:
            GenerationResult with all returned tracks.
        Raises:
            TimeoutError on timeout, RuntimeError on API failure.
        """
        path = "/generate/record-info"
        params = {"taskId": task_id}
        if poll_s is None:
            poll_s = float(os.getenv("PROVIDER_C_POLL_S", "10.0"))
        poll_s = max(0.1, float(poll_s))
        start_delay_s = 30.0
        deadline = time.monotonic() + timeout_s

        if start_delay_s > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EdennProviderTimeoutError(
                    f"Timed out waiting for ProviderC task {task_id}",
                    provider_name="provider_c",
                    operation="poll_generation",
                    retryable=True,
                    context={"task_id": task_id},
                )
            await asyncio.sleep(min(start_delay_s, remaining))

        while time.monotonic() < deadline:
            try:
                resp = await self.client.get(
                    f"{self.base}{path}", params=params, headers=self._headers_for(task_id))
                resp.raise_for_status()
                body = self._decode_json_body(
                    resp,
                    operation="poll_generation",
                    path=path,
                    context={"task_id": task_id},
                )
                if body.get("code") != 200:
                    self._raise_api_body_error(
                        body,
                        operation="poll_generation",
                        path=path,
                        context={"task_id": task_id},
                    )
            except Exception as exc:
                await self._tolerate_transient_poll_error(
                    exc,
                    operation="poll_generation",
                    path=path,
                    task_id=task_id,
                    deadline=deadline,
                    poll_s=poll_s,
                )
                continue

            data = body["data"]
            status = data.get("status")

            if status == "SUCCESS":
                provider_c_data = (data.get("response") or {}).get("provider_cData") or []
                tracks = [
                    ProviderCTrack(
                        audio_id=t["id"],
                        audio_url=t["audioUrl"],
                        prompt=t.get("prompt", ""),
                        title=t.get("title", ""),
                        image_url=t.get("imageUrl"),
                    )
                    for t in provider_c_data
                ]
                return GenerationResult(task_id=task_id, status=status, tracks=tracks)

            if status in PROVIDER_C_TERMINAL_FAILURE_STATUSES:
                raise EdennProviderResponseError(
                    f"ProviderC job reached terminal failure status={status}",
                    provider_name="provider_c",
                    operation="poll_generation",
                    context={
                        "task_id": task_id,
                        "status": status,
                        "provider_error_code": data.get("errorCode"),
                        "provider_error_message": data.get("errorMessage"),
                    },
                )

            await asyncio.sleep(poll_s)

        raise EdennProviderTimeoutError(
            f"Timed out waiting for ProviderC task {task_id}",
            provider_name="provider_c",
            operation="poll_generation",
            retryable=True,
            context={"task_id": task_id},
        )

    async def poll_generation_first_track(
            self,
            task_id: str,
            *,
            timeout_s: float = 180.0,
            poll_s: Optional[float] = None,
            max_poll_s: Optional[float] = None,
    ) -> ProviderCTrack:
        """
        Poll a generation job until the FIRST track is ready.

        Args:
            task_id: Job identifier from generate().
            timeout_s: Max seconds to wait.
            poll_s: Interval between polls (uniform).
            max_poll_s: Unused (kept for compatibility).
        Returns:
            First completed ProviderCTrack.
        Raises:
            TimeoutError on timeout, RuntimeError on API failure.
        """
        path = "/generate/record-info"
        params = {"taskId": task_id}
        if poll_s is None:
            poll_s = float(os.getenv("PROVIDER_C_POLL_S", "10.0"))
        poll_s = max(0.1, float(poll_s))
        start_delay_s = 30.0

        deadline = time.monotonic() + timeout_s

        if start_delay_s > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EdennProviderTimeoutError(
                    f"Timed out waiting for ProviderC task {task_id}",
                    provider_name="provider_c",
                    operation="poll_generation_first_track",
                    retryable=True,
                    context={"task_id": task_id},
                )
            await asyncio.sleep(min(start_delay_s, remaining))

        while time.monotonic() < deadline:
            try:
                resp = await self.client.get(
                    f"{self.base}{path}", params=params, headers=self._headers_for(task_id))
                resp.raise_for_status()
                body = self._decode_json_body(
                    resp,
                    operation="poll_generation_first_track",
                    path=path,
                    context={"task_id": task_id},
                )
                if body.get("code") != 200:
                    self._raise_api_body_error(
                        body,
                        operation="poll_generation_first_track",
                        path=path,
                        context={"task_id": task_id},
                    )
            except Exception as exc:
                await self._tolerate_transient_poll_error(
                    exc,
                    operation="poll_generation_first_track",
                    path=path,
                    task_id=task_id,
                    deadline=deadline,
                    poll_s=poll_s,
                )
                continue

            data = body["data"]
            status = data.get("status")

            # Check if we have any completed tracks in the response
            provider_c_data = (data.get("response") or {}).get("provider_cData") or []

            # Look for the first track with an audio URL (indicating it's ready)
            for t in provider_c_data:
                if t.get("audioUrl"):
                    # First track is ready! Return immediately
                    return ProviderCTrack(
                        audio_id=t["id"],
                        audio_url=t["audioUrl"],
                        prompt=t.get("prompt", ""),
                        title=t.get("title", ""),
                        image_url=t.get("imageUrl"),
                    )

            # Any terminal failure ends the wait now: the provider will not
            # change its mind, and polling on only wastes the caller's budget
            # and replaces a precise cause with a timeout.
            if status in PROVIDER_C_TERMINAL_FAILURE_STATUSES:
                raise EdennProviderResponseError(
                    f"ProviderC job reached terminal failure status={status}",
                    provider_name="provider_c",
                    operation="poll_generation_first_track",
                    context={
                        "task_id": task_id,
                        "status": status,
                        "provider_error_code": data.get("errorCode"),
                        "provider_error_message": data.get("errorMessage"),
                    },
                )

            await asyncio.sleep(poll_s)

        raise EdennProviderTimeoutError(
            f"Timed out waiting for ProviderC task {task_id}",
            provider_name="provider_c",
            operation="poll_generation_first_track",
            retryable=True,
            context={"task_id": task_id},
        )

    # ---- lyrics ----

    async def generate_lyrics(
            self,
            prompt: str,
            *,
            title: Optional[str] = None,
            style: Optional[str] = None,
            callback_url: Optional[str] = None,
            timeout_s: float = 60.0,
            poll_s: float = 5.0,
            max_poll_s: float = 10.0,
            backoff: float = 1.2,
            extra: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Create a lyrics generation task and poll until completion.
        Returns the lyrics text as a string.

        Args:
            prompt: Lyrics generation prompt
            title: Optional song title
            style: Optional style/genre
            callback_url: Optional callback URL
            timeout_s: Maximum wait time
            poll_s: Initial polling interval
            max_poll_s: Maximum polling interval
            backoff: Polling backoff multiplier
            extra: Additional payload fields

        Returns:
            Lyrics text string

        Raises:
            RuntimeError: If task creation or generation fails
            TimeoutError: If polling exceeds timeout_s
        """
        effective_callback = (
            callback_url
            or os.getenv("PROVIDER_C_CALLBACK_URL")
            or "http://localhost:3000/provider_c-callback"
        )
        payload: Dict[str, Any] = {
            "prompt": prompt,
            "callBackUrl": effective_callback,
        }
        if title:
            payload["title"] = title
        if style:
            payload["style"] = style
        if extra:
            payload.update(extra)

        request_timeout_s = min(30.0, max(5.0, float(timeout_s)))

        lyrics_key = _ACTIVE_PROVIDER_C_KEY.get() or self._attempt_keys()[0]
        with self._use_key(lyrics_key):
            data = await self._json_request(
                "POST",
                "/lyrics",
                payload,
                timeout_s=request_timeout_s,
            )
        task_id = data.get("taskId")
        self._bind_id_key(task_id, lyrics_key)
        if not task_id:
            raise EdennProviderResponseError(
                "ProviderC lyrics task creation did not return taskId",
                provider_name="provider_c",
                operation="generate_lyrics",
                context={"path": "/lyrics", "response": data},
            )

        deadline = time.monotonic() + timeout_s
        fail_statuses = {
            "CREATE_TASK_FAILED",
            "GENERATE_LYRICS_FAILED",
            "CALLBACK_EXCEPTION",
            "SENSITIVE_WORD_ERROR",
        }

        while time.monotonic() < deadline:
            try:
                resp = await self.client.get(
                    f"{self.base}/lyrics/record-info",
                    params={"taskId": task_id},
                    headers=self._headers_for(task_id),
                    timeout=request_timeout_s,
                )
                resp.raise_for_status()
                body = self._decode_json_body(
                    resp,
                    operation="generate_lyrics",
                    path="/lyrics/record-info",
                    context={"task_id": task_id},
                )
                if body.get("code") != 200:
                    self._raise_api_body_error(
                        body,
                        operation="generate_lyrics",
                        path="/lyrics/record-info",
                        context={"task_id": task_id},
                    )
            except Exception as exc:
                await self._tolerate_transient_poll_error(
                    exc,
                    operation="generate_lyrics",
                    path="/lyrics/record-info",
                    task_id=task_id,
                    deadline=deadline,
                    poll_s=poll_s,
                )
                continue

            payload_data = body.get("data") or {}
            status = payload_data.get(
                "status") or payload_data.get("taskStatus")

            if status == "SUCCESS":
                # Try multiple paths to get lyrics text
                response = payload_data.get("response") or {}
                data_list = response.get("data") or []

                if data_list and isinstance(data_list, list):
                    first_item = data_list[0]
                    if isinstance(first_item, dict) and "text" in first_item:
                        return first_item["text"]

                raise EdennProviderResponseError(
                    "ProviderC lyrics response missing text field",
                    provider_name="provider_c",
                    operation="generate_lyrics",
                    context={"task_id": task_id, "response": payload_data},
                )

            if status in fail_statuses:
                raise EdennProviderResponseError(
                    f"ProviderC lyrics task failed with status={status}",
                    provider_name="provider_c",
                    operation="generate_lyrics",
                    context={"task_id": task_id, "response": payload_data},
                )

            await asyncio.sleep(poll_s)
            if backoff > 1.0:
                poll_s = min(max_poll_s, poll_s * backoff)

        raise EdennProviderTimeoutError(
            f"Timed out waiting for ProviderC lyrics task {task_id}",
            provider_name="provider_c",
            operation="generate_lyrics",
            retryable=True,
            context={"task_id": task_id},
        )

    async def get_timestamped_lyrics(self, task_id: str, audio_id: str) -> List[WordTS]:
        """
        Fetch word-level timestamps for a specific generated track.

        Args:
            task_id: Job id that produced the audio.
            audio_id: Audio id from a ProviderCTrack.
        Returns:
            Sorted list of WordTS entries.
        """
        payload = {"taskId": task_id, "audioId": audio_id}
        path = "/generate/get-timestamped-lyrics"
        bound = self._key_for_id(task_id) or self._key_for_id(audio_id)
        if bound is not None or self._key_pool.count == 1 or _ACTIVE_PROVIDER_C_KEY.get() is not None:
            with self._use_key(bound or _ACTIVE_PROVIDER_C_KEY.get()):
                data = await self._json_request("POST", path, payload)
            return LyricsProcessor.from_aligned_words(data.get("alignedWords") or [])

        # Cross-process caller (e.g. the callback service): the creating key is
        # unknown here, so try each key until one owns the task. A non-owner
        # key answers 200 with an empty record (live-verified), so keep trying
        # while the words come back empty; an empty result from EVERY key is
        # legit (e.g. instrumental) and is returned as such.
        last_err: Optional[Exception] = None
        saw_empty_success = False
        for key in self._attempt_keys():
            try:
                with self._use_key(key):
                    data = await self._json_request("POST", path, payload)
            except (EdennProviderAuthenticationError, EdennProviderResponseError) as exc:
                last_err = exc
                continue
            words = LyricsProcessor.from_aligned_words(data.get("alignedWords") or [])
            if words:
                self._bind_id_key(task_id, key)
                return words
            saw_empty_success = True
        if saw_empty_success:
            return []
        assert last_err is not None
        raise last_err

    async def wait_for_timestamped_lyrics(
            self,
            task_id: str,
            audio_id: str,
            *,
            timeout_s: float = 100.0,
            poll_s: Optional[float] = None,
            max_poll_s: Optional[float] = None,
            backoff: Optional[float] = None,
    ) -> List[WordTS]:
        """
        Poll for timestamped lyrics until available or timeout.
        Returns an empty list on timeout or error.

        Args:
            task_id: Generation task ID
            audio_id: Audio track ID
            timeout_s: Maximum wait time
            poll_s: Initial polling interval
            max_poll_s: Maximum polling interval
            backoff: Polling backoff multiplier

        Returns:
            List of WordTS objects, or empty list on timeout/error
        """
        if timeout_s <= 0:
            return []
        if poll_s is None:
            poll_s = float(os.getenv("PROVIDER_C_LYRICS_POLL_S",
                                     os.getenv("PROVIDER_C_POLL_S", "5.0")))
        if max_poll_s is None:
            max_poll_s = float(os.getenv("PROVIDER_C_LYRICS_MAX_POLL_S",
                                         os.getenv("PROVIDER_C_MAX_POLL_S", "10.0")))
        if backoff is None:
            backoff = float(os.getenv("PROVIDER_C_LYRICS_POLL_BACKOFF",
                                      os.getenv("PROVIDER_C_POLL_BACKOFF", "1.2")))

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                words = await self.get_timestamped_lyrics(task_id, audio_id)
                if words:
                    return self._clean_section_labels(words)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Treat transient failures as "not ready" and keep polling.
                pass

            await asyncio.sleep(poll_s)
            if backoff > 1.0:
                poll_s = min(max_poll_s, poll_s * backoff)

        # Silence lyrics failures after timeout so callers can proceed.
        return []

    @staticmethod
    def _clean_section_labels(words: List[WordTS]) -> List[WordTS]:
        """Remove pure section markers like [Chorus], [Verse2] from ProviderC timestamps."""
        import re

        pattern = re.compile(r"^\s*\[[^\]]+\]\s*$")
        cleaned: List[WordTS] = []
        for w in words:
            text = w.text or ""
            if pattern.match(text):
                continue
            cleaned.append(
                WordTS(text=text, startS=w.startS, endS=w.endS, i=w.i))
        return cleaned or words

    # ---- downloading ----

    async def download(self, track: ProviderCTrack, dest_path: Path) -> Path:
        """
        Download a single audio file.

        Args:
            track: ProviderCTrack with audio_url.
            dest_path: Destination file path.
        Returns:
            Path object of the saved file.
        """
        url = track.audio_url
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            async with self.client.stream("GET", url, timeout=120) as r:
                r.raise_for_status()
                with open(dest_path, "wb") as f:
                    async for chunk in r.aiter_bytes(chunk_size=1024 * 256):
                        if chunk:
                            f.write(chunk)
        except Exception as exc:
            raise self._map_httpx_exception(
                exc,
                operation="download",
                path=url,
                context={"audio_id": track.audio_id, "destination": str(dest_path)},
            ) from exc
        return dest_path

    async def generate_track_lyrics_audio_parallel(
            self,
            task_id: str,
            dest_path: Path,
            *,
            timeout_s: float = 180.0,
            poll_s: float = 30,
            max_poll_s: Optional[float] = None,
            download_timeout_s: float = 100.0,
            lyrics_timeout_s: float = 100.0,
            initial_poll_delay_s: float = 30.0,
    ) -> tuple[ProviderCTrack, List[WordTS], Path]:
        """
        Step-by-step flow: delay before polling, wait for first track, download,
        then fetch lyrics.

        Args:
            task_id: Generation task ID
            dest_path: Where to save the audio file
            timeout_s: Max wait time for track generation
            poll_s: Polling interval (uniform)
            max_poll_s: Max polling interval (backoff in poll_generation_first_track)
            download_timeout_s: Timeout for audio download
            lyrics_timeout_s: Max seconds to wait for timestamped lyrics
            initial_poll_delay_s: Delay before polling starts

        Returns:
            Tuple of (track, lyrics_or_empty, downloaded_file_path)

        Raises:
            TimeoutError: If track polling or download exceeds timeout
            RuntimeError: If generation fails or download fails
        """
        deadline = time.monotonic() + timeout_s
        poll_s = max(0.1, float(poll_s))

        if initial_poll_delay_s > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EdennProviderTimeoutError(
                    f"Timed out waiting for ProviderC task {task_id}",
                    provider_name="provider_c",
                    operation="generate_track_lyrics_audio_parallel",
                    retryable=True,
                    context={"task_id": task_id},
                )
            await asyncio.sleep(min(initial_poll_delay_s, remaining))

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EdennProviderTimeoutError(
                f"Timed out waiting for ProviderC task {task_id}",
                provider_name="provider_c",
                operation="generate_track_lyrics_audio_parallel",
                retryable=True,
                context={"task_id": task_id},
            )

        first_track = await self.poll_generation_first_track(
            task_id,
            timeout_s=remaining,
            poll_s=poll_s,
            max_poll_s=max_poll_s,
        )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EdennProviderTimeoutError(
                f"Timed out waiting for ProviderC task {task_id}",
                provider_name="provider_c",
                operation="generate_track_lyrics_audio_parallel",
                retryable=True,
                context={"task_id": task_id},
            )

        download_timeout = min(download_timeout_s, remaining)
        try:
            downloaded_path = await asyncio.wait_for(
                self.download(first_track, dest_path),
                timeout=download_timeout,
            )
        except TimeoutError as exc:
            raise EdennProviderTimeoutError(
                f"ProviderC audio download timed out for task {task_id}",
                provider_name="provider_c",
                operation="generate_track_lyrics_audio_parallel",
                retryable=True,
                context={"task_id": task_id, "audio_id": first_track.audio_id},
                cause=exc,
            ) from exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EdennProviderTimeoutError(
                f"Timed out waiting for ProviderC task {task_id}",
                provider_name="provider_c",
                operation="generate_track_lyrics_audio_parallel",
                retryable=True,
                context={"task_id": task_id},
            )

        lyrics_timeout = min(lyrics_timeout_s, remaining)
        try:
            lyrics_result = await asyncio.wait_for(
                self.get_timestamped_lyrics(task_id, first_track.audio_id),
                timeout=lyrics_timeout,
            )
        except TimeoutError as exc:
            raise EdennProviderTimeoutError(
                f"ProviderC timestamped lyrics timed out for task {task_id}",
                provider_name="provider_c",
                operation="generate_track_lyrics_audio_parallel",
                retryable=True,
                context={"task_id": task_id, "audio_id": first_track.audio_id},
                cause=exc,
            ) from exc

        return first_track, lyrics_result, downloaded_path

    async def download_tracks(
            self,
            tracks: Iterable[ProviderCTrack],
            dest_dir: str | Path
    ) -> List[Path]:
        """
        Download multiple tracks, auto-naming each file by audio_id.

        Args:
            tracks: Iterable of ProviderCTrack objects.
            dest_dir: Directory to place files.
        Returns:
            List of saved file Paths.
        """
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        saved: List[Path] = []

        for idx, track in enumerate(tracks):
            # Use audio_id as filename, fallback to index if missing
            filename = track.audio_id if track.audio_id else f"track_{idx}"
            path = dest_dir / f"{filename}.wav"
            saved.append(await self.download(track, path))

        return saved


if __name__ == "__main__":
    async def test():
        async with ProviderCApi() as provider_c:
            lyrics = await provider_c.generate_lyrics(prompt="pop song about coding")
            print(f"Generated lyrics: {lyrics}")

    asyncio.run(test())
