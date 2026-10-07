from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Dict, Optional, List, Sequence, TypeVar, Union

if __package__ in {None, ""}:
    repo_root = Path(__file__).resolve().parents[5]
    if str(repo_root) not in sys.path:
        sys.path.append(str(repo_root))

from EdennCode.MusicGenerationCore import SectionTiming, TimestampedWord
from EdennCode.MusicGenerationCore.provider_registry import build_default_music_generation_service
from EdennCode.ModelFactory.PromptFactory.prompts import Prompt
from EdennCode.Util.MediaUtils import extract_audio_window, get_video_duration
from EdennCode.Util.MediaUtils.pipeline_util import build_azure_client
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.UserIntentUnderstandingStage.user_prompt_preprocessor import (
    UserPromptPreprocessorAgent,
)
from EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.BeatAlignmentStage.beat_alignment_stage import (
    BeatAlignmentStage,
    BeatAlignmentStageInput,
    BeatAlignmentStageOutput,
)
from EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.ImageSequencePlanningStage.image_sequence_planning_stage import (
    ImageSequencePlanningStage,
    ImageSequencePlanningStageInput,
    ImageSequencePlanningStageOutput,
)
from EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.MusicGenerationStage.music_generation_stage import (
    MultiImageMusicGenerationStage,
    MultiImageMusicGenerationStageInput,
    MultiImageMusicGenerationStageOutput,
)
from EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.PreprocessStage.preprocess_stage import (
    PreprocessStage,
    PreprocessStageInput,
    PreprocessStageOutput,
)
from EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.SlideshowAssemblyStage.slideshow_assembly_stage import (
    SlideshowAssemblyStage,
    SlideshowAssemblyStageInput,
    SlideshowAssemblyStageOutput,
)
from EdennCode.exceptions import EdennProviderTimeoutError, EdennValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Default per-stage time budgets. The LLM planning calls should resolve within a
# couple of minutes; music generation legitimately runs longer, so it mirrors the
# 600s provider task budget used elsewhere. Setting the env var to ``0`` (or a
# negative value) disables the timeout for that stage.
_DEFAULT_PLANNING_TIMEOUT_S = 180.0
_DEFAULT_MUSIC_TIMEOUT_S = 600.0
_PLANNING_TIMEOUT_ENV = "MULTI_IMAGE_PLANNING_TIMEOUT_S"
_MUSIC_TIMEOUT_ENV = "MULTI_IMAGE_MUSIC_TIMEOUT_S"


def _timeout_seconds(env_var: str, default: float) -> Optional[float]:
    """Resolve a per-stage timeout from the environment.

    Returns ``None`` (no timeout) when the value is non-positive so operators can
    opt out without code changes.
    """

    raw = os.getenv(env_var)
    if raw is None or not raw.strip():
        return default if default > 0 else None
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r; falling back to default %.1fs.", env_var, raw, default
        )
        return default if default > 0 else None
    return value if value > 0 else None


