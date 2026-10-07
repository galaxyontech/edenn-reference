from __future__ import annotations

from pathlib import Path
from typing import Tuple

import cv2
import numpy as np


def get_image_dimensions(image_path: Path) -> Tuple[int, int]:
    data = np.frombuffer(image_path.read_bytes(), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError(f"Unable to read image: {image_path}")
    height, width = image.shape[:2]
    return int(width), int(height)


def compress_image_to_max_dimension(
    image_path: Path,
    *,
    output_path: Path,
    max_dimension: int = 1280,
    jpeg_quality: int = 90,
) -> Path:
    if max_dimension <= 0:
        raise ValueError("max_dimension must be greater than 0")

    data = np.frombuffer(image_path.read_bytes(), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError(f"Unable to read image: {image_path}")

    height, width = image.shape[:2]
    longest_edge = max(width, height)
    if longest_edge <= max_dimension:
        return image_path

    scale = float(max_dimension) / float(longest_edge)
    resized_width = max(1, int(round(width * scale)))
    resized_height = max(1, int(round(height * scale)))
    resized = cv2.resize(
        image,
        (resized_width, resized_height),
        interpolation=cv2.INTER_AREA,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    extension = output_path.suffix.lower()
    encode_params: list[int] = []
    if extension in {".jpg", ".jpeg"}:
        encode_params = [cv2.IMWRITE_JPEG_QUALITY, int(jpeg_quality)]
    elif extension == ".png":
        encode_params = [cv2.IMWRITE_PNG_COMPRESSION, 6]
    elif extension == ".webp":
        encode_params = [cv2.IMWRITE_WEBP_QUALITY, int(jpeg_quality)]

    success, encoded = cv2.imencode(extension or ".png", resized, encode_params)
    if not success:
        raise RuntimeError(f"Failed to encode resized image: {output_path}")
    output_path.write_bytes(encoded.tobytes())
    return output_path
