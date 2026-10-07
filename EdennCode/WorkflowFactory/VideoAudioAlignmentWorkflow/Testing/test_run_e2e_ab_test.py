import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from EdennCode.WorkflowFactory.VideoAudioAlignmentWorkflow.Testing.run_e2e_ab_test import (
    _resolve_full_track,
    _resolve_full_track_lyrics,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.word_ts import (
    WordTS,
)


class RunE2EABTestHelpers(unittest.TestCase):
    def test_resolve_full_track_returns_primary_when_matching_primary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            primary_track = tmp_dir / "primary.wav"
            primary_track.write_bytes(b"primary")

            result = SimpleNamespace(
                matching_used_track="primary",
                complete_generated_music_path=primary_track,
                secondary_complete_generated_music_path=None,
            )

            resolved_track, source = _resolve_full_track(result)

            self.assertEqual(resolved_track, primary_track)
            self.assertEqual(source, "complete_generated_music_path")

    def test_resolve_full_track_returns_primary_when_matching_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            primary_track = tmp_dir / "primary.wav"
            primary_track.write_bytes(b"primary")

            result = SimpleNamespace(
                matching_used_track=None,
                complete_generated_music_path=primary_track,
                secondary_complete_generated_music_path=None,
            )

            resolved_track, source = _resolve_full_track(result)

            self.assertEqual(resolved_track, primary_track)
            self.assertEqual(source, "complete_generated_music_path")

    def test_resolve_full_track_prefers_selected_pipeline_track(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            primary_track = tmp_dir / "primary.wav"
            secondary_track = tmp_dir / "secondary.wav"
            primary_track.write_bytes(b"primary")
            secondary_track.write_bytes(b"secondary")

            result = SimpleNamespace(
                matching_used_track="secondary",
                complete_generated_music_path=primary_track,
                secondary_complete_generated_music_path=secondary_track,
            )

            resolved_track, source = _resolve_full_track(result)

            self.assertEqual(resolved_track, secondary_track)
            self.assertEqual(source, "secondary_complete_generated_music_path")

    def test_resolve_full_track_lyrics_prefers_full_track_fields_and_scales_ms(self) -> None:
        result = SimpleNamespace(
            matching_used_track="primary",
            primary_full_word_level_lyrics_timestamps=[
                WordTS(text="hello", startS=1500.0, endS=3000.0, i=0),
            ],
            primary_full_lyrics_timestamps=[],
            secondary_full_word_level_lyrics_timestamps=[],
            secondary_full_lyrics_timestamps=[],
            word_level_lyrics_timestamps=[
                WordTS(text="bad_fallback", startS=200.0, endS=400.0, i=0),
            ],
            lyrics_timestamps=[],
        )

        lyrics, source = _resolve_full_track_lyrics(
            result,
            reference_duration_s=30.0,
        )

        self.assertEqual(source, "primary_full_word_level_lyrics_timestamps")
        self.assertEqual(len(lyrics), 1)
        self.assertAlmostEqual(lyrics[0].startS, 1.5)
        self.assertAlmostEqual(lyrics[0].endS, 3.0)

    def test_resolve_full_track_lyrics_does_not_use_window_aligned_fallback(self) -> None:
        result = SimpleNamespace(
            matching_used_track="primary",
            primary_full_word_level_lyrics_timestamps=[],
            primary_full_lyrics_timestamps=[],
            secondary_full_word_level_lyrics_timestamps=[],
            secondary_full_lyrics_timestamps=[],
            word_level_lyrics_timestamps=[
                WordTS(text="window_only", startS=500.0, endS=900.0, i=0),
            ],
            lyrics_timestamps=[
                WordTS(text="window_only", startS=500.0, endS=900.0, i=0),
            ],
        )

        lyrics, source = _resolve_full_track_lyrics(
            result,
            reference_duration_s=30.0,
        )

        self.assertEqual(lyrics, [])
        self.assertEqual(source, "none")


if __name__ == "__main__":
    unittest.main()
