import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from EdennCode.WorkflowFactory.MultiImageWorkflow import (
    MultiImageGenerationE2EStage,
    MultiImageWorkflowStageOutput,
    run_multi_image_pipeline,
)


class MultiImageWorkflowSurfaceTests(unittest.TestCase):
    def test_package_exports_load(self) -> None:
        self.assertTrue(callable(run_multi_image_pipeline))
        self.assertIsNotNone(MultiImageGenerationE2EStage)

    def test_run_multi_image_pipeline_invokes_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            folder_path = tmp_dir / "images"
            output_path = tmp_dir / "slideshow.mp4"
            folder_path.mkdir(parents=True, exist_ok=True)

            expected = MultiImageWorkflowStageOutput(
                final_video_path=output_path,
                silent_video_path=tmp_dir / "slideshow_silent.mp4",
                music_path=tmp_dir / "music.wav",
                matched_music_path=tmp_dir / "music.wav",
                full_track_paths=[tmp_dir / "music.wav"],
                processed_image_paths=[folder_path / "frame1.png"],
                planning_metadata={"image_order": [1]},
                music_prompt="bright cinematic pop",
                lyrics_timestamps=[],
                used_modelspec="edenn_basic",
                section_timeline=[],
                video_title="Story",
                music_title="Story Song",
                video_description="Description",
                include_vocals=True,
                vocal_gender="male",
                lyrics_language="EN",
                user_requested_language="ENGLISH_US",
                compression_applied=False,
            )

            with patch(
                "EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.MultiImageE2EGenerationStage.multi_image_generation_e2e_stage.build_azure_client",
                return_value=MagicMock(),
            ), patch(
                "EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.MultiImageE2EGenerationStage.multi_image_generation_e2e_stage.build_music_generation_service",
                return_value=MagicMock(),
            ), patch.object(
                MultiImageGenerationE2EStage,
                "run",
                new=AsyncMock(return_value=expected),
            ) as run_mock:
                result = run_multi_image_pipeline(
                    folder_path,
                    output_path,
                    user_prompt="Make it energetic",
                    include_vocals=True,
                    vocal_gender="male",
                    align_to_beats=False,
                    lyrics_language="EN",
                    modelspec="edenn_studio",
                )

            self.assertEqual(result, expected)
            run_mock.assert_awaited_once()
            stage_input = run_mock.await_args.args[0]
            self.assertEqual(stage_input.folder_path, folder_path)
            self.assertEqual(stage_input.output_path, output_path)
            self.assertEqual(stage_input.user_prompt, "Make it energetic")
            self.assertTrue(stage_input.include_vocals)
            self.assertEqual(stage_input.vocal_gender, "male")
            self.assertFalse(stage_input.align_to_beats)
            self.assertEqual(stage_input.lyrics_language, "EN")
            self.assertEqual(stage_input.modelspec, "edenn_studio")


if __name__ == "__main__":
    unittest.main()
