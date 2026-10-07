from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

import httpx

from EdennCode.ModelFactory.VideoSFXModelFactory.VideoConditionedGen.base import (
    VideoConditionedSfxProvider,
)
from EdennCode.Util.MediaUtils.ffmpeg_utils import resolve_ffmpeg_binary

logger = logging.getLogger(__name__)

# Verified via preflight 2026-08-18: 10 credits per second of audio per sample.
DEFAULT_ENDPOINT = "https://api.provider-e.example.invalid/v2/video-to-sfx/v1.6"
MAX_SINGLE_CALL_S = 60.0  # native generation cap per the vendor's launch notes


class ProviderEVideoSfxProvider(VideoConditionedSfxProvider):
    """First-party adapter for the windowed video→SFX endpoint (sync mode)."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        endpoint: Optional[str] = None,
        timeout: int = 180,
    ) -> None:
        super().__init__(timeout=timeout)
        self.api_key = (api_key or os.getenv("PROVIDER_E_API_KEY", "")).strip()
        self.endpoint = (endpoint or os.getenv("PROVIDER_E_SFX_ENDPOINT", "") or DEFAULT_ENDPOINT).rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def generate_for_video(
        self,
        *,
        video_url: str,
        output_stem: str,
        output_dir: Path,
        start_offset_s: float = 0.0,
        duration_s: Optional[float] = None,
        prompt: Optional[str] = None,
        num_samples: int = 1,
        sample_rate: int = 44100,
    ) -> list[Path]:
        if not self.configured:
            raise RuntimeError("video-conditioned SFX provider key is not configured")
        output_dir.mkdir(parents=True, exist_ok=True)
        duration_s = min(float(duration_s or MAX_SINGLE_CALL_S), MAX_SINGLE_CALL_S)
        payload = {
            "video": {"type": "url", "video_url": video_url},
            "duration_ms": max(1000, int(round(duration_s * 1000))),
            "start_offset_ms": max(0, int(round(start_offset_s * 1000))),
            "num_samples": max(1, min(4, num_samples)),
            "output_format": "wav",
        }
        if prompt and prompt.strip():
            payload["prompt"] = prompt.strip()

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.endpoint}/sync", headers=headers, json=payload)
            response.raise_for_status()
            result_urls = response.json().get("result_urls") or []
            paths: list[Path] = []
            for idx, url in enumerate(result_urls):
                raw = output_dir / f"{output_stem}_v{idx}_raw.wav"
                download = await client.get(url)
                download.raise_for_status()
                raw.write_bytes(download.content)
                paths.append(
                    _normalize_wav(raw, output_dir / f"{output_stem}_v{idx}.wav", sample_rate)
                )
        if not paths:
            raise RuntimeError("video-conditioned SFX provider returned no results")
        return paths


def _normalize_wav(src: Path, dst: Path, sample_rate: int) -> Path:
    subprocess.run(
        [resolve_ffmpeg_binary(), "-y", "-i", str(src), "-ac", "1", "-ar", str(sample_rate), str(dst)],
        check=True,
        capture_output=True,
    )
    src.unlink(missing_ok=True)
    return dst


def build_video_conditioned_provider() -> Optional[ProviderEVideoSfxProvider]:
    """Returns the configured provider, or None when no key is present."""
    provider = ProviderEVideoSfxProvider()
    return provider if provider.configured else None
