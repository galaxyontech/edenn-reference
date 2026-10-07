from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class SoundEffectProvider(ABC):
    """
    Base interface for sound-effect generation providers.
    """

    def __init__(self, *, timeout: int = 60) -> None:
        self.timeout = timeout

    @abstractmethod
    async def generate(
        self,
        *,
        prompt: str,
        duration_seconds: float,
        output_stem: str,
        output_dir: Path,
        sample_rate: int = 44100,
    ) -> Path:
        raise NotImplementedError
