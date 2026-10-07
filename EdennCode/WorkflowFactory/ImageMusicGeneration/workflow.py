from __future__ import annotations

import asyncio
import base64
import mimetypes
import time
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple

from EdennCode.ModelFactory.LanguageModelFactory.azure_based_model_model_gateway import AzureMultimodalClient
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.base import MusicProvider
from EdennCode.ModelFactory.PromptFactory.prompts import PromptBuilder, ResponseSchemas

from EdennCode.ModelFactory.PromptFactory.prompts import PromptBuilder, ResponseSchemas

logger = logging.getLogger(__name__)

@dataclass
class MusicGenResult:
    description: str
    music_prompt: str
    audio_url: str
    rationale: str


class ImageMusicWorkflow:
    """
    Image-to-music workflow that asks Azure for a description/prompt and delegates
    music generation to the configured provider.
    """

    def __init__(
        self,
        *,
        azure_client: AzureMultimodalClient,
        music_provider: MusicProvider,
    ) -> None:
        self.azure = azure_client
        self.music_provider = music_provider

    def run(self, image_path: Path) -> MusicGenResult:
        """
        Synchronous entry point for scripts/CLIs.
        """
        return asyncio.run(self.run_async(image_path))

    async def run_async(self, image_path: Path) -> MusicGenResult:
        """
        Async-capable version that can be awaited inside existing event loops.
        """
        start_time = time.perf_counter()
        logger.info(f"Starting ImageMusicWorkflow for {image_path}")

        normalized_path = image_path.expanduser()
        if not normalized_path.exists():
            raise FileNotFoundError(f"Image not found: {normalized_path}")

        mime_type, image_b64 = self._encode_image(normalized_path)
        plan = await self._plan_music(image_b64=image_b64, mime_type=mime_type)
        if not plan:
            raise RuntimeError("Azure returned an empty plan for the provided image.")

        description = plan.get("image_summary") or f"Visual inspired by {normalized_path.stem}"
        music_prompt = plan.get("music_prompt")
        if not music_prompt:
            raise RuntimeError("Azure response missing 'music_prompt'.")

        if not music_prompt:
            raise RuntimeError("Azure response missing 'music_prompt'.")

        gen_start = time.perf_counter()
        audio_url = await self.music_provider.generate(music_prompt, music_length_ms=15_000)
        logger.info(f"Music generation took {time.perf_counter() - gen_start:.2f}s")

        rationale = self._build_rationale(description=description, plan=plan)
        
        total_duration = time.perf_counter() - start_time
        logger.info(f"ImageMusicWorkflow finished in {total_duration:.2f}s")

        return MusicGenResult(
            description=description,
            music_prompt=music_prompt,
            audio_url=audio_url,
            rationale=rationale,
        )

    @staticmethod
    def _encode_image(image_path: Path) -> Tuple[str, str]:
        data = image_path.read_bytes()
        image_b64 = base64.b64encode(data).decode("utf-8")
        mime_type, _ = mimetypes.guess_type(str(image_path))
        return (mime_type or "image/png", image_b64)

    async def _plan_music(self, *, image_b64: str, mime_type: str) -> Dict:
        start = time.perf_counter()
        messages = PromptBuilder.build_image_music_messages(image_b64=image_b64, mime_type=mime_type)
        schema = ResponseSchemas.image_music()
        res = await self.azure.complete_messages(messages, json_schema=schema)
        logger.info(f"Music planning (Azure) took {time.perf_counter() - start:.2f}s")
        return res


    @staticmethod
    def _build_rationale(*, description: str, plan: Dict) -> str:
        mood = plan.get("recommended_mood")
        tempo = plan.get("tempo_bpm")
        instruments = plan.get("instruments")

        extras = []
        if mood:
            extras.append(f"mood '{mood}'")
        if tempo:
            extras.append(f"{tempo} BPM")
        if instruments:
            extras.append(f"instruments: {', '.join(instruments)}")

        extras_str = f" ({'; '.join(extras)})" if extras else ""
        return f"Music chosen to amplify engagement for: {description}{extras_str}"
