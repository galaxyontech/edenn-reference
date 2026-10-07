import os
from pathlib import Path


TESTS_ROOT = Path(__file__).resolve().parents[1]
ASSETS_ROOT = TESTS_ROOT / "assets"

SMOKE_IMAGES_DIR = ASSETS_ROOT / "smoke" / "images"
SMOKE_AUDIO_DIR = ASSETS_ROOT / "smoke" / "audio"
SMOKE_VIDEOS_DIR = ASSETS_ROOT / "smoke" / "videos"
PRODUCTION_VIDEOS_DIR = ASSETS_ROOT / "production" / "videos"
MULTI_IMAGE_DESIGN_IMAGES_DIR = SMOKE_IMAGES_DIR / "multi_image_design"
REMOTE_VOCAL_CLONE_SAMPLE_PATH = SMOKE_AUDIO_DIR / "cn_vocal_clone_sample_30s.m4a"

# Keep the representative assets explicit so tests are easy to read.
SMOKE_VIDEO_PATH = SMOKE_VIDEOS_DIR / (
    "sample_clip.mp4"
)
SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH = SMOKE_VIDEOS_DIR / (
    "Videos2026-04-07_195857_387.mp4"
)
CONTENT_POLICY_SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH = SMOKE_VIDEOS_DIR / (
    "Videos2026-04-08_202746_494.mp4"
)
ROTATED_SMOKE_VIDEO_PATH = SMOKE_VIDEOS_DIR / (
    "Videos2026-04-08_104426_347.mp4"
)
WEIXIN_EXAMPLE_VIDEO_PATH = Path(
    os.getenv(
        "WEIXIN_EXAMPLE_VIDEO_PATH",
        "/Users/example/Downloads/Weixin Videos2026-04-14_201237_239.mp4",
    )
).expanduser().resolve()
PRODUCTION_VIDEO_PATH = PRODUCTION_VIDEOS_DIR / (
    "sample_clip.mp4"
)
