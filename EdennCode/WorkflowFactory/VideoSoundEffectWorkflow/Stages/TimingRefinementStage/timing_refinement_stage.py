"""
Local visual timing refinement for SFX events.

Multimodal-LLM event analysis localizes events at roughly second level. This
stage closes the gap to perceptual sync (~100 ms) without any model call: it
decodes a small grayscale frame stream around each event window, computes a
frame-difference motion-energy curve, and snaps the event start to the motion
onset (the steepest energy rise) near the LLM's estimate.

The same motion-energy primitive doubles as the visual ground-truth proxy in
the eval harness, so refinement quality is directly measurable.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from EdennCode.Util.MediaUtils.ffmpeg_utils import resolve_ffmpeg_binary
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.VideoSoundEffectDataModel.datamodel import (
    SoundFXEvent,
)

logger = logging.getLogger(__name__)

# Event types whose onset is a discrete visual moment worth snapping to.
DISCRETE_EVENT_TYPES = {"IMPACT", "CONTACT", "TRANSITION", "REVEAL", "EMPHASIS"}
# Long ACTION events are sustained beds (walking, camera moves) — no single onset.
MAX_SNAPPABLE_ACTION_DURATION_S = 2.5
# When the window's median energy is this fraction of its peak, motion is
# continuous (handheld pans, walking) and any "onset" would be spurious.
CONTINUOUS_MOTION_FLOOR = 0.45


def is_discrete_event(event_type: str, duration_s: float) -> bool:
    event_type = (event_type or "").upper()
    if event_type in DISCRETE_EVENT_TYPES:
        return True
    if event_type in {"ACTION", ""}:
        return duration_s <= MAX_SNAPPABLE_ACTION_DURATION_S
    return False


def compute_motion_energy(
    video_path: Path,
    *,
    start_s: float,
    end_s: float,
    sample_fps: float = 24.0,
    width: int = 160,
    height: int = 90,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Frame-difference motion energy over ``[start_s, end_s]``.

    Returns ``(times, energy)`` where ``energy[i]`` is the mean absolute
    luminance change landing at ``times[i]`` (attributed to the later frame of
    each pair). Aspect distortion from the fixed analysis size is irrelevant to
    the differencing.
    """
    start_s = max(0.0, float(start_s))
    duration = max(0.0, float(end_s) - start_s)
    if duration <= 0.0:
        return np.array([]), np.array([])

    ffmpeg_bin = resolve_ffmpeg_binary()
    cmd = [
        ffmpeg_bin,
        "-ss",
        f"{start_s:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(video_path),
        "-vf",
        f"fps={sample_fps},scale={width}:{height}",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]
    result = subprocess.run(cmd, check=True, capture_output=True)
    frame_bytes = width * height
    frame_count = len(result.stdout) // frame_bytes
    if frame_count < 2:
        return np.array([]), np.array([])

    frames = (
        np.frombuffer(result.stdout[: frame_count * frame_bytes], dtype=np.uint8)
        .reshape(frame_count, height, width)
        .astype(np.float32)
    )
    diffs = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    times = start_s + (np.arange(1, frame_count, dtype=np.float64) / sample_fps)
    return times, diffs


