import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from EdennCode.Util.MediaUtils import get_image_dimensions
from EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.PreprocessStage.preprocess_stage import (
    PreprocessStage,
    PreprocessStageInput,
)


def _write_test_image(path: Path, *, width: int, height: int) -> None:
    image = np.full((height, width, 3), 180, dtype=np.uint8)
    ok, encoded = cv2.imencode(path.suffix or ".png", image)
    if not ok:
        raise RuntimeError("Failed to encode image")
    path.write_bytes(encoded.tobytes())


class PreprocessStageTests(unittest.TestCase):
    def test_stage_compresses_images_whose_long_edge_exceeds_1280(self) -> None:
        with tempfile.TemporaryDirectory(prefix="multi-image-preprocess-") as tmp:
            tmp_dir = Path(tmp)
            source_dir = tmp_dir / "images"
            output_dir = tmp_dir / "processed"
            source_dir.mkdir(parents=True, exist_ok=True)
            oversized = source_dir / "oversized.png"
            _write_test_image(oversized, width=2200, height=1500)

            result = PreprocessStage().run(
                PreprocessStageInput(
                    folder_path=source_dir,
                    output_dir=output_dir,
                )
            )

            self.assertTrue(result.compression_applied)
            self.assertEqual(len(result.preprocessed_images), 1)
            self.assertNotEqual(result.preprocessed_images[0], oversized)
            width, height = get_image_dimensions(result.preprocessed_images[0])
            self.assertLessEqual(max(width, height), 1280)

    def test_stage_keeps_images_within_threshold_unchanged(self) -> None:
        with tempfile.TemporaryDirectory(prefix="multi-image-preprocess-") as tmp:
            tmp_dir = Path(tmp)
            source_dir = tmp_dir / "images"
            source_dir.mkdir(parents=True, exist_ok=True)
            normal = source_dir / "frame.png"
            _write_test_image(normal, width=1000, height=800)

            result = PreprocessStage().run(
                PreprocessStageInput(folder_path=source_dir)
            )

            self.assertFalse(result.compression_applied)
            self.assertEqual(result.preprocessed_images, [normal.resolve()])


if __name__ == "__main__":
    unittest.main()
