from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_c import GenerateParams, ProviderCApi, ProviderCTrack

from ..audio import (
    build_section_timeline,
    coerce_timestamped_words,
    measure_audio_duration,
    strip_section_header_words,
)
from ..models import MusicGenerationRequest, MusicGenerationResult, MusicVariant, ProviderJobRef
from .base import MusicGenerationStrategy


class ProviderCMusicGenerationStrategy(MusicGenerationStrategy):
    # Verified against production audio 2026-08-23: five delivered studio tracks
    # (including extended ones) fade out musically to -76..-81 dB with no
    # isolated trailing segment anywhere in the file, while enhanced tracks all
    # close with a 1.95s tag after a 3-4s silence gap. This provider does not
    # tag its output, so the tail chop here only removed real music.
    appends_provider_tail = False

    def __init__(self, *, music_provider: ProviderCApi) -> None:
        self.music_provider = music_provider
        self.extension_tolerance_s = 0.5
        self.max_extension_rounds = 2

    @staticmethod
    def _to_vocal_gender(value: Optional[str]) -> Optional[str]:
        normalized = (value or "").strip().lower()
        if normalized in {"m", "male", "man", "masculine"}:
            return "m"
        if normalized in {"f", "female", "woman", "feminine"}:
            return "f"
        return None

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
            f"{plan.music_prompt_summary or plan.summary}. "
            f"Mood: {plan.overall_mood}. "
            f"Tempo around {plan.target_bpm or 100} BPM. "
            f"Instruments: {', '.join(plan.primary_instruments)}. "
            f"Arc: {arc}."
            f"{vocal_guidance}"
        ).strip()
        lyrics_blocks = []
        for section in plan.sections:
            header = f"[{section.label}]"
            lines = [line.strip() for line in section.lyric_lines if str(line).strip()]
            if not lines:
                lines = [section.objective.strip()]
            lyrics_blocks.append("\n".join([header, *lines]))
        simple_prompt = style_prompt.replace("\n", " ").strip()
        if len(simple_prompt) > 500:
            simple_prompt = simple_prompt[:497].rstrip() + "..."
        return {
            "style_prompt": style_prompt,
            "lyrics_text": "\n\n".join(lyrics_blocks).strip(),
            "simple_prompt": simple_prompt,
        }

    def _build_output_path(self, request: MusicGenerationRequest, stem: str) -> Path:
        request.output_dir.mkdir(parents=True, exist_ok=True)
        # ProviderC serves MP3 bytes; the .mp3 name keeps the shared tail trim
        # re-encoding as MP3 instead of blowing the full track up to PCM WAV.
        return request.output_dir / f"{request.request_id}_{stem}.mp3"

    def _needs_extension(self, path: Path, target_duration_s: float) -> bool:
        duration_s = measure_audio_duration(path, fallback_s=target_duration_s)
        return duration_s + self.extension_tolerance_s < target_duration_s

    async def _extend_track_if_needed(
        self,
        *,
        request: MusicGenerationRequest,
        task_id: str,
        track: ProviderCTrack,
        local_path: Path,
        take_stem: str = "primary",
    ) -> tuple[str, ProviderCTrack, Path]:
        current_task_id = task_id
        current_track = track
        current_path = local_path
        model_name = str(request.provider_overrides.get("provider_c_model", "V5"))
        for _ in range(self.max_extension_rounds):
            if not self._needs_extension(current_path, request.section_plan.total_duration_s):
                break
            current_task_id, current_track, _ = await self.music_provider.extend_and_poll_track(
                current_track.audio_id,
                model=model_name,
                timeout_s=300.0,
                poll_s=10.0,
                # Restated every round: the provider does not inherit it from the
                # source track, so an unqualified extension of an instrumental
                # comes back with vocals over it.
                instrumental=not request.options.include_vocals,
            )
            # Per-take stem: the alternate must not download onto the file the
            # primary variant already points at, or the delivered primary
            # silently becomes the alternate's audio.
            next_path = self._build_output_path(request, f"{take_stem}_extend")
            current_path = await self.music_provider.download(current_track, next_path)
            # Re-chop the tail each round (parity with enhanced) so an extension's
            # trailing seconds never survive into the delivered full track.
            current_path, _ = self._trim_full_track_tail(current_path)
        return current_task_id, current_track, current_path

    async def generate(self, request: MusicGenerationRequest) -> MusicGenerationResult:
        compiled = self._compile(request)
        extra = {}
        vocal_gender = self._to_vocal_gender(request.options.vocal_gender)
        if request.options.include_vocals and vocal_gender:
            extra["vocalGender"] = vocal_gender

        if request.options.include_vocals:
            params = GenerateParams(
                prompt=compiled["lyrics_text"],
                custom_mode=True,
                instrumental=False,
                callback_url=request.options.callback_url,
                style=compiled["style_prompt"],
                model=str(request.provider_overrides.get("provider_c_model", "V5")),
                extra=extra or None,
            )
        else:
            params = GenerateParams(
                prompt=compiled["simple_prompt"],
                custom_mode=False,
                instrumental=True,
                callback_url=request.options.callback_url,
                model=str(request.provider_overrides.get("provider_c_model", "V5")),
                extra=extra or None,
            )

        task_id, generation_result, _base_used = await self.music_provider.generate_and_poll_tracks(
            params,
            timeout_s=300.0,
            poll_s=10.0,
        )
        tracks = generation_result.tracks
        if not tracks:
            raise RuntimeError("ProviderC generation completed without tracks.")

        first_track = tracks[0]
        first_path = self._build_output_path(request, "primary")
        first_path = await self.music_provider.download(first_track, first_path)
        # Chop the tail up front (parity with enhanced), then extend toward the
        # target; extension re-trims each round, so the delivered studio full
        # track always loses its last ~6s, independent of the watermark. Trimming
        # BEFORE extension is what makes the chop reliable — trimming only after
        # would be skipped whenever extension lands the track near the target.
        first_path, _ = self._trim_full_track_tail(first_path)
        task_id, first_track, first_path = await self._extend_track_if_needed(
            request=request,
            task_id=task_id,
            track=first_track,
            local_path=first_path,
        )

        timestamp_words = []
        if request.options.include_vocals and request.options.require_word_timestamps:
            timestamp_words = await self.music_provider.wait_for_timestamped_lyrics(
                task_id,
                first_track.audio_id,
                timeout_s=200.0,
            )

        primary_duration = measure_audio_duration(
            first_path,
            fallback_s=request.section_plan.total_duration_s,
        )
        primary = MusicVariant(
            variant_id="primary",
            audio_path=first_path,
            duration_s=primary_duration,
            # The provider aligns the compiled sheet, headers included — strip the
            # section-tag tokens so "[Verse]" never surfaces as a sung word, and
            # drop any words past the trimmed track end.
            lyrics_timestamps=strip_section_header_words(
                self._clip_words_to_duration(timestamp_words, primary_duration),
                full_lyrics=compiled["lyrics_text"],
            ),
            # The sheet the track was asked to sing — ProviderB sets this; leaving it
            # unset here made image-music studio runs return an empty full_lyrics.
            full_lyrics=(
                compiled["lyrics_text"] if request.options.include_vocals else None
            ),
            section_timeline=build_section_timeline(
                request.section_plan,
                actual_total_duration_s=primary_duration,
            ),
        )

        alternates: list[MusicVariant] = []
        second_track = next(
            (track for track in tracks[1:] if track.audio_id != first_track.audio_id),
            None,
        )
        if second_track:
            second_path = self._build_output_path(request, "alt2")
            second_path = await self.music_provider.download(second_track, second_path)
            second_path, _ = self._trim_full_track_tail(second_path)
            _, _, second_path = await self._extend_track_if_needed(
                request=request,
                task_id=task_id,
                track=second_track,
                local_path=second_path,
                take_stem="alt2",
            )
            second_duration = measure_audio_duration(
                second_path,
                fallback_s=request.section_plan.total_duration_s,
            )
            alternates.append(
                MusicVariant(
                    variant_id="alt2",
                    audio_path=second_path,
                    duration_s=second_duration,
                    lyrics_timestamps=[],
                    section_timeline=build_section_timeline(
                        request.section_plan,
                        actual_total_duration_s=second_duration,
                    ),
                )
            )

        return MusicGenerationResult(
            used_modelspec=request.modelspec,
            primary=primary,
            alternates=alternates,
            job_ref=ProviderJobRef(
                provider="provider_c",
                task_id=task_id,
                audio_id=first_track.audio_id,
            ),
            prompt_manifest=compiled,
            prompt_summary=compiled["style_prompt"] if request.options.include_vocals else compiled["simple_prompt"],
        )
