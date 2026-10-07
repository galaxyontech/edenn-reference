from __future__ import annotations
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.base import MusicProvider
from pathlib import Path
import sys

from typing import Dict, Any, Optional, Union
import time

import logging
import httpx
import asyncio
import base64
import json

from EdennCode.Util.MediaUtils.pipeline_util import hash_str

logger = logging.getLogger(__name__)


sys.path.append(str(Path(__file__).resolve().parents[2]))


class ProviderAMusicProvider(MusicProvider):
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
        api_key: str,
        endpoint: Optional[str] = None,
        use_stream: bool = False,
        default_output_dir: Union[str, Path] = ".",
        timeout: int = 30,
    ) -> None:
        super().__init__(timeout=timeout)
        self.use_stream = use_stream

        # Default endpoints per ProviderA docs
        # - Compose: POST https://api.provider-a.example.invalid/v1/music
        # - Stream:  POST https://api.provider-a.example.invalid/v1/music/stream
        if endpoint is None:
            if use_stream:
                endpoint = "https://api.provider-a.example.invalid/v1/music/stream"
            else:
                endpoint = "https://api.provider-a.example.invalid/v1/music"

        self.endpoint = endpoint
        self.headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json",
        }
        self.default_output_dir = Path(default_output_dir)

    async def generate(
            self,
            prompt: str,
            *,
            music_length_ms: int = 10_000,
            extra: Optional[Dict[str, Any]] = None,
            output_path: Optional[Path] = None,
    ) -> str:
        start_time = time.perf_counter()

        payload: Dict[str, Any] = {
            "prompt": prompt,
            "music_length_ms": music_length_ms,
            "with_timestamps": True,
        }
        if extra:
            payload.update(extra)

        self.default_output_dir.mkdir(parents=True, exist_ok=True)
        if output_path is None:
            timestamp = int(time.time())
            hash_256 = hash_str()
            output_path = self.default_output_dir / \
                f"eleven_music_{timestamp}_{hash_256}.wav"
        else:
            output_path = Path(output_path)

        headers = dict(self.headers)
        if not self.use_stream:
            headers["Accept"] = "application/json"
        try:
            async with asyncio.timeout(self.timeout):
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    async with client.stream(
                            "POST",
                            self.endpoint,
                            headers=headers,
                            json=payload,
                    ) as resp:
                        if resp.status_code != 200:
                            body = await resp.aread()
                            raise RuntimeError(
                                f"ProviderA {resp.status_code}: {body.decode('utf-8', 'ignore')}")

                        resp.raise_for_status()
                        content_type = resp.headers.get("content-type", "")
                        if "application/json" in content_type:
                            body = await resp.aread()
                            data = json.loads(body.decode("utf-8"))
                            metadata_path = output_path.with_suffix(".json")
                            metadata_path.write_text(json.dumps(data, indent=2))
                            audio_bytes = self._extract_audio_bytes(data)
                            audio_url = self._extract_audio_url(data)
                            if audio_bytes:
                                output_path.write_bytes(audio_bytes)
                            elif audio_url:
                                await self._download_audio_url(client, audio_url, output_path)
                            else:
                                raise RuntimeError("ProviderA compose response missing audio payload.")
                        else:
                            with open(output_path, "wb") as f:
                                async for chunk in resp.aiter_bytes(chunk_size=8192):
                                    if chunk:
                                        f.write(chunk)
        except TimeoutError as err:
            raise TimeoutError(
                f"ProviderA music generation exceeded {self.timeout}s. Increase PROVIDER_A_TIMEOUT if needed."
            ) from err

        elapsed = time.perf_counter() - start_time
        logger.info(f"ProviderA music generation took {elapsed:.2f}s")
        return str(output_path)

    async def generate_from_plan(
        self,
        composition_plan: Dict[str, Any],
        *,
        output_path: Optional[Path] = None,
    ) -> str:
        payload = {
            "composition_plan": composition_plan,
            "model_id": "music_v1",
            "respect_sections_durations": True,
        }

        if output_path is None:
            output_path = self.default_output_dir / \
                f"eleven_plan_{int(time.time())}.mp3"
        self.default_output_dir.mkdir(parents=True, exist_ok=True)
        headers = dict(self.headers)
        if not self.use_stream:
            headers["Accept"] = "application/json"
        try:
            async with asyncio.timeout(self.timeout):
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    async with client.stream(
                        "POST",
                        self.endpoint,
                        headers=headers,
                        json=payload,
                    ) as resp:
                        resp.raise_for_status()
                        content_type = resp.headers.get("content-type", "")
                        if "application/json" in content_type:
                            body = await resp.aread()
                            data = json.loads(body.decode("utf-8"))
                            metadata_path = output_path.with_suffix(".json")
                            metadata_path.write_text(json.dumps(data, indent=2))
                            audio_bytes = self._extract_audio_bytes(data)
                            audio_url = self._extract_audio_url(data)
                            if audio_bytes:
                                output_path.write_bytes(audio_bytes)
                            elif audio_url:
                                await self._download_audio_url(client, audio_url, output_path)
                            else:
                                raise RuntimeError("ProviderA compose response missing audio payload.")
                        else:
                            with open(output_path, "wb") as f:
                                async for chunk in resp.aiter_bytes(8192):
                                    f.write(chunk)
        except TimeoutError as err:
            raise TimeoutError(
                f"ProviderA music generation exceeded {self.timeout}s. Increase PROVIDER_A_TIMEOUT if needed."
            ) from err

        return str(output_path)

    @staticmethod
    def _extract_audio_bytes(data: Dict[str, Any]) -> Optional[bytes]:
        for key in ("audio", "audio_data", "audioBase64", "audio_base64"):
            value = data.get(key)
            if isinstance(value, str) and value:
                try:
                    return base64.b64decode(value)
                except Exception:
                    return None
        return None

    @staticmethod
    def _extract_audio_url(data: Dict[str, Any]) -> Optional[str]:
        for key in ("audio_url", "audioUrl", "audioURL", "audio_link"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    async def _download_audio_url(client: httpx.AsyncClient, audio_url: str, output_path: Path) -> None:
        async with client.stream("GET", audio_url) as resp:
            resp.raise_for_status()
            with open(output_path, "wb") as f:
                async for chunk in resp.aiter_bytes(8192):
                    if chunk:
                        f.write(chunk)
