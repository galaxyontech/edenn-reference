from __future__ import annotations
from EdennCode.Util.MediaUtils.pipeline_util import hash_str
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.lyrics_processor import WordTS
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.base import MusicProvider

import binascii
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from EdennCode.exceptions import (
    EdennConfigurationError,
    EdennProviderError,
    EdennProviderResponseError,
)
from dotenv import load_dotenv

# Load env from standard locations plus optional env.env
load_dotenv()
_extra_env = Path.cwd() / "env.env"
if _extra_env.exists():
    load_dotenv(dotenv_path=_extra_env, override=False)


class ProviderDMusicProvider(MusicProvider):
    """Async client for ProviderD text+lyrics → music (music-2.5).

    API reference: https://platform.provider-d.example.invalid/docs/api-reference/music-generation

    Notes
    -----
    - ProviderD requires lyrics; prompt is optional for `music-2.5`.
    - Supports `output_format` = "url" (recommended; expires in ~24h) or "hex".
    - No word-level timestamps are returned; this provider returns an empty list for lyrics_timestamps.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        group_id: Optional[str] = None,
        endpoint: str = "https://api.provider-d.example.invalid/v1/music_generation",
        lyrics_endpoint: str = "https://api.provider-d.example.invalid/v1/lyrics_generation",
        model: str = "music-2.5",
        default_output_dir: Path | str = Path("outputs/audio/provider_d"),
        timeout: int = 120,
    ) -> None:
        super().__init__(timeout=timeout)

        self.api_key = (api_key or os.getenv("PROVIDER_D_API_KEY", "")).strip()
        if not self.api_key:
            raise EdennConfigurationError(
                "PROVIDER_D_API_KEY must be set",
                component="provider_d",
                operation="init",
            )

        self.group_id = (group_id or os.getenv("PROVIDER_D_GROUP_ID", "")).strip()

        self.endpoint = endpoint
        self.lyrics_endpoint = lyrics_endpoint
        self.model = model
        self.default_output_dir = Path(default_output_dir)
        self.logger = logging.getLogger(__name__)

    async def _post_with_auth(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        auth_candidates = [f"Bearer {self.api_key}", self.api_key]
        headers_base = {"Content-Type": "application/json"}
        if self.group_id:
            # Different tenants use different header names; send both for compatibility.
            headers_base["Group-Id"] = self.group_id
            headers_base["X-Group-Id"] = self.group_id

        body: Dict[str, Any] = {}
        last_error: Optional[str] = None
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for idx, auth_value in enumerate(auth_candidates):
                headers = dict(headers_base)
                headers["Authorization"] = auth_value
                resp = await client.post(endpoint, json=payload, headers=headers)
                try:
                    body = resp.json()
                except Exception:
                    body = {}

                if body.get("base_resp", {}).get("status_code") == 0:
                    return body

                status_msg = str(
                    body.get("base_resp", {}).get("status_msg", ""))
                last_error = status_msg or resp.text

                # Retry once with alternate authorization format when auth fails.
                is_auth_error = "login fail" in status_msg.lower(
                ) or "authorization" in status_msg.lower()
                if idx + 1 < len(auth_candidates) and is_auth_error:
                    self.logger.info(
                        "ProviderD auth retry with alternate Authorization header format.")
                    continue

                if resp.is_error:
                    resp.raise_for_status()
                break

        raise EdennProviderError(
            f"ProviderD API error: {body or last_error}",
            provider_name="provider_d",
            retryable=True,
        )

    async def generate_lyrics(
        self,
        *,
        prompt: str,
        mode: str = "g",
        title: str = "",
    ) -> Dict[str, str]:
        """
        Call ProviderD /v1/lyrics_generation.
        Exact request syntax follows docs:
        - mode: write_full_song | edit
        - prompt: lyrics-generation instruction
        - title: optional
        """
        payload: Dict[str, Any] = {
            "mode": mode,
            "prompt": prompt,
            "title": title or "",
        }
        request_start = time.time()
        body = await self._post_with_auth(self.lyrics_endpoint, payload)
        self.logger.info(
            "ProviderD lyrics_generation request took %.2fs", time.time() - request_start)

        return {
            "song_title": str(body.get("song_title") or ""),
            "style_tags": str(body.get("style_tags") or ""),
            "lyrics": str(body.get("lyrics") or ""),
        }

    async def generate(
        self,
        prompt: str,
        *,
        lyrics: str,
        audio_setting: Optional[Dict[str, Any]] = None,
        output_format: str = "url",
        save_lyrics: bool = True,
    ) -> Tuple[Path, List[WordTS]]:
        """Generate music and save to disk.

        Returns
        -------
        Path to local audio file, empty list (no timestamps supplied by API).
        """

        payload: Dict[str, Any] = {
            "model": self.model,
            "prompt": prompt or "",
            "lyrics": lyrics,
            "output_format": output_format,
        }

        if audio_setting:
            payload["audio_setting"] = audio_setting

        self.default_output_dir.mkdir(parents=True, exist_ok=True)
        fname_base = f"provider_d_music_{hash_str()}"
        out_path = self.default_output_dir / f"{fname_base}.mp3"

        request_start = time.time()
        body = await self._post_with_auth(self.endpoint, payload)
        self.logger.info("ProviderD API request took %.2fs",
                         time.time() - request_start)

        data = body.get("data") or {}
        audio_field = data.get("audio")
        if not audio_field:
            raise EdennProviderResponseError(
                "ProviderD response missing audio data",
                provider_name="provider_d",
            )

        if output_format == "url":
            # audio contains a temporary URL
            audio_url = audio_field
            download_start = time.time()
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    audio_resp = await client.get(audio_url)
                    audio_resp.raise_for_status()
                    out_path.write_bytes(audio_resp.content)
            except httpx.TimeoutException as exc:
                raise EdennProviderError(
                    f"ProviderD audio download timed out: {exc}",
                    provider_name="provider_d",
                    retryable=True,
                ) from exc
            except httpx.HTTPStatusError as exc:
                raise EdennProviderError(
                    f"ProviderD audio download failed: HTTP {exc.response.status_code}",
                    provider_name="provider_d",
                    retryable=True,
                ) from exc
            self.logger.info("ProviderD audio download took %.2fs",
                             time.time() - download_start)
        else:
            # hex-encoded audio bytes
            out_path.write_bytes(binascii.unhexlify(audio_field))

        if save_lyrics:
            # plain text sidecar for quick inspection
            out_path.with_suffix(".lyrics.txt").write_text(
                lyrics or "", encoding="utf-8")
            # minimal JSON metadata (exclude api_key)
            meta = {
                "prompt": prompt,
                "lyrics": lyrics,
                "model": self.model,
                "output_format": output_format,
                "audio_setting": audio_setting or {},
                "endpoint": self.endpoint,
            }
            out_path.with_suffix(".json").write_text(json.dumps(
                meta, ensure_ascii=False, indent=2), encoding="utf-8")

        self.logger.info("ProviderD generation completed: %s", out_path)
        return out_path, []


if __name__ == "__main__":
    import requests
    import json
    import os
    start_time = time.time()
    url = "https://api.provider-d.example.invalid/v1/music_generation"
    api_key = "REDACTED_API_KEY"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    payload = {
        "model": "music-2.5",
        "prompt": "Mandopop, Festive, Upbeat, Celebration, New Year",
        "lyrics": "[Intro]\n嘿！新年到！\n(新年快乐！)\n大家一起笑！\n(哈哈！)\n鞭炮声声响，锣鼓敲起来！\n一，二，三，四，一起嗨！\n\n[Verse 1]\n"
                  "旧的一年已经过去，烟花点亮夜空\n(点亮夜空)\n新的一年已经来临，充满希望和感动\n家家户户贴春联，红红火火多喜庆\n"
                  "(多喜庆)\n孩子们换上新衣裳，脸上洋溢着笑容\n街头巷尾人潮涌，热闹非凡真开心\n(真开心)\n暖暖的祝福在传递，"
                  "温暖了我的心\n空气中弥漫着年味，饺子和汤圆香\n(香喷喷)\n这个时刻属于我们，一起尽情地歌唱",
        "audio_setting": {
            "sample_rate": 44100,
            "bitrate": 256000,
            "format": "mp3"
        },
        "output_format": "url"
    }

    response = requests.post(url, headers=headers, json=payload)
    result = response.json()

    end_time = time.time()
    print(start_time, end_time - start_time)
    print(json.dumps(result, ensure_ascii=False, indent=2))

__all__ = ["ProviderDMusicProvider"]
