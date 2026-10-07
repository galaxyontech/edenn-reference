from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.Stages.TimingRefinementStage.timing_refinement_stage import (
    TimingRefinementStage,
    TimingRefinementStageInput,
    compute_motion_energy,
    find_motion_onset,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.VideoSoundEffectDataModel.datamodel import (
    SoundFXEvent,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.tests._synthetic_media import (
    make_flash_video,
)


class MotionEnergyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_flash_produces_energy_peak_at_flash_time(self) -> None:
        video = make_flash_video(self.tmp / "flash.mp4", duration_s=3.0, flash_times_s=[1.5])
        times, energy = compute_motion_energy(video, start_s=0.0, end_s=3.0, sample_fps=24.0)
        self.assertGreater(times.size, 10)
        onset = find_motion_onset(times, energy)
        self.assertIsNotNone(onset)
        self.assertAlmostEqual(onset, 1.5, delta=0.15)

    def test_static_video_yields_no_onset(self) -> None:
        video = make_flash_video(self.tmp / "static.mp4", duration_s=2.0, flash_times_s=[])
        times, energy = compute_motion_energy(video, start_s=0.0, end_s=2.0, sample_fps=24.0)
        onset = find_motion_onset(times, energy)
        self.assertIsNone(onset)


class TimingRefinementStageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_snaps_event_start_to_visual_onset(self) -> None:
        video = make_flash_video(self.tmp / "flash.mp4", duration_s=3.0, flash_times_s=[1.5])
        # LLM was ~0.4s early — a realistic second-level error.
        event = SoundFXEvent(
            event_id="event_001",
            start_time=1.1,
            end_time=2.0,
            event_description="white flash impact",
            sound_event_local_path="",
            event_type="IMPACT",
        )
        output = asyncio.run(
            TimingRefinementStage().run(
                TimingRefinementStageInput(
                    video_path=video,
                    events=[event],
                    video_duration=3.0,
                )
            )
        )
        refinement = output.refinements[0]
        self.assertTrue(refinement.snapped)
        self.assertAlmostEqual(event.refined_start_time, 1.5, delta=0.15)
        self.assertAlmostEqual(event.effective_start, event.refined_start_time)
        # Original LLM estimate stays inspectable.
        self.assertAlmostEqual(event.start_time, 1.1)

    def test_flat_window_keeps_llm_timing(self) -> None:
        video = make_flash_video(self.tmp / "static.mp4", duration_s=2.0, flash_times_s=[])
        event = SoundFXEvent(
            event_id="event_001",
            start_time=1.0,
            end_time=1.6,
            event_description="nothing visible",
            sound_event_local_path="",
            event_type="IMPACT",
        )
        output = asyncio.run(
            TimingRefinementStage().run(
                TimingRefinementStageInput(video_path=video, events=[event], video_duration=2.0)
            )
        )
        self.assertFalse(output.refinements[0].snapped)
        self.assertIsNone(event.refined_start_time)
        self.assertAlmostEqual(event.effective_start, 1.0)


if __name__ == "__main__":
    unittest.main()
