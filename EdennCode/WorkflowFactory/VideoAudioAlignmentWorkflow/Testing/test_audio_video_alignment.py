import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from EdennCode.WorkflowFactory.VideoAudioAlignmentWorkflow.Stages.AudioVideoAlignmentStage.audio_video_alignment_stage import (
    AudioVideoAlignmentStage,
    AudioVideoAlignmentStageInput,
)
from EdennCode.WorkflowFactory.VideoAudioAlignmentWorkflow.Stages.AudioVideoAlignmentStage.alignment_window_scorer import (
    AlignmentWindowScorer,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata.video_metadata import (
    VideoMetadata,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.data_models import (
    MusicEvent,
    VideoEvent,
    WindowScore,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.word_ts import (
    WordTS,
)
from EdennCode.TestSuites.helpers.paths import SMOKE_VIDEO_PATH


def _fake_video_metadata(tmp_dir: Path) -> VideoMetadata:
    workdir = tmp_dir / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    return VideoMetadata(
        path=SMOKE_VIDEO_PATH,
        duration=6.0,
        size_bytes=SMOKE_VIDEO_PATH.stat().st_size,
        width=360,
        height=640,
        fps=30.0,
        video_codec="h264",
        video_bit_rate=350000,
        has_audio=False,
        audio_codec=None,
        audio_channels=None,
        audio_sample_rate=None,
        audio_bit_rate=None,
        temp_folder=str(workdir),
    )


class _FakeMusicAnalyzer:
    def rms_slice(self, _rms_curve, _sr, start_s, end_s):
        if end_s <= start_s:
            return np.array([], dtype=float)
        return np.array([0.2, 0.4], dtype=float)


class _FakeLyricExtractor:
    def __init__(self, word_count: int, coverage_ratio: float) -> None:
        self.word_count = word_count
        self.coverage_ratio = coverage_ratio

    def lyric_presence(self, _word_ts, _start_s, _end_s):
        return {
            "word_count": self.word_count,
            "coverage_ratio": self.coverage_ratio,
        }


class AudioVideoAlignmentTests(unittest.TestCase):
    def test_alignment_window_scorer_allows_no_lyrics_without_penalty(self) -> None:
        scorer = AlignmentWindowScorer()
        result = scorer.score(
            t0=0.0,
            video_duration_s=6.0,
            video_events=[VideoEvent(t=1.0, weight=1.0, kind="motion")],
            lyric_events=[],
            onset_times_s=np.array([0.8, 1.1, 3.0], dtype=float),
            rms_curve=np.array([0.2, 0.3], dtype=float),
            sr=16000,
            hop_length=512,
            music_analyzer=_FakeMusicAnalyzer(),
            lyric_extractor=_FakeLyricExtractor(word_count=0, coverage_ratio=0.0),
            word_ts=[],
            use_lyrics=False,
        )

        self.assertGreater(result.score, -1e8)
        self.assertFalse(result.details["rejected"])
        self.assertFalse(result.details["use_lyrics"])

    def test_alignment_window_scorer_enforces_lyrics_when_provided(self) -> None:
        scorer = AlignmentWindowScorer()
        result = scorer.score(
            t0=0.0,
            video_duration_s=6.0,
            video_events=[VideoEvent(t=1.0, weight=1.0, kind="motion")],
            lyric_events=[MusicEvent(t=0.9, weight=1.0, kind="line_start")],
            onset_times_s=np.array([0.8, 1.1, 3.0], dtype=float),
            rms_curve=np.array([0.2, 0.3], dtype=float),
            sr=16000,
            hop_length=512,
            music_analyzer=_FakeMusicAnalyzer(),
            lyric_extractor=_FakeLyricExtractor(word_count=1, coverage_ratio=0.02),
            word_ts=[WordTS(text="hi", startS=0.1, endS=0.2, i=0)],
            use_lyrics=True,
        )

        self.assertTrue(result.details["rejected"])
        self.assertEqual(result.details["reason"], "insufficient_lyrics")
        self.assertLess(result.score, -1e8)

    def test_alignment_stage_returns_segments_without_lyrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            audio_path = tmp_dir / "track.wav"
            audio_path.write_bytes(b"audio")

            stage = AudioVideoAlignmentStage()
            fake_windows = [
                WindowScore(
                    music_start_s=1.0,
                    music_end_s=7.0,
                    score=9.5,
                    details={"segment": "a"},
                ),
                WindowScore(
                    music_start_s=2.0,
                    music_end_s=8.0,
                    score=8.5,
                    details={"segment": "b"},
                ),
            ]

            def _fake_render_audio(*, out_path: str, **_kwargs):
                Path(out_path).write_bytes(b"rendered")
                return out_path

            with patch.object(stage.matcher, "match", return_value=(fake_windows[0], fake_windows)), patch.object(
                stage.renderer,
                "render_audio",
                side_effect=_fake_render_audio,
            ):
                output = asyncio.run(
                    stage.run(
                        AudioVideoAlignmentStageInput(
                            video_metadata=video_metadata,
                            local_audio_path=audio_path,
                            lyrics_timestamps=[],
                            top_k=2,
                        )
                    )
                )

            self.assertEqual(output.best_segment.rank, 1)
            self.assertEqual(len(output.segments), 2)
            self.assertEqual(output.segments[0].aligned_lyrics, [])
            self.assertEqual(output.segments[1].aligned_lyrics, [])
            self.assertTrue(output.segments[0].rendered_audio_path.exists())

    def test_alignment_stage_offsets_lyrics_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            audio_path = tmp_dir / "track.wav"
            audio_path.write_bytes(b"audio")
            lyrics = [
                WordTS(text="line1", startS=1.2, endS=2.1, i=0),
                WordTS(text="line2", startS=2.5, endS=3.5, i=1),
            ]

            stage = AudioVideoAlignmentStage()
            fake_window = WindowScore(
                music_start_s=1.0,
                music_end_s=7.0,
                score=9.5,
                details={"segment": "a"},
            )

            def _fake_render_audio(*, out_path: str, **_kwargs):
                Path(out_path).write_bytes(b"rendered")
                return out_path

            with patch.object(stage.matcher, "match", return_value=(fake_window, [fake_window])), patch.object(
                stage.renderer,
                "render_audio",
                side_effect=_fake_render_audio,
            ):
                output = asyncio.run(
                    stage.run(
                        AudioVideoAlignmentStageInput(
                            video_metadata=video_metadata,
                            local_audio_path=audio_path,
                            lyrics_timestamps=lyrics,
                            top_k=1,
                        )
                    )
                )

            self.assertEqual(len(output.segments[0].aligned_lyrics), 2)
            self.assertAlmostEqual(output.segments[0].aligned_lyrics[0].startS, 0.2)
            self.assertAlmostEqual(output.segments[0].aligned_lyrics[1].startS, 1.5)


if __name__ == "__main__":
    unittest.main()
