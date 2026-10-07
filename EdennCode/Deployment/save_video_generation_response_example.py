from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from EdennCode.Deployment.api_common import ApiContext
from EdennCode.Deployment.api_video_generation import create_video_generation_router
from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Deployment.storage import create_storage_service
from EdennCode.Deployment.workflows import VideoGenerationOrchestrator
from EdennCode.env import load_env


def _default_local_temp_dir(modelspec: str) -> str:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    return f"temp_video_workdir/video_api_example_{modelspec}_{timestamp}"


def _build_app(*, local_temp_dir: str) -> FastAPI:
    load_env()
    os.environ["USE_LOCAL_TEMP_DIR"] = "true"
    os.environ["LOCAL_TEMP_DIR"] = local_temp_dir

    settings = DeploymentSettings.from_env()
    storage = create_storage_service(settings)
    workflow = VideoGenerationOrchestrator(
        storage=storage,
        llm_image_container=settings.llm_image_container,
        llm_image_sas_ttl_minutes=settings.llm_image_sas_ttl_minutes,
        llm_image_cleanup_delay_seconds=settings.llm_image_cleanup_delay_seconds,
    )
    context = ApiContext(
        settings=settings,
        storage=storage,
        workflow=workflow,
        alignment_workflow=None,
        audio_creative_edit_workflow=None,
        logger=logging.getLogger("save_video_generation_response_example"),
    )
    app = FastAPI()
    app.include_router(create_video_generation_router(context))
    return app


def save_video_generation_response_example(
    *,
    video_path: Path,
    user_prompt: str,
    modelspec: str,
    include_vocals: bool,
    vocal_gender: str,
    output_json: Path,
    audio_output_format: str | None = None,
    local_temp_dir: str | None = None,
) -> dict:
    app = _build_app(local_temp_dir=local_temp_dir or _default_local_temp_dir(modelspec))
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/jobs/video",
            data={
                "user_prompt": user_prompt,
                "include_vocals": str(include_vocals).lower(),
                "vocal_gender": vocal_gender,
                "modelspec": modelspec,
                **(
                    {"audio_output_format": audio_output_format}
                    if audio_output_format
                    else {}
                ),
            },
            files={
                "video": (
                    video_path.name,
                    video_path.read_bytes(),
                    "video/mp4",
                )
            },
        )

    if response.status_code != 200:
        raise RuntimeError(
            f"Video generation example failed with status {response.status_code}: {response.text}"
        )

    payload = response.json()
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the video generation API end-to-end and save the JSON response locally.",
    )
    parser.add_argument("--video", type=Path, required=True, help="Path to the input video file.")
    parser.add_argument(
        "--user-prompt",
        default="Create a modern female vocal pop song with clear lyrics for this video.",
        help="Creative prompt used for music generation.",
    )
    parser.add_argument(
        "--modelspec",
        default="edenn_enhanced",
        choices=["edenn_basic", "edenn_enhanced", "edenn_studio"],
        help="Music generation branch to use.",
    )
    parser.add_argument(
        "--include-vocals",
        action="store_true",
        default=True,
        help="Hint that the request should contain vocals.",
    )
    parser.add_argument(
        "--vocal-gender",
        default="female",
        help="Preferred vocal gender when vocals are requested.",
    )
    parser.add_argument(
        "--audio-output-format",
        default=None,
        help="Optional provider-specific audio output format.",
    )
    parser.add_argument(
        "--local-temp-dir",
        default=None,
        help="Optional local temp workdir override.",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=Path("EdennCode/Deployment/video_generation_response_example.json"),
        help="Local path where the API JSON response will be written.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    payload = save_video_generation_response_example(
        video_path=args.video.expanduser().resolve(),
        user_prompt=args.user_prompt,
        modelspec=args.modelspec,
        include_vocals=args.include_vocals,
        vocal_gender=args.vocal_gender,
        output_json=args.output_json.expanduser().resolve(),
        audio_output_format=args.audio_output_format,
        local_temp_dir=args.local_temp_dir,
    )
    print("Saved JSON:", args.output_json.expanduser().resolve())
    print("Modelspec:", payload.get("modelspec"))
    print("Lyrics timestamps:", len(payload.get("lyrics_timestamps") or []))
    print(
        "Word-level lyrics timestamps:",
        len(payload.get("word_level_lyrics_timestamps") or []),
    )
