from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from EdennCode.Util.MediaUtils import compress_image_to_max_dimension, get_image_dimensions

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


@dataclass
class PreprocessStageInput:
    folder_path: Path
    output_dir: Optional[Path] = None
    max_image_dimension: int = 1280


@dataclass
class PreprocessStageOutput:
    preprocessed_images: List[Path]
    compression_applied: bool = False


class PreprocessStage:
    """Basic preprocessing that validates and sorts image inputs."""

    def run(self, stage_input: PreprocessStageInput) -> PreprocessStageOutput:
        folder = stage_input.folder_path.expanduser().resolve()
        if not folder.exists() or not folder.is_dir():
            raise FileNotFoundError(f"Input folder does not exist: {folder}")

        images: List[Path] = []
        for item in folder.iterdir():
            if not item.is_file():
                continue
            if item.suffix.lower() in SUPPORTED_EXTENSIONS:
                images.append(item)

        images.sort(key=lambda path: path.name.lower())
        if not images:
            raise RuntimeError(f"No image files found in folder: {folder}")

        output_dir = (
            stage_input.output_dir.expanduser().resolve()
            if stage_input.output_dir is not None
            else folder / "_preprocessed"
        )

        processed_images: List[Path] = []
        compression_applied = False
        for item in images:
            width, height = get_image_dimensions(item)
            if max(width, height) <= stage_input.max_image_dimension:
                processed_images.append(item)
                continue

            compression_applied = True
            compressed_path = output_dir / item.name
            processed_images.append(
                compress_image_to_max_dimension(
                    item,
                    output_path=compressed_path,
                    max_dimension=stage_input.max_image_dimension,
                )
            )

        return PreprocessStageOutput(
            preprocessed_images=processed_images,
            compression_applied=compression_applied,
        )
