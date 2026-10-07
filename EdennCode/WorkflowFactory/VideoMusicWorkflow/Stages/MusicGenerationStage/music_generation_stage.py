from __future__ import annotations
import asyncio
import inspect
import json
import logging
import subprocess
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, List

import os
import time

from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_a_compose import ProviderALyrics
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_c import ProviderCApi, GenerateParams, ProviderCTrack
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_music import (
    ProviderBMusicProvider,
    ProviderBTimestampedLyrics,
)
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_payload import (
    extract_audio_url,
)
from EdennCode.Util.MediaUtils import resolve_ffmpeg_binary
from EdennCode.Util.MediaUtils.audio_watermark import append_voice_watermark_to_audio
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata import VideoMetadata
from EdennCode.exceptions import EdennConfigurationError, EdennProviderResponseError, EdennValidationError
from EdennCode.Util.MediaUtils.pipeline_util import build_provider_a_music_prompt
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage import WordTS
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.music_matching_stage import \
    MusicMatchingStageInput, MusicMatchingStage, MusicMatchingStageOutput
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.media_tools import MediaTools
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicGenerationStage.music_generation_stage_utils import (
    align_line_level_lyrics_to_window,
    strip_section_tags,
    to_ms_wordts,
)
from EdennCode.Annotation.core.annotation_dispatcher import (
    AnnotationDispatcher,
    safe_emit_annotation,
)
from EdennCode.Annotation.events.music_generation_event import MusicGenerationEvent

logger = logging.getLogger("Music Generation")


def _fake_provider_enabled() -> bool:
    """Performance-test switch (shared with the v2 split provider stage)."""
    return (os.getenv("ASYNC_V2_FAKE_PROVIDER") or "").strip().lower() in {
        "1", "true", "yes", "on", "y"}


def _fake_provider_delay_s() -> float:
    raw = (os.getenv("ASYNC_V2_FAKE_PROVIDER_DELAY_MS") or "").strip()
    try:
        return max(0.0, float(raw) / 1000.0)
    except ValueError:
        return 0.0


async def _fake_generated_audio(video_metadata) -> Path:
    """Return a deterministic ~30s audio after the simulated generation delay,
    so the v1 provider call costs nothing while matching/remix stay real."""
    delay_s = _fake_provider_delay_s()
    if delay_s > 0:
        await asyncio.sleep(delay_s)
    temp_folder = Path(video_metadata.temp_folder)
    temp_folder.mkdir(parents=True, exist_ok=True)
    audio_path = temp_folder / "fake_provider_audio.wav"
    if not (audio_path.exists() and audio_path.stat().st_size > 0):
        cmd = [
            resolve_ffmpeg_binary(), "-y", "-f", "lavfi",
            "-i", "sine=frequency=220:duration=30",
            "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(audio_path),
        ]
        await asyncio.to_thread(subprocess.run, cmd, check=True, capture_output=True)
    logger.info("fake provider (v1): canned audio (delay=%.1fs)", delay_s)
    return audio_path


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

ENHANCED_FULL_TRACK_TAIL_TRIM_S = 6.0
MIN_TRIMMED_AUDIO_DURATION_S = 0.25

# Which model generates an instrumental. Measured 2026-08-24 on one prompt, n=1:
# provider_b-8 returned 62s and ignored an explicit "about 5 minutes" request (66s
# when asked), while provider_b-9 returned 148s and auto 158s from the same words.
# The endpoint has no duration parameter, so the model version is the only lever
# that moves length — and 8 was short enough to leave four production jobs with
# 23-51% of the video unscored. Overridable without a deploy so a regression in
# musical quality can be rolled back to "provider_b-8" from configuration.
ENHANCED_INSTRUMENTAL_MODEL = os.getenv("EDENN_ENHANCED_INSTRUMENTAL_MODEL", "provider_b-9")


def _log_provider_timing(
    provider: str,
    duration_s: float,
    profile: str | None = None,
    ts_start: float | None = None,
) -> None:
    """Emit a structured JSON timing log for a music provider API call."""
    ts_start_utc = (
        datetime.fromtimestamp(ts_start, tz=timezone.utc).isoformat()
        if ts_start else None
    )
    ts_end_utc = (
        datetime.fromtimestamp(ts_start + duration_s, tz=timezone.utc).isoformat()
        if ts_start else None
    )
    logger.info(
        json.dumps({
            "event": "pipeline_timing",
            "stage": "provider_api_call",
            "duration_s": round(duration_s, 3),
            "profile": profile,
            "provider": provider,
            "ts_start_utc": ts_start_utc,
            "ts_end_utc": ts_end_utc,
        })
    )


async def _notify_provider_task_created(
    callback: Optional[Callable[[Dict[str, Any]], Any]],
    payload: Dict[str, Any],
) -> None:
    """Notify a caller that a provider task exists before polling starts.

    The default workflow does not pass a callback, so generation behavior is
    unchanged. Durable async workers use this hook to persist provider task IDs
    before long polling windows where a worker crash could otherwise cause a
    duplicate provider submission on retry.
    """

    if callback is None:
        return
    try:
        maybe_result = callback(payload)
        if inspect.isawaitable(maybe_result):
            await maybe_result
    except Exception:
        # Bookkeeping must never kill a generation the provider already
        # accepted (and billed): without the durable record a retry would
        # re-submit and pay again, which is strictly worse than a retry that
        # merely cannot resume.
        logger.exception(
            "provider task recorder failed; continuing without a durable "
            "record (task_id=%s role=%s) — a retry of this stage may "
            "re-submit instead of resuming",
            payload.get("task_id"), payload.get("role"),
        )


@dataclass
class MusicGenertionModelEnum:
    EDENN_BASIC: str = "edenn_basic"
    EDENN_ENHANCED: str = "edenn_enhanced"
    EDENN_STUDIO: str = "edenn_studio"


# Which providers close a finished track with their own audible tag, and so owe
# us a tail chop before anything a user hears. DECLARED, not assumed: verified
# against production audio 2026-08-23 — enhanced tracks all end with a 1.95s
# segment after a 3-4s silence gap, while studio and basic tracks fade out
# musically with no isolated trailing segment anywhere in the file. A chop
# applied to a provider that does not tag just deletes the customer's last six
# seconds of music, which is what the original "parity with enhanced" chop did.
PROVIDER_APPENDS_TRACK_TAIL: Dict[str, bool] = {
    MusicGenertionModelEnum.EDENN_ENHANCED: True,
    MusicGenertionModelEnum.EDENN_STUDIO: False,
    MusicGenertionModelEnum.EDENN_BASIC: False,
}


def provider_appends_track_tail(model_spec: Optional[str]) -> bool:
    """Does this tier's provider leave its own tag on the end of a track?

    Unknown or missing tiers answer no: a chop we cannot justify is six seconds
    of the customer's music, while a missed chop is a visible tag we will hear
    about and can then declare.
    """

    if not model_spec:
        return False
    return PROVIDER_APPENDS_TRACK_TAIL.get(str(model_spec).strip().lower(), False)


