import unittest
from pathlib import Path
from unittest.mock import patch

from EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.BeatAlignmentStage.beat_alignment_stage import (
    BeatAlignmentStage,
    BeatAlignmentStageInput,
)


class BeatAlignmentStageTests(unittest.IsolatedAsyncioTestCase):
    def test_align_sections_to_beats_preserves_section_windows(self) -> None:
        beats = [0.0, 1.0, 2.0, 3.0, 4.5, 6.0]
        durations = BeatAlignmentStage._align_sections_to_beats(
            beats,
            section_boundaries_s=[0.0, 3.0, 6.0],
            section_image_counts=[2, 2],
        )

        self.assertIsNotNone(durations)
        self.assertEqual(len(durations), 4)
        self.assertAlmostEqual(sum(durations[:2]), 3.0)
        self.assertAlmostEqual(sum(durations[2:]), 3.0)

    def test_align_sections_to_beats_falls_back_uniformly_with_sparse_beats(self) -> None:
        durations = BeatAlignmentStage._align_sections_to_beats(
            [0.0, 6.0],
            section_boundaries_s=[0.0, 3.0, 6.0],
            section_image_counts=[2, 2],
        )

        self.assertEqual(durations, [1.5, 1.5, 1.5, 1.5])

    async def test_run_uses_section_aware_alignment_when_beats_are_available(self) -> None:
        stage = BeatAlignmentStage()
        stage_input = BeatAlignmentStageInput(
            music_path=Path("/tmp/fake_music.wav"),
            image_count=3,
            fallback_duration=2.0,
            section_boundaries_s=[0.0, 2.0, 6.0],
            section_image_counts=[1, 2],
        )

        with patch.object(stage, "_extract_beats", return_value=[0.0, 1.0, 3.0, 4.0, 6.0]):
            output = await stage.run(stage_input)

        self.assertTrue(output.beats_detected)
        self.assertEqual(len(output.durations), 3)
        self.assertAlmostEqual(sum(output.durations[:1]), 2.0)
        self.assertAlmostEqual(sum(output.durations[1:]), 4.0)

    async def test_run_returns_uniform_fallback_when_beat_extraction_fails(self) -> None:
        stage = BeatAlignmentStage()
        stage_input = BeatAlignmentStageInput(
            music_path=Path("/tmp/fake_music.wav"),
            image_count=2,
            fallback_duration=1.25,
        )

        with patch.object(stage, "_extract_beats", side_effect=ValueError("no beats")):
            output = await stage.run(stage_input)

        self.assertFalse(output.beats_detected)
        self.assertEqual(output.durations, [1.25, 1.25])
