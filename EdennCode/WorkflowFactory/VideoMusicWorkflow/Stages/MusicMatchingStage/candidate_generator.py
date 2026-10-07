from __future__ import annotations

from typing import List, Optional, Sequence

import numpy as np

from .data_models import MusicEvent


class CandidateGenerator:
    """
    Generates plausible music start times t0 from lyric events, beat times,
    and onset anchors with small temporal offsets.
    """

    def __init__(
        self,
        *,
        offsets_s: Sequence[float] = (-0.20, 0.0, 0.20),
        max_candidates: int = 2500,
        max_onset_anchors: int = 800,
    ) -> None:
        self.offsets_s = list(offsets_s)
        self.max_candidates = max_candidates
        self.max_onset_anchors = max_onset_anchors

    def generate(
        self,
        *,
        music_duration_s: float,
        video_duration_s: float,
        onset_times_s: np.ndarray,
        lyric_events: Sequence[MusicEvent],
        line_start_times: Sequence[float],
        beat_times_s: Optional[np.ndarray] = None,
    ) -> List[float]:
        max_t0 = max(0.0, float(music_duration_s - video_duration_s))

        base: List[float] = []
        base.extend(float(e.t) for e in lyric_events)
        base.extend(float(t) for t in line_start_times)

        # Beat positions are strong structural anchors — include all of them.
        if beat_times_s is not None and len(beat_times_s) > 0:
            base.extend(float(t) for t in beat_times_s)

        # Subsample onsets to avoid too many anchors.
        if len(onset_times_s) > 0:
            if len(onset_times_s) <= self.max_onset_anchors:
                onset_anchors = onset_times_s
            else:
                idx = np.linspace(0, len(onset_times_s) - 1, self.max_onset_anchors).astype(int)
                onset_anchors = onset_times_s[idx]
            base.extend(float(t) for t in onset_anchors)
        if not base:
            base.append(0.0)

        cands: List[float] = []
        for t in base:
            for off in self.offsets_s:
                cands.append(t + off)

        seen = set()
        out: List[float] = []
        for t0 in cands:
            t0 = max(0.0, min(max_t0, t0))
            key = round(t0, 3)
            if key in seen:
                continue
            seen.add(key)
            out.append(float(key))

        out.sort()

        if len(out) > self.max_candidates:
            idx = np.linspace(0, len(out) - 1, self.max_candidates).astype(int)
            out = [out[i] for i in idx]

        return out