@dataclass
class MusicGenerationStageInput:
    prompt_metadata: Dict[str, str]
    video_metadata: VideoMetadata
    include_vocals: bool
    vocal_gender: str
    music_generation_model: str = MusicGenertionModelEnum.EDENN_BASIC
    provider_c_custom_mode: bool = True
    audio_output_format: Optional[str] = None
    lyrics_language: Optional[str] = None
    vocal_id: Optional[str] = None
    vocal_sample_path: Optional[Path] = None
    job_id: str = ""
    annotation_dispatcher: Optional[AnnotationDispatcher] = None
    video_category: str = ""
    scene_count: int = 0
    overall_mood: str = ""
    water_mark: bool = False


@dataclass
class MusicGenerationStageOutput:
    music_path: Path
    complete_music_path: Optional[Path] = None
    secondary_complete_music_path: Optional[Path] = None
    task_id: Optional[str] = None
    audio_id: Optional[str] = None
    lyrics_timestamps: List[WordTS] = field(default_factory=list)
    word_level_lyrics_timestamps: List[WordTS] = field(default_factory=list)
    primary_full_lyrics: Optional[str] = None
    primary_full_lyrics_timestamps: List[WordTS] = field(default_factory=list)
    primary_full_word_level_lyrics_timestamps: List[WordTS] = field(default_factory=list)
    secondary_full_lyrics: Optional[str] = None
    secondary_full_lyrics_timestamps: List[WordTS] = field(default_factory=list)
    secondary_full_word_level_lyrics_timestamps: List[WordTS] = field(default_factory=list)
    matching_used_track: Optional[str] = None
    vocal_id_used: Optional[str] = None
    music_start_s: float = 0.0
    alignment_score: float = 0.0
    alignment_details: Dict[str, Any] = field(default_factory=dict)
    critical_warning: Optional[str] = None
    generation_api_call_count: int = 1


@dataclass
class FullTrackLyricsData:
    full_lyrics: Optional[str] = None
    lyrics_timestamps: List[WordTS] = field(default_factory=list)
    word_level_lyrics_timestamps: List[WordTS] = field(default_factory=list)
    generation_api_call_count: int = 1


def to_eleven_ms(seconds: float) -> int:
    ms = int(seconds * 1000)
    return max(10_000, min(300_000, ms))


