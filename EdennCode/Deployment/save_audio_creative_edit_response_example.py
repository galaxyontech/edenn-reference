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

from EdennCode.Deployment.api_audio_creative_edit import create_audio_creative_edit_router
from EdennCode.Deployment.api_common import ApiContext
from EdennCode.Deployment.audio_edit_workflows import AudioCreativeEditOrchestrator
from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Deployment.storage import create_storage_service
from EdennCode.env import load_env


def _default_local_temp_dir(modelspec: str) -> str:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    return f"temp_video_workdir/audio_creative_edit_example_{modelspec}_{timestamp}"


def _build_app(*, local_temp_dir: str) -> FastAPI:
    load_env()
    os.environ["USE_LOCAL_TEMP_DIR"] = "true"
    os.environ["LOCAL_TEMP_DIR"] = local_temp_dir

    settings = DeploymentSettings.from_env()
    storage = create_storage_service(settings)
    workflow = AudioCreativeEditOrchestrator(
        storage=storage,
        llm_image_container=settings.llm_image_container,
        llm_image_sas_ttl_minutes=settings.llm_image_sas_ttl_minutes,
        llm_image_cleanup_delay_seconds=settings.llm_image_cleanup_delay_seconds,
    )
    context = ApiContext(
        settings=settings,
        storage=storage,
        workflow=None,
        alignment_workflow=None,
        audio_creative_edit_workflow=workflow,
        logger=logging.getLogger("save_audio_creative_edit_response_example"),
    )
    app = FastAPI()
    app.include_router(create_audio_creative_edit_router(context))
    return app


def save_audio_creative_edit_response_example(
    *,
    output_json: Path,
    user_prompt: str,
    modelspec: str,
    studio_mode: str = "simple",
    studio_style_weight: float | None = None,
    studio_audio_weight: float | None = None,
    studio_weirdness_constraint: float | None = None,
    audio_path: Path | None = None,
    audio_url: str | None = None,
    image_paths: list[Path] | None = None,
    video_path: Path | None = None,
    local_temp_dir: str | None = None,
) -> dict:
    if bool(audio_path) == bool(audio_url):
        raise ValueError("Provide exactly one of audio_path or audio_url.")
    if image_paths and video_path:
        raise ValueError("Provide either image_paths or video_path, not both.")

    app = _build_app(local_temp_dir=local_temp_dir or _default_local_temp_dir(modelspec))

    data: dict[str, str] = {
        "user_prompt": user_prompt,
        "modelspec": modelspec,
        "studio_mode": studio_mode,
    }
    if studio_style_weight is not None:
        data["studio_style_weight"] = f"{studio_style_weight:.2f}"
    if studio_audio_weight is not None:
        data["studio_audio_weight"] = f"{studio_audio_weight:.2f}"
    if studio_weirdness_constraint is not None:
        data["studio_weirdness_constraint"] = f"{studio_weirdness_constraint:.2f}"
    files: list[tuple[str, tuple[str, bytes, str]]] = []

    if audio_path is not None:
        files.append(
            (
                "audio",
                (
                    audio_path.name,
                    audio_path.read_bytes(),
                    "audio/wav" if audio_path.suffix.lower() == ".wav" else "audio/mpeg",
                ),
            )
        )
    else:
        data["audio_url"] = str(audio_url)

    for image_path in image_paths or []:
        files.append(
            (
                "images",
                (
                    image_path.name,
                    image_path.read_bytes(),
                    "image/jpeg" if image_path.suffix.lower() in {".jpg", ".jpeg"} else "image/png",
                ),
            )
        )

    if video_path is not None:
        files.append(
            (
                "video",
                (
                    video_path.name,
                    video_path.read_bytes(),
                    "video/mp4",
                ),
            )
        )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/jobs/audio-creative-edit",
            data=data,
            files=files,
        )

    if response.status_code != 200:
        raise RuntimeError(
            f"Audio creative edit example failed with status {response.status_code}: {response.text}"
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
        description="Run the audio creative edit API end-to-end and save the JSON response locally.",
    )
    parser.add_argument(
        "--audio",
        type=Path,
        default=None,
        help="Local source audio file to upload.",
    )
    parser.add_argument(
        "--audio-url",
        default=None,
        help="Remote source audio URL to use instead of uploading a file.",
    )
    parser.add_argument(
        "--image",
        type=Path,
        action="append",
        default=[],
        help="Optional repeated image conditioning input.",
    )
    parser.add_argument(
        "--video",
        type=Path,
        default=None,
        help="Optional visual conditioning video input.",
    )
    parser.add_argument(
        "--user-prompt",
        required=True,
        help="Creative edit prompt.",
    )
    parser.add_argument(
        "--modelspec",
        default="edenn_enhanced",
        choices=["edenn_enhanced", "edenn_studio"],
        help="Creative edit branch to use.",
    )
    parser.add_argument(
        "--studio-mode",
        default="simple",
        choices=["simple", "custom"],
        help="For edenn_studio only: simple maps to non-custom upload-cover, custom maps to ProviderC customMode.",
    )
    parser.add_argument(
        "--studio-style-weight",
        type=float,
        default=None,
        help="Optional edenn_studio custom-mode style influence weight (0.00-1.00).",
    )
    parser.add_argument(
        "--studio-audio-weight",
        type=float,
        default=None,
        help="Optional edenn_studio custom-mode audio influence weight (0.00-1.00).",
    )
    parser.add_argument(
        "--studio-weirdness-constraint",
        type=float,
        default=None,
        help="Optional edenn_studio custom-mode weirdness constraint (0.00-1.00).",
    )
    parser.add_argument(
        "--local-temp-dir",
        default=None,
        help="Optional local temp workdir override.",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        required=True,
        help="Local path where the API JSON response will be written.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    payload = save_audio_creative_edit_response_example(
        output_json=args.output_json.expanduser().resolve(),
        user_prompt=args.user_prompt,
        modelspec=args.modelspec,
        studio_mode=args.studio_mode,
        studio_style_weight=args.studio_style_weight,
        studio_audio_weight=args.studio_audio_weight,
        studio_weirdness_constraint=args.studio_weirdness_constraint,
        audio_path=args.audio.expanduser().resolve() if args.audio else None,
        audio_url=args.audio_url,
        image_paths=[path.expanduser().resolve() for path in args.image],
        video_path=args.video.expanduser().resolve() if args.video else None,
        local_temp_dir=args.local_temp_dir,
    )
    print("Saved JSON:", args.output_json.expanduser().resolve())
    print("Modelspec:", payload.get("modelspec"))
    print("Input type:", (payload.get("visual_analysis") or {}).get("input_type"))
    print("Lyrics timestamps:", len(payload.get("lyrics_timestamps") or []))
