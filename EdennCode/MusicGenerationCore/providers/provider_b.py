from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Optional

from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_music import ProviderBMusicProvider
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_payload import (
    extract_audio_urls,
    extract_choice_audio_url,
    extract_timestamped_lines,
    extract_timestamped_lyrics_for_choice,
    extract_timestamped_words,
)

logger = logging.getLogger(__name__)
from EdennCode.Util.MediaUtils import resolve_ffmpeg_binary

from ..audio import (
    build_section_timeline,
    coerce_timestamped_words,
    measure_audio_duration,
    strip_section_header_words,
)
from ..models import (
    MusicGenerationRequest,
    MusicGenerationResult,
    MusicVariant,
    ProviderJobRef,
    TimestampedWord,
)
from .base import MusicGenerationStrategy


ENHANCED_FULL_TRACK_TAIL_TRIM_S = 6.0
MIN_TRIMMED_AUDIO_DURATION_S = 0.25


class ProviderBMusicGenerationStrategy(MusicGenerationStrategy):
    appends_provider_tail = True

    def __init__(self, *, music_provider: ProviderBMusicProvider) -> None:
        self.music_provider = music_provider
        self.extension_tolerance_s = 0.5
        self.max_extension_rounds = 2

    @staticmethod
    def _trimmed_tail_path(path: Path, *, tail_trim_s: float) -> Path:
        suffix = path.suffix or ".mp3"
        label = ("%g" % tail_trim_s).replace(".", "p")
        return path.with_name(f"{path.stem}_trimmed_tail{label}s{suffix}")

    @staticmethod
    def _ffmpeg_audio_codec_args(path: Path) -> list[str]:
        suffix = path.suffix.lower()
        if suffix == ".wav":
            return ["-c:a", "pcm_s16le"]
        if suffix == ".mp3":
            return ["-c:a", "libmp3lame", "-b:a", "192k"]
        if suffix in {".m4a", ".mp4", ".aac"}:
            return ["-c:a", "aac", "-b:a", "192k"]
        return ["-c:a", "aac", "-b:a", "192k"]

    def _trim_full_track_tail(
        self,
        audio_path: Path,
        *,
        minimum_required_duration_s: float = 0.0,
        tail_trim_s: float = ENHANCED_FULL_TRACK_TAIL_TRIM_S,
    ) -> tuple[Path, float]:
        duration_s = measure_audio_duration(audio_path, fallback_s=0.0)
        if duration_s <= 0 or tail_trim_s <= 0:
            return audio_path, duration_s

        trimmed_duration_s = duration_s - float(tail_trim_s)
        if (
            trimmed_duration_s + self.extension_tolerance_s
            < float(minimum_required_duration_s)
            or trimmed_duration_s <= MIN_TRIMMED_AUDIO_DURATION_S
        ):
            return audio_path, duration_s

        output_path = self._trimmed_tail_path(audio_path, tail_trim_s=tail_trim_s)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            resolve_ffmpeg_binary(),
            "-y",
            "-i",
            str(audio_path),
            "-vn",
            "-t",
            f"{trimmed_duration_s:.3f}",
            *self._ffmpeg_audio_codec_args(output_path),
            str(output_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return output_path, measure_audio_duration(
            output_path,
            fallback_s=trimmed_duration_s,
        )

    @staticmethod
    def _clip_words_to_duration(
        words: list[object],
        duration_s: float,
    ) -> list[TimestampedWord]:
        if duration_s <= 0:
            return coerce_timestamped_words(words)
        clipped: list[TimestampedWord] = []
        duration_s = max(0.0, float(duration_s))
        for word in coerce_timestamped_words(words):
            if word.startS >= duration_s:
                continue
            clipped_end_s = min(word.endS, duration_s)
            if clipped_end_s <= word.startS:
                continue
            clipped.append(
                TimestampedWord(
                    text=word.text,
                    startS=word.startS,
                    endS=clipped_end_s,
                    i=word.i,
                )
            )
        return clipped

    def _compile(self, request: MusicGenerationRequest) -> dict[str, Any]:
        plan = request.section_plan
        arc = " -> ".join(section.label for section in plan.sections)
        vocal_guidance = ""
        if request.options.include_vocals:
            language = (request.options.lyrics_language or "").strip()
            gender = (request.options.vocal_gender or "").strip()
            details = []
            if language:
                details.append(f"lyrics in {language}")
            if gender:
                details.append(f"{gender} lead vocal")
            if details:
                vocal_guidance = " Vocal guidance: " + ", ".join(details) + "."
        style_prompt = (
            f"{plan.music_prompt_summary or plan.summary} "
            f"Mood: {plan.overall_mood}. Tempo around {plan.target_bpm or 100} BPM. "
            f"Instruments: {', '.join(plan.primary_instruments)}. Arc: {arc}."
            f"{vocal_guidance}"
        ).strip()
        lyrics_blocks = []
        for section in plan.sections:
            header = f"[{section.label}]"
            lines = [line.strip() for line in section.lyric_lines if str(line).strip()]
            if not lines:
                lines = [section.objective.strip()]
            lyrics_blocks.append("\n".join([header, *lines]))
        return {
            "style_prompt": style_prompt,
            "lyrics_text": "\n\n".join(lyrics_blocks).strip(),
        }

    def _build_output_path(self, request: MusicGenerationRequest, stem: str) -> Path:
        request.output_dir.mkdir(parents=True, exist_ok=True)
        return request.output_dir / f"{request.request_id}_{stem}.mp3"

    def _extract_variants(
        self,
        request: MusicGenerationRequest,
        *,
        task_id: str,
        raw_payload: dict[str, Any],
        duration_fallback_s: float,
    ) -> tuple[MusicVariant, list[MusicVariant]]:
        audio_urls = extract_audio_urls(
            raw_payload,
            limit=max(1, request.options.max_variants),
        )
        if not audio_urls:
            raise RuntimeError("ProviderB generation completed without audio URLs.")
        timestamped_words = coerce_timestamped_words(
            extract_timestamped_words(raw_payload)
        )
        primary_path = self._build_output_path(request, "primary")
        alternates: list[MusicVariant] = []
        return audio_urls, timestamped_words, primary_path, alternates

    def _needs_extension(self, path: Path, target_duration_s: float) -> bool:
        duration_s = measure_audio_duration(path, fallback_s=target_duration_s)
        return duration_s + self.extension_tolerance_s < target_duration_s

    async def _extend_if_needed(
        self,
        *,
        audio_path: Path,
        prompt: str,
        lyrics: str,
        target_duration_s: float,
    ) -> tuple[Path, list, list, str]:
        current_path = audio_path
        current_lyrics = lyrics
        current_words: list = []
        current_line_words: list = []
        for round_idx in range(self.max_extension_rounds):
            if not self._needs_extension(current_path, target_duration_s):
                break
            next_path = current_path.with_name(
                f"{current_path.stem}_extend{round_idx + 1}{current_path.suffix}"
            )
            # Use the DETAILED extend so BOTH word- and line-level timestamps for
            # the extended track are captured. The flat extend_song_from_audio
            # returns only word-level, which left line-level stale after an
            # extension (word-level + duration were post-extension, line-level was
            # not).
            current_path, timestamps, current_lyrics = await self.music_provider.extend_song_from_audio_detailed(
                audio_path=current_path,
                prompt=prompt,
                lyrics=current_lyrics,
                output_path=next_path,
            )
            current_words = list(getattr(timestamps, "word_level", None) or [])
            current_line_words = list(getattr(timestamps, "line_level", None) or [])
            current_path, current_duration_s = self._trim_full_track_tail(current_path)
            current_words = self._clip_words_to_duration(
                current_words,
                current_duration_s,
            )
            current_line_words = self._clip_words_to_duration(
                current_line_words,
                current_duration_s,
            )
        return current_path, current_words, current_line_words, current_lyrics

    async def generate(self, request: MusicGenerationRequest) -> MusicGenerationResult:
        compiled = self._compile(request)
        alternates: list[MusicVariant] = []
        raw_ids: dict[str, str] = {}
        normalized_vocal_id = (request.options.vocal_id or "").strip() or None

        if normalized_vocal_id and request.options.vocal_sample_path is not None:
            raise ValueError("Provide either options.vocal_id or options.vocal_sample_path, not both.")
        if not request.options.include_vocals and (
            normalized_vocal_id or request.options.vocal_sample_path is not None
        ):
            raise ValueError("Vocal clone inputs can only be used for vocal ProviderB generation.")

        if request.options.include_vocals:
            if request.options.vocal_sample_path is not None and not normalized_vocal_id:
                normalized_vocal_id = await self.music_provider.clone_vocal(
                    request.options.vocal_sample_path
                )
            task = await self.music_provider.generate_song_task(
                lyrics=compiled["lyrics_text"],
                prompt=compiled["style_prompt"],
                vocal_id=normalized_vocal_id,
                model=str(request.provider_overrides.get("provider_b_model", "provider_b-8")),
                n=max(1, min(request.options.max_variants, 2)),
            )
            finished = await self.music_provider.wait_song_task(task.task_id)
            raw_ids["trace_id"] = finished.trace_id
            if normalized_vocal_id:
                raw_ids["vocal_id"] = normalized_vocal_id
            audio_urls = extract_audio_urls(
                finished.raw or {},
                limit=max(1, request.options.max_variants),
            )
            # Deterministic take pairing: the task returns explicit takes, and
            # the delivered audio and its lyric alignment must come from the
            # SAME take (the first one). The generic URL walk does not
            # guarantee take order, so pin take 0's URL to the front.
            choice0_url = extract_choice_audio_url(finished.raw or {}, 0)
            if choice0_url:
                audio_urls = [choice0_url, *[u for u in audio_urls if u != choice0_url]]
            if not audio_urls:
                raise RuntimeError("ProviderB generation completed without audio URLs.")
            primary_path = self._build_output_path(request, "primary")
            await self.music_provider.download_audio(audio_urls[0], primary_path)
            alt_path: Optional[Path] = None
            if len(audio_urls) > 1:
                alt_path = self._build_output_path(request, "alt2")
                await self.music_provider.download_audio(audio_urls[1], alt_path)
                alt_path, _ = self._trim_full_track_tail(alt_path)
            # Timestamps must come from the SAME take as the downloaded audio.
            # The task returns n takes of the same lyric sheet with different
            # timing; the task-level extractors return the first take that has
            # alignments, which is not necessarily take 0 — that mismatch
            # shipped word timestamps that drifted seconds off the delivered
            # track. Strictly per-choice: another take's timing is worse than
            # no timing, so when explicit takes exist there is NO cross-take
            # fallback; the task-level extractors only serve payloads without
            # a choices list.
            choice0 = extract_timestamped_lyrics_for_choice(finished.raw or {}, 0)
            has_choices = bool((finished.raw or {}).get("choices"))
            if choice0.word_level or choice0.line_level:
                # Same word->line fallback the task-level extractor applies.
                words = choice0.word_level or choice0.line_level
                line_words = choice0.line_level
            elif has_choices:
                logger.warning(
                    "generation take 0 carried no lyric alignment; delivering "
                    "without timestamps rather than another take's timing "
                    "(task=%s)", finished.task_id,
                )
                words = []
                line_words = []
            else:
                words = extract_timestamped_words(finished.raw or {})
                line_words = extract_timestamped_lines(finished.raw or {})
            primary_path, primary_duration_after_trim_s = self._trim_full_track_tail(primary_path)
            words = self._clip_words_to_duration(
                words,
                primary_duration_after_trim_s,
            )
            line_words = self._clip_words_to_duration(
                line_words,
                primary_duration_after_trim_s,
            )
            primary_path, extended_words, extended_line_words, _ = await self._extend_if_needed(
                audio_path=primary_path,
                prompt=compiled["style_prompt"],
                lyrics=compiled["lyrics_text"],
                target_duration_s=request.section_plan.total_duration_s,
            )
            # When an extension ran, use its (post-extension) timestamps for BOTH
            # levels; otherwise keep the pre-extension take-0 timestamps.
            final_words = extended_words or words
            final_line_words = extended_line_words or line_words
            duration_s = measure_audio_duration(
                primary_path,
                fallback_s=request.section_plan.total_duration_s,
            )
            # Re-clip both levels to the FINAL delivered duration so line-level can
            # never lag word-level or the audio length after an extension.
            final_words = self._clip_words_to_duration(final_words, duration_s)
            final_line_words = self._clip_words_to_duration(final_line_words, duration_s)
            # Plain generated lyrics text (what the vocal track actually sings).
            full_lyrics_text = (compiled.get("lyrics_text") or "").strip() or None
            primary = MusicVariant(
                variant_id="primary",
                audio_path=primary_path,
                duration_s=duration_s,
                # The provider aligns the compiled sheet, headers included — strip
                # the section-tag tokens so "[Warm Build]" never surfaces as lyrics.
                lyrics_timestamps=strip_section_header_words(
                    coerce_timestamped_words(final_words),
                    full_lyrics=full_lyrics_text,
                ),
                line_level_lyrics_timestamps=strip_section_header_words(
                    coerce_timestamped_words(final_line_words),
                    full_lyrics=full_lyrics_text,
                ),
                full_lyrics=full_lyrics_text,
                section_timeline=build_section_timeline(
                    request.section_plan,
                    actual_total_duration_s=duration_s,
                ),
            )
            if alt_path:
                alt_duration = measure_audio_duration(
                    alt_path,
                    fallback_s=request.section_plan.total_duration_s,
                )
                alternates.append(
                    MusicVariant(
                        variant_id="alt2",
                        audio_path=alt_path,
                        duration_s=alt_duration,
                        lyrics_timestamps=[],
                        section_timeline=build_section_timeline(
                            request.section_plan,
                            actual_total_duration_s=alt_duration,
                        ),
                    )
                )
            return MusicGenerationResult(
                used_modelspec=request.modelspec,
                primary=primary,
                alternates=alternates,
                job_ref=ProviderJobRef(
                    provider="provider_b",
                    task_id=finished.task_id,
                    raw_ids=raw_ids,
                ),
                prompt_manifest=compiled,
                prompt_summary=compiled["style_prompt"],
                vocal_id_used=normalized_vocal_id,
            )

        task = await self.music_provider.generate_instrumental_task(
            prompt=compiled["style_prompt"],
            model=str(request.provider_overrides.get("provider_b_model", "provider_b-8")),
            n=max(1, min(request.options.max_variants, 2)),
        )
        finished = await self.music_provider.wait_instrumental_task(task.task_id)
        raw_ids["trace_id"] = finished.trace_id
        audio_urls = extract_audio_urls(
            finished.raw or {},
            limit=max(1, request.options.max_variants),
        )
        if not audio_urls:
            raise RuntimeError("ProviderB instrumental generation completed without audio URLs.")
        primary_path = self._build_output_path(request, "primary")
        await self.music_provider.download_audio(audio_urls[0], primary_path)
        primary_path, _ = self._trim_full_track_tail(primary_path)
        duration_s = measure_audio_duration(
            primary_path,
            fallback_s=request.section_plan.total_duration_s,
        )
        primary = MusicVariant(
            variant_id="primary",
            audio_path=primary_path,
            duration_s=duration_s,
            lyrics_timestamps=[],
            section_timeline=build_section_timeline(
                request.section_plan,
                actual_total_duration_s=duration_s,
            ),
        )
        if len(audio_urls) > 1:
            alt_path = self._build_output_path(request, "alt2")
            await self.music_provider.download_audio(audio_urls[1], alt_path)
            alt_path, _ = self._trim_full_track_tail(alt_path)
            alt_duration = measure_audio_duration(
                alt_path,
                fallback_s=request.section_plan.total_duration_s,
            )
            alternates.append(
                MusicVariant(
                    variant_id="alt2",
                    audio_path=alt_path,
                    duration_s=alt_duration,
                    lyrics_timestamps=[],
                    section_timeline=build_section_timeline(
                        request.section_plan,
                        actual_total_duration_s=alt_duration,
                    ),
                )
            )
        return MusicGenerationResult(
            used_modelspec=request.modelspec,
            primary=primary,
            alternates=alternates,
            job_ref=ProviderJobRef(
                provider="provider_b",
                task_id=finished.task_id,
                raw_ids=raw_ids,
            ),
            prompt_manifest=compiled,
            prompt_summary=compiled["style_prompt"],
            vocal_id_used=None,
        )