def find_motion_onset(
    times: np.ndarray,
    energy: np.ndarray,
    *,
    min_peak_ratio: float = 1.5,
    prefer_near_s: Optional[float] = None,
) -> Optional[float]:
    """
    Time of a prominent motion onset, or None when nothing stands out.

    Candidate onsets are prominent positive rises of the energy curve. When
    ``prefer_near_s`` is given, the candidate NEAREST that time wins rather
    than the globally strongest — two visual onsets often share one search
    window (a scene cut next to an impact), and the strongest rise is not
    necessarily the event the model meant (the bake-off's reproduced failure:
    an impact snapped 0.65s early onto the adjacent cut).

    Two gates guard against fake snaps: the peak must exceed
    ``min_peak_ratio`` × the window's median energy (flat/noisy windows), and
    the median must sit well below the peak (continuous motion such as
    handheld walking has no discrete onset).
    """
    if energy.size < 3:
        return None
    baseline = float(np.median(energy))
    peak_value = float(np.max(energy))
    if baseline > 0 and peak_value / baseline < min_peak_ratio:
        return None
    if baseline == 0 and peak_value <= 0:
        return None
    if peak_value > 0 and baseline / peak_value > CONTINUOUS_MOTION_FLOOR:
        return None

    rise = np.diff(energy)
    max_rise = float(np.max(rise)) if rise.size else 0.0
    if max_rise <= 0:
        return None
    # Prominent candidates: rises within 40% of the strongest rise.
    candidate_idx = [int(i) + 1 for i in np.flatnonzero(rise >= 0.4 * max_rise)]
    if not candidate_idx:
        return None
    # Collapse runs of adjacent samples to their first (the true onset frame).
    collapsed = [candidate_idx[0]]
    for idx in candidate_idx[1:]:
        if idx - collapsed[-1] > 1:
            collapsed.append(idx)
    if prefer_near_s is not None:
        onset_idx = min(collapsed, key=lambda i: abs(float(times[i]) - prefer_near_s))
    else:
        onset_idx = max(collapsed, key=lambda i: float(rise[i - 1]))
    return float(times[onset_idx])


@dataclass
class EventTimingRefinement:
    event_id: str
    original_start: float
    refined_start: Optional[float]
    shift_s: float = 0.0
    snapped: bool = False


@dataclass
class TimingRefinementStageInput:
    video_path: Path
    events: List[SoundFXEvent]
    video_duration: float
    search_before_s: float = 0.6
    search_after_s: float = 0.6
    sample_fps: float = 24.0
    max_shift_s: float = 1.0
    min_peak_ratio: float = 1.5


@dataclass
class TimingRefinementStageOutput:
    events: List[SoundFXEvent]
    refinements: List[EventTimingRefinement] = field(default_factory=list)


class TimingRefinementStage:
    """
    Snap event starts to visual motion onsets. Mutation-free: events are
    updated via ``refined_start_time`` so the LLM estimate stays inspectable.
    """

    async def run(self, stage_input: TimingRefinementStageInput) -> TimingRefinementStageOutput:
        refinements = await asyncio.gather(
            *[
                asyncio.to_thread(self._refine_one, stage_input, event)
                for event in stage_input.events
            ]
        )
        for event, refinement in zip(stage_input.events, refinements):
            # Authoritative: a declined snap clears any stale earlier snap.
            event.refined_start_time = (
                refinement.refined_start if refinement.snapped else None
            )
        return TimingRefinementStageOutput(events=stage_input.events, refinements=list(refinements))

    @staticmethod
    def _refine_one(
        stage_input: TimingRefinementStageInput, event: SoundFXEvent
    ) -> EventTimingRefinement:
        result = EventTimingRefinement(
            event_id=event.event_id,
            original_start=event.start_time,
            refined_start=None,
        )
        if not is_discrete_event(event.event_type, event.duration):
            return result

        window_start = max(0.0, event.start_time - stage_input.search_before_s)
        window_end = min(
            stage_input.video_duration,
            max(event.start_time + stage_input.search_after_s, window_start + 0.2),
        )
        try:
            times, energy = compute_motion_energy(
                stage_input.video_path,
                start_s=window_start,
                end_s=window_end,
                sample_fps=stage_input.sample_fps,
            )
        except subprocess.CalledProcessError as err:
            logger.warning(
                "Motion analysis failed for event %s (%s); keeping LLM timing.",
                event.event_id,
                err.stderr[-200:] if err.stderr else err,
            )
            return result

        onset = find_motion_onset(
            times,
            energy,
            min_peak_ratio=stage_input.min_peak_ratio,
            prefer_near_s=event.start_time,
        )
        if onset is None:
            return result

        shift = onset - event.start_time
        if abs(shift) > stage_input.max_shift_s:
            return result

        result.refined_start = round(float(onset), 4)
        result.shift_s = round(float(shift), 4)
        result.snapped = True
        return result
