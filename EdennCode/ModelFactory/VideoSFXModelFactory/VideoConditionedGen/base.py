from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


class VideoConditionedSfxProvider(ABC):
    """
    Engines that watch the pixels: real uploaded video in, synchronized audio
    out. Complements the text-conditioned ``SoundEffectProvider`` — routing
    between the two is the generation stage's job, never a caller's.
    """

    def __init__(self, *, timeout: int = 180) -> None:
        self.timeout = timeout

    @abstractmethod
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
        """Generate audio conditioned on ``video_url``; returns one WAV per sample."""
        raise NotImplementedError