async def _run_stage(
    name: str,
    awaitable: Awaitable[T],
    *,
    timeout_s: Optional[float] = None,
    provider_name: Optional[str] = None,
) -> T:
    """Await a stage with timing telemetry and an optional timeout.

    Emits structured start/finish logs so per-stage latency is observable in
    production, and converts a timeout into a typed Edenn error that maps to a
    retryable provider-timeout response.
    """

    start = time.perf_counter()
    logger.info("multi_image stage '%s' started (timeout=%s)", name, timeout_s)
    try:
        if timeout_s is not None:
            result = await asyncio.wait_for(awaitable, timeout=timeout_s)
        else:
            result = await awaitable
    except asyncio.TimeoutError as exc:
        elapsed = time.perf_counter() - start
        logger.error(
            "multi_image stage '%s' timed out after %.1fs (limit %.1fs).",
            name,
            elapsed,
            timeout_s,
        )
        raise EdennProviderTimeoutError(
            f"Multi-image stage '{name}' exceeded its {timeout_s:.0f}s time budget.",
            provider_name=provider_name,
            component="multi_image",
            operation=name,
        ) from exc
    elapsed = time.perf_counter() - start
    logger.info("multi_image stage '%s' completed in %.2fs.", name, elapsed)
    return result


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _choose_music_start_s(
    *,
    video_duration_s: float,
    track_duration_s: float,
    lyrics_timestamps: List[TimestampedWord],
    section_timeline: List[SectionTiming],
    lead_in_s: float = 1.5,
    min_margin_s: float = 0.5,
) -> float:
    """Pick where in the full track the short slideshow's audio window should start.

    For vocal tracks the raw intro is often several seconds of dead air before the
    first sung word, so a short slideshow anchored at t=0 lands entirely on the
    intro. This slides a ``video_duration_s`` window across the track, keeps the
    candidates with a good vocal ratio (fraction of the window that is actually
    sung), and among those picks the one whose start best matches a musical
    section boundary — a clean phrase entry. Instrumental tracks (no lyric
    timestamps) keep the natural start.
    """

    if track_duration_s <= video_duration_s + min_margin_s:
        return 0.0
    max_start = max(0.0, track_duration_s - video_duration_s)

    onsets = sorted(
        float(w.startS)
        for w in lyrics_timestamps
        if getattr(w, "startS", None) is not None and getattr(w, "endS", None) is not None
    )
    if not onsets:
        # Instrumental / no vocals: nothing to anchor to — keep the natural start.
        return 0.0

    # The "matching" anchor: open the window a short lead-in before the first sung
    # word so the slideshow lands on the song's vocal entry, not the dead intro.
    lead_anchor = _clamp(onsets[0] - lead_in_s, 0.0, max_start)

    # Candidate window starts: the lead-in anchor, each vocal onset (minus lead-in),
    # each section boundary, and a coarse grid as a backstop.
    section_starts = [
        float(t.actual_start_s if t.actual_start_s is not None else t.expected_start_s)
        for t in section_timeline
        if (t.actual_start_s is not None or t.expected_start_s is not None)
    ]
    candidates: set[float] = {lead_anchor}
    for onset in onsets:
        candidates.add(_clamp(onset - lead_in_s, 0.0, max_start))
    for start in section_starts:
        candidates.add(_clamp(start, 0.0, max_start))
    grid_step = max(1.0, video_duration_s / 4.0)
    grid = 0.0
    while grid <= max_start:
        candidates.add(round(grid, 3))
        grid += grid_step
    candidates.add(round(max_start, 3))

    def vocal_ratio(start: float) -> float:
        end = start + video_duration_s
        covered = 0.0
        for w in lyrics_timestamps:
            a = max(start, float(w.startS))
            b = min(end, float(w.endS))
            if b > a:
                covered += b - a
        return covered / video_duration_s if video_duration_s > 0 else 0.0

    ratios = {start: vocal_ratio(start) for start in candidates}
    best_ratio = max(ratios.values())
    if best_ratio <= 0.0:
        # No window contains vocals (short/edge track): anchor at the vocal entry.
        return lead_anchor

    # "Good vocal ratio" = within reach of the best window, with a sane floor.
    threshold = max(0.6 * best_ratio, 0.15)
    good = [start for start, ratio in ratios.items() if ratio >= threshold]
    if not good:
        # Lyrics are sparse enough that no window clears the bar; keep the windows
        # that capture the most singing (the distance tie-break below then anchors
        # them to the vocal entry).
        good = [start for start, ratio in ratios.items() if ratio >= best_ratio - 1e-9]
    # Among the vocal-rich windows, "best matching" = the one nearest the song's
    # vocal entry, so the slideshow opens on the singing rather than a later slice.
    return min(good, key=lambda start: (abs(start - lead_anchor), start))


def build_music_generation_service():
    return build_default_music_generation_service()


