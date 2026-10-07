from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.cloud_music_gen_util import (
    build_edenn_enhanced_music_provider,
)


@dataclass
class VocalCloneResult:
    source_audio_path: Path
    vocal_id: str


class VocalCloneOrchestrator:
    def __init__(self) -> None:
        self.music_provider = build_edenn_enhanced_music_provider()

    async def run(
        self,
        *,
        source_audio_path: Path,
    ) -> VocalCloneResult:
        vocal_id = await self.music_provider.clone_vocal(source_audio_path)
        return VocalCloneResult(
            source_audio_path=source_audio_path,
            vocal_id=vocal_id,
        )


__all__ = ["VocalCloneOrchestrator", "VocalCloneResult"]
