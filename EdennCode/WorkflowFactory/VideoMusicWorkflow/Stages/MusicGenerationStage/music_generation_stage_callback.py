"""
Callback-based Music Generation Stage

Split version of provider_c_music_generation_workflow for callback flow.
Same logic as music_generation_stage.py but non-blocking.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx

from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_c import (
    ProviderCApi,
    GenerateParams,
    LyricsResponse,
)
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.lyrics_processor import WordTS
from EdennCode.exceptions import EdennProviderResponseError, EdennValidationError

logger = logging.getLogger("MusicGenerationCallback")


@dataclass
class CallbackStageInput:
    """Same as MusicGenerationStageInput but for callback flow."""
    prompt_metadata: Dict[str, str]
    include_vocals: bool = False
    provider_c_custom_mode: bool = False


@dataclass
class CallbackStageOutput:
    """Same as provider_c_music_generation_workflow output."""
    music_path: Path
    timestamp_lyrics: List[WordTS]


class MusicGenerationStageCallback:
    """
    Callback version of provider_c_music_generation_workflow.

    Instead of:
        audio_path, timestamps = await stage.provider_c_music_generation_workflow(...)

    You do:
        task_id = await stage.start_provider_c_generation(...)
        # ... wait for callback ...
        audio_path, timestamps = await stage.process_provider_c_result(task_id, tracks_from_callback)
    """

    def __init__(self, provider_c_api: Optional[ProviderCApi] = None):
        self.provider_c_api = provider_c_api or ProviderCApi()
        self._pending_tasks: Dict[str, CallbackStageInput] = {}

    async def start_provider_c_generation(
        self,
        input_data: CallbackStageInput,
    ) -> str:
        """
        Start generation. Returns task_id immediately.

        This is the first part of provider_c_music_generation_workflow - triggering the API.
        """
        prompt_metadata = input_data.prompt_metadata
        provider_c_custom_mode = input_data.provider_c_custom_mode
        include_vocals = input_data.include_vocals

        # Same logic as original stage
        if provider_c_custom_mode and include_vocals:
            lyrics_prompt = prompt_metadata.get("lyrics_prompt")
            style_prompt = prompt_metadata.get("style_prompt")

            if not lyrics_prompt:
                raise EdennValidationError("lyrics_prompt required for custom mode", component="provider_c_callback", operation="validate_params")
            if not style_prompt:
                raise EdennValidationError("style_prompt required for custom mode", component="provider_c_callback", operation="validate_params")

            # Generate lyrics (still blocking here - could be split too)
            lyrics_response = await self.provider_c_api.generate_lyrics(
                prompt=lyrics_prompt,
                timeout_s=60.0,
            )
            lyrics = ""
            title = ""
            if isinstance(lyrics_response, str):
                lyrics = lyrics_response.strip()
            elif isinstance(lyrics_response, dict):
                lyrics_payload = LyricsResponse.from_payload(lyrics_response)
                primary = lyrics_payload.primary
                lyrics = (primary.text if primary else "").strip()
                title = (primary.title if primary else "").strip()

            if not lyrics:
                raise EdennProviderResponseError("ProviderC lyrics generation returned empty text.", provider_name="provider_c")

            if not title:
                title = (lyrics_prompt[:80]).strip() or "Untitled"
            if len(title) > 80:
                title = title[:80].rstrip()

            style = style_prompt.strip()
            if not style:
                raise EdennValidationError("ProviderC custom mode requires a non-empty style_prompt.", component="provider_c_callback", operation="validate_style_prompt")
            if len(style) > 980:
                style = style[:980].rstrip()

            gen_params = GenerateParams(
                prompt=lyrics,
                custom_mode=True,
                instrumental=not include_vocals,
                title=title,
                style=style,
                callback_url=os.getenv("PROVIDER_C_CALLBACK_URL"),
            )
        else:
            prompt = ""
            if isinstance(prompt_metadata, dict):
                prompt = (
                    prompt_metadata.get("prompt")
                    or prompt_metadata.get("style_prompt")
                    or ""
                ).strip()

            prompt = prompt[:500] + " Do not generate full song, less than 1 minutes"

            gen_params = GenerateParams(
                prompt=prompt,
                custom_mode=False,
                instrumental=not include_vocals,
                callback_url=os.getenv("PROVIDER_C_CALLBACK_URL"),
            )

        # Trigger generation (non-blocking)
        task_id = await self.provider_c_api.generate(gen_params)

        # Track for later
        self._pending_tasks[task_id] = input_data

        logger.info(f"Started ProviderC generation: {task_id}")
        return task_id

    async def process_provider_c_result(
        self,
        task_id: str,
        tracks_data: List[Dict],  # Parsed from callback payload
    ) -> Tuple[Path, List[WordTS]]:
        """
        Process completed generation.

        This is the second part of provider_c_music_generation_workflow - download + timestamps.
        Call this when the callback arrives with track data.

        Returns: (audio_path, timestamp_lyrics) - same as original workflow
        """
        if not tracks_data:
            raise EdennProviderResponseError("No tracks in callback", provider_name="provider_c")

        # Use first track (same as original)
        track = tracks_data[0]
        audio_id = (
            track.get("id")
            or track.get("audioId")
            or track.get("trackId")
            or track.get("audio_id")
        )
        audio_url = (
            track.get("audioUrl")
            or track.get("audio_url")
            or track.get("streamAudioUrl")
            or track.get("stream_audio_url")
        )

        if not audio_id or not audio_url:
            raise EdennValidationError("Track missing audio_id or audio_url", component="provider_c_callback", operation="validate_track")

        # Download audio (same as original)
        dest_dir = Path(os.getenv("OUTPUT_AUDIO_DIR", "outputs/audio"))
        dest_dir.mkdir(parents=True, exist_ok=True)
        # Hash the upstream id so the on-disk cache key stays per-track without leaking the provider's id format.
        track_key = hashlib.sha1(audio_id.encode("utf-8")).hexdigest()[:16]
        dest_path = dest_dir / f"track_{track_key}.mp3"

        if not dest_path.exists():
            async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                async with client.stream("GET", audio_url) as resp:
                    resp.raise_for_status()
                    with open(dest_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(256 * 1024):
                            if chunk:
                                f.write(chunk)

        pending_input = self._pending_tasks.get(task_id)
        if pending_input is not None and not pending_input.include_vocals:
            timestamp_lyrics_raw = []
        else:
            # Get timestamped lyrics (same as original)
            try:
                timestamp_lyrics_raw = await self.provider_c_api.get_timestamped_lyrics(task_id, audio_id)
            except Exception as e:
                logger.warning(f"Failed to get timestamps: {e}")
                timestamp_lyrics_raw = []

        # Convert to WordTS (same as original)
        timestamp_lyrics = [
            WordTS(text=w.text, startS=w.startS, endS=w.endS, i=w.i)
            for w in timestamp_lyrics_raw
        ]

        # Cleanup
        self._pending_tasks.pop(task_id, None)

        logger.info(f"Completed: {dest_path}, {len(timestamp_lyrics)} timestamps")
        return dest_path, timestamp_lyrics
