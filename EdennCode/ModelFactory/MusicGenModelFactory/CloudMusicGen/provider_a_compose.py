from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import lru_cache
import threading

from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.base import MusicProvider
from pathlib import Path
import sys

from typing import Dict, Any, Optional, Union, List, Tuple
import time

import logging
import httpx
import json
import struct

from EdennCode.exceptions import (
    EdennConfigurationError,
    EdennProviderAuthenticationError,
    EdennProviderError,
    EdennProviderRateLimitError,
    EdennProviderResponseError,
    EdennProviderTimeoutError,
)
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.lyrics_processor import WordTS
from EdennCode.Util.MediaUtils.pipeline_util import hash_str

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ProviderAApiKey:
    label: str
    value: str


def _pcm_to_wav(
    pcm_data: bytes,
    *,
    sample_rate: int = 44100,
    channels: int = 2,
    bits_per_sample: int = 16,
) -> bytes:
    """Wrap raw PCM bytes in a standard 44-byte WAV header."""
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(pcm_data),
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM format
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        len(pcm_data),
    )
    return header + pcm_data


sys.path.append(str(Path(__file__).resolve().parents[2]))


class ProviderALyrics(MusicProvider):
    """
    ProviderA Music generation provider.

    By default it hits the streaming endpoint:
      POST https://api.provider-a.example.invalid/v1/music/stream

    and saves the returned MP3 bytes to disk, returning the file path as a string.
    If use_stream=False, it instead calls:
      POST https://api.provider-a.example.invalid/v1/music

    which is closer to the official `provider_a.music.compose(...)` SDK call.
    """

    def __init__(
            self,
            *,
            api_key: str = "",
            api_keys: Optional[List[Tuple[str, str]]] = None,
            endpoint: Optional[str] = None,
            use_stream: bool = True,
            default_output_dir: Union[str, Path] = ".",
            timeout: int = 30,
            max_retries: int = 2,
            retry_backoff_s: float = 1.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.use_stream = use_stream

        # Default endpoints per ProviderA docs
        # We always use the detailed endpoint now as it supports lyrics/timestamps
        # - Detailed: POST https://api.provider-a.example.invalid/v1/music/detailed
        if endpoint is None:
            endpoint = "https://api.provider-a.example.invalid/v1/music/detailed"

        self.endpoint = endpoint
        self._api_keys = self._normalize_api_keys(
            api_key=api_key,
            api_keys=api_keys,
        )
        if not self._api_keys:
            raise EdennConfigurationError(
                "PROVIDER_A_API_KEY must be set",
                component="provider_a",
                operation="initialize",
            )
        if len(self._api_keys) > 1:
            logger.info(
                "ProviderA key pool initialised with %d keys",
                len(self._api_keys),
            )
        self._api_key_lock = threading.Lock()
        self._next_api_key_index = 0
        self.headers = self._headers_for_key(self._api_keys[0])
        self.default_output_dir = Path(default_output_dir)
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_s = max(0.2, float(retry_backoff_s))

    @staticmethod
    def _normalize_api_keys(
        *,
        api_key: str = "",
        api_keys: Optional[List[Tuple[str, str]]] = None,
    ) -> List[_ProviderAApiKey]:
        normalized: List[_ProviderAApiKey] = []
        seen_values: set[str] = set()
        raw_items = api_keys if api_keys is not None else []
        for label, raw_value in raw_items:
            value = str(raw_value or "").strip()
            if not value or value in seen_values:
                continue
            normalized.append(_ProviderAApiKey(str(label or "api_key"), value))
            seen_values.add(value)

        if api_keys is None:
            value = str(api_key or "").strip()
            if value:
                normalized.append(_ProviderAApiKey("constructor_api_key", value))

        return normalized

    @staticmethod
    def _headers_for_key(api_key: _ProviderAApiKey) -> Dict[str, str]:
        return {
            "xi-api-key": api_key.value,
            "Content-Type": "application/json",
        }

    def _select_api_key(
        self,
        *,
        excluded_labels: Optional[set[str]] = None,
    ) -> _ProviderAApiKey:
        excluded = excluded_labels or set()
        with self._api_key_lock:
            for _ in range(len(self._api_keys)):
                key = self._api_keys[self._next_api_key_index]
                self._next_api_key_index = (
                    self._next_api_key_index + 1
                ) % len(self._api_keys)
                if key.label not in excluded:
                    return key
        raise EdennProviderRateLimitError(
            "All configured ProviderA API keys are currently at concurrency capacity",
            provider_name="provider_a",
            operation="select_api_key",
            retryable=True,
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
    def _classify_429(body_bytes: bytes) -> Tuple[bool, str]:
        """
        Parse ProviderA 429 body to decide whether to retry.

        Two documented sub-codes:
          - system_busy: platform congestion → retry with backoff
          - too_many_concurrent_requests: tier concurrency cap → DO NOT retry,
            queue or upgrade tier instead.

        Returns (retryable, sub_status). Unknown sub-codes default to retryable=True
        so we don't fail-fast on a new variant.
        """
        try:
            parsed = json.loads(body_bytes.decode("utf-8", "ignore"))
        except (ValueError, UnicodeDecodeError):
            return True, ""
        detail = parsed.get("detail") if isinstance(parsed, dict) else None
        if isinstance(detail, dict):
            sub_status = str(detail.get("status") or "").lower()
        elif isinstance(detail, str):
            sub_status = detail.lower()
        else:
            sub_status = ""
        if sub_status == "too_many_concurrent_requests":
            return False, sub_status
        return True, sub_status

    async def generate(
            self,
            prompt: str,
            *,
            music_length_ms: int = 10_000,
            with_timestamps: bool = False,
            extra: Optional[Dict[str, Any]] = None,
            output_format: Optional[str] = None,
    ) -> Tuple[Path, List[WordTS]]:

        start_time = time.perf_counter()

        lyrics_body: List[WordTS] = []
        requested_format = (output_format or "").strip()
        params: Optional[Dict[str, Any]] = None
        if requested_format:
            params = {"output_format": requested_format}

        suffix = ".wav" if requested_format == "pcm_44100" else ".mp3"
        payload = self._build_payload(
            prompt=prompt,
            music_length_ms=music_length_ms,
            with_timestamps=with_timestamps,
            extra=extra,
        )

        self.default_output_dir.mkdir(parents=True, exist_ok=True)
        hash = hash_str()

        output_path = self.default_output_dir / f"eleven_music_{hash}{suffix}"

        # We always use the configured endpoint (defaulting to /detailed)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            base_request_kwargs: Dict[str, Any] = {
                "method": "POST",
                "url": self.endpoint,
                "json": payload,
            }
            if params:
                base_request_kwargs["params"] = params

            total_attempts = self.max_retries + len(self._api_keys)
            concurrency_limited_labels: set[str] = set()
            for attempt in range(total_attempts):
                retry_wait_s: Optional[float] = None
                selected_key = self._select_api_key(
                    excluded_labels=concurrency_limited_labels,
                )
                request_kwargs = {
                    **base_request_kwargs,
                    "headers": self._headers_for_key(selected_key),
                }
                try:
                    async with client.stream(**request_kwargs) as resp:
                        status_code = resp.status_code
                        if status_code >= 400:
                            body_bytes = await resp.aread()
                            sub_status = ""
                            is_retryable_status = False
                            if status_code == 429:
                                is_retryable_status, sub_status = self._classify_429(body_bytes)
                            elif status_code in {500, 502, 503, 504}:
                                is_retryable_status = True

                            if (
                                status_code == 429
                                and sub_status == "too_many_concurrent_requests"
                                and len(concurrency_limited_labels) < len(self._api_keys) - 1
                            ):
                                concurrency_limited_labels.add(selected_key.label)
                                logger.warning(
                                    "ProviderA key %s hit concurrency capacity; retrying with another configured key",
                                    selected_key.label,
                                )
                                continue

                            if is_retryable_status and attempt < total_attempts - 1:
                                retry_after = self._parse_retry_after_s(resp.headers.get("Retry-After"))
                                retry_wait_s = retry_after if retry_after is not None else self.retry_backoff_s * (2 ** attempt)
                                logger.warning(
                                    "ProviderA HTTP %d (%s) on attempt %d/%d, retrying in %.2fs",
                                    status_code, sub_status or "no-sub-code",
                                    attempt + 1, total_attempts, retry_wait_s,
                                )
                            else:
                                detail = body_bytes.decode("utf-8", "ignore").strip()
                                message = f"ProviderA request failed with HTTP {status_code}: {detail}"
                                err_kwargs = {
                                    "provider_name": "provider_a",
                                    "operation": "POST /v1/music/detailed",
                                    "status_code": status_code,
                                    "retryable": is_retryable_status,
                                }
                                if status_code in {401, 403}:
                                    raise EdennProviderAuthenticationError(message, **err_kwargs)
                                if status_code == 429:
                                    raise EdennProviderRateLimitError(message, **err_kwargs)
                                raise EdennProviderResponseError(message, **err_kwargs)
                        else:
                            content_type = resp.headers.get("content-type", "")
                            if "multipart/mixed" in content_type:
                                body = await resp.aread()
                                from email.parser import BytesParser
                                from email.policy import default

                                headers_bytes = f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
                                msg = BytesParser(policy=default).parsebytes(headers_bytes + body)

                                audio_found = False
                                for part in msg.iter_parts():
                                    part_ct = part.get_content_type()
                                    if part_ct in ("audio/mpeg", "audio/pcm", "audio/wav", "audio/x-wav", "application/octet-stream"):
                                        payload_data = part.get_payload(decode=True)
                                        if requested_format == "pcm_44100":
                                            payload_data = _pcm_to_wav(payload_data)
                                        with open(output_path, "wb") as f:
                                            f.write(payload_data)
                                        audio_found = True
                                    elif part_ct == "application/json":
                                        music_metadata_json = part.get_payload(decode=True)
                                        if music_metadata_json is None:
                                            continue
                                        music_metadata = json.loads(music_metadata_json)
                                        lists_of_lyrics_in_str = music_metadata['words_timestamps']
                                        if lists_of_lyrics_in_str:
                                            lyrics_body = []
                                            for line in lists_of_lyrics_in_str:
                                                try:
                                                    text = line.get("word", "")
                                                    start_ms = float(line.get("start_ms", 0))
                                                    end_ms = float(line.get("end_ms", 0))
                                                except Exception:
                                                    continue
                                                lyrics_body.append(WordTS(
                                                    startS=start_ms / 1000.0,
                                                    endS=end_ms / 1000.0,
                                                    text=text,
                                                ))

                                if not audio_found:
                                    logger.warning("Multipart response received but no audio part found.")
                            else:
                                chunks = []
                                async for chunk in resp.aiter_bytes(chunk_size=8192):
                                    if chunk:
                                        chunks.append(chunk)
                                raw = b"".join(chunks)
                                if requested_format == "pcm_44100":
                                    raw = _pcm_to_wav(raw)
                                with open(output_path, "wb") as f:
                                    f.write(raw)

                            elapsed = time.perf_counter() - start_time
                            logger.info(f"ProviderA music generation took {elapsed:.2f}s")
                            return output_path, lyrics_body
                except (httpx.TimeoutException, httpx.RequestError) as exc:
                    if attempt < total_attempts - 1:
                        wait_s = self.retry_backoff_s * (2 ** attempt)
                        logger.warning(
                            "ProviderA transport error, retrying in %.2fs (%d/%d): %s",
                            wait_s, attempt + 1, total_attempts, exc,
                        )
                        await asyncio.sleep(wait_s)
                        continue
                    if isinstance(exc, httpx.TimeoutException):
                        raise EdennProviderTimeoutError(
                            "ProviderA request timed out",
                            provider_name="provider_a",
                            operation="POST /v1/music/detailed",
                            retryable=True,
                            cause=exc,
                        ) from exc
                    raise EdennProviderError(
                        "ProviderA request failed before a response was returned",
                        provider_name="provider_a",
                        operation="POST /v1/music/detailed",
                        retryable=True,
                        cause=exc,
                    ) from exc

                if retry_wait_s is not None:
                    await asyncio.sleep(retry_wait_s)
                    continue

            raise EdennProviderError(
                "ProviderA request failed after retries",
                provider_name="provider_a",
                operation="POST /v1/music/detailed",
                retryable=False,
            )

    @staticmethod
    def _build_payload(
        *,
        prompt: str,
        music_length_ms: int,
        with_timestamps: bool,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"with_timestamps": with_timestamps}
        options = dict(extra or {})
        composition_plan = options.pop("composition_plan", None)
        respect_sections_durations = options.pop(
            "respect_sections_durations",
            None,
        )
        model_id = options.pop("model_id", None)
        seed = options.pop("seed", None)
        song_metadata = options.pop("song_metadata", None)

        if composition_plan is not None:
            payload["composition_plan"] = composition_plan
            if respect_sections_durations is not None:
                payload["respect_sections_durations"] = bool(
                    respect_sections_durations)
            if model_id is not None:
                payload["model_id"] = model_id
            if seed is not None:
                payload["seed"] = seed
            # song_metadata appears in the detailed response example, but is not
            # documented as a request field. Avoid sending it until verified.
            _ = song_metadata
        else:
            payload["prompt"] = prompt
            payload["music_length_ms"] = music_length_ms
            if song_metadata is not None:
                payload["song_metadata"] = song_metadata
            if options:
                payload.update(options)

        return payload
