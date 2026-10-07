from __future__ import annotations

from dataclasses import replace
from typing import Callable

from EdennCode.Util.MediaUtils.audio_watermark import append_voice_watermark_to_audio

from .audio import build_section_timeline, measure_audio_duration
from .models import (
    MusicGenerationRequest,
    MusicGenerationResult,
    MusicModelSpec,
    MusicVariant,
    normalize_modelspec,
)
from .providers.base import FULL_TRACK_TAIL_TRIM_S, MusicGenerationStrategy


class MusicGenerationService:
    def __init__(
        self,
        *,
        strategy_builders: dict[
            MusicModelSpec | str,
            MusicGenerationStrategy | Callable[[], MusicGenerationStrategy],
        ],
    ) -> None:
        self._strategy_builders = {
            normalize_modelspec(key): value for key, value in strategy_builders.items()
        }
        self._strategy_cache: dict[MusicModelSpec, MusicGenerationStrategy] = {}

    def _resolve_strategy(self, modelspec: MusicModelSpec) -> MusicGenerationStrategy:
        if modelspec in self._strategy_cache:
            return self._strategy_cache[modelspec]
        builder = self._strategy_builders[modelspec]
        strategy = builder() if callable(builder) else builder
        self._strategy_cache[modelspec] = strategy
        return strategy

    @staticmethod
    def _tail_already_chopped(variant: MusicVariant) -> bool:
        label = ("%g" % FULL_TRACK_TAIL_TRIM_S).replace(".", "p")
        # Trailing marker only: an extension names its output `{stem}_extend{n}`,
        # so a track chopped before extension carries the marker mid-stem while
        # its actual tail is whatever the provider just appended.
        return variant.audio_path.stem.endswith(f"_trimmed_tail{label}s")

    def _without_provider_tail(
        self,
        variant: MusicVariant,
        *,
        strategy: MusicGenerationStrategy,
        request: MusicGenerationRequest,
    ) -> MusicVariant:
        """Return the variant with the provider's trailing tag removed.

        The strategies chop unconditionally themselves, so this is normally a
        no-op on an already-marked file — it exists as the delivery boundary's
        own guarantee, so a strategy path that misses its chop (or a file whose
        marker was lost) still cannot hand a tagged track to a caller.
        """

        if self._tail_already_chopped(variant):
            return variant
        trimmed_path, trimmed_duration_s = strategy._trim_full_track_tail(
            variant.audio_path,
            minimum_required_duration_s=0.0,
        )
        if trimmed_path == variant.audio_path:
            return variant
        return replace(
            variant,
            audio_path=trimmed_path,
            duration_s=trimmed_duration_s,
            lyrics_timestamps=strategy._clip_words_to_duration(
                variant.lyrics_timestamps, trimmed_duration_s
            ),
            line_level_lyrics_timestamps=strategy._clip_words_to_duration(
                variant.line_level_lyrics_timestamps, trimmed_duration_s
            ),
            section_timeline=build_section_timeline(
                request.section_plan,
                actual_total_duration_s=trimmed_duration_s,
            ),
        )

    def _strip_provider_tails(
        self,
        result: MusicGenerationResult,
        *,
        strategy: MusicGenerationStrategy,
        request: MusicGenerationRequest,
    ) -> MusicGenerationResult:
        if not getattr(strategy, "appends_provider_tail", False):
            return result
        return replace(
            result,
            primary=self._without_provider_tail(
                result.primary, strategy=strategy, request=request
            ),
            alternates=[
                self._without_provider_tail(
                    variant, strategy=strategy, request=request
                )
                for variant in result.alternates
            ],
        )

    async def generate(self, request: MusicGenerationRequest) -> MusicGenerationResult:
        strategy = self._resolve_strategy(normalize_modelspec(request.modelspec))
        result = await strategy.generate(request)
        # Strip before watermarking, never after: our clip is concatenated onto
        # the end, so a tag left in place here is sealed mid-file where no tail
        # chop can ever reach it again.
        result = self._strip_provider_tails(
            result, strategy=strategy, request=request
        )
        if not request.options.water_mark:
            return result
        primary_path = append_voice_watermark_to_audio(result.primary.audio_path)
        primary = replace(
            result.primary,
            audio_path=primary_path,
            duration_s=measure_audio_duration(
                primary_path,
                fallback_s=result.primary.duration_s,
            ),
        )
        alternates = [
            replace(
                variant,
                audio_path=(watermarked_path := append_voice_watermark_to_audio(variant.audio_path)),
                duration_s=measure_audio_duration(
                    watermarked_path,
                    fallback_s=variant.duration_s,
                ),
            )
            for variant in result.alternates
        ]
        return replace(result, primary=primary, alternates=alternates)