def _default_cli_folder() -> Path:
    repo_root = Path(__file__).resolve().parents[5]
    candidates = [
        repo_root / "EdennCode" / "TestSuites" / "assets" / "smoke" / "images" / "multi_image_design",
        repo_root / "EdennCode" / "WorkflowExamples" / "multi_image_workflow" / "multi_image_design",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _default_cli_output() -> Path:
    repo_root = Path(__file__).resolve().parents[5]
    return repo_root / "Workflow_Outputs_Local" / "multi_image_story.mp4"


@dataclass
class MultiImageWorkflowStageInput:
    folder_path: Path
    output_path: Path
    user_prompt: str = ""
    include_vocals: bool = False
    vocal_gender: Optional[str] = None
    align_to_beats: Optional[bool] = None
    lyrics_language: Optional[str] = None
    modelspec: str = "edenn_basic"
    vocal_id: Optional[str] = None
    vocal_sample_path: Optional[Path] = None
    water_mark: bool = False
    audio_output_format: Optional[str] = None
    user_lyrics_prompt: Optional[str] = None
    # "none" | "random" | any SUPPORTED_TRANSITIONS name (see SlideshowAssemblyStage).
    # An explicit per-boundary ``transitions`` list, when set, overrides the mode.
    transition_mode: str = "none"
    transitions: Optional[Sequence[str]] = None
    # A single blend length for every boundary, or one length per boundary.
    transition_duration_s: "float | Sequence[float]" = 0.4
    transition_seed: Optional[int] = None
    # When True the user's upload order is authoritative; planning may not
    # reorder the images (it still plans narrative/music FOR that order).
    fixed_image_order: bool = False
    # Explicit per-image display seconds, one per image in delivery order. When
    # set the slideshow uses exactly these durations and beat alignment is
    # skipped (the caller's timing is authoritative). Only meaningful with
    # fixed_image_order (the list is index-aligned to the images).
    per_image_durations: Optional[Sequence[float]] = None


@dataclass
class MultiImageWorkflowStageOutput:
    final_video_path: Path
    silent_video_path: Path
    music_path: Path
    matched_music_path: Path
    full_track_paths: List[Path]
    processed_image_paths: List[Path]
    planning_metadata: Dict[str, object]
    music_prompt: str
    lyrics_timestamps: List[TimestampedWord]
    used_modelspec: str
    section_timeline: List[SectionTiming]
    video_title: str
    music_title: str
    video_description: str
    include_vocals: bool
    vocal_gender: str
    lyrics_language: str
    user_requested_language: str
    compression_applied: bool
    vocal_id_used: Optional[str] = None
    line_level_lyrics_timestamps: List[TimestampedWord] = field(default_factory=list)
    full_lyrics: Optional[str] = None
    token_usage: Optional[Dict[str, Any]] = None
    token_usage_breakdown: Optional[Dict[str, Any]] = None
    music_start_s: float = 0.0


class MultiImageGenerationE2EStage:
    """Connect preprocessing, planning, music generation, and assembly into one stage."""

    def __init__(
        self,
        *,

        per_image_duration: float = 3.0,
        music_volume: float = 1.0,
        align_to_beats: bool = True,
        plan_cache: Optional[object] = None,
    ) -> None:
        if per_image_duration <= 0:
            raise ValueError("per_image_duration must be positive")

        self.azure_client = build_azure_client()
        self.music_generation_service = build_music_generation_service()
        self.prompt_preprocessor = UserPromptPreprocessorAgent(
            llm_client=self.azure_client
        )

        self.per_image_duration = per_image_duration
        self.music_volume = music_volume
        self.align_to_beats = align_to_beats
        self.planning_timeout_s = _timeout_seconds(
            _PLANNING_TIMEOUT_ENV, _DEFAULT_PLANNING_TIMEOUT_S
        )
        self.music_timeout_s = _timeout_seconds(
            _MUSIC_TIMEOUT_ENV, _DEFAULT_MUSIC_TIMEOUT_S
        )

        self.preprocess_stage = PreprocessStage()
        self.planning_stage = ImageSequencePlanningStage(
            llm_client=self.azure_client, plan_cache=plan_cache)
        self.music_stage = MultiImageMusicGenerationStage(
            music_generation_service=self.music_generation_service,
        )
        self.beat_stage = BeatAlignmentStage()
        self.assembly_stage = SlideshowAssemblyStage()

    async def run(self, stage_input: MultiImageWorkflowStageInput) -> MultiImageWorkflowStageOutput:
        run_started = time.perf_counter()
        align_to_beats = self.align_to_beats if stage_input.align_to_beats is None else stage_input.align_to_beats
        logger.info(
            "multi_image workflow started (modelspec=%s, align_to_beats=%s, folder=%s).",
            stage_input.modelspec,
            align_to_beats,
            stage_input.folder_path,
        )

        transformed_prompt = (stage_input.user_prompt or "").strip()
        detected_language = "ENGLISH_US"
        detected_vocal_language = ""
        detected_vocal_gender = "unknown"
        inferred_include_vocals = False
        if transformed_prompt:
            preprocess_result = await _run_stage(
                "prompt_preprocess",
                self.prompt_preprocessor.preprocess(transformed_prompt),
                timeout_s=self.planning_timeout_s,
                provider_name=None,
            )
            transformed_prompt = preprocess_result.transformed_prompt or transformed_prompt
            detected_language = preprocess_result.detected_language or detected_language
            detected_vocal_language = preprocess_result.detected_vocal_language or ""
            detected_vocal_gender = preprocess_result.detected_vocal_gender or "unknown"
            inferred_include_vocals = bool(preprocess_result.detected_include_vocals)

        # An explicit lyric direction is itself a request for vocals, so it turns
        # them on even when neither the (now always-default) include_vocals flag
        # nor the prompt inference did.
        has_user_lyrics_prompt = bool((stage_input.user_lyrics_prompt or "").strip())
        effective_include_vocals = bool(
            stage_input.include_vocals or inferred_include_vocals or has_user_lyrics_prompt
        )
        effective_vocal_gender = (stage_input.vocal_gender or "").strip().lower()
        if not effective_vocal_gender and effective_include_vocals and detected_vocal_gender in {"male", "female"}:
            effective_vocal_gender = detected_vocal_gender
        if not effective_vocal_gender:
            effective_vocal_gender = "female"

        effective_lyrics_language = (stage_input.lyrics_language or "").strip()
        if effective_include_vocals and not effective_lyrics_language:
            effective_lyrics_language = detected_vocal_language or detected_language
        if (stage_input.vocal_id or stage_input.vocal_sample_path) and not effective_include_vocals:
            raise EdennValidationError(
                "Vocal clone inputs require a vocal generation request.",
                public_message="Vocal clone inputs can only be used for vocal edenn_enhanced multi-image generation.",
                component="multi_image",
                operation="run",
            )

        preferred_output_language = Prompt._normalize_language_name(detected_language)
        preferred_lyric_language = (
            effective_lyrics_language or detected_vocal_language or detected_language
        )

        preprocess_input = PreprocessStageInput(
            folder_path=stage_input.folder_path,
            output_dir=stage_input.output_path.parent / "preprocessed_images",
        )
        # Image decode/compression (OpenCV) is CPU-bound and blocking; run it off
        # the event loop so concurrent jobs on the same worker are not stalled.
        preprocess_output: PreprocessStageOutput = await _run_stage(
            "preprocess",
            asyncio.to_thread(self.preprocess_stage.run, preprocess_input),
        )

        # A user lyric direction is only meaningful when vocals are generated —
        # and a non-empty one already forced effective_include_vocals True above.
        effective_user_lyrics_prompt = (
            (stage_input.user_lyrics_prompt or "").strip() if effective_include_vocals else ""
        )
        if effective_user_lyrics_prompt:
            # The lyric direction gets the same sanitization pass as the video
            # pipeline's lyrics_prompt before any LLM sees it. Sanitization
            # failure falls back to the raw direction rather than failing a
            # paid job.
            try:
                lyrics_preprocess_result = await _run_stage(
                    "lyrics_prompt_preprocess",
                    self.prompt_preprocessor.preprocess_lyrics_prompt(
                        effective_user_lyrics_prompt
                    ),
                    timeout_s=self.planning_timeout_s,
                    provider_name=None,
                )
                effective_user_lyrics_prompt = (
                    (lyrics_preprocess_result.transformed_prompt or "").strip()
                    or effective_user_lyrics_prompt
                )
            except Exception:
                logger.warning(
                    "Lyric direction sanitization failed; using the raw direction.",
                    exc_info=True,
                )
        planning_input = ImageSequencePlanningStageInput(
            image_paths=preprocess_output.preprocessed_images,
            default_image_duration_s=self.per_image_duration,
            user_prompt=transformed_prompt,
            preferred_output_language=preferred_output_language,
            include_vocals=effective_include_vocals,
            preferred_lyric_language=preferred_lyric_language,
            user_lyrics_prompt=effective_user_lyrics_prompt,
            fixed_image_order=stage_input.fixed_image_order,
        )
        planning_output: ImageSequencePlanningStageOutput = await _run_stage(
            "image_sequence_planning",
            self.planning_stage.run(planning_input),
            timeout_s=self.planning_timeout_s,
            provider_name="model_gateway",
        )
        # Explicit per-image durations (fixed-order timing) take precedence over
        # the uniform per_image_duration and disable beat alignment so the
        # caller's seconds are honored exactly. The list is validated at submit
        # to match the image count.
        explicit_durations: Optional[List[float]] = None
        if stage_input.per_image_durations:
            explicit_durations = [float(v) for v in stage_input.per_image_durations]
            if len(explicit_durations) != len(planning_output.ordered_images):
                raise ValueError(
                    "per_image_durations length "
                    f"({len(explicit_durations)}) must match the image count "
                    f"({len(planning_output.ordered_images)})."
                )
            align_to_beats = False

        if explicit_durations is not None:
            total_duration = sum(explicit_durations)
        else:
            total_duration = len(planning_output.ordered_images) * \
                self.per_image_duration

        music_input = MultiImageMusicGenerationStageInput(
            plan_metadata=planning_output.plan,
            section_plan=planning_output.section_plan,
            total_duration=total_duration,
            output_dir=stage_input.output_path.parent / "audio",
            include_vocals=effective_include_vocals,
            vocal_gender=effective_vocal_gender,
            lyrics_language=effective_lyrics_language or None,
            modelspec=stage_input.modelspec,
            vocal_id=stage_input.vocal_id,
            vocal_sample_path=stage_input.vocal_sample_path,
            water_mark=stage_input.water_mark,
            audio_output_format=(stage_input.audio_output_format or "").strip() or None,
        )
        music_output: MultiImageMusicGenerationStageOutput = await _run_stage(
            "music_generation",
            self.music_stage.run(music_input),
            timeout_s=self.music_timeout_s,
            provider_name=stage_input.modelspec,
        )

        # The generated track is usually far longer than the slideshow. Pick the
        # audio window (start offset) that best captures the sung content instead
        # of muxing the raw intro. Instrumental tracks resolve to 0.0.
        music_start_s = _choose_music_start_s(
            video_duration_s=total_duration,
            track_duration_s=float(getattr(music_output, "music_duration_s", 0.0) or 0.0),
            lyrics_timestamps=music_output.lyrics_timestamps,
            section_timeline=music_output.section_timeline,
        )
        logger.info(
            "multi_image audio window selected: start=%.2fs (track=%.2fs, window=%.2fs).",
            music_start_s,
            float(getattr(music_output, "music_duration_s", 0.0) or 0.0),
            total_duration,
        )

        beat_output: Optional[BeatAlignmentStageOutput] = None
        if align_to_beats:
            # Beats are detected across the full track, so express the section
            # boundaries as absolute times inside the chosen [start, start+window]
            # slice — this aligns image cuts to the beats actually heard, and keeps
            # per-section durations in a sane range (the old full-track boundaries
            # spanned the whole song and always fell back to uniform).
            section_image_counts = planning_output.section_plan.section_image_counts()
            image_count = len(planning_output.ordered_images)
            per_image = total_duration / image_count if image_count else self.per_image_duration
            section_boundaries = [music_start_s]
            cursor = music_start_s
            for count in section_image_counts:
                cursor += count * per_image
                section_boundaries.append(cursor)
            # Guard against image-count rounding drift so the last boundary lands
            # exactly at the window end.
            section_boundaries[-1] = music_start_s + total_duration
            beat_input = BeatAlignmentStageInput(
                music_path=music_output.music_path,
                image_count=image_count,
                fallback_duration=self.per_image_duration,
                section_boundaries_s=section_boundaries,
                section_image_counts=section_image_counts,
            )
            beat_output = await _run_stage(
                "beat_alignment",
                self.beat_stage.run(beat_input),
            )

        assembly_input = SlideshowAssemblyStageInput(
            image_paths=planning_output.ordered_images,
            per_image_duration=self.per_image_duration,
            music_path=music_output.music_path,
            output_path=stage_input.output_path,
            music_volume=self.music_volume,
            # Explicit caller durations win; else beat-aligned; else uniform.
            durations=(
                explicit_durations
                if explicit_durations is not None
                else (beat_output.durations if beat_output else None)
            ),
            music_start_s=music_start_s,
            transition_mode=stage_input.transition_mode,
            transitions=stage_input.transitions,
            transition_duration_s=stage_input.transition_duration_s,
            transition_seed=stage_input.transition_seed,
        )
        # FFmpeg slideshow build + audio mux is CPU/IO-bound and blocking; keep it
        # off the event loop so the worker stays responsive during final assembly.
        assembly_output: SlideshowAssemblyStageOutput = await _run_stage(
            "slideshow_assembly",
            asyncio.to_thread(self.assembly_stage.run, assembly_input),
        )
        planning_output.plan["visual_transitions"] = {
            "mode": stage_input.transition_mode,
            "duration_s": assembly_output.applied_transition_duration_s,
            "applied": list(assembly_output.applied_transitions),
        }

        # The primary track is usually far longer than the slideshow, and only the
        # [music_start_s, music_start_s + video_len] window is actually muxed into
        # the video. Cut that exact window into a standalone clip so the response's
        # audio_url is the selected part heard in the slideshow, while the full
        # track stays available as complete_audio_url. When the whole track fits the
        # video there is no window to select — reuse the full track as-is.
        track_duration_s = float(getattr(music_output, "music_duration_s", 0.0) or 0.0)
        # The slideshow length is the audio window length. Prefer the assembled
        # video's real duration; fall back to the intended total when the probe
        # fails (e.g. a stubbed video in tests) so a probe error never fails the job.
        try:
            window_duration_s = (
                get_video_duration(assembly_output.final_video_path) or total_duration
            )
        except Exception:
            window_duration_s = total_duration
        matched_music_path = music_output.music_path
        if track_duration_s > window_duration_s + 0.05:
            source = music_output.music_path
            windowed_path = source.with_name(f"{source.stem}_window{source.suffix}")
            matched_music_path = await _run_stage(
                "audio_window_extract",
                asyncio.to_thread(
                    extract_audio_window,
                    source,
                    windowed_path,
                    start_s=music_start_s,
                    duration_s=window_duration_s,
                ),
            )

        logger.info(
            "multi_image workflow completed in %.2fs (modelspec=%s, images=%d, beats_detected=%s).",
            time.perf_counter() - run_started,
            music_output.used_modelspec,
            len(planning_output.ordered_images),
            beat_output.beats_detected if beat_output else False,
        )
        # The image-sequence planning call is the sole model call on this path;
        # surface its token usage (empty on a plan-cache hit) as both the total and
        # a per-stage breakdown so downstream cost reporting mirrors the video flow.
        planning_usage = getattr(planning_output, "token_usage", None) or {}
        token_usage_breakdown = {"image_sequence_planning": dict(planning_usage)}

        return MultiImageWorkflowStageOutput(
            final_video_path=assembly_output.final_video_path,
            silent_video_path=assembly_output.silent_video_path,
            music_path=music_output.music_path,
            matched_music_path=matched_music_path,
            full_track_paths=music_output.full_track_paths,
            processed_image_paths=preprocess_output.preprocessed_images,
            planning_metadata=planning_output.plan,
            music_prompt=music_output.prompt,
            lyrics_timestamps=music_output.lyrics_timestamps,
            line_level_lyrics_timestamps=getattr(music_output, "line_level_lyrics_timestamps", []) or [],
            full_lyrics=getattr(music_output, "full_lyrics", None),
            used_modelspec=music_output.used_modelspec,
            section_timeline=music_output.section_timeline,
            video_title=str(planning_output.plan.get("video_title", "Generated Slideshow")),
            music_title=str(
                planning_output.plan.get(
                    "music_title",
                    planning_output.plan.get("video_title", "Generated Slideshow"),
                )
            ),
            video_description=str(
                planning_output.plan.get(
                    "video_description",
                    planning_output.plan.get("storyline_summary", ""),
                )
            ),
            include_vocals=effective_include_vocals,
            vocal_gender=effective_vocal_gender,
            lyrics_language=effective_lyrics_language,
            user_requested_language=detected_language,
            compression_applied=preprocess_output.compression_applied,
            vocal_id_used=getattr(music_output, "vocal_id_used", None),
            token_usage=dict(planning_usage),
            token_usage_breakdown=token_usage_breakdown,
            music_start_s=music_start_s,
        )


def run_multi_image_pipeline(
    folder_path: Union[str, Path],
    output_path: Union[str, Path],
    *,
    include_vocals: bool = False,
    vocal_gender: Optional[str] = None,
    align_to_beats: Optional[bool] = None,
    lyrics_language: Optional[str] = None,
    modelspec: str = "edenn_basic",
    per_image_duration: float = 3.0,
    music_volume: float = 1.0,
    user_prompt: str = "",
    water_mark: bool = False,
    transition_mode: str = "none",
    transitions: Optional[Sequence[str]] = None,
    transition_duration_s: float = 0.4,
    transition_seed: Optional[int] = None,
) -> MultiImageWorkflowStageOutput:
    stage = MultiImageGenerationE2EStage(
        per_image_duration=per_image_duration,
        music_volume=music_volume,
        align_to_beats=True if align_to_beats is None else align_to_beats,
    )
    stage_input = MultiImageWorkflowStageInput(
        folder_path=Path(folder_path),
        output_path=Path(output_path),
        user_prompt=user_prompt,
        include_vocals=include_vocals,
        vocal_gender=vocal_gender,
        align_to_beats=align_to_beats,
        lyrics_language=lyrics_language,
        modelspec=modelspec,
        water_mark=water_mark,
        transition_mode=transition_mode,
        transitions=transitions,
        transition_duration_s=transition_duration_s,
        transition_seed=transition_seed,
    )
    return asyncio.run(stage.run(stage_input))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the multi-image music generation workflow.")
    parser.add_argument(
        "--folder",
        type=Path,
        default=_default_cli_folder(),
        help="Folder containing the input images.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_default_cli_output(),
        help="Path to the output video file.",
    )
    parser.add_argument(
        "--modelspec",
        default="edenn_basic",
        choices=["edenn_basic", "edenn_enhanced", "edenn_studio"],
        help="Music generation branch to use.",
    )
    parser.add_argument(
        "--include-vocals",
        action="store_true",
        help="Generate a vocal track instead of pure music.",
    )
    parser.add_argument(
        "--vocal-gender",
        default=None,
        help="Preferred vocal gender when vocals are enabled.",
    )
    parser.add_argument(
        "--user-prompt",
        default="",
        help="Optional creative prompt used to infer language, vocal intent, and style guidance.",
    )
    parser.add_argument(
        "--lyrics-language",
        default=None,
        help="Optional lyrics language hint, for example EN or CN.",
    )
    parser.add_argument(
        "--align-to-beats",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Whether to align image durations to detected beats.",
    )
    parser.add_argument(
        "--per-image-duration",
        type=float,
        default=3.0,
        help="Fallback duration per image in seconds.",
    )
    parser.add_argument(
        "--music-volume",
        type=float,
        default=1.0,
        help="Music gain multiplier used during final mux.",
    )
    parser.add_argument(
        "--water-mark",
        action="store_true",
        help="Append the Edenn spoken watermark to full-track audio outputs.",
    )
    parser.add_argument(
        "--transition",
        default="none",
        help=(
            "Visual transition(s) between images: none, random, a single xfade name "
            "(e.g. fade), or a comma-separated list applied per boundary and cycled "
            "(e.g. fade,dissolve,wipeleft)."
        ),
    )
    parser.add_argument(
        "--transition-duration",
        type=float,
        default=0.4,
        help="Transition length in seconds; clamped to half the shortest image duration.",
    )
    parser.add_argument(
        "--transition-seed",
        type=int,
        default=None,
        help="Optional seed for reproducible random transition picks.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    from EdennCode.Util.MediaUtils import parse_transition_spec

    args = _parse_args()
    transition_mode, transition_list = parse_transition_spec(args.transition)
    result = run_multi_image_pipeline(
        args.folder,
        args.output,
        include_vocals=args.include_vocals,
        vocal_gender=args.vocal_gender,
        align_to_beats=args.align_to_beats,
        lyrics_language=args.lyrics_language,
        modelspec=args.modelspec,
        per_image_duration=args.per_image_duration,
        music_volume=args.music_volume,
        user_prompt=args.user_prompt,
        water_mark=args.water_mark,
        transition_mode=transition_mode,
        transitions=transition_list,
        transition_duration_s=args.transition_duration,
        transition_seed=args.transition_seed,
    )
    print("Final video:", result.final_video_path)
    print("Music track:", result.music_path)
    print("Full tracks:", [str(path) for path in result.full_track_paths])
    print("Used model:", result.used_modelspec)
    print("Lyrics timestamps:", len(result.lyrics_timestamps))
