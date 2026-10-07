from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.candidate_generator import (
    CandidateGenerator,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.data_models import (
    WindowScore,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.feature_flags import (
    beat_aware_enabled,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.lyric_structure_extractor import (
    LyricStructureExtractor,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.media_tools import (
    MediaTools,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.music_analyzer import (
    MusicAnalyzer,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.video_event_extractor import (
    VideoEventExtractor,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.word_ts import (
    WordTS,
)

from .alignment_window_scorer import AlignmentWindowScorer


class AudioVideoWindowMatcher:
    """
    Standalone matcher for the alignment endpoint.

    Reuses the same low-level analyzers and improved scorer as the main pipeline.
    """

    def __init__(
        self,
        *,
        video_extractor: Optional[VideoEventExtractor] = None,
        lyric_extractor: Optional[LyricStructureExtractor] = None,
        music_analyzer: Optional[MusicAnalyzer] = None,
        candidate_gen: Optional[CandidateGenerator] = None,
        scorer: Optional[AlignmentWindowScorer] = None,
    ) -> None:
        beat_aware = beat_aware_enabled()
        self.video_extractor = video_extractor or VideoEventExtractor(detect_cuts=beat_aware)
        self.lyric_extractor = lyric_extractor or LyricStructureExtractor()
        self.music_analyzer = music_analyzer or MusicAnalyzer(hop_length=512, use_beats=beat_aware)
        self.candidate_gen = candidate_gen or CandidateGenerator()
        self.scorer = scorer or AlignmentWindowScorer(
            beat_adaptive_sigma=beat_aware,
            dedup_onset_penalty=beat_aware,
        )

    def match(
        self,
        *,
        video_path: str,
        music_path: str,
        word_ts: Optional[Sequence[WordTS]] = None,
        topk: int = 5,
    ) -> Tuple[WindowScore, List[WindowScore]]:
        provided_word_ts = list(word_ts or [])
        use_lyrics = bool(provided_word_ts)

        video_duration_s = MediaTools.duration_seconds(video_path)
        music_duration_s = MediaTools.duration_seconds(music_path)
        video_events = self.video_extractor.extract(video_path)

        analysis = self.music_analyzer.analyze(music_path)

        if use_lyrics:
            lyric_events, line_starts = self.lyric_extractor.extract_events(provided_word_ts)
        else:
            lyric_events, line_starts = [], []

        candidates = self.candidate_gen.generate(
            music_duration_s=music_duration_s,
            video_duration_s=video_duration_s,
            onset_times_s=analysis.onset_times_s,
            lyric_events=lyric_events,
            line_start_times=line_starts,
            beat_times_s=analysis.beat_times_s,
        )
        if not candidates:
            candidates = [0.0]

        scored: List[WindowScore] = []
        for candidate_start_s in candidates:
            scored.append(
                self.scorer.score(
                    t0=candidate_start_s,
                    video_duration_s=video_duration_s,
                    video_events=video_events,
                    lyric_events=lyric_events,
                    onset_times_s=analysis.onset_times_s,
                    rms_curve=analysis.rms_curve,
                    sr=analysis.sr,
                    hop_length=analysis.hop_length,
                    music_analyzer=self.music_analyzer,
                    lyric_extractor=self.lyric_extractor,
                    word_ts=provided_word_ts,
                    use_lyrics=use_lyrics,
                    beat_times_s=analysis.beat_times_s,
                    tempo_bpm=analysis.tempo_bpm,
                )
            )

        scored.sort(key=lambda window: window.score, reverse=True)
        best = scored[0]
        top = scored[: max(1, topk)]
        return best, top
