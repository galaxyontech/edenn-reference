from __future__ import annotations

from typing import Any

from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_a_compose import ProviderALyrics

from ..audio import build_section_timeline, coerce_timestamped_words, measure_audio_duration
from ..models import (
    MusicGenerationRequest,
    MusicGenerationResult,
    MusicVariant,
    ProviderJobRef,
    TimestampedWord,
)
from .base import MusicGenerationStrategy


class ProviderAMusicGenerationStrategy(MusicGenerationStrategy):
    def __init__(self, *, music_provider: ProviderALyrics) -> None:
        self.music_provider = music_provider

    @staticmethod
    def _style_tags(request: MusicGenerationRequest) -> list[str]:
        section_plan = request.section_plan
        tags = [section_plan.overall_mood]
        tags.extend(section_plan.primary_instruments)
        for section in section_plan.sections:
            tags.append(section.label)
            tags.extend(section.instrumentation_focus)
        deduped: list[str] = []
        for tag in tags:
            cleaned = str(tag or "").strip()
            if cleaned and cleaned not in deduped:
                deduped.append(cleaned)
        return deduped[:12]

    def _compile(self, request: MusicGenerationRequest) -> dict[str, Any]:
        section_plan = request.section_plan
        language = (request.options.lyrics_language or "en").strip().lower()
        sections = []
        for section in section_plan.sections:
            section_payload = {
                "section_name": section.label,
                "positive_local_styles": [
                    section.objective,
                    *section.instrumentation_focus,
                ],
                "negative_local_styles": [],
                "duration_ms": max(3000, int(section.target_duration_s * 1000)),
            }
            lines = list(section.lyric_lines) if request.options.include_vocals else []
            section_payload["lines"] = lines
            sections.append(section_payload)
        composition_plan = {
            "positive_global_styles": self._style_tags(request),
            "negative_global_styles": [],
            "sections": sections,
        }
        prompt_summary = section_plan.music_prompt_summary or (
            f"{section_plan.summary} Mood: {section_plan.overall_mood}. "
            f"Tempo around {section_plan.target_bpm or 100} BPM. "
            f"Focus instruments: {', '.join(section_plan.primary_instruments)}."
        )
        song_metadata = {
            "title": request.request_id[:80],
            "description": section_plan.summary[:160],
            "genres": self._style_tags(request)[:6],
            "languages": [language] if request.options.include_vocals else [],
            "is_explicit": False,
        }
        return {
            "prompt_summary": prompt_summary.strip(),
            "composition_plan": composition_plan,
            "song_metadata": song_metadata,
            "respect_sections_durations": False,
        }

    async def generate(self, request: MusicGenerationRequest) -> MusicGenerationResult:
        compiled = self._compile(request)
        output_path, lyrics = await self.music_provider.generate(
            compiled["prompt_summary"],
            with_timestamps=request.options.require_word_timestamps,
            extra={
                "composition_plan": compiled["composition_plan"],
                "respect_sections_durations": compiled["respect_sections_durations"],
            },
            output_format=request.options.output_format,
        )
        duration_s = measure_audio_duration(
            output_path,
            fallback_s=request.section_plan.total_duration_s,
        )
        primary = MusicVariant(
            variant_id="primary",
            audio_path=output_path,
            duration_s=duration_s,
            lyrics_timestamps=coerce_timestamped_words(lyrics),
            section_timeline=build_section_timeline(
                request.section_plan,
                actual_total_duration_s=duration_s,
            ),
        )
        return MusicGenerationResult(
            used_modelspec=request.modelspec,
            primary=primary,
            alternates=[],
            job_ref=ProviderJobRef(provider="provider_a"),
            prompt_manifest=compiled,
            prompt_summary=compiled["prompt_summary"],
        )
