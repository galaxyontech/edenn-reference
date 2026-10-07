import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from EdennCode.Util.MediaUtils import get_video_duration
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.scene_segmentation_stage import (
    SceneSegmentationStage,
    extract_frame_jpeg_bytes,
)
from EdennCode.TestSuites.helpers.paths import (
    CONTENT_POLICY_SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH,
    SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH,
)


REGRESSION_TIMESTAMPS = (
    0.0,
    6.0,
    7.033333333333333,
    8.1,
    9.133333333333333,
    10.166666666666666,
    11.133333333333333,
    12.266666666666667,
    14.133333333333333,
    15.6,
    18.535,
)

CONTENT_POLICY_REGRESSION_TIMESTAMPS = (
    41.93333333333333,
    42.13333333333333,
    43.7,
)


class _AlwaysFailingCapture:
    def __init__(self) -> None:
        self.released = False

    def isOpened(self) -> bool:
        return True

    def get(self, prop: int) -> float:
        if prop == cv2.CAP_PROP_FPS:
            return 30.0
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return 557.0
        return 0.0

    def set(self, prop: int, value: float) -> bool:
        return True

    def read(self):
        return False, None

    def release(self) -> None:
        self.released = True


class SceneSegmentationFrameExtractionTests(unittest.TestCase):
    def test_extract_frame_jpeg_bytes_handles_regression_video_timestamps(self) -> None:
        self.assertTrue(SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.exists())

        duration = get_video_duration(SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH)
        for timestamp in REGRESSION_TIMESTAMPS:
            with self.subTest(timestamp=timestamp):
                frame_bytes = extract_frame_jpeg_bytes(
                    SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH,
                    timestamp,
                    duration_hint=duration,
                )
                self.assertGreater(len(frame_bytes), 0)

                frame = cv2.imdecode(
                    np.frombuffer(frame_bytes, dtype=np.uint8),
                    cv2.IMREAD_COLOR,
                )
                self.assertIsNotNone(frame)
                self.assertGreater(frame.size, 0)

    def test_extract_frame_jpeg_bytes_handles_content_policy_regression_video_timestamps(self) -> None:
        self.assertTrue(CONTENT_POLICY_SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.exists())

        duration = get_video_duration(CONTENT_POLICY_SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH)
        for timestamp in CONTENT_POLICY_REGRESSION_TIMESTAMPS:
            with self.subTest(timestamp=timestamp):
                frame_bytes = extract_frame_jpeg_bytes(
                    CONTENT_POLICY_SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH,
                    timestamp,
                    duration_hint=duration,
                )
                self.assertGreater(len(frame_bytes), 0)

                frame = cv2.imdecode(
                    np.frombuffer(frame_bytes, dtype=np.uint8),
                    cv2.IMREAD_COLOR,
                )
                self.assertIsNotNone(frame)
                self.assertGreater(frame.size, 0)

    def test_extract_frame_jpeg_bytes_falls_back_to_ffmpeg_after_opencv_retries(self) -> None:
        failing_capture = _AlwaysFailingCapture()
        with patch(
            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.scene_segmentation_stage.cv2.VideoCapture",
            return_value=failing_capture,
        ), patch(
            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.scene_segmentation_stage._extract_frame_jpeg_bytes_with_ffmpeg",
            return_value=b"ffmpeg-frame",
        ) as ffmpeg_fallback:
            output = extract_frame_jpeg_bytes(
                SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH,
                18.535,
                duration_hint=18.566667,
            )

        self.assertEqual(output, b"ffmpeg-frame")
        ffmpeg_fallback.assert_called_once_with(
            SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH,
            18.535,
            duration=18.566667,
            fps=30.0,
        )
        self.assertTrue(failing_capture.released)

    def test_persist_thumbnail_from_b64_writes_webp_thumbnail(self) -> None:
        frame = np.full((12, 18, 3), 180, dtype=np.uint8)
        ok, jpeg_buffer = cv2.imencode(".jpg", frame)
        self.assertTrue(ok)

        with tempfile.TemporaryDirectory(prefix="thumbnail-webp-") as tmp:
            tmp_dir = Path(tmp)
            video_path = tmp_dir / "sample.mp4"
            video_path.write_bytes(b"video")

            thumbnail_path = SceneSegmentationStage._persist_thumbnail_from_b64(
                video_path,
                base64.b64encode(jpeg_buffer.tobytes()).decode("ascii"),
            )

            self.assertIsNotNone(thumbnail_path)
            self.assertEqual(thumbnail_path.suffix, ".webp")
            self.assertTrue(thumbnail_path.exists())

            decoded = cv2.imdecode(
                np.frombuffer(thumbnail_path.read_bytes(), dtype=np.uint8),
                cv2.IMREAD_COLOR,
            )
            self.assertIsNotNone(decoded)
            self.assertEqual(decoded.shape[:2], (12, 18))


if __name__ == "__main__":
    unittest.main()
