from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from EdennCode.Util.MediaUtils.sfx_timeline import SfxTimelineClip, render_sfx_timeline_wav
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.tests._synthetic_media import (
    make_click_wav,
    read_wav,
)

SR = 44_100


class SfxTimelineRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _click(self, name: str, **kwargs) -> Path:
        return make_click_wav(self.tmp / name, sample_rate=SR, **kwargs)

    def test_clip_placed_at_start_time(self) -> None:
        clip_path = self._click("click.wav", duration_s=0.2, click_duration_s=0.2)
        out = render_sfx_timeline_wav(
            [SfxTimelineClip(audio_path=clip_path, start_s=1.0, fade_in_s=0.0, fade_out_s=0.0)],
            self.tmp / "timeline.wav",
            total_duration_s=2.0,
            sample_rate=SR,
        )
        samples, rate, channels = read_wav(out)
        self.assertEqual(rate, SR)
        self.assertEqual(channels, 1)
        self.assertEqual(samples.shape[0], 2 * SR)
        mono = samples[:, 0]
        # Silent before 1.0s, energetic right after.
        self.assertAlmostEqual(float(np.abs(mono[: int(0.99 * SR)]).max()), 0.0, places=3)
        self.assertGreater(float(np.abs(mono[int(1.0 * SR): int(1.2 * SR)]).max()), 0.4)

    def test_gain_and_mute(self) -> None:
        clip_path = self._click("click.wav", duration_s=0.2, click_duration_s=0.2, amplitude=0.5)
        loud = render_sfx_timeline_wav(
            [SfxTimelineClip(audio_path=clip_path, start_s=0.0, gain_db=-20.0, fade_in_s=0.0, fade_out_s=0.0)],
            self.tmp / "quiet.wav",
            total_duration_s=0.5,
            sample_rate=SR,
        )
        samples, _, _ = read_wav(loud)
        peak = float(np.abs(samples).max())
        self.assertAlmostEqual(peak, 0.05, delta=0.01)  # 0.5 at -20dB

        muted = render_sfx_timeline_wav(
            [SfxTimelineClip(audio_path=clip_path, start_s=0.0, muted=True)],
            self.tmp / "muted.wav",
            total_duration_s=0.5,
            sample_rate=SR,
        )
        samples, _, _ = read_wav(muted)
        self.assertAlmostEqual(float(np.abs(samples).max()), 0.0, places=4)

    def test_loop_fills_to_target(self) -> None:
        clip_path = self._click("bed.wav", duration_s=0.4, click_duration_s=0.4, amplitude=0.3)
        out = render_sfx_timeline_wav(
            [
                SfxTimelineClip(
                    audio_path=clip_path,
                    start_s=0.0,
                    loop_until_s=2.0,
                    fade_in_s=0.0,
                    fade_out_s=0.0,
                )
            ],
            self.tmp / "looped.wav",
            total_duration_s=2.0,
            sample_rate=SR,
        )
        samples, _, _ = read_wav(out)
        mono = samples[:, 0]
        # Energy present deep into the timeline, well past the source length.
        tail = mono[int(1.6 * SR): int(1.9 * SR)]
        self.assertGreater(float(np.sqrt((tail**2).mean())), 0.05)

    def test_stereo_pan(self) -> None:
        clip_path = self._click("click.wav", duration_s=0.2, click_duration_s=0.2)
        out = render_sfx_timeline_wav(
            [SfxTimelineClip(audio_path=clip_path, start_s=0.0, pan=-1.0, fade_in_s=0.0, fade_out_s=0.0)],
            self.tmp / "stereo.wav",
            total_duration_s=0.5,
            sample_rate=SR,
            channels=2,
        )
        samples, _, channels = read_wav(out)
        self.assertEqual(channels, 2)
        left = float(np.abs(samples[:, 0]).max())
        right = float(np.abs(samples[:, 1]).max())
        self.assertGreater(left, 0.4)
        self.assertLess(right, 0.05)

    def test_soft_limiter_preserves_shape(self) -> None:
        clip_path = self._click("click.wav", duration_s=0.2, click_duration_s=0.2, amplitude=0.9)
        # Two identical overlapping clips would sum to 1.8 — limiter must scale, not clip.
        out = render_sfx_timeline_wav(
            [
                SfxTimelineClip(audio_path=clip_path, start_s=0.0, fade_in_s=0.0, fade_out_s=0.0),
                SfxTimelineClip(audio_path=clip_path, start_s=0.0, fade_in_s=0.0, fade_out_s=0.0),
            ],
            self.tmp / "limited.wav",
            total_duration_s=0.5,
            sample_rate=SR,
        )
        samples, _, _ = read_wav(out)
        peak = float(np.abs(samples).max())
        self.assertLessEqual(peak, 0.99)
        self.assertGreater(peak, 0.9)


if __name__ == "__main__":
    unittest.main()
