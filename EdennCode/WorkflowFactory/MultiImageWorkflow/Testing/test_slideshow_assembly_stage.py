import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from EdennCode.Util.MediaUtils import XFADE_TRANSITIONS
from EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.SlideshowAssemblyStage.slideshow_assembly_stage import (
    SlideshowAssemblyStage,
    SlideshowAssemblyStageInput,
)

_STAGE_MODULE = (
    "EdennCode.WorkflowFactory.MultiImageWorkflow.Stages."
    "SlideshowAssemblyStage.slideshow_assembly_stage"
)


class SlideshowAssemblyTransitionTests(unittest.TestCase):
    def _run_stage(self, **input_overrides):
        """Run the stage with ffmpeg calls mocked; returns (output, build_kwargs)."""

        with TemporaryDirectory(prefix="slideshow-stage-") as tmp:
            tmp_dir = Path(tmp)
            images = [tmp_dir / f"img{i}.png" for i in range(4)]
            defaults = dict(
                image_paths=images,
                per_image_duration=2.0,
                music_path=tmp_dir / "music.mp3",
                output_path=tmp_dir / "out.mp4",
            )
            defaults.update(input_overrides)
            captured = {}

            def _fake_build(image_paths, per_image_duration, output_path, **kwargs):
                captured.update(kwargs)
                return output_path

            with patch(f"{_STAGE_MODULE}.build_slideshow_video", side_effect=_fake_build), patch(
                f"{_STAGE_MODULE}.overlay_music_on_video",
                side_effect=lambda silent, music, final, **kwargs: final,
            ):
                output = SlideshowAssemblyStage().run(
                    SlideshowAssemblyStageInput(**defaults)
                )
            return output, captured

    def test_default_mode_keeps_hard_cuts(self) -> None:
        output, captured = self._run_stage()
        self.assertEqual(output.applied_transitions, [])
        self.assertEqual(output.applied_transition_duration_s, 0.0)
        self.assertIsNone(captured["transitions"])

    def test_random_mode_picks_one_transition_per_boundary(self) -> None:
        output, captured = self._run_stage(
            transition_mode="random", transition_seed=7
        )
        self.assertEqual(len(output.applied_transitions), 3)
        self.assertTrue(set(output.applied_transitions).issubset(XFADE_TRANSITIONS))
        self.assertEqual(captured["transitions"], output.applied_transitions)
        self.assertAlmostEqual(output.applied_transition_duration_s, 0.4)

    def test_random_mode_is_reproducible_with_seed(self) -> None:
        first, _ = self._run_stage(transition_mode="random", transition_seed=7)
        second, _ = self._run_stage(transition_mode="random", transition_seed=7)
        self.assertEqual(first.applied_transitions, second.applied_transitions)

    def test_explicit_effect_applies_to_every_boundary(self) -> None:
        output, _ = self._run_stage(transition_mode="fade")
        self.assertEqual(output.applied_transitions, ["fade", "fade", "fade"])

    def test_short_beat_durations_degrade_to_hard_cuts(self) -> None:
        output, captured = self._run_stage(
            transition_mode="random",
            durations=[0.12, 0.12, 0.12, 0.12],
        )
        self.assertEqual(output.applied_transitions, [])
        self.assertEqual(output.applied_transition_duration_s, 0.0)
        self.assertIsNone(captured["transitions"])

    def test_unknown_mode_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._run_stage(transition_mode="sparkle_explosion")

    def test_explicit_list_cycles_to_boundary_count(self) -> None:
        # 4 images -> 3 boundaries; a 2-name list cycles.
        output, captured = self._run_stage(transitions=["fade", "circleopen"])
        self.assertEqual(
            output.applied_transitions, ["fade", "circleopen", "fade"]
        )
        self.assertEqual(captured["transitions"], ["fade", "circleopen", "fade"])

    def test_explicit_list_overrides_mode(self) -> None:
        # transitions list wins even when a mode is also supplied.
        output, _ = self._run_stage(
            transition_mode="random", transitions=["wipeleft", "wiperight", "fade"]
        )
        self.assertEqual(
            output.applied_transitions, ["wipeleft", "wiperight", "fade"]
        )

    def test_explicit_list_accepts_supported_non_pool_effect(self) -> None:
        output, _ = self._run_stage(transitions=["coverleft"])
        self.assertEqual(
            output.applied_transitions, ["coverleft", "coverleft", "coverleft"]
        )

    def test_explicit_list_rejects_unsupported_name(self) -> None:
        with self.assertRaises(ValueError):
            self._run_stage(transitions=["fade", "zoomout"])


if __name__ == "__main__":
    unittest.main()
