from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.music_analyzer import MusicAnalyzer


@dataclass
class MusicMatchingStageInput:
    music_path: Path
    target_duration: float
    max_candidates: int = 120


@dataclass
class MusicMatchingStageOutput:
    start_offset_s: float
    window_duration_s: float
    candidates_evaluated: int


class MusicMatchingStage:
    """Select a high-energy window in the music track that fits the target duration."""

    def __init__(self, *, analyzer: Optional[MusicAnalyzer] = None) -> None:
        self.analyzer = analyzer or MusicAnalyzer(hop_length=512)

    async def run(self, stage_input: MusicMatchingStageInput) -> MusicMatchingStageOutput:
        target_duration = max(0.0, float(stage_input.target_duration))
        if target_duration <= 0:
            return MusicMatchingStageOutput(
                start_offset_s=0.0,
                window_duration_s=0.0,
                candidates_evaluated=0,
            )

        onset_times, rms_curve, sr = self.analyzer.analyze(str(stage_input.music_path))
        if sr <= 0 or rms_curve.size == 0:
            return MusicMatchingStageOutput(
                start_offset_s=0.0,
                window_duration_s=0.0,
                candidates_evaluated=0,
            )

        total_duration = (len(rms_curve) * self.analyzer.hop_length) / float(sr)
        window_duration = min(target_duration, total_duration)
        if window_duration <= 0:
            return MusicMatchingStageOutput(
                start_offset_s=0.0,
                window_duration_s=0.0,
                candidates_evaluated=0,
            )

        candidates = [float(t) for t in onset_times if t + window_duration <= total_duration]
        if 0.0 + window_duration <= total_duration:
            candidates.append(0.0)
        if not candidates:
            return MusicMatchingStageOutput(
                start_offset_s=0.0,
                window_duration_s=window_duration,
                candidates_evaluated=0,
            )

        candidates = sorted(set(candidates))
        if len(candidates) > stage_input.max_candidates:
            step = len(candidates) / stage_input.max_candidates
            candidates = [candidates[int(i * step)] for i in range(stage_input.max_candidates)]

        best_start = candidates[0]
        best_score = float("-inf")
        for start in candidates:
            segment = self.analyzer.rms_slice(rms_curve, sr, start, start + window_duration)
            score = float(segment.mean()) if segment.size else 0.0
            if score > best_score:
                best_score = score
                best_start = start

        return MusicMatchingStageOutput(
            start_offset_s=best_start,
            window_duration_s=window_duration,
            candidates_evaluated=len(candidates),
        )
