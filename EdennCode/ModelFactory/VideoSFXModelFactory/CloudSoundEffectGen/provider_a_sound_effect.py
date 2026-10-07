from __future__ import annotations

import base64
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from EdennCode.ModelFactory.VideoSFXModelFactory.CloudSoundEffectGen.base import (
    SoundEffectProvider,
)
from EdennCode.Util.MediaUtils.ffmpeg_utils import resolve_ffmpeg_binary

logger = logging.getLogger(__name__)


class ProviderASoundEffectProvider(SoundEffectProvider):
    """
    Dedicated ProviderA text-to-sound-effect provider.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        endpoint: Optional[str] = None,
        model_id: Optional[str] = None,
        output_format: Optional[str] = None,
        timeout: int = 60,
    ) -> None:
        super().__init__(timeout=timeout)
        self.api_key = (api_key or os.getenv("PROVIDER_A_API_KEY", "")).strip()

        self.endpoint = (
            endpoint
            or os.getenv("PROVIDER_A_SFX_ENDPOINT", "")
            or "https://api.provider-a.example.invalid/v1/sound-generation"
        )
        self.model_id = (
            model_id
            or os.getenv("PROVIDER_A_SFX_MODEL_ID", "")
            or "eleven_text_to_sound_v2"
        )
        self.output_format = (output_format or os.getenv("PROVIDER_A_SFX_OUTPUT_FORMAT", "")).strip() or None

    async def generate(
        self,
        *,
        prompt: str,
        duration_seconds: float,
        output_stem: str,
        output_dir: Path,
        sample_rate: int = 44100,
        loop: bool = False,
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        # The sound-generation endpoint accepts 0.5–30s; below 0.5 it returns
        # 400. Longer event windows are loop-filled at render time
        # (see rendering.build_timeline_clips).
        clipped_duration = max(0.5, min(float(duration_seconds), 30.0))
        cleaned_prompt = prompt
        payload: Dict[str, Any] = {
            "text": cleaned_prompt,
            "duration_seconds": clipped_duration,
            "model_id": self.model_id,
        }
        if loop:
            payload["loop"] = True
        params: Dict[str, Any] = {}
        if self.output_format:
            params["output_format"] = self.output_format

        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "*/*",
        }

        raw_ext = ".mp3"
        raw_bytes = b""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                self.endpoint,
                headers=headers,
                params=params or None,
                json=payload,
            )
            resp.raise_for_status()

            content_type = (resp.headers.get("content-type") or "").lower()
            if "application/json" in content_type:
                body = resp.json()
                raw_bytes = await self._extract_audio_bytes(client, body)
                raw_ext = self._detect_audio_ext(body)
            else:
                raw_bytes = resp.content
                if "wav" in content_type:
                    raw_ext = ".wav"

        raw_path = output_dir / f"{output_stem}{raw_ext}"
        raw_path.write_bytes(raw_bytes)

        if raw_ext == ".wav":
            converted = self._convert_wav_sample_rate(
                raw_path=raw_path,
                output_path=output_dir / f"{output_stem}_44k.wav",
                sample_rate=sample_rate,
            )
            return converted

        return self._convert_to_wav(
            src_path=raw_path,
            output_path=output_dir / f"{output_stem}.wav",
            sample_rate=sample_rate,
        )

    async def _extract_audio_bytes(self, client: httpx.AsyncClient, body: Dict[str, Any]) -> bytes:
        for key in ("audio", "audio_base64", "audioBase64", "audio_data"):
            value = body.get(key)
            if isinstance(value, str) and value:
                try:
                    return base64.b64decode(value)
                except Exception:
                    continue

        for key in ("audio_url", "audioUrl", "audioURL", "audio_link"):
            value = body.get(key)
            if isinstance(value, str) and value:
                dl = await client.get(value, timeout=self.timeout)
                dl.raise_for_status()
                return dl.content

        return b""

    @staticmethod
    def _detect_audio_ext(body: Dict[str, Any]) -> str:
        file_ext = body.get("file_extension")
        if isinstance(file_ext, str) and file_ext.strip():
            ext = file_ext.strip().lower()
            if not ext.startswith("."):
                ext = "." + ext
            return ext
        mime = body.get("mime_type")
        if isinstance(mime, str) and "wav" in mime.lower():
            return ".wav"
        return ".mp3"

    @staticmethod
    def _convert_to_wav(*, src_path: Path, output_path: Path, sample_rate: int) -> Path:
        ffmpeg_bin = resolve_ffmpeg_binary()
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i",
            str(src_path),
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            str(output_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return output_path

    @staticmethod
    def _convert_wav_sample_rate(*, raw_path: Path, output_path: Path, sample_rate: int) -> Path:
        ffmpeg_bin = resolve_ffmpeg_binary()
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i",
            str(raw_path),
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            str(output_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return output_path


if __name__ == "__main__":
    import asyncio
    from pathlib import Path

    async def main():
        provider = ProviderASoundEffectProvider()  # uses PROVIDER_A_API_KEY from env
        wav_path = await provider.generate(
            prompt="short cinematic whoosh transition",
            duration_seconds=1.2,
            output_stem="whoosh_01",
            output_dir=Path("outputs/sfx"),
            sample_rate=44100,
        )
        print("Generated:", wav_path)


    asyncio.run(main())
