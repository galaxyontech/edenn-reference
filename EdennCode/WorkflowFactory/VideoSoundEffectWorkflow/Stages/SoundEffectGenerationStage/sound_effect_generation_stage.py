from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from EdennCode.ModelFactory.VideoSFXModelFactory.CloudSoundEffectGen.base import (
    SoundEffectProvider,
)
from EdennCode.ModelFactory.VideoSFXModelFactory.CloudSoundEffectGen.cloud_sound_effect_gen_util import (
    build_sound_effect_provider,
)
from EdennCode.ModelFactory.VideoSFXModelFactory.VideoConditionedGen.base import (
    VideoConditionedSfxProvider,
)
from EdennCode.ModelFactory.VideoSFXModelFactory.VideoConditionedGen.provider_e_video_sfx import (
    build_video_conditioned_provider,
)
from EdennCode.Util.MediaUtils.pipeline_util import hash_str
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.VideoSoundEffectDataModel.datamodel import (
    ROUTE_TEXT,
    ROUTE_VIDEO_NATIVE,
    AmbienceBed,
    SoundFXEvent,
)

logger = logging.getLogger(__name__)

# Providers are remote APIs; keep concurrent requests bounded.
MAX_CONCURRENT_GENERATIONS = 8


@dataclass
class SoundEffectGenerationStageInput:
    list_of_generation_packages: List[SoundFXEvent]
    output_directory: Path
    sample_rate: int = 44100
    num_variants: int = 1


@dataclass
class SoundEffectGenerationStageOutput:
    generated_sound_events: List[SoundFXEvent]


