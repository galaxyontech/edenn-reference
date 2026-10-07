from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Set

import numpy as np

from .data_models import MusicEvent, VideoEvent, WindowScore
from .lyric_structure_extractor import LyricStructureExtractor
from .music_analyzer import MusicAnalyzer
from .word_ts import WordTS

_BEAT_EVENT_WEIGHT = 2.0  # beats are metrically stronger than generic onsets
_DEDUP_SECOND_CLAIM_FACTOR = 0.5  # penalty when two video events claim the same music anchor


class WindowScorer:
    """
    Scores a candidate window [t0, t0+video_duration].

    Improvements over the baseline:
    - beat_adaptive_sigma: sigma scales with tempo so tolerance is always
      ~one quarter-beat, regardless of BPM.
    - Beat grid events are injected with higher weight so video events
      prefer landing on metrically strong positions.
    - dedup_onset_penalty: a music anchor that has already been claimed by
      one video event contributes half-weight to any subsequent claimant,
      preventing artificial score inflation.
    """

    def __init__(
        self,
        *,
        alignment_sigma_s: float = 0.18,
        w_align: float = 1.20,
        w_shape: float = 1.00,
        w_lyric: float = 0.35,
        min_word_tokens: int = 3,
        min_coverage_ratio: float = 0.10,
        beat_adaptive_sigma: bool = True,
        dedup_onset_penalty: bool = True,
    ) -> None:
        self.alignment_sigma_s = alignment_sigma_s
        self.w_align = w_align
        self.w_shape = w_shape
        self.w_lyric = w_lyric
        self.min_word_tokens = min_word_tokens
        self.min_coverage_ratio = min_coverage_ratio
        self.beat_adaptive_sigma = beat_adaptive_sigma
        self.dedup_onset_penalty = dedup_onset_penalty

    @staticmethod
    def _gauss(d: float, sigma: float) -> float:
        return math.exp(-(d * d) / (2.0 * sigma * sigma))

    def _sigma(self, tempo_bpm: float) -> float:
        if not self.beat_adaptive_sigma:
            return self.alignment_sigma_s
        beat_period_s = 60.0 / max(40.0, min(250.0, tempo_bpm))
        return max(0.10, min(0.40, beat_period_s * 0.25))

    @staticmethod
    def _target_lyric_coverage(video_duration_s: float) -> float:
        duration = max(1.0, float(video_duration_s))
        if duration <= 15.0:
            return 0.60
        if duration <= 30.0:
            return 0.60 - ((duration - 15.0) / 15.0) * 0.10
        if duration <= 60.0:
            return 0.50 - ((duration - 30.0) / 30.0) * 0.12
        return 0.35

    def _min_coverage_for_duration(self, video_duration_s: float) -> float:
        duration = max(1.0, float(video_duration_s))
        if duration <= 15.0:
            return max(self.min_coverage_ratio, 0.16)
        if duration <= 30.0:
            return max(self.min_coverage_ratio, 0.12)
        return self.min_coverage_ratio

    def score(
        self,
        *,
        t0: float,
        video_duration_s: float,
        video_events: Sequence[VideoEvent],
        lyric_events: Sequence[MusicEvent],
        onset_times_s: np.ndarray,
        rms_curve: np.ndarray,
        sr: int,
        hop_length: int,
        music_analyzer: MusicAnalyzer,
        lyric_extractor: LyricStructureExtractor,
        word_ts: Sequence[WordTS],
        require_lyrics: bool = True,
        beat_times_s: Optional[np.ndarray] = None,
        tempo_bpm: float = 120.0,
    ) -> WindowScore:
        del hop_length
        t0 = float(t0)
        t1 = t0 + float(video_duration_s)
        sigma_s = self._sigma(tempo_bpm)

        lyr = lyric_extractor.lyric_presence(word_ts, t0, t1)
        min_coverage_required = self._min_coverage_for_duration(video_duration_s)
        has_insufficient_lyrics = (
            lyr["word_count"] < self.min_word_tokens
            or lyr["coverage_ratio"] < min_coverage_required
        )
        if require_lyrics and has_insufficient_lyrics:
            return WindowScore(
                music_start_s=t0,
                music_end_s=t1,
                score=-1e9,
                details={
                    "rejected": True,
                    "reason": "insufficient_lyrics",
                    "require_lyrics": True,
                    "lyrics": lyr,
                    "required_min_coverage": float(min_coverage_required),
                    "target_lyric_coverage": float(self._target_lyric_coverage(video_duration_s)),
                },
            )

        combined: List[MusicEvent] = list(lyric_events)
        if beat_times_s is not None and len(beat_times_s) > 0:
            combined.extend(
                MusicEvent(t=float(bt), weight=_BEAT_EVENT_WEIGHT, kind="beat")
                for bt in beat_times_s
            )
        combined.extend(
            MusicEvent(t=float(t), weight=1.0, kind="onset") for t in onset_times_s
        )
        combined.sort(key=lambda e: e.t)

        times = np.array([e.t for e in combined], dtype=float)
        weights = np.array([e.weight for e in combined], dtype=float)

        align_score = 0.0
        per_event: List[Dict] = []
        claimed_indices: Set[int] = set()

        for ve in video_events:
            target = t0 + float(ve.t)
            if len(times) == 0:
                nearest_idx = -1
                nearest_t = target
                nearest_w = 1.0
                dist = 0.0
            else:
                nearest_idx = int(np.argmin(np.abs(times - target)))
                nearest_t = float(times[nearest_idx])
                nearest_w = float(weights[nearest_idx])
                dist = abs(nearest_t - target)

            if self.dedup_onset_penalty and nearest_idx >= 0 and nearest_idx in claimed_indices:
                nearest_w *= _DEDUP_SECOND_CLAIM_FACTOR
            if nearest_idx >= 0:
                claimed_indices.add(nearest_idx)

            s = float(ve.weight) * nearest_w * self._gauss(dist, sigma_s)
            align_score += s
            per_event.append({
                "video_t": float(ve.t),
                "video_w": float(ve.weight),
                "target_music_t": float(target),
                "nearest_music_event_t": float(nearest_t),
                "delta_s": float(dist),
                "partial_score": float(s),
            })

        if video_events:
            main_evt = max(video_events, key=lambda e: e.weight)
            main_t = float(main_evt.t)
        else:
            main_t = float(video_duration_s * 0.33)

        build_a, build_b = t0, min(t0 + main_t, t1)
        drop_a, drop_b = min(t0 + main_t, t1), min(t0 + main_t + 3.0, t1)

        rms_build = music_analyzer.rms_slice(rms_curve, sr, build_a, build_b)
        rms_drop = music_analyzer.rms_slice(rms_curve, sr, drop_a, drop_b)

        build_level = float(np.median(rms_build)) if len(rms_build) else 0.0
        drop_level = float(np.median(rms_drop)) if len(rms_drop) else 0.0

        eps = 1e-6
        shape_raw = (drop_level - build_level) / (drop_level + build_level + eps)
        shape_score = max(0.0, min(1.0, (shape_raw + 1.0) * 0.5))

        cov = float(lyr["coverage_ratio"])
        target_cov = float(self._target_lyric_coverage(video_duration_s))
        if require_lyrics:
            denom = max(1e-6, target_cov if cov <= target_cov else 1.0 - target_cov)
            lyric_score = 1.0 - min(1.0, abs(cov - target_cov) / denom)
        else:
            lyric_score = 0.0

        final = (self.w_align * align_score) + (self.w_shape * shape_score) + (self.w_lyric * lyric_score)

        return WindowScore(
            music_start_s=t0,
            music_end_s=t1,
            score=float(final),
            details={
                "rejected": False,
                "require_lyrics": bool(require_lyrics),
                "sigma_s": float(sigma_s),
                "tempo_bpm": float(tempo_bpm),
                "align_score": float(align_score),
                "shape_score": float(shape_score),
                "lyric_score": float(lyric_score),
                "target_lyric_coverage": float(target_cov),
                "required_min_coverage": float(min_coverage_required),
                "build_level": build_level,
                "drop_level": drop_level,
                "lyrics": lyr,
                "per_event_alignment": per_event,
            },
        )
