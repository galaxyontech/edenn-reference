"""Unit tests for the shared complete-music metadata helpers used across the
multi-image, video-music, and audio-creative-edit responses."""
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from EdennCode.Deployment.api_common import probe_audio_metrics
from EdennCode.Deployment.api_video_generation import (
    MUSIC_TITLE_FALLBACK,
    _music_title_from_summary,
)
from EdennCode.Util.MediaUtils.ffmpeg_utils import resolve_ffmpeg_binary


class ProbeAudioMetricsTests(unittest.TestCase):
    def test_returns_duration_and_size_for_real_audio(self) -> None:
        with TemporaryDirectory(prefix="probe-audio-") as tmp:
            wav = Path(tmp) / "tone.wav"
            subprocess.run(
                [resolve_ffmpeg_binary(), "-y", "-f", "lavfi", "-i",
                 "sine=frequency=440:duration=2", str(wav)],
                check=True, capture_output=True, text=True,
            )
            duration_s, size_bytes = probe_audio_metrics(wav)
            self.assertIsNotNone(duration_s)
            self.assertAlmostEqual(duration_s, 2.0, delta=0.2)
            self.assertIsNotNone(size_bytes)
            self.assertEqual(size_bytes, wav.stat().st_size)
            self.assertGreater(size_bytes, 0)

    def test_none_path_degrades_to_none(self) -> None:
        self.assertEqual(probe_audio_metrics(None), (None, None))

    def test_missing_file_degrades_to_none(self) -> None:
        duration_s, size_bytes = probe_audio_metrics(Path("/no/such/file.wav"))
        self.assertIsNone(duration_s)
        self.assertIsNone(size_bytes)


class MusicTitleFromSummaryTests(unittest.TestCase):
    def test_extracts_music_title_from_dict(self) -> None:
        # music_title wins over video_title whenever it is present and non-blank.
        summary = {"video_title": "Summer Trip Recap", "music_title": "Golden Hour"}
        self.assertEqual(_music_title_from_summary(summary), "Golden Hour")
        self.assertEqual(
            _music_title_from_summary({"music_title": "  Golden Hour  "}), "Golden Hour"
        )

    def test_missing_or_blank_music_title_falls_back_to_video_title(self) -> None:
        self.assertEqual(_music_title_from_summary({"video_title": "X"}), "X")
        self.assertEqual(
            _music_title_from_summary({"music_title": "   ", "video_title": "X"}), "X"
        )
        self.assertEqual(
            _music_title_from_summary({"music_title": None, "video_title": "  X  "}), "X"
        )

    def test_falls_back_to_constant_when_no_usable_title(self) -> None:
        # The whole point of the fallback chain: the field is never empty.
        self.assertTrue(MUSIC_TITLE_FALLBACK.strip())
        self.assertEqual(_music_title_from_summary({}), MUSIC_TITLE_FALLBACK)
        self.assertEqual(
            _music_title_from_summary({"music_title": "   ", "video_title": "  "}),
            MUSIC_TITLE_FALLBACK,
        )

    def test_non_dict_summary_falls_back_to_constant(self) -> None:
        self.assertEqual(_music_title_from_summary("just a string"), MUSIC_TITLE_FALLBACK)
        self.assertEqual(_music_title_from_summary(None), MUSIC_TITLE_FALLBACK)


if __name__ == "__main__":
    unittest.main()
