from __future__ import annotations

import os
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

from EdennCode.ModelFactory.LanguageModelFactory.azure_based_model_model_gateway import AzureMultimodalClient
from EdennCode.WorkflowFactory.ImageMusicGeneration.workflow import ImageMusicWorkflow, MusicGenResult
from EdennCode.Util.MediaUtils import mux_image_with_audio
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_a_wrapper import ProviderAMusicProvider  # type: ignore
from EdennCode.env import load_env


def pick_image(path_hint: Path) -> Path:
    """
    Resolve a usable image path.
    - If a file is given, return it.
    - If a directory is given, pick a random image inside.
    """
    if path_hint.is_file():
        return path_hint
    if path_hint.is_dir():
        candidates = []
        for pattern in ("*.jpg", "*.jpeg", "*.png"):
            candidates.extend(path_hint.glob(pattern))
        if not candidates:
            raise FileNotFoundError(f"No images found under {path_hint}.")
        return random.choice(candidates)
    raise FileNotFoundError(f"{path_hint} is neither a file nor a directory.")


def require_env(key: str) -> str:
    """Fetch an env var or fail fast with a clear message."""
    value = os.getenv(key, "").strip()
    if not value:
        raise RuntimeError(f"Set {key} in your environment or .env.")
    return value


def build_pipeline() -> tuple[ImageMusicWorkflow, Path]:
    default_image_dir = REPO_ROOT / "StaticAssets" / "LocalAdsExample"
    image_hint_env = "/path/to/repo/EdennCode/ImageMusicGeneration/Screenshot 2025-12-09 at 11.00.26 AM.png"
    image_hint = Path(image_hint_env).expanduser() if image_hint_env else default_image_dir
    if not image_hint.exists():
        raise FileNotFoundError(f"No such image path: {image_hint}")

    azure_endpoint = require_env("AZURE_ENDPOINT")
    if not azure_endpoint.startswith(("http://", "https://")):
        raise ValueError("AZURE_ENDPOINT must start with http:// or https://")

    azure_client = AzureMultimodalClient(
        azure_endpoint=azure_endpoint,
        azure_api_version=os.getenv("AZURE_API_VERSION", "2024-12-01-preview"),
        azure_model=require_env("AZURE_MODEL"),
        api_key=require_env("AZURE_API_KEY"),
    )

    audio_output_dir = Path(
        os.getenv("OUTPUT_AUDIO_DIR", str(REPO_ROOT / "outputs" / "audio"))
    ).expanduser()
    audio_output_dir.mkdir(parents=True, exist_ok=True)

    eleven_provider = ProviderAMusicProvider(
        api_key=require_env("PROVIDER_A_API_KEY"),
        use_stream=True,
        default_output_dir=audio_output_dir,
    )

    processor = ImageMusicWorkflow(
        azure_client=azure_client,
        music_provider=eleven_provider,
    )
    return processor, image_hint


def main() -> None:
    load_env()
    processor, image_dir = build_pipeline()
    image_path = pick_image(image_dir)

    print(f"Selected image: {image_path}")
    result: MusicGenResult = processor.run(image_path)
    print(f"Azure description: {result.description}")
    print(f"Music prompt: {result.music_prompt}")

    audio_path = Path(result.audio_url)
    if not audio_path.exists():
        raise FileNotFoundError(f"ProviderA did not return a local path: {result.audio_url}")
    print(f"Audio written to: {audio_path}")
    import datetime
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    default_preview = REPO_ROOT / "outputs" / "previews" / f"{image_path.stem}{timestamp}_with_music.mp4"
    video_output = Path(os.getenv("DEMO_VIDEO_OUTPUT", str(default_preview))).expanduser()
    video_output.parent.mkdir(parents=True, exist_ok=True)
    mux_image_with_audio(image_path, audio_path, video_output)
    print(f"Preview ready: {video_output}")


if __name__ == "__main__":
    main()
