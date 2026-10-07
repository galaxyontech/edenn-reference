from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import librosa
import numpy as np

from EdennCode.exceptions import EdennMediaProcessingError, EdennValidationError

logger = logging.getLogger(__name__)


@dataclass
class BeatAlignmentStageInput:
    music_path: Path
    image_count: int
    fallback_duration: float
    section_boundaries_s: Optional[List[float]] = None
    section_image_counts: Optional[List[int]] = None


@dataclass
class BeatAlignmentStageOutput:
    durations: List[float]
    beats_detected: bool


class BeatAlignmentStage:
    """Estimate per-image durations by aligning frames to audio beats."""

    def __init__(self, *, min_duration: float = 0.5, max_duration: float = 10.0) -> None:
        self.min_duration = min_duration
        self.max_duration = max_duration

    async def run(self, stage_input: BeatAlignmentStageInput) -> BeatAlignmentStageOutput:
        if stage_input.image_count <= 0:
            raise EdennValidationError("image_count must be positive", component="beat_alignment", operation="validate_input")

        durations = self._uniform(stage_input.image_count, stage_input.fallback_duration)

        try:
            beats = self._extract_beats(stage_input.music_path)
        except Exception:
            # Beat detection is a best-effort enhancement: if librosa fails to
            # load the audio or finds no beats we degrade to uniform durations
            # rather than failing the whole job. Log it so the degradation is
            # observable in production instead of silently masked.
            logger.warning(
                "Beat extraction failed for %s; falling back to uniform durations.",
                stage_input.music_path,
                exc_info=True,
            )
            return BeatAlignmentStageOutput(durations=durations, beats_detected=False)

        aligned: Optional[List[float]] = None
        if stage_input.section_boundaries_s and stage_input.section_image_counts:
            aligned = self._align_sections_to_beats(
                beats,
                section_boundaries_s=stage_input.section_boundaries_s,
                section_image_counts=stage_input.section_image_counts,
            )
        if not aligned:
            aligned = self._align_to_beats(beats, stage_input.image_count)
        if not aligned:
            logger.info(
                "Beat alignment produced no usable durations for %s "
                "(image_count=%s); falling back to uniform durations.",
                stage_input.music_path,
                stage_input.image_count,
            )
            return BeatAlignmentStageOutput(durations=durations, beats_detected=False)
        if any(dur < self.min_duration or dur > self.max_duration for dur in aligned):
            logger.info(
                "Beat-aligned durations fell outside [%.2f, %.2f]s for %s; "
                "falling back to uniform durations.",
                self.min_duration,
                self.max_duration,
                stage_input.music_path,
            )
            return BeatAlignmentStageOutput(durations=durations, beats_detected=False)

        return BeatAlignmentStageOutput(durations=aligned, beats_detected=True)

    def _extract_beats(self, music_path: Path) -> List[float]:
        audio, sr = librosa.load(str(music_path), sr=None, mono=True)
        if audio.size == 0:
            raise EdennValidationError("Empty audio data", component="beat_alignment", operation="validate_audio")

        tempo, beat_frames = librosa.beat.beat_track(y=audio, sr=sr, units="frames")
        if beat_frames.size == 0:
            raise EdennMediaProcessingError("No beats detected", component="beat_alignment", operation="extract_beats")

        beat_times = librosa.frames_to_time(beat_frames, sr=sr)
        beat_times = np.unique(np.concatenate(([0.0], beat_times, [len(audio) / sr])))
        return beat_times.tolist()

    @staticmethod
    def _align_to_beats(beats: List[float], image_count: int) -> Optional[List[float]]:
        if len(beats) < image_count + 1:
            return None

        intervals = len(beats) - 1
        indices = [int(i * intervals / image_count) for i in range(image_count + 1)]
        chosen = [beats[idx] for idx in indices]
        durations = [chosen[i + 1] - chosen[i] for i in range(image_count)]
        if any(dur <= 0 for dur in durations):
            return None
        return durations

    @classmethod
    def _align_sections_to_beats(
        cls,
        beats: List[float],
        *,
        section_boundaries_s: List[float],
        section_image_counts: List[int],
    ) -> Optional[List[float]]:
        if len(section_boundaries_s) != len(section_image_counts) + 1:
            return None
        if any(count <= 0 for count in section_image_counts):
            return None

        durations: List[float] = []
        for idx, image_count in enumerate(section_image_counts):
            start_s = float(section_boundaries_s[idx])
            end_s = float(section_boundaries_s[idx + 1])
            if end_s <= start_s:
                return None
            section_beats = [beat for beat in beats if start_s < beat < end_s]
            anchors = [start_s, *section_beats, end_s]
            if len(anchors) < image_count + 1:
                uniform = (end_s - start_s) / image_count
                durations.extend([uniform] * image_count)
                continue

            intervals = len(anchors) - 1
            indices = [int(i * intervals / image_count) for i in range(image_count + 1)]
            chosen = [anchors[index] for index in indices]
            section_durations = [chosen[i + 1] - chosen[i] for i in range(image_count)]
            if any(duration <= 0 for duration in section_durations):
                return None
            durations.extend(section_durations)
        return durations

    @staticmethod
    def _uniform(count: int, duration: float) -> List[float]:
        return [duration for _ in range(count)]