class MusicGenerationStage:
    """
    Trigger music generation via the configured provider.
    """

    def __init__(
        self,
        *,
        provider_a_music_provider: ProviderALyrics | None = None,
        provider_c_music_provider: ProviderCApi | None = None,
        provider_b_music_provider: ProviderBMusicProvider | None = None,
        provider_a_music_provider_factory: Callable[[], ProviderALyrics] | None = None,
        provider_c_music_provider_factory: Callable[[], ProviderCApi] | None = None,
        provider_b_music_provider_factory: Callable[[], ProviderBMusicProvider] | None = None,
    ) -> None:
        self.music_provider = provider_a_music_provider
        self.provider_c_music_provider = provider_c_music_provider
        self.provider_b_music_provider = provider_b_music_provider
        self._provider_a_music_provider_factory = provider_a_music_provider_factory
        self._provider_c_music_provider_factory = provider_c_music_provider_factory
        self._provider_b_music_provider_factory = provider_b_music_provider_factory
        self.music_reranker_stage = MusicMatchingStage()
        self.extension_tolerance_s = 0.5
        try:
            self.max_extension_rounds = max(
                1,
                int(os.getenv("EDENN_MUSIC_MAX_EXTENSION_ROUNDS", "6")),
            )
        except ValueError:
            self.max_extension_rounds = 6

    def _require_provider_a_music_provider(self) -> ProviderALyrics:
        if self.music_provider is None and self._provider_a_music_provider_factory is not None:
            self.music_provider = self._provider_a_music_provider_factory()
        if self.music_provider is None:
            raise EdennConfigurationError(
                "PROVIDER_A_API_KEY not set; cannot use edenn_basic model",
                component="music_generation",
                operation="init_provider_a",
            )
        return self.music_provider

    def _require_provider_c_music_provider(self) -> ProviderCApi:
        if self.provider_c_music_provider is None and self._provider_c_music_provider_factory is not None:
            self.provider_c_music_provider = self._provider_c_music_provider_factory()
        if self.provider_c_music_provider is None:
            raise EdennConfigurationError(
                "PROVIDER_C_API_KEY (or PROVIDER_C_API_KEY_1..N) not set; cannot use edenn_studio model",
                component="music_generation",
                operation="init_provider_c",
            )
        return self.provider_c_music_provider

    def _require_provider_b_music_provider(self) -> ProviderBMusicProvider:
        if self.provider_b_music_provider is None and self._provider_b_music_provider_factory is not None:
            self.provider_b_music_provider = self._provider_b_music_provider_factory()
        if self.provider_b_music_provider is None:
            raise EdennConfigurationError(
                "PROVIDER_B_API_KEY not set; cannot use edenn_enhanced model",
                component="music_generation",
                operation="init_provider_b",
            )
        return self.provider_b_music_provider

    @staticmethod
    def _provider_c_callback_config() -> tuple[Optional[str], Optional[Callable[[str, float], Any]]]:
        try:
            from EdennCode.Deployment.provider_music_callbacks import (
                provider_callback_url,
                should_wait_for_provider_callback,
                wait_for_provider_c_generation_callback,
            )
        except Exception as exc:
            logger.warning("ProviderC callback helpers unavailable; using polling: %s", exc)
            return os.getenv("PROVIDER_C_CALLBACK_URL") or None, None

        callback_url = provider_callback_url("provider_c")
        if not should_wait_for_provider_callback("provider_c", callback_url):
            return callback_url, None

        async def _waiter(task_id: str, timeout_s: float):
            return await wait_for_provider_c_generation_callback(
                task_id,
                timeout_s=timeout_s,
            )

        return callback_url, _waiter

    async def run(self, stage_input: MusicGenerationStageInput) -> MusicGenerationStageOutput:
        logger.info("Starting music generation stage")
        logger.info(f"Music generation stage input: {stage_input}")
        _gen_start = time.time()
        output: MusicGenerationStageOutput
        if stage_input.music_generation_model == MusicGenertionModelEnum.EDENN_STUDIO:
            """
            ProviderC full song Generation
            """
            _provider_start = time.time()
            (
                audio_path,
                _,
                primary_track_lyrics,
                _,
            ) = await self.provider_c_music_generation_workflow(
                stage_input.prompt_metadata,
                stage_input.include_vocals,
                stage_input.vocal_gender,
                video_metadata=stage_input.video_metadata,
                provider_c_custom_mode=stage_input.provider_c_custom_mode,
            )
            _log_provider_timing("provider_c", time.time() - _provider_start, profile="edenn_studio", ts_start=_provider_start)
            audio_path, primary_track_lyrics, _ = self.clean_track_and_lyrics_for_alignment(
                audio_path,
                primary_track_lyrics,
                model_spec=MusicGenertionModelEnum.EDENN_STUDIO,
            )
            timestamp_lyrics = (
                primary_track_lyrics.word_level_lyrics_timestamps
                or primary_track_lyrics.lyrics_timestamps
            )
            music_rerank_stage_input: MusicMatchingStageInput = MusicMatchingStageInput(
                provider_c_music_provider=self.provider_c_music_provider,
                video_metadata=stage_input.video_metadata,
                local_music_path=audio_path,
                timestamp_lyrics=timestamp_lyrics,
                source_track_label="primary",
                require_lyrics=stage_input.include_vocals,
            )
            music_matching_stage_output = await self.music_reranker_stage.run(music_rerank_stage_input)
            music_path_for_remix = music_matching_stage_output.reranked_music_outputs_path

            cleaned = strip_section_tags(
                music_matching_stage_output.aligned_lyrics)
            cleaned_ms = to_ms_wordts(cleaned)

            output = MusicGenerationStageOutput(
                music_path=music_path_for_remix,
                complete_music_path=audio_path,
                secondary_complete_music_path=None,
                lyrics_timestamps=cleaned_ms,
                word_level_lyrics_timestamps=list(cleaned_ms),
                primary_full_lyrics=primary_track_lyrics.full_lyrics,
                # Strip section tags here too — the windowed lists above are
                # stripped, but a raw full-track list leaks tokens like
                # "[Verse]\nSnowflakes" into the response.
                primary_full_lyrics_timestamps=to_ms_wordts(
                    strip_section_tags(primary_track_lyrics.lyrics_timestamps)
                ),
                primary_full_word_level_lyrics_timestamps=to_ms_wordts(
                    strip_section_tags(primary_track_lyrics.word_level_lyrics_timestamps)
                ),
                secondary_full_lyrics=None,
                secondary_full_lyrics_timestamps=[],
                secondary_full_word_level_lyrics_timestamps=[],
                matching_used_track="primary",
                music_start_s=music_matching_stage_output.music_start_s,
                alignment_score=music_matching_stage_output.alignment_score,
                alignment_details=dict(music_matching_stage_output.alignment_details),
                generation_api_call_count=primary_track_lyrics.generation_api_call_count,
            )
        elif stage_input.music_generation_model == MusicGenertionModelEnum.EDENN_ENHANCED:
            provider_b_provider = self._require_provider_b_music_provider()

            # Use style_prompt as prompt; lyrics_prompt for lyrics generation
            style_prompt = stage_input.prompt_metadata.get(
                "style_prompt") or stage_input.prompt_metadata.get("prompt", "")
            lyrics_prompt = stage_input.prompt_metadata.get(
                "lyrics_prompt") or style_prompt
            _provider_start = time.time()
            warning_token = provider_b_provider.begin_warning_collection()
            critical_warning: Optional[str] = None
            try:
                if stage_input.include_vocals:
                    (
                        audio_path,
                        _,
                        primary_track_lyrics,
                        _,
                        vocal_id_used,
                    ) = await self.provider_b_music_generation_workflow(
                        style_prompt,
                        lyrics_prompt,
                        video_metadata=stage_input.video_metadata,
                        vocal_id=stage_input.vocal_id,
                        vocal_sample_path=stage_input.vocal_sample_path,
                    )
                else:
                    if stage_input.vocal_id or stage_input.vocal_sample_path is not None:
                        raise EdennValidationError(
                            "Vocal clone inputs can only be used for vocal ProviderB generation.",
                            component="video_music",
                            operation="music_generation_stage",
                        )
                    (
                        audio_path,
                        _,
                        primary_track_lyrics,
                        _,
                        vocal_id_used,
                    ) = await self.provider_b_instrumental_generation_workflow(
                        style_prompt,
                        video_metadata=stage_input.video_metadata,
                    )
            finally:
                critical_warning = provider_b_provider.finish_warning_collection(warning_token)
            _log_provider_timing("provider_b", time.time() - _provider_start, profile="edenn_enhanced", ts_start=_provider_start)
            audio_path, primary_track_lyrics, _ = self.clean_track_and_lyrics_for_alignment(
                audio_path,
                primary_track_lyrics,
                model_spec=MusicGenertionModelEnum.EDENN_ENHANCED,
            )
            word_level_lyrics = primary_track_lyrics.word_level_lyrics_timestamps
            line_level_lyrics = primary_track_lyrics.lyrics_timestamps
            matching_lyrics = word_level_lyrics or line_level_lyrics
            music_rerank_stage_input: MusicMatchingStageInput = MusicMatchingStageInput(
                provider_c_music_provider=self.provider_c_music_provider,
                video_metadata=stage_input.video_metadata,
                local_music_path=audio_path,
                timestamp_lyrics=matching_lyrics,
                source_track_label="primary",
                require_lyrics=stage_input.include_vocals,
            )
            matching_start = time.time()
            music_matching_stage_output = await self.music_reranker_stage.run(music_rerank_stage_input)
            logger.info(
                "[edenn_enhanced] Music matching took %.2fs",
                time.time() - matching_start,
            )
            aligned_word_level = MusicMatchingStage._offset_word_ts(
                word_level_lyrics,
                music_matching_stage_output.music_start_s,
                stage_input.video_metadata.duration,
            )
            aligned_line_level = align_line_level_lyrics_to_window(
                line_level_lyrics,
                word_level_lyrics,
                music_matching_stage_output.music_start_s,
                stage_input.video_metadata.duration,
            )
            aligned_line_level_ms = to_ms_wordts(
                strip_section_tags(aligned_line_level)
            )
            aligned_word_level_ms = to_ms_wordts(aligned_word_level)
            output = MusicGenerationStageOutput(
                music_path=music_matching_stage_output.reranked_music_outputs_path,
                complete_music_path=audio_path,
                secondary_complete_music_path=None,
                lyrics_timestamps=aligned_line_level_ms,
                word_level_lyrics_timestamps=aligned_word_level_ms,
                primary_full_lyrics=primary_track_lyrics.full_lyrics,
                # Strip section tags here too — the windowed lists above are
                # stripped, but a raw full-track list leaks tokens like
                # "[Verse]\nSnowflakes" into the response.
                primary_full_lyrics_timestamps=to_ms_wordts(
                    strip_section_tags(primary_track_lyrics.lyrics_timestamps)
                ),
                primary_full_word_level_lyrics_timestamps=to_ms_wordts(
                    strip_section_tags(primary_track_lyrics.word_level_lyrics_timestamps)
                ),
                secondary_full_lyrics=None,
                secondary_full_lyrics_timestamps=[],
                secondary_full_word_level_lyrics_timestamps=[],
                matching_used_track="primary",
                vocal_id_used=vocal_id_used,
                music_start_s=music_matching_stage_output.music_start_s,
                alignment_score=music_matching_stage_output.alignment_score,
                alignment_details=dict(music_matching_stage_output.alignment_details),
                critical_warning=critical_warning,
                generation_api_call_count=primary_track_lyrics.generation_api_call_count,
            )
        else:
            """
            ProviderA Music Generation
            """
            _provider_start = time.time()
            local_music_path, word_lyrics = await self.provider_a_music_generation_workflow(
                stage_input.prompt_metadata,
                stage_input.include_vocals,
                stage_input.vocal_gender,
                video_metadata=stage_input.video_metadata,
                lyrics_language=stage_input.lyrics_language,
                output_format=stage_input.audio_output_format,
            )
            _log_provider_timing("provider_a", time.time() - _provider_start, profile="edenn_basic", ts_start=_provider_start)
            output = MusicGenerationStageOutput(
                music_path=local_music_path,
                lyrics_timestamps=to_ms_wordts(word_lyrics),
                word_level_lyrics_timestamps=to_ms_wordts(word_lyrics),
                generation_api_call_count=1,
            )
        output.complete_music_path, primary_full_duration_s = (
            self.prepare_full_track_for_delivery(
                output.complete_music_path,
                model_spec=stage_input.music_generation_model,
                water_mark=stage_input.water_mark,
            )
        )
        output.primary_full_lyrics_timestamps = self.clip_full_track_timestamps_ms(
            output.primary_full_lyrics_timestamps, primary_full_duration_s
        )
        output.primary_full_word_level_lyrics_timestamps = (
            self.clip_full_track_timestamps_ms(
                output.primary_full_word_level_lyrics_timestamps,
                primary_full_duration_s,
            )
        )
        output.secondary_complete_music_path, secondary_full_duration_s = (
            self.prepare_full_track_for_delivery(
                output.secondary_complete_music_path,
                model_spec=stage_input.music_generation_model,
                water_mark=stage_input.water_mark,
            )
        )
        output.secondary_full_lyrics_timestamps = self.clip_full_track_timestamps_ms(
            output.secondary_full_lyrics_timestamps, secondary_full_duration_s
        )
        output.secondary_full_word_level_lyrics_timestamps = (
            self.clip_full_track_timestamps_ms(
                output.secondary_full_word_level_lyrics_timestamps,
                secondary_full_duration_s,
            )
        )
        safe_emit_annotation(
            stage_input.annotation_dispatcher,
            lambda: MusicGenerationEvent(
                job_id=stage_input.job_id,
                model_spec=stage_input.music_generation_model,
                provider_name=MusicGenerationEvent.provider_name_for_spec(stage_input.music_generation_model),
                task_id=output.task_id,
                audio_id=output.audio_id,
                include_vocals=stage_input.include_vocals,
                vocal_gender=stage_input.vocal_gender,
                vocal_id_used=output.vocal_id_used,
                lyrics_language=stage_input.lyrics_language,
                style_prompt=stage_input.prompt_metadata.get("style_prompt"),
                lyrics_prompt=stage_input.prompt_metadata.get("lyrics_prompt"),
                combined_prompt=stage_input.prompt_metadata.get("prompt"),
                music_filename=output.music_path.name if output.music_path else None,
                complete_music_filename=output.complete_music_path.name if output.complete_music_path else None,
                has_lyrics=bool(output.primary_full_lyrics),
                full_lyrics_text=output.primary_full_lyrics,
                line_timestamp_count=len(output.lyrics_timestamps),
                word_timestamp_count=len(output.word_level_lyrics_timestamps),
                matching_used_track=output.matching_used_track,
                video_duration_s=stage_input.video_metadata.duration,
                video_category=stage_input.video_category,
                scene_count=stage_input.scene_count,
                overall_mood=stage_input.overall_mood,
                generation_latency_s=time.time() - _gen_start,
            ),
        )
        return output

    @classmethod
    def _is_tail_trimmed(
        cls,
        audio_path: Path,
        *,
        tail_trim_s: float = ENHANCED_FULL_TRACK_TAIL_TRIM_S,
    ) -> bool:
        """Was the tail already chopped off *this* file?

        Only a trailing marker counts. An extension round names its output
        ``{stem}_extend{n}``, so a track trimmed before extension carries the
        marker mid-stem while its actual tail is whatever the provider just
        appended — matching that would hand the vendor tag straight to the
        watermark concat.
        """

        return audio_path.stem.endswith(cls._tail_trim_marker(tail_trim_s))

    def prepare_full_track_for_delivery(
        self,
        audio_path: Optional[Path],
        *,
        model_spec: Optional[str],
        water_mark: bool,
    ) -> Tuple[Optional[Path], Optional[float]]:
        """Make a full track fit to hand out: provider tail off, our voice on.

        The chop fires only for a provider that actually tags its output, and for
        one of those it is unconditional — unlike the generation-time chop, which
        stands down when the trimmed track would no longer cover the video. That
        stand-down is exactly how a delivered full track keeps the provider's
        trailing tag, and the delivered track is never the thing that has to
        cover the video, so nothing is owed to that length.

        Returns the delivered path plus the music duration it now runs to when
        this call shortened it (``None`` when nothing was cut), so the caller can
        pull full-track lyric timings back inside the audio that ships.
        """

        if audio_path is None:
            return None, None
        trimmed_music_duration_s: Optional[float] = None
        if provider_appends_track_tail(model_spec) and not self._is_tail_trimmed(
            audio_path
        ):
            trimmed_path, trimmed_duration_s = self._trim_enhanced_full_track_tail(
                audio_path
            )
            if trimmed_path != audio_path:
                audio_path = trimmed_path
                trimmed_music_duration_s = trimmed_duration_s
        if not water_mark:
            return audio_path, trimmed_music_duration_s
        return append_voice_watermark_to_audio(audio_path), trimmed_music_duration_s

    def clean_track_for_alignment(
        self,
        audio_path: Path,
        *,
        model_spec: Optional[str],
    ) -> Tuple[Path, Optional[float]]:
        """Hand alignment a track with the provider's trailing tag already gone.

        The matcher is free to pick a window that runs to the very end of what
        it is given, and that window is what the delivered video plays — so a
        tagged track cannot be the input. For a provider that leaves no tag this
        is a no-op, because cutting there would remove music the matcher should
        be free to use.

        For a tagging provider it never stands down for length, unlike the
        generation-time chop: by this point extension has already finished, so
        there is no round left for a longer track to come from, and a window that
        covers the video with the tag in it is not the trade to make.
        """

        if not provider_appends_track_tail(model_spec):
            return audio_path, None
        if self._is_tail_trimmed(audio_path):
            return audio_path, None
        trimmed_path, trimmed_duration_s = self._trim_enhanced_full_track_tail(audio_path)
        if trimmed_path == audio_path:
            return audio_path, None
        return trimmed_path, trimmed_duration_s

    def clip_track_timestamps_s(
        self,
        words: Sequence[Any],
        music_duration_s: Optional[float],
    ) -> List[WordTS]:
        """Clip in-seconds timings to a track the chop just shortened."""

        if music_duration_s is None:
            return list(words or [])
        return self._clip_word_ts_to_duration(words, float(music_duration_s))

    def clean_track_and_lyrics_for_alignment(
        self,
        audio_path: Path,
        lyrics: "FullTrackLyricsData",
        *,
        model_spec: Optional[str],
    ) -> Tuple[Path, "FullTrackLyricsData", Optional[float]]:
        """Clean the track and pull its lyric lists back inside it.

        The same file goes on to be the delivered full track, so timings that
        outlive the chop would describe audio nobody receives.
        """

        cleaned_path, cleaned_duration_s = self.clean_track_for_alignment(
            audio_path, model_spec=model_spec
        )
        if cleaned_duration_s is None:
            return cleaned_path, lyrics, None
        return (
            cleaned_path,
            replace(
                lyrics,
                lyrics_timestamps=self.clip_track_timestamps_s(
                    lyrics.lyrics_timestamps, cleaned_duration_s
                ),
                word_level_lyrics_timestamps=self.clip_track_timestamps_s(
                    lyrics.word_level_lyrics_timestamps, cleaned_duration_s
                ),
            ),
            cleaned_duration_s,
        )

    def clip_full_track_timestamps_ms(
        self,
        words_ms: Sequence[Any],
        music_duration_s: Optional[float],
    ) -> List[WordTS]:
        """Clip already-in-milliseconds full-track timings to a trimmed track."""

        if music_duration_s is None:
            return list(words_ms or [])
        return self._clip_word_ts_to_duration(words_ms, float(music_duration_s) * 1000.0)

    async def provider_c_music_generation_workflow(
        self,
        prompt_metadata,
        include_vocals: bool,
        vocal_gender: str,
        video_metadata: VideoMetadata,
        provider_c_custom_mode: bool = False,
        provider_task_recorder: Optional[Callable[[Dict[str, Any]], Any]] = None,
        resume_task_id: Optional[str] = None,
        resume_key_label: Optional[str] = None,
        resume_base: Optional[str] = None,
    ) -> Tuple[Path, Optional[Path], FullTrackLyricsData, Optional[FullTrackLyricsData]]:
        """
        Mirror the async usage in ProviderCApi._demo:
          - build prompt
          - create generation task
          - poll until tracks are ready
          - download the first track for ranking and an optional second track
        """
        provider_c_provider = self._require_provider_c_music_provider()
        provider_c_callback_url, provider_c_callback_waiter = self._provider_c_callback_config()

        provider_c_vocal_gender = self._to_provider_c_vocal_gender(vocal_gender)
        provider_c_extra = {
            "vocalGender": provider_c_vocal_gender} if include_vocals and provider_c_vocal_gender else None

        generated_lyrics: Optional[str] = None
        if provider_c_custom_mode and include_vocals:
            lyrics_prompt = prompt_metadata.get(
                "lyrics_prompt")
            style_prompt = prompt_metadata.get("style_prompt")
            """
            Generate Lyrics
            """

            if resume_task_id:
                lyrics = str(lyrics_prompt or "")
            else:
                # Env-tunable like the timed-lyrics wait below; the worker's
                # heartbeat-extended lease is the real ceiling, and the old
                # hardcoded 150s deadline failed real generations (30200)
                # whenever the provider ran slow.
                lyrics_generation_timeout_s = float(
                    os.getenv("PROVIDER_C_LYRICS_GENERATION_TIMEOUT_S", "200.0")
                )
                lyrics = await provider_c_provider.generate_lyrics(
                    prompt=lyrics_prompt,
                    callback_url=provider_c_callback_url,
                    timeout_s=lyrics_generation_timeout_s,
                )
            generated_lyrics = lyrics
            gen_params = GenerateParams(
                prompt=lyrics,
                custom_mode=True,
                instrumental=False,
                style=style_prompt,
                callback_url=provider_c_callback_url,
                extra=provider_c_extra,
            )
        else:
            prompt = (
                prompt_metadata.get("prompt")
                or prompt_metadata.get("style_prompt")
                or ""
            )

            prompt = str(prompt)[:500]

            logger.info(f"[ProviderC] Generated prompt: {prompt}")
            gen_params = GenerateParams(
                prompt=prompt,
                custom_mode=False,
                instrumental=not include_vocals,
                callback_url=provider_c_callback_url,
                extra=provider_c_extra,
            )

        # The v2 provider worker polls under a heartbeat-renewed queue lease, so
        # no HTTP ingress timeout applies here; the budget must cover real ProviderC
        # generation time (full V5 songs routinely take 120-300s+ — a 200s cap
        # abandoned a prod generation that finished 8.5s after we gave up).
        poll_timeout_s = float(os.getenv("PROVIDER_C_POLL_TIMEOUT_S", "600.0"))
        poll_s = 10.0

        async def _record_provider_c_generation_task(
            task_id: str, base_used: str, key_label: str = "",
        ) -> None:
            await _notify_provider_task_created(
                provider_task_recorder,
                {
                    "provider_name": "provider_c",
                    "operation": "generate",
                    "role": "primary_generation",
                    "task_id": task_id,
                    "base_used": base_used,
                    "key_label": key_label,
                },
            )

        if resume_task_id:
            task_id = resume_task_id
            # Task ids are account-scoped and the in-memory task->key binding
            # does not survive a process change: rebind from the durably
            # recorded key label (and base) or the resumed poll hits a
            # non-owner account and sees a silent-empty record.
            if hasattr(provider_c_provider, "rebind_task_key"):
                provider_c_provider.rebind_task_key(resume_task_id, resume_key_label)
            if resume_base:
                provider_c_provider.base = resume_base
            generation_result = await provider_c_provider.poll_generation(
                task_id,
                timeout_s=poll_timeout_s,
                poll_s=poll_s,
            )
        elif hasattr(provider_c_provider, "generate_and_wait_tracks"):
            task_id, generation_result, _base_used = await provider_c_provider.generate_and_wait_tracks(
                gen_params,
                timeout_s=poll_timeout_s,
                poll_s=poll_s,
                callback_waiter=provider_c_callback_waiter,
                on_task_created=_record_provider_c_generation_task,
            )
        else:
            task_id, generation_result, _base_used = await provider_c_provider.generate_and_poll_tracks(
                gen_params,
                timeout_s=poll_timeout_s,
                poll_s=poll_s,
                on_task_created=_record_provider_c_generation_task,
            )
        tracks = generation_result.tracks
        if not tracks:
            raise EdennProviderResponseError(f"ProviderC generation completed but returned no tracks: task_id={task_id}", provider_name="provider_c")

        first_track = tracks[0]
        if resume_task_id and include_vocals and (first_track.prompt or "").strip():
            # A resumed custom-mode task skipped lyrics generation; the track's
            # prompt is the lyric text that was actually sung.
            generated_lyrics = first_track.prompt
        _ = next(
            (track for track in tracks[1:] if track.audio_id != first_track.audio_id),
            None,
        )
        dest_dir = Path(video_metadata.temp_folder)
        # ProviderC serves MP3 bytes; the .mp3 name keeps the tail trim and watermark
        # re-encoding as MP3 instead of blowing the full track up to PCM WAV.
        dest_path = dest_dir / "primary.mp3"
        downloaded_path = await provider_c_provider.download(first_track, dest_path)
        logger.info(f"[ProviderC] Downloaded {downloaded_path}")

        # No tail chop here: this provider ends its tracks musically (verified
        # against production audio 2026-08-23 — see PROVIDER_APPENDS_TRACK_TAIL).
        # The chop this line used to do was copied "for parity with enhanced" and
        # only ever deleted the last six seconds of the customer's music, at
        # times cutting off mid-phrase.
        task_id, first_track, downloaded_path, extension_count = await self._extend_provider_c_track_if_needed(
            task_id=task_id,
            track=first_track,
            local_path=downloaded_path,
            video_duration_s=video_metadata.duration,
            dest_dir=dest_dir,
            instrumental=not include_vocals,
            provider_task_recorder=provider_task_recorder,
        )
        trimmed_duration_s = self._duration_seconds(downloaded_path)

        timestamp_lyrics = []
        if include_vocals:
            lyrics_timeout_s = float(os.getenv("PROVIDER_C_LYRICS_TIMEOUT_S", "200.0"))
            timestamp_lyrics = await provider_c_provider.wait_for_timestamped_lyrics(
                task_id,
                first_track.audio_id,
                timeout_s=lyrics_timeout_s,
            )
        # Drop/clamp any lyric words past the trimmed track end (shared clipper
        # with the zero-length guard). Fall back to pass-through only if the
        # trimmed duration is unknown, so a probe failure never drops all lyrics.
        if trimmed_duration_s and trimmed_duration_s > 0:
            clipped_lyrics = self._clip_word_ts_to_duration(timestamp_lyrics, trimmed_duration_s)
        else:
            clipped_lyrics = [
                WordTS(text=w.text, startS=w.startS, endS=w.endS, i=w.i)
                for w in timestamp_lyrics
            ]
        primary_track_lyrics = FullTrackLyricsData(
            full_lyrics=generated_lyrics if include_vocals else None,
            lyrics_timestamps=list(clipped_lyrics),
            word_level_lyrics_timestamps=list(clipped_lyrics),
            generation_api_call_count=1 + extension_count,
        )

        return downloaded_path, None, primary_track_lyrics, None

    @staticmethod
    def _to_provider_c_vocal_gender(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        normalized = value.strip().lower()
        if normalized in {"m", "male", "man", "masculine"}:
            return "m"
        if normalized in {"f", "female", "woman", "feminine"}:
            return "f"
        return None


    async def provider_a_music_generation_workflow(self, prompt_metadata, include_vocals: bool, vocal_gender, video_metadata, lyrics_language: Optional[str] = None, output_format: Optional[str] = None) -> Tuple[Path, List[WordTS]]:
        if _fake_provider_enabled():
            # Performance test: skip the real provider call; matching/remix downstream stay real.
            return await _fake_generated_audio(video_metadata), []
        music_provider = self._require_provider_a_music_provider()
        prompt: str = build_provider_a_music_prompt(
            prompt_metadata,
            include_vocals=include_vocals,
            vocal_gender=vocal_gender,
            lyrics_language=lyrics_language,
        )
        logger.info(f"Generated music prompt: {prompt}")
        music_local_temp_path, lyrics_timestamp = await music_provider.generate(
            prompt,
            music_length_ms=to_eleven_ms(video_metadata.duration),
            with_timestamps=True,
            output_format=output_format,
        )

        return music_local_temp_path, lyrics_timestamp

    async def provider_b_music_generation_workflow(
        self,
        prompt: str,
        lyrics_prompt: str,
        video_metadata: VideoMetadata,
        vocal_id: Optional[str] = None,
        vocal_sample_path: Optional[Path] = None,
        provider_task_recorder: Optional[Callable[[Dict[str, Any]], Any]] = None,
        resume_task_id: Optional[str] = None,
    ) -> Tuple[Path, Optional[Path], FullTrackLyricsData, Optional[FullTrackLyricsData], Optional[str]]:
        """
        Call ProviderB: generate lyrics (if needed) + song, then synthesize WordTS across duration.
        """
        if not lyrics_prompt.strip():
            raise EdennValidationError(
                "ProviderB requires non-empty lyrics_prompt text.",
                component="music_generation",
                operation="validate_provider_b_input",
            )
        normalized_vocal_id = (vocal_id or "").strip() or None
        if normalized_vocal_id and vocal_sample_path is not None:
            raise EdennValidationError(
                "Provide either vocal_id or vocal_sample_path, not both.",
                component="video_music",
                operation="provider_b_music_generation_workflow",
            )
        provider_b_provider = self._require_provider_b_music_provider()
        if vocal_sample_path is not None and not normalized_vocal_id:
            normalized_vocal_id = await provider_b_provider.clone_vocal(vocal_sample_path)

        async def _record_provider_b_generation_task(task_id: str) -> None:
            await _notify_provider_task_created(
                provider_task_recorder,
                {
                    "provider_name": "provider_b",
                    "operation": "generate",
                    "role": "primary_generation",
                    "task_id": task_id,
                },
            )

        (
            audio_path,
            _,
            timestamped_lyrics,
            _,
        ) = await provider_b_provider.generate_with_variants_detailed(
            prompt=prompt,
            lyrics_prompt=lyrics_prompt,
            vocal_id=normalized_vocal_id,
            n=1,
            output_path=None,
            resume_task_id=resume_task_id,
            on_task_created=_record_provider_b_generation_task,
        )
        generated_lyrics = self._read_generated_lyrics(audio_path) or lyrics_prompt
        base_generated_lyrics = generated_lyrics
        # Unconditional: a short chopped track triggers another extension
        # round below instead of keeping the provider's tail for coverage.
        audio_path, trimmed_duration_s = self._trim_enhanced_full_track_tail(audio_path)
        timestamped_lyrics = self._clip_provider_b_timestamps_to_duration(
            timestamped_lyrics,
            trimmed_duration_s,
        )
        audio_path, timestamped_lyrics, generated_lyrics, extension_count = await self._extend_provider_b_track_if_needed(
            audio_path=audio_path,
            prompt=prompt,
            lyrics=generated_lyrics,
            timestamped_lyrics=timestamped_lyrics,
            video_duration_s=video_metadata.duration,
        )
        generated_lyrics = generated_lyrics or self._read_generated_lyrics(audio_path) or base_generated_lyrics
        primary_track_lyrics = FullTrackLyricsData(
            full_lyrics=generated_lyrics,
            lyrics_timestamps=list(timestamped_lyrics.line_level),
            word_level_lyrics_timestamps=list(timestamped_lyrics.word_level),
            generation_api_call_count=1 + extension_count,
        )
        return (
            audio_path,
            None,
            primary_track_lyrics,
            None,
            normalized_vocal_id,
        )

    async def provider_b_instrumental_generation_workflow(
        self,
        prompt: str,
        video_metadata: VideoMetadata,
        provider_task_recorder: Optional[Callable[[Dict[str, Any]], Any]] = None,
        resume_task_id: Optional[str] = None,
    ) -> Tuple[Path, Optional[Path], FullTrackLyricsData, Optional[FullTrackLyricsData], Optional[str]]:
        if not prompt.strip():
            raise EdennValidationError(
                "ProviderB instrumental generation requires non-empty style prompt text.",
                component="music_generation",
                operation="validate_provider_b_instrumental_input",
            )
        provider_b_provider = self._require_provider_b_music_provider()

        async def _record_provider_b_instrumental_task(task_id: str) -> None:
            await _notify_provider_task_created(
                provider_task_recorder,
                {
                    "provider_name": "provider_b",
                    "operation": "generate_instrumental",
                    "role": "primary_generation",
                    "task_id": task_id,
                },
            )

        async def _generate_once() -> Path:
            if resume_task_id:
                finished = await provider_b_provider.wait_instrumental_task(
                    resume_task_id,
                    timeout_s=600.0,
                    poll_s=4.0,
                )
            else:
                task = await provider_b_provider.generate_instrumental_task(
                    prompt=prompt,
                    model=ENHANCED_INSTRUMENTAL_MODEL,
                    n=1,
                )
                await _record_provider_b_instrumental_task(task.task_id)
                finished = await provider_b_provider.wait_instrumental_task(
                    task.task_id,
                    timeout_s=600.0,
                    poll_s=4.0,
                )

            audio_url = extract_audio_url(finished.raw or {})
            if not audio_url:
                raise EdennProviderResponseError(
                    "ProviderB instrumental task completed but no audio URL was found",
                    provider_name="provider_b",
                    operation="provider_b_instrumental_generation_workflow",
                    context={
                        "task_id": finished.task_id,
                        "response": finished.raw,
                    },
                )

            dest_dir = Path(video_metadata.temp_folder)
            dest_dir.mkdir(parents=True, exist_ok=True)
            audio_path = dest_dir / "instrumental.mp3"
            await provider_b_provider.download_audio(audio_url, audio_path)
            return audio_path

        if hasattr(provider_b_provider, "_run_with_cycle_failover"):
            audio_path = await provider_b_provider._run_with_cycle_failover(
                operation="provider_b_instrumental_generation_workflow",
                required_key=None,
                body=_generate_once,
            )
        else:
            audio_path = await _generate_once()

        # Unconditional even though instrumentals cannot extend (the provider's
        # extend endpoint requires lyrics): a track shorter than the video is a
        # coverage gap the mux pads with silence, while a kept tail is the
        # provider's tag in the deliverable.
        audio_path, _ = self._trim_enhanced_full_track_tail(audio_path)
        return (
            audio_path,
            None,
            FullTrackLyricsData(),
            None,
            None,
        )

    @staticmethod
    def _duration_seconds(path: Path) -> float:
        return MediaTools.duration_seconds(str(path))

    @staticmethod
    def _tail_trim_marker(tail_trim_s: float) -> str:
        label = ("%g" % tail_trim_s).replace(".", "p")
        return f"_trimmed_tail{label}s"

    @classmethod
    def _trimmed_tail_path(cls, path: Path, *, tail_trim_s: float) -> Path:
        suffix = path.suffix or ".wav"
        return path.with_name(f"{path.stem}{cls._tail_trim_marker(tail_trim_s)}{suffix}")

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

    @staticmethod
    def _copy_lyrics_sidecar(source_path: Path, target_path: Path) -> None:
        source_lyrics = source_path.with_suffix(".lyrics.txt")
        if not source_lyrics.exists():
            return
        try:
            target_path.with_suffix(".lyrics.txt").write_text(
                source_lyrics.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(
                "Could not copy generated lyrics sidecar from %s to %s: %s",
                source_lyrics,
                target_path.with_suffix(".lyrics.txt"),
                exc,
            )

    def _trim_enhanced_full_track_tail(
        self,
        audio_path: Path,
        *,
        tail_trim_s: float = ENHANCED_FULL_TRACK_TAIL_TRIM_S,
        minimum_required_duration_s: float | None = None,
    ) -> tuple[Path, float]:
        if tail_trim_s <= 0:
            return audio_path, self._duration_seconds(audio_path)

        duration_s = self._duration_seconds(audio_path)
        trimmed_duration_s = duration_s - float(tail_trim_s)
        if (
            minimum_required_duration_s is not None
            and trimmed_duration_s + self.extension_tolerance_s
            < float(minimum_required_duration_s)
        ):
            logger.warning(
                "[edenn_enhanced] Skipping %.2fs tail trim for %s because trimmed duration %.2fs "
                "would be shorter than required duration %.2fs.",
                tail_trim_s,
                audio_path,
                trimmed_duration_s,
                float(minimum_required_duration_s),
            )
            return audio_path, duration_s
        if trimmed_duration_s <= MIN_TRIMMED_AUDIO_DURATION_S:
            logger.warning(
                "[edenn_enhanced] Skipping %.2fs tail trim for %s because duration %.2fs is too short.",
                tail_trim_s,
                audio_path,
                duration_s,
            )
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
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            raise EdennProviderResponseError(
                "Failed to remove the final tail from the ProviderB enhanced full track.",
                provider_name="provider_b",
                operation="trim_edenn_enhanced_full_track_tail",
                context={
                    "audio_path": str(audio_path),
                    "output_path": str(output_path),
                    "duration_s": duration_s,
                    "tail_trim_s": tail_trim_s,
                },
                cause=exc,
            ) from exc

        actual_duration_s = self._duration_seconds(output_path)
        self._copy_lyrics_sidecar(audio_path, output_path)
        logger.info(
            "[edenn_enhanced] Trimmed %.2fs tail from full track: %.2fs -> %.2fs (%s)",
            tail_trim_s,
            duration_s,
            actual_duration_s,
            output_path,
        )
        return output_path, actual_duration_s

    @staticmethod
    def _clip_word_ts_to_duration(words: Sequence[Any], duration_s: float) -> List[WordTS]:
        # ``words`` is duck-typed (any object with startS/endS/text/i) so this
        # serves both ProviderB line/word timestamps and ProviderC's provider words.
        clipped: List[WordTS] = []
        duration_s = max(0.0, float(duration_s))
        for word in words or []:
            try:
                start_s = float(getattr(word, "startS", 0.0))
                end_s = float(getattr(word, "endS", 0.0))
            except (TypeError, ValueError):
                continue
            if start_s >= duration_s:
                continue
            clipped_end_s = min(end_s, duration_s)
            if clipped_end_s <= start_s:
                continue
            clipped.append(
                WordTS(
                    text=getattr(word, "text", ""),
                    startS=start_s,
                    endS=clipped_end_s,
                    i=getattr(word, "i", None),
                )
            )
        return clipped

    def _clip_provider_b_timestamps_to_duration(
        self,
        timestamped_lyrics: ProviderBTimestampedLyrics,
        duration_s: float,
    ) -> ProviderBTimestampedLyrics:
        return ProviderBTimestampedLyrics(
            line_level=self._clip_word_ts_to_duration(
                timestamped_lyrics.line_level,
                duration_s,
            ),
            word_level=self._clip_word_ts_to_duration(
                timestamped_lyrics.word_level,
                duration_s,
            ),
        )

    def _needs_extension(self, music_path: Path, *, video_duration_s: float) -> bool:
        try:
            music_duration_s = self._duration_seconds(music_path)
        except Exception as exc:
            logger.warning("Could not measure audio duration for %s: %s", music_path, exc)
            return False
        return music_duration_s + self.extension_tolerance_s < float(video_duration_s)

    def _raise_if_track_still_short(
        self,
        *,
        provider_name: str,
        operation: str,
        music_path: Path,
        video_duration_s: float,
        attempted_rounds: int,
    ) -> None:
        try:
            music_duration_s = self._duration_seconds(music_path)
        except Exception as exc:
            raise EdennProviderResponseError(
                f"{provider_name} extension completed but the final audio duration could not be measured.",
                provider_name=provider_name,
                operation=operation,
                context={
                    "music_path": str(music_path),
                    "video_duration_s": float(video_duration_s),
                    "attempted_extension_rounds": attempted_rounds,
                },
                cause=exc,
            ) from exc

        if music_duration_s + self.extension_tolerance_s >= float(video_duration_s):
            return

        raise EdennProviderResponseError(
            (
                f"{provider_name} extension completed but the final audio is still shorter than "
                f"the video ({music_duration_s:.2f}s audio vs {float(video_duration_s):.2f}s video)."
            ),
            provider_name=provider_name,
            operation=operation,
            context={
                "music_path": str(music_path),
                "music_duration_s": music_duration_s,
                "video_duration_s": float(video_duration_s),
                "attempted_extension_rounds": attempted_rounds,
                "extension_tolerance_s": self.extension_tolerance_s,
            },
        )

    @staticmethod
    def _read_generated_lyrics(audio_path: Path) -> str:
        lyrics_path = audio_path.with_suffix(".lyrics.txt")
        try:
            return lyrics_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    async def _extend_provider_c_track_if_needed(
        self,
        *,
        task_id: str,
        track: ProviderCTrack,
        local_path: Path,
        video_duration_s: float,
        dest_dir: Path,
        instrumental: bool,
        provider_task_recorder: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> Tuple[str, ProviderCTrack, Path, int]:
        """Grow a studio track until it covers the video.

        ``instrumental`` has to be restated on every round: the provider does not
        inherit it from the source track, so an unqualified extension of an
        instrumental comes back singing.
        """

        provider_c_provider = self._require_provider_c_music_provider()
        provider_c_callback_url, provider_c_callback_waiter = self._provider_c_callback_config()
        current_task_id = task_id
        current_track = track
        current_path = local_path
        extension_count = 0

        for round_idx in range(self.max_extension_rounds):
            if not self._needs_extension(current_path, video_duration_s=video_duration_s):
                return current_task_id, current_track, current_path, extension_count

            current_duration_s = self._duration_seconds(current_path)
            logger.info(
                "[ProviderC] Track %.2fs is shorter than video %.2fs. Extending (round %d/%d).",
                current_duration_s,
                video_duration_s,
                round_idx + 1,
                self.max_extension_rounds,
            )
            async def _record_provider_c_extension_task(
                extend_task_id: str, base_used: str, key_label: str = "",
            ) -> None:
                await _notify_provider_task_created(
                    provider_task_recorder,
                    {
                        "provider_name": "provider_c",
                        "operation": "extend",
                        "role": f"extension_{round_idx + 1}",
                        "task_id": extend_task_id,
                        "source_audio_id": current_track.audio_id,
                        "base_used": base_used,
                        "key_label": key_label,
                    },
                )

            # Same rationale as the generation budget: an extend that times out
            # is already paid for, and a stage retry re-submits it (extension
            # tasks are not resumed), so starving the poll here double-spends.
            extend_timeout_s = float(
                os.getenv("PROVIDER_C_EXTEND_TIMEOUT_S",
                          os.getenv("PROVIDER_C_POLL_TIMEOUT_S", "600.0")))
            if hasattr(provider_c_provider, "extend_and_wait_track"):
                current_task_id, current_track, _ = await provider_c_provider.extend_and_wait_track(
                    current_track.audio_id,
                    timeout_s=extend_timeout_s,
                    poll_s=10.0,
                    callback_url=provider_c_callback_url,
                    callback_waiter=provider_c_callback_waiter,
                    on_task_created=_record_provider_c_extension_task,
                    instrumental=instrumental,
                )
            else:
                current_task_id, current_track, _ = await provider_c_provider.extend_and_poll_track(
                    current_track.audio_id,
                    timeout_s=extend_timeout_s,
                    poll_s=10.0,
                    on_task_created=_record_provider_c_extension_task,
                    instrumental=instrumental,
                )
            next_path = dest_dir / "primary.mp3"
            current_path = await provider_c_provider.download(current_track, next_path)
            # No re-chop: this provider does not tag its tracks, so each
            # extension round keeps every second it produced.
            extension_count += 1
        self._raise_if_track_still_short(
            provider_name="provider_c",
            operation="extend_provider_c_track_to_video_duration",
            music_path=current_path,
            video_duration_s=video_duration_s,
            attempted_rounds=self.max_extension_rounds,
        )
        return current_task_id, current_track, current_path, extension_count

    async def _extend_provider_b_track_if_needed(
        self,
        *,
        audio_path: Path,
        prompt: str,
        lyrics: str,
        timestamped_lyrics: ProviderBTimestampedLyrics,
        video_duration_s: float,
    ) -> Tuple[Path, ProviderBTimestampedLyrics, str, int]:
        provider_b_provider = self.provider_b_music_provider
        if not provider_b_provider:
            return audio_path, timestamped_lyrics, lyrics, 0

        current_path = audio_path
        current_timestamps = timestamped_lyrics
        current_lyrics = lyrics
        extension_count = 0

        for round_idx in range(self.max_extension_rounds):
            if not self._needs_extension(current_path, video_duration_s=video_duration_s):
                return current_path, current_timestamps, current_lyrics, extension_count

            current_duration_s = self._duration_seconds(current_path)
            logger.info(
                "[edenn_enhanced] Track %.2fs is shorter than video %.2fs. Extending (round %d/%d).",
                current_duration_s,
                video_duration_s,
                round_idx + 1,
                self.max_extension_rounds,
            )
            next_path = current_path.with_name(
                f"{current_path.stem}_extend{round_idx + 1}{current_path.suffix}"
            )
            # The provider clamps extend_at up to its own floor; pointing it
            # past the end of a very short seed is worse than letting it pick
            # the natural extension point.
            extend_at_ms = round(current_duration_s * 1000)
            (
                next_path,
                next_timestamps,
                current_lyrics,
            ) = await provider_b_provider.extend_song_from_audio_detailed(
                audio_path=current_path,
                prompt=prompt,
                lyrics=current_lyrics,
                output_path=next_path,
                extend_type="tail",
                extend_at_ms=extend_at_ms if extend_at_ms >= 8000 else None,
            )
            # Unconditional, same reason as the pre-loop chop: the loop exit
            # reads the chopped length.
            current_path, trimmed_duration_s = self._trim_enhanced_full_track_tail(next_path)
            current_timestamps = self._clip_provider_b_timestamps_to_duration(
                next_timestamps,
                trimmed_duration_s,
            )
            extension_count += 1
            # Each round costs a paid generation; a round that gains nothing
            # over the chopped seed will not do better next time, so fail now
            # instead of burning the remaining budget.
            if trimmed_duration_s <= current_duration_s + 0.05:
                logger.warning(
                    "[edenn_enhanced] Extension round %d made no progress "
                    "(%.2fs -> %.2fs after the tail chop); stopping early.",
                    round_idx + 1,
                    current_duration_s,
                    trimmed_duration_s,
                )
                break
        self._raise_if_track_still_short(
            provider_name="provider_b",
            operation="extend_provider_b_track_to_video_duration",
            music_path=current_path,
            video_duration_s=video_duration_s,
            attempted_rounds=self.max_extension_rounds,
        )
        return current_path, current_timestamps, current_lyrics, extension_count