class SoundEffectGenerationStage:
    """
    Generate sound effects for each timeline event (optionally N variants each).
    """

    _UNSET = object()

    def __init__(
        self,
        sound_effect_generation_model: Optional[SoundEffectProvider] = None,
        video_conditioned_model=_UNSET,
    ):
        self.sound_effect_generation_model = sound_effect_generation_model or build_sound_effect_provider()
        # Auto-built from env only when not explicitly provided; pass None to
        # disable (offline tests). The text route is always the fallback.
        self.video_conditioned_model: Optional[VideoConditionedSfxProvider] = (
            build_video_conditioned_provider()
            if video_conditioned_model is self._UNSET
            else video_conditioned_model
        )
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_GENERATIONS)

    async def run(self, stage_input: SoundEffectGenerationStageInput) -> SoundEffectGenerationStageOutput:
        output_directory = stage_input.output_directory
        output_directory.mkdir(parents=True, exist_ok=True)
        generated = await self._run_async(
            events=stage_input.list_of_generation_packages,
            output_directory=output_directory,
            sample_rate=stage_input.sample_rate,
            num_variants=max(1, stage_input.num_variants),
        )
        return SoundEffectGenerationStageOutput(generated_sound_events=generated)

    async def _run(self, stage_input: SoundEffectGenerationStageInput) -> SoundEffectGenerationStageOutput:
        # Backward compatibility alias.
        return await self.run(stage_input)

    async def _run_async(
        self,
        *,
        events: List[SoundFXEvent],
        output_directory: Path,
        sample_rate: int,
        num_variants: int,
    ) -> List[SoundFXEvent]:
        tasks = [
            self._generate_one_event(
                idx=idx,
                event=event,
                output_directory=output_directory,
                sample_rate=sample_rate,
                num_variants=num_variants,
            )
            for idx, event in enumerate(events)
        ]
        return await asyncio.gather(*tasks)

    async def generate_event_variants(
        self,
        *,
        event: SoundFXEvent,
        output_directory: Path,
        sample_rate: int = 44100,
        num_variants: int = 1,
        prompt_override: Optional[str] = None,
        route: str = ROUTE_TEXT,
        video_url: str = "",
    ) -> List[Path]:
        """
        Generate ``num_variants`` clips for one event. Used both by the batch
        run and by the iteration loop (regenerate/swap on a single event).

        route=video_native sends the event's window of the source video to the
        video-conditioned engine (it sees pixels); the text route synthesizes
        from the event's sound prompt. Beyond-visual events (stylistic,
        narrative, offscreen, user) are text-only by necessity.
        """
        output_directory.mkdir(parents=True, exist_ok=True)
        duration_seconds = max(0.2, event.duration)
        prompt = (prompt_override or event.generation_prompt).strip()

        if route == ROUTE_VIDEO_NATIVE:
            if self.video_conditioned_model is None:
                raise RuntimeError("video_native route requested but no engine key is configured")
            if not video_url:
                raise ValueError("video_native route requires the source video URL")
            output_stem = f"sfx_{event.event_id}_{hash_str()[:8]}_vn"
            async with self._semaphore:
                return await self.video_conditioned_model.generate_for_video(
                    video_url=video_url,
                    output_stem=output_stem,
                    output_dir=output_directory,
                    start_offset_s=event.effective_start,
                    duration_s=duration_seconds,
                    prompt=prompt or None,
                    num_samples=num_variants,
                    sample_rate=sample_rate,
                )

        async def _one(variant_idx: int) -> Path:
            output_stem = f"sfx_{event.event_id}_{hash_str()[:8]}_v{variant_idx}"
            async with self._semaphore:
                return await self.sound_effect_generation_model.generate(
                    prompt=prompt,
                    duration_seconds=duration_seconds,
                    output_stem=output_stem,
                    output_dir=output_directory,
                    sample_rate=sample_rate,
                )

        results = await asyncio.gather(
            *[_one(i) for i in range(max(1, num_variants))], return_exceptions=True
        )
        paths = [r for r in results if isinstance(r, Path)]
        failures = [r for r in results if isinstance(r, BaseException)]
        for failure in failures:
            logger.warning(
                "Variant generation failed for event %s (%r): %s",
                event.event_id,
                prompt[:60],
                failure,
            )
        if not paths:
            raise failures[0]
        return paths

    async def _generate_one_event(
        self,
        *,
        idx: int,
        event: SoundFXEvent,
        output_directory: Path,
        sample_rate: int,
        num_variants: int,
    ) -> SoundFXEvent:
        try:
            variant_paths = await self.generate_event_variants(
                event=event,
                output_directory=output_directory,
                sample_rate=sample_rate,
                num_variants=num_variants,
            )
        except Exception:
            # One bad event must not sink the batch: the project persists with
            # this event silent, and the editor can regenerate it individually.
            logger.exception("All variants failed for event %s; leaving it silent.", event.event_id)
            return event
        event.variants = []
        for path in variant_paths:
            event.add_variant(str(path), select=False, route=ROUTE_TEXT, prompt=event.generation_prompt)
        event.select_variant(0)
        return event

    async def generate_ambience(
        self,
        *,
        ambience: AmbienceBed,
        output_directory: Path,
        duration_seconds: float,
        sample_rate: int = 44100,
        video_url: str = "",
    ) -> AmbienceBed:
        """
        Generate the background ambience bed.

        route=video_native asks the video-conditioned engine to score the
        footage itself (single call, engine cap ~60s; longer videos fall back
        to the text route for now). Text route: providers that support
        seamless looping get ``loop=True``; others get a plain generation that
        the timeline renderer loop-tiles with crossfades.
        """
        output_directory.mkdir(parents=True, exist_ok=True)

        if ambience.route == ROUTE_VIDEO_NATIVE:
            if self.video_conditioned_model is not None and video_url and duration_seconds <= 60.0:
                paths = await self.video_conditioned_model.generate_for_video(
                    video_url=video_url,
                    output_stem=f"ambience_vn_{hash_str()[:8]}",
                    output_dir=output_directory,
                    start_offset_s=0.0,
                    duration_s=duration_seconds,
                    prompt=(ambience.prompt or None),
                    num_samples=1,
                    sample_rate=sample_rate,
                )
                ambience.loop = False  # the bed already spans the video
                ambience.add_variant(str(paths[0]), select=True)
                return ambience
            logger.info(
                "video_native bed unavailable (key=%s, url=%s, duration=%.1fs); "
                "falling back to text route.",
                self.video_conditioned_model is not None,
                bool(video_url),
                duration_seconds,
            )
            ambience.route = ROUTE_TEXT
        output_stem = f"ambience_{hash_str()[:8]}"
        generate = self.sound_effect_generation_model.generate
        kwargs = dict(
            prompt=ambience.prompt,
            duration_seconds=duration_seconds,
            output_stem=output_stem,
            output_dir=output_directory,
            sample_rate=sample_rate,
        )
        supports_loop = "loop" in inspect.signature(generate).parameters
        async with self._semaphore:
            if supports_loop and ambience.loop:
                path = await generate(loop=True, **kwargs)
            else:
                path = await generate(**kwargs)
        ambience.add_variant(str(path), select=True)
        return ambience
