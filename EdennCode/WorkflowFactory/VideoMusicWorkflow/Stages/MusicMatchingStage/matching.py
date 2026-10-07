from __future__ import annotations

import logging
from typing import List, Optional, Sequence, Tuple

from .data_models import WindowScore
from .feature_flags import beat_aware_enabled
from .word_ts import WordTS
from .media_tools import MediaTools
from .video_event_extractor import VideoEventExtractor
from .lyric_structure_extractor import LyricStructureExtractor
from .music_analyzer import MusicAnalyzer
from .candidate_generator import CandidateGenerator
from .window_scorer import WindowScorer

logger = logging.getLogger(__name__)


class EdennMusicWindowMatcher:
    """
    Entry point: given (video_path, music_path, word_ts) ranks best windows.
    """

    def __init__(
        self,
        *,
        video_extractor: Optional[VideoEventExtractor] = None,
        lyric_extractor: Optional[LyricStructureExtractor] = None,
        music_analyzer: Optional[MusicAnalyzer] = None,
        candidate_gen: Optional[CandidateGenerator] = None,
        scorer: Optional[WindowScorer] = None,
    ) -> None:
        beat_aware = beat_aware_enabled()
        self.video_extractor = video_extractor or VideoEventExtractor(detect_cuts=beat_aware)
        self.lyric_extractor = lyric_extractor or LyricStructureExtractor()
        self.music_analyzer = music_analyzer or MusicAnalyzer(hop_length=512, use_beats=beat_aware)
        self.candidate_gen = candidate_gen or CandidateGenerator()
        self.scorer = scorer or WindowScorer(
            beat_adaptive_sigma=beat_aware,
            dedup_onset_penalty=beat_aware,
        )

    def match(
        self,
        *,
        video_path: str,
        music_path: str,
        word_ts: Sequence[WordTS],
        require_lyrics: bool = True,
        topk: int = 5,
    ) -> Tuple[WindowScore, List[WindowScore]]:
        video_duration_s = MediaTools.duration_seconds(video_path)
        music_duration_s = MediaTools.duration_seconds(music_path)

        video_events = self.video_extractor.extract(video_path)
        analysis = self.music_analyzer.analyze(music_path)
        lyric_events, line_starts = self.lyric_extractor.extract_events(word_ts)

        candidates = self.candidate_gen.generate(
            music_duration_s=music_duration_s,
            video_duration_s=video_duration_s,
            onset_times_s=analysis.onset_times_s,
            lyric_events=lyric_events,
            line_start_times=line_starts,
            beat_times_s=analysis.beat_times_s,
        )

        # A matcher with nothing to rank must still return a window rather than
        # raising on scored[0]; the sibling alignment matcher guards this the
        # same way.
        if not candidates:
            logger.warning(
                "No window candidates for a %.2fs video against %.2fs of music; "
                "falling back to the head of the track.",
                video_duration_s, music_duration_s,
            )
            candidates = [0.0]

        def score_all(*, require_lyrics_now: bool) -> List[WindowScore]:
            return [
                self.scorer.score(
                    t0=t0,
                    video_duration_s=video_duration_s,
                    video_events=video_events,
                    lyric_events=lyric_events,
                    onset_times_s=analysis.onset_times_s,
                    rms_curve=analysis.rms_curve,
                    sr=analysis.sr,
                    hop_length=analysis.hop_length,
                    music_analyzer=self.music_analyzer,
                    lyric_extractor=self.lyric_extractor,
                    word_ts=word_ts,
                    require_lyrics=require_lyrics_now,
                    beat_times_s=analysis.beat_times_s,
                    tempo_bpm=analysis.tempo_bpm,
                )
                for t0 in candidates
            ]

        scored = score_all(require_lyrics_now=require_lyrics)

        # The lyric gate rejects a window by scoring it -1e9, and candidates
        # arrive in ascending t0. So if EVERY window fails the gate, a stable
        # descending sort returns the lowest t0 -- the head of the track, which
        # is the one outcome this whole stage exists to avoid. It happens for
        # real: require_lyrics is set from a request flag, not from whether any
        # word timestamps actually arrived, so a vocal track with no alignment
        # rejects everything. Score again without the gate and say so, rather
        # than delivering a trim from zero dressed as a chosen window.
        if scored and all(w.details.get("rejected") for w in scored):
            logger.warning(
                "All %d music windows failed the lyric gate (%s); re-ranking "
                "without it. The chosen window is the best musical fit "
                "available, not a lyric-aligned one.",
                len(scored), scored[0].details.get("reason", "unknown"),
            )
            scored = score_all(require_lyrics_now=False)
            for w in scored:
                w.details["lyric_gate_relaxed"] = True

        scored.sort(key=lambda s: s.score, reverse=True)
        best = scored[0]
        top = scored[: max(1, topk)]
        return best, top

