from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

try:
    from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen import provider_c as default_provider_c_mod
except Exception:
    default_provider_c_mod = None


@dataclass
class TrackInfo:
    track_id: str
    stream_audio_url: Optional[str] = None
    audio_url: Optional[str] = None


@dataclass
class TaskInfo:
    task_id: str
    callback_type: str


@dataclass
class CallbackEvent:
    task: TaskInfo
    tracks: List[TrackInfo]
    raw: Dict[str, Any]

    @property
    def task_id(self) -> str:
        return self.task.task_id

    @property
    def callback_type(self) -> str:
        return self.task.callback_type


def _first_str(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _safe_token(value: str) -> str:
    return "".join(
        ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in (value or "unknown")
    )


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, payload: Any) -> None:
    _ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as file_obj:
        json.dump(payload, file_obj, ensure_ascii=True, indent=2)


def _now_tag() -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.localtime())


def _run_async(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("handle_callback_event cannot run inside an active event loop")


def _extract_tracks(payload: Dict[str, Any]) -> List[TrackInfo]:
    candidates: List[Any] = [
        payload.get("tracks"),
        payload.get("data"),
        (payload.get("response") or {}).get("provider_cData"),
        (payload.get("response") or {}).get("data"),
        (payload.get("response") or {}).get("tracks"),
    ]
    tracks: List[TrackInfo] = []
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
            if not isinstance(item, dict):
                continue
            track_id = _first_str(
                item.get("id"),
                item.get("audioId"),
                item.get("trackId"),
                item.get("audio_id"),
            ) or ""
            stream_audio_url = _first_str(
                item.get("streamAudioUrl"),
                item.get("stream_audio_url"),
                item.get("streamUrl"),
                item.get("stream_url"),
            )
            audio_url = _first_str(
                item.get("audioUrl"),
                item.get("audio_url"),
                item.get("url"),
            )
            tracks.append(
                TrackInfo(
                    track_id=track_id,
                    stream_audio_url=stream_audio_url,
                    audio_url=audio_url,
                )
            )
    return tracks


class ProviderCCallbackService:
    def __init__(
        self,
        *,
        base_dir: Path,
        logger: logging.Logger,
        provider_c_module: Any = default_provider_c_mod,
    ) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.logger = logger
        self.provider_c_mod = provider_c_module
        self.webhook_log_dir = self.base_dir / "webhook_logs"
        self.download_dir = self.base_dir / "downloads"
        self.lyrics_ts_dir = self.base_dir / "lyrics_ts"

    def parse_callback_payload(self, payload: dict) -> CallbackEvent:
        callback_type = _first_str(
            payload.get("callbackType"),
            payload.get("callback_type"),
            payload.get("type"),
        ) or "unknown"
        task_id = _first_str(
            payload.get("taskId"),
            payload.get("task_id"),
            (payload.get("data") or {}).get("taskId"),
            (payload.get("response") or {}).get("taskId"),
        ) or "unknown"
        tracks = _extract_tracks(payload)

        event = CallbackEvent(
            task=TaskInfo(task_id=task_id, callback_type=callback_type),
            tracks=tracks,
            raw=payload,
        )
        self._persist_payload(payload, callback_type, task_id)
        return event

    def handle_callback_event(
        self,
        event: CallbackEvent,
        *,
        download: bool = True,
        fetch_ts_lyrics: bool = True,
        download_retries: int = 3,
        retry_backoff_s: float = 1.5,
        timeout_s: float = 120.0,
    ) -> Dict[str, Any]:
        return _run_async(
            self._handle_callback_event_async(
                event,
                download=download,
                fetch_ts_lyrics=fetch_ts_lyrics,
                download_retries=download_retries,
                retry_backoff_s=retry_backoff_s,
                timeout_s=timeout_s,
            )
        )

    def run_post_analysis_hook(self, task_id: str, audio_paths: List[str]) -> None:
        self.logger.info(
            "Post analysis stub: task_id=%s, audio_paths=%s",
            task_id,
            audio_paths,
        )

    async def _handle_callback_event_async(
        self,
        event: CallbackEvent,
        *,
        download: bool,
        fetch_ts_lyrics: bool,
        download_retries: int,
        retry_backoff_s: float,
        timeout_s: float,
    ) -> Dict[str, Any]:
        _ensure_dir(self.download_dir)
        _ensure_dir(self.lyrics_ts_dir)

        errors: List[str] = []
        downloaded_files: List[str] = []
        ts_files: List[str] = []

        callback_type = event.callback_type.lower()
        text_only_types = {"text", "lyrics"}
        light_types = {"first"}

        provider_c_client = await self._maybe_create_provider_c_client()
        try:
            async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
                if callback_type in text_only_types:
                    try:
                        record = await self._fetch_lyrics_record(
                            event.task_id,
                            client,
                            timeout_s=timeout_s,
                        )
                        if record is not None:
                            out_path = self.lyrics_ts_dir / f"{_safe_token(event.task_id)}_lyrics.json"
                            _write_json(out_path, record)
                            ts_files.append(str(out_path))
                    except Exception as exc:
                        errors.append(f"lyrics_record_error:{exc}")
                    return self._summary(event, downloaded_files, ts_files, errors)

                if callback_type in light_types:
                    return self._summary(event, downloaded_files, ts_files, errors)

                tasks: List[asyncio.Task] = []
                for track in event.tracks:
                    if download and track.audio_url:
                        tasks.append(
                            asyncio.create_task(
                                self._download_track(
                                    track,
                                    task_id=event.task_id,
                                    provider_c_client=provider_c_client,
                                    client=client,
                                    download_retries=download_retries,
                                    retry_backoff_s=retry_backoff_s,
                                    timeout_s=timeout_s,
                                )
                            )
                        )
                    if fetch_ts_lyrics and track.track_id:
                        tasks.append(
                            asyncio.create_task(
                                self._fetch_ts_lyrics(
                                    task_id=event.task_id,
                                    track_id=track.track_id,
                                    provider_c_client=provider_c_client,
                                    client=client,
                                    timeout_s=timeout_s,
                                )
                            )
                        )

                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for result in results:
                        if isinstance(result, Exception):
                            errors.append(str(result))
                        elif isinstance(result, str):
                            if result.endswith(".mp3"):
                                downloaded_files.append(result)
                            elif result.endswith(".json"):
                                ts_files.append(result)

                return self._summary(event, downloaded_files, ts_files, errors)
        finally:
            await self._maybe_close_provider_c_client(provider_c_client)

    async def _download_track(
        self,
        track: TrackInfo,
        *,
        task_id: str,
        provider_c_client: Optional[Any],
        client: httpx.AsyncClient,
        download_retries: int,
        retry_backoff_s: float,
        timeout_s: float,
    ) -> Optional[str]:
        if not track.audio_url:
            return None

        dest_path = self.download_dir / f"{_safe_token(task_id)}_{track.track_id}.mp3"
        if dest_path.exists():
            return str(dest_path)

        last_error: Optional[Exception] = None
        for attempt in range(download_retries):
            try:
                if provider_c_client and hasattr(provider_c_client, "download") and self.provider_c_mod:
                    provider_c_track = self.provider_c_mod.ProviderCTrack(
                        audio_id=track.track_id or "",
                        audio_url=track.audio_url,
                    )
                    await provider_c_client.download(provider_c_track, dest_path)
                    return str(dest_path)

                async with client.stream("GET", track.audio_url, timeout=timeout_s) as response:
                    response.raise_for_status()
                    with dest_path.open("wb") as file_obj:
                        async for chunk in response.aiter_bytes(chunk_size=1024 * 256):
                            if chunk:
                                file_obj.write(chunk)
                return str(dest_path)
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(retry_backoff_s * (2 ** attempt))

        raise RuntimeError(f"download_failed:{track.track_id}:{last_error}")

    async def _fetch_ts_lyrics(
        self,
        *,
        task_id: str,
        track_id: str,
        provider_c_client: Optional[Any],
        client: httpx.AsyncClient,
        timeout_s: float,
    ) -> Optional[str]:
        out_path = self.lyrics_ts_dir / f"{_safe_token(task_id)}_{track_id}.json"
        if out_path.exists():
            return str(out_path)

        if provider_c_client and hasattr(provider_c_client, "get_timestamped_lyrics"):
            words = await provider_c_client.get_timestamped_lyrics(task_id, track_id)
            payload = {
                "task_id": task_id,
                "audio_id": track_id,
                "words": [word.__dict__ for word in words],
            }
            _write_json(out_path, payload)
            return str(out_path)

        candidates = self._candidate_api_keys()
        if not candidates:
            raise RuntimeError(
                "missing PROVIDER_C_API_KEY (or PROVIDER_C_API_KEY_1..N) for timestamped lyrics"
            )

        base = self._resolve_base_url()
        # The creating key is unknown in this process; task ids are
        # account-scoped, so try each configured key until one owns the task.
        # A non-owner key answers 200 with an empty record (live-verified), so
        # prefer a body that actually carries aligned words and keep the last
        # non-rejected body as fallback (legit empty, e.g. instrumental).
        last_exc: Optional[Exception] = None
        fallback_body: Optional[Any] = None
        for _label, api_key in candidates:
            try:
                response = await client.post(
                    f"{base}/generate/get-timestamped-lyrics",
                    json={"taskId": task_id, "audioId": track_id},
                    headers=self._auth_headers(api_key),
                    timeout=timeout_s,
                )
                response.raise_for_status()
                body = response.json()
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                continue
            if self._body_rejects_task(body) and len(candidates) > 1:
                continue
            data = body.get("data") if isinstance(body, dict) else None
            has_words = bool(isinstance(data, dict) and data.get("alignedWords"))
            if has_words or len(candidates) == 1:
                _write_json(out_path, body)
                return str(out_path)
            fallback_body = body
        if fallback_body is not None:
            _write_json(out_path, fallback_body)
            return str(out_path)
        if last_exc is not None:
            raise last_exc
        return None

    async def _fetch_lyrics_record(
        self,
        task_id: str,
        client: httpx.AsyncClient,
        *,
        timeout_s: float,
    ) -> Optional[Dict[str, Any]]:
        candidates = self._candidate_api_keys()
        if not candidates:
            return None

        base = self._resolve_base_url()
        # Try each configured key — the lyrics task belongs to exactly one.
        last_exc: Optional[Exception] = None
        for _label, api_key in candidates:
            try:
                response = await client.get(
                    f"{base}/lyrics/record-info",
                    params={"taskId": task_id},
                    headers=self._auth_headers(api_key),
                    timeout=timeout_s,
                )
                response.raise_for_status()
                body = response.json()
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                continue
            if self._body_rejects_task(body) and len(candidates) > 1:
                continue
            return body
        if last_exc is not None:
            raise last_exc
        return None

    async def _maybe_create_provider_c_client(self) -> Optional[Any]:
        if not self.provider_c_mod or not hasattr(self.provider_c_mod, "ProviderCApi"):
            return None
        try:
            return self.provider_c_mod.ProviderCApi()
        except Exception:
            return None

    async def _maybe_close_provider_c_client(self, provider_c_client: Optional[Any]) -> None:
        if provider_c_client and hasattr(provider_c_client, "aclose"):
            await provider_c_client.aclose()

    def _resolve_base_url(self) -> str:
        if self.provider_c_mod and hasattr(self.provider_c_mod, "BASE_URL"):
            return str(self.provider_c_mod.BASE_URL).rstrip("/")
        return os.getenv("PROVIDER_C_BASE_URL", "https://api.provider-c.example.invalid/api/v1").rstrip("/")

    def _candidate_api_keys(self) -> List[tuple]:
        """(label, value) ProviderC key candidates: numbered first, legacy last.

        Prefers the canonical parser in the provider_c module; falls back to an
        equivalent inline scan when the module is unavailable in this process.
        """
        if self.provider_c_mod is not None and hasattr(self.provider_c_mod, "provider_c_api_key_candidates_from_env"):
            return list(self.provider_c_mod.provider_c_api_key_candidates_from_env())
        numbered = []
        for name, raw in os.environ.items():
            if name.startswith("PROVIDER_C_API_KEY_") and name[len("PROVIDER_C_API_KEY_"):].isdigit():
                if raw.strip():
                    numbered.append((int(name[len("PROVIDER_C_API_KEY_"):]), name, raw.strip()))
        pairs = [(name, value) for _, name, value in sorted(numbered)]
        legacy = os.getenv("PROVIDER_C_API_KEY", "").strip()
        if legacy:
            pairs.append(("PROVIDER_C_API_KEY", legacy))
        return pairs

    @staticmethod
    def _body_rejects_task(body: Any) -> bool:
        """True when the API body says this key does not own the task.

        Live-verified 2026-08-17: a wrong key does NOT error — the API answers
        HTTP 200 / body code 200 with a null record. Treat both an explicit
        non-200 body code and an empty ``data`` as "not the owner".
        """
        if not isinstance(body, dict):
            return False
        code = body.get("code")
        if isinstance(code, int) and code != 200:
            return True
        return body.get("data") is None

    @staticmethod
    def _auth_headers(api_key: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _summary(
        event: CallbackEvent,
        downloaded_files: List[str],
        ts_files: List[str],
        errors: List[str],
    ) -> Dict[str, Any]:
        return {
            "task_id": event.task_id,
            "callback_type": event.callback_type,
            "downloaded_files": downloaded_files,
            "timestamped_lyrics_files": ts_files,
            "errors": errors,
        }

    def _persist_payload(self, payload: Dict[str, Any], callback_type: str, task_id: str) -> None:
        name = f"{_now_tag()}_{_safe_token(callback_type)}_{_safe_token(task_id)}.json"
        _write_json(self.webhook_log_dir / name, payload)


__all__ = [
    "CallbackEvent",
    "ProviderCCallbackService",
    "TaskInfo",
    "TrackInfo",
]
