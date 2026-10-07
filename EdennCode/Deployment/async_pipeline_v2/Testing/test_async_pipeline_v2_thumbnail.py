"""Thorough tests for the <100KB thumbnail guarantee across all input shapes."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from EdennCode.Deployment.async_pipeline_v2.workers.monolith_worker import (
    THUMBNAIL_MAX_BYTES,
    _compress_thumbnail_under_limit,
    _generate_thumbnail_from_video,
)

SMOKE_VIDEO = Path(
    "EdennCode/TestSuites/assets/smoke/videos/"
    "sample_clip.mp4"
)


def _make_frame(height: int, width: int, content: str) -> np.ndarray:
    if content == "noise":  # worst case for compression
        rng = np.random.default_rng(1234)
        return rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)
    if content == "checkerboard":  # high spatial frequency
        ys, xs = np.indices((height, width))
        board = (((xs // 4) + (ys // 4)) % 2 * 255).astype(np.uint8)
        return np.stack([board, board, board], axis=-1)
    if content == "gradient":
        row = np.linspace(0, 255, width, dtype=np.uint8)
        grad = np.tile(row, (height, 1))
        return np.stack([grad, grad[::-1], grad], axis=-1)
    return np.full((height, width, 3), 127, dtype=np.uint8)  # solid


@pytest.mark.parametrize(
    "height,width",
    [
        (2160, 3840),   # 4K landscape
        (3840, 2160),   # 4K portrait
        (1080, 1920),   # 1080p
        (720, 1280),    # 720p
        (1080, 1080),   # square
        (4320, 7680),   # 8K landscape
        (999, 1001),    # odd dims
        (1920, 1080),   # portrait phone video
        (240, 320),     # already small but detailed
    ],
)
@pytest.mark.parametrize("content", ["noise", "checkerboard", "gradient", "solid"])
def test_thumbnail_always_under_100kb(tmp_path: Path, height: int, width: int, content: str) -> None:
    frame = _make_frame(height, width, content)
    src = tmp_path / f"src_{height}x{width}_{content}.png"
    assert cv2.imwrite(str(src), frame)

    out = _compress_thumbnail_under_limit(src, tmp_path)

    assert out is not None and out.exists()
    size = out.stat().st_size
    assert size <= THUMBNAIL_MAX_BYTES, f"{content} {width}x{height} -> {size} bytes (>100KB)"
    # Output must be a valid, decodable image.
    decoded = cv2.imread(str(out), cv2.IMREAD_COLOR)
    assert decoded is not None and decoded.size > 0


def test_thumbnail_already_small_is_returned_unchanged(tmp_path: Path) -> None:
    frame = np.full((120, 120, 3), 200, dtype=np.uint8)
    src = tmp_path / "tiny.webp"
    cv2.imwrite(str(src), frame, [int(cv2.IMWRITE_WEBP_QUALITY), 80])
    assert src.stat().st_size <= THUMBNAIL_MAX_BYTES
    out = _compress_thumbnail_under_limit(src, tmp_path)
    assert out == src  # untouched when already within budget


def test_thumbnail_unreadable_source_returns_original(tmp_path: Path) -> None:
    bad = tmp_path / "not_an_image.webp"
    bad.write_bytes(b"this is not an image" * 100)
    out = _compress_thumbnail_under_limit(bad, tmp_path)
    assert out == bad  # graceful: never fail the pipeline over a thumbnail


def test_thumbnail_missing_source_returns_original(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.png"
    out = _compress_thumbnail_under_limit(missing, tmp_path)
    assert out == missing


@pytest.mark.skipif(not SMOKE_VIDEO.exists(), reason="smoke video asset not present")
def test_generate_thumbnail_from_real_video_under_100kb(tmp_path: Path) -> None:
    src = tmp_path / SMOKE_VIDEO.name
    src.write_bytes(SMOKE_VIDEO.read_bytes())
    out = _generate_thumbnail_from_video(src, tmp_path)
    assert out is not None and out.exists()
    assert out.stat().st_size <= THUMBNAIL_MAX_BYTES
    assert cv2.imread(str(out), cv2.IMREAD_COLOR) is not None


def test_pick_representative_frame_prefers_first_frame(tmp_path: Path) -> None:
    """Matches v1: a video whose first frame has content yields that first frame."""
    from EdennCode.Deployment.async_pipeline_v2.workers.monolith_worker import (
        _pick_representative_frame,
    )

    h, w = 240, 320
    path = tmp_path / "distinct.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 24.0, (w, h))
    if not writer.isOpened():
        pytest.skip("mp4 VideoWriter unavailable in this OpenCV build")
    try:
        # Frame 0..23: distinctive red gradient; rest: blue gradient (different content)
        row = np.linspace(0, 255, w, dtype=np.uint8)
        red = np.stack([np.zeros((h, w), np.uint8), np.zeros((h, w), np.uint8),
                        np.tile(row, (h, 1))], axis=-1)
        blue = np.stack([np.tile(row[::-1], (h, 1)), np.zeros((h, w), np.uint8),
                         np.zeros((h, w), np.uint8)], axis=-1)
        for _ in range(24):
            writer.write(red)
        for _ in range(72):
            writer.write(blue)
    finally:
        writer.release()
    if not path.exists() or path.stat().st_size == 0:
        pytest.skip("synthetic video not written")

    frame = _pick_representative_frame(path)
    assert frame is not None
    # The opening (red) frame should be chosen, not a later (blue) frame.
    b, g, r = frame[:, :, 0].mean(), frame[:, :, 1].mean(), frame[:, :, 2].mean()
    assert r > b, f"expected the opening red frame, got means b={b:.0f} g={g:.0f} r={r:.0f}"


def test_generate_thumbnail_skips_black_intro(tmp_path: Path) -> None:
    """A fade-in-from-black video must not produce a black thumbnail (the cache-hit bug)."""
    from EdennCode.Deployment.async_pipeline_v2.workers.monolith_worker import (
        _THUMBNAIL_MIN_CONTENT_STD,
    )

    h, w = 240, 320
    path = tmp_path / "fadein.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 24.0, (w, h))
    if not writer.isOpened():
        pytest.skip("mp4 VideoWriter unavailable in this OpenCV build")
    try:
        for _ in range(30):  # ~1.25s pure black intro
            writer.write(np.zeros((h, w, 3), dtype=np.uint8))
        for i in range(60):  # ~2.5s of colorful content
            row = np.linspace(0, 255, w, dtype=np.uint8)
            frame = np.stack([np.tile(row, (h, 1)), np.full((h, w), (i * 4) % 256, np.uint8),
                              np.tile(row[::-1], (h, 1))], axis=-1)
            writer.write(frame)
    finally:
        writer.release()
    if not path.exists() or path.stat().st_size == 0:
        pytest.skip("synthetic video not written")

    out = _generate_thumbnail_from_video(path, tmp_path)
    assert out is not None and out.exists()
    img = cv2.imread(str(out), cv2.IMREAD_COLOR)
    assert img is not None
    assert float(img.std()) >= _THUMBNAIL_MIN_CONTENT_STD, f"thumbnail is blank (std={img.std():.1f})"
    assert out.stat().st_size <= THUMBNAIL_MAX_BYTES
