import unittest

try:
    import numpy as np
except ModuleNotFoundError:  # pragma: no cover - local test env may omit optional deps
    np = None

if np is not None:
    from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.data_models import VideoEvent
    from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.window_scorer import WindowScorer
    from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.word_ts import WordTS

    class _StubLyricExtractor:
        def __init__(self, coverage_ratio: float, word_count: float) -> None:
            self.coverage_ratio = float(coverage_ratio)
            self.word_count = float(word_count)

        def lyric_presence(self, word_ts, win_start, win_end):
            duration = max(1e-6, float(win_end) - float(win_start))
            return {
                "coverage_s": self.coverage_ratio * duration,
                "coverage_ratio": self.coverage_ratio,
                "word_count": self.word_count,
            }

    class _StubMusicAnalyzer:
        hop_length = 512

        @staticmethod
        def rms_slice(rms_curve, sr, start_s, end_s):
            return np.array([0.5, 0.6], dtype=float)

    class WindowScorerTests(unittest.TestCase):
        def test_target_lyric_coverage_decreases_with_duration(self) -> None:
            scorer = WindowScorer()
            self.assertGreater(
                scorer._target_lyric_coverage(10.0),
                scorer._target_lyric_coverage(30.0),
            )
            self.assertGreater(
                scorer._target_lyric_coverage(30.0),
                scorer._target_lyric_coverage(90.0),
            )

        def test_short_video_raises_min_coverage_gate(self) -> None:
            scorer = WindowScorer()
            word_ts = [WordTS(text="hello", startS=0.0, endS=1.0, i=0)]
            out = scorer.score(
                t0=0.0,
                video_duration_s=12.0,
                video_events=[],
                lyric_events=[],
                onset_times_s=np.array([], dtype=float),
                rms_curve=np.array([0.1, 0.2], dtype=float),
                sr=44100,
                hop_length=512,
                music_analyzer=_StubMusicAnalyzer(),
                lyric_extractor=_StubLyricExtractor(coverage_ratio=0.11, word_count=6.0),
                word_ts=word_ts,
            )
            self.assertTrue(out.details.get("rejected"))
            self.assertEqual(out.details.get("reason"), "insufficient_lyrics")
            self.assertAlmostEqual(out.details.get("required_min_coverage"), 0.16, places=6)

        def test_instrumental_mode_skips_lyric_coverage_gate(self) -> None:
            scorer = WindowScorer()
            out = scorer.score(
                t0=0.0,
                video_duration_s=12.0,
                video_events=[VideoEvent(t=1.0, weight=1.0, kind="cut")],
                lyric_events=[],
                onset_times_s=np.array([1.0], dtype=float),
                rms_curve=np.array([0.1, 0.2], dtype=float),
                sr=44100,
                hop_length=512,
                music_analyzer=_StubMusicAnalyzer(),
                lyric_extractor=_StubLyricExtractor(coverage_ratio=0.0, word_count=0.0),
                word_ts=[],
                require_lyrics=False,
            )

            self.assertFalse(out.details.get("rejected"))
            self.assertFalse(out.details.get("require_lyrics"))
            self.assertGreater(out.score, -1e9)

        def test_same_coverage_scores_better_for_longer_video(self) -> None:
            scorer = WindowScorer()
            word_ts = [WordTS(text="line", startS=0.0, endS=2.0, i=0)]
            kwargs = dict(
                t0=0.0,
                video_events=[VideoEvent(t=1.0, weight=1.0, kind="cut")],
                lyric_events=[],
                onset_times_s=np.array([1.0], dtype=float),
                rms_curve=np.array([0.2, 0.3, 0.4], dtype=float),
                sr=44100,
                hop_length=512,
                music_analyzer=_StubMusicAnalyzer(),
                lyric_extractor=_StubLyricExtractor(coverage_ratio=0.45, word_count=10.0),
                word_ts=word_ts,
            )

            short_out = scorer.score(video_duration_s=12.0, **kwargs)
            long_out = scorer.score(video_duration_s=60.0, **kwargs)

            self.assertFalse(short_out.details.get("rejected"))
            self.assertFalse(long_out.details.get("rejected"))
            self.assertLess(
                short_out.details.get("lyric_score"),
                long_out.details.get("lyric_score"),
            )
else:
    @unittest.skip("numpy is not installed in this environment")
    class WindowScorerTests(unittest.TestCase):
        def test_numpy_required(self) -> None:
            self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
