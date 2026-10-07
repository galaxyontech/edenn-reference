from __future__ import annotations

from abc import ABC, abstractmethod


class MusicProvider(ABC):
    """
    Base interface for music generation providers.
    """

    def __init__(self, *, timeout: int = 30) -> None:
        self.timeout = timeout

    @abstractmethod
    async def generate(self, prompt: str) -> str:
        """
        Generate music (or return an audio URL/ID) for the given prompt.
        """
        raise NotImplementedError
