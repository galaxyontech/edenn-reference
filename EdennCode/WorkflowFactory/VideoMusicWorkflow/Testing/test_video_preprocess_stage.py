import os
from pathlib import Path
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata.video_metadata import (
    VideoMetadata,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoPreprocessStage.media_preprocess_stage import (
    InputMediaAssetTyps,
    MAX_INPUT_VIDEO_DURATION_S,
    PreprocessStage,
    PreprocessStageInput,
)
from EdennCode.exceptions import EdennValidationError
from EdennCode.TestSuites.helpers.paths import (
    ROTATED_SMOKE_VIDEO_PATH,
    SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH,
    SMOKE_VIDEO_PATH,
)


class VideoPreprocessStageTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_uses_original_video_path(self) -> None:
        resolved = SMOKE_VIDEO_PATH.resolve()
        fake_metadata = SimpleNamespace(has_audio=False, duration=12.5, path=resolved)

        with patch(
            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoPreprocessStage.media_preprocess_stage.VideoMetadata.from_file",
            return_value=fake_metadata,
        ) as metadata_mock:
            stage = PreprocessStage()
            output = await stage.run(
                PreprocessStageInput(
                    asset_path=str(SMOKE_VIDEO_PATH),
                    asset_type=InputMediaAssetTyps.VIDEO,
                )
            )

        metadata_mock.assert_called_once_with(resolved)
        self.assertIs(output.video_metadata, fake_metadata)

    async def test_run_accepts_path_objects_and_normalized_asset_types(self) -> None:
        resolved = SMOKE_VIDEO_PATH.resolve()
        fake_metadata = SimpleNamespace(has_audio=False, duration=12.5, path=resolved)

        with patch(
            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoPreprocessStage.media_preprocess_stage.VideoMetadata.from_file",
            return_value=fake_metadata,
        ) as metadata_mock:
            stage = PreprocessStage()
            output = await stage.run(
                PreprocessStageInput(
                    asset_path=SMOKE_VIDEO_PATH,
                    asset_type=" VIDEO ",
                )
            )

        metadata_mock.assert_called_once_with(resolved)
        self.assertIs(output.video_metadata, fake_metadata)

    async def test_run_populates_audio_activity_for_videos_with_audio(self) -> None:
        resolved = SMOKE_VIDEO_PATH.resolve()
        fake_metadata = SimpleNamespace(
            has_audio=True,
            path=resolved,
            duration=12.5,
            audio_activity=[],
        )

        with patch(
            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoPreprocessStage.media_preprocess_stage.VideoMetadata.from_file",
            return_value=fake_metadata,
        ) as metadata_mock, patch(
            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoPreprocessStage.media_preprocess_stage.detect_audio_activity",
            return_value=[(0.0, 0.5), (1.0, 2.25)],
        ) as audio_mock:
            stage = PreprocessStage()
            output = await stage.run(
                PreprocessStageInput(
                    asset_path=str(SMOKE_VIDEO_PATH),
                    asset_type=InputMediaAssetTyps.VIDEO,
                )
            )

        metadata_mock.assert_called_once_with(resolved)
        audio_mock.assert_called_once_with(
            resolved,
            duration_hint=12.5,
        )
        self.assertEqual(output.video_metadata.audio_activity, [(0.0, 0.5), (1.0, 2.25)])

    async def test_run_rejects_videos_longer_than_max_input_duration(self) -> None:
        resolved = SMOKE_VIDEO_PATH.resolve()
        fake_metadata = SimpleNamespace(
            has_audio=True,
            path=resolved,
            duration=MAX_INPUT_VIDEO_DURATION_S + 0.1,
            audio_activity=[],
        )

        with patch(
            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoPreprocessStage.media_preprocess_stage.VideoMetadata.from_file",
            return_value=fake_metadata,
        ), patch(
            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoPreprocessStage.media_preprocess_stage.detect_audio_activity",
        ) as audio_mock:
            stage = PreprocessStage()
            with self.assertRaises(EdennValidationError) as ctx:
                await stage.run(
                    PreprocessStageInput(
                        asset_path=str(SMOKE_VIDEO_PATH),
                        asset_type=InputMediaAssetTyps.VIDEO,
                    )
                )

        self.assertIn("300 seconds", ctx.exception.public_message)
        audio_mock.assert_not_called()

    async def test_run_rejects_missing_local_paths(self) -> None:
        stage = PreprocessStage()

        with self.assertRaises(FileNotFoundError):
            await stage.run(
                PreprocessStageInput(
                    asset_path="does/not/exist.mp4",
                    asset_type=InputMediaAssetTyps.VIDEO,
                )
            )

    async def test_run_rejects_unsupported_asset_types(self) -> None:
        stage = PreprocessStage()

        with self.assertRaises(NotImplementedError):
            await stage.run(
                PreprocessStageInput(
                    asset_path=str(SMOKE_VIDEO_PATH),
                    asset_type=InputMediaAssetTyps.IMAGE,
                )
            )

    def test_video_metadata_reports_rotation_aware_dimensions(self) -> None:
        metadata = VideoMetadata.from_file(ROTATED_SMOKE_VIDEO_PATH)

        self.assertEqual(metadata.width, 320)
        self.assertEqual(metadata.height, 568)
        self.assertAlmostEqual(metadata.duration, 20.585, places=3)

    def test_video_metadata_preserves_unrotated_dimensions(self) -> None:
        metadata = VideoMetadata.from_file(SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH)

        self.assertEqual(metadata.width, 1280)
        self.assertEqual(metadata.height, 720)
        self.assertAlmostEqual(metadata.duration, 18.567, places=3)

    def test_video_metadata_uses_system_temp_dir_by_default(self) -> None:
        with patch.dict(os.environ, {"USE_LOCAL_TEMP_DIR": "false"}, clear=False):
            metadata = VideoMetadata.from_file(SMOKE_VIDEO_PATH)

        temp_dir = Path(metadata.temp_folder)
        self.assertTrue(temp_dir.exists())
        self.assertTrue(str(temp_dir).startswith(tempfile.gettempdir()))
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_video_metadata_uses_configured_local_temp_dir_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory(prefix="video-metadata-local-") as tmp:
            with patch.dict(
                os.environ,
                {
                    "USE_LOCAL_TEMP_DIR": "true",
                    "LOCAL_TEMP_DIR": tmp,
                },
                clear=False,
            ):
                metadata = VideoMetadata.from_file(SMOKE_VIDEO_PATH)

            temp_dir = Path(metadata.temp_folder)
            self.assertEqual(temp_dir, Path(tmp) / SMOKE_VIDEO_PATH.stem)
            self.assertTrue(temp_dir.exists())


if __name__ == "__main__":
    unittest.main()
