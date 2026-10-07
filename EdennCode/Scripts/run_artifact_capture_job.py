#!/usr/bin/env python3
"""Run a real pipeline against real providers and dump the raw job response.

Deliberately shape-agnostic: it saves whatever JSON the API returns, so the exact
same script runs on both sides of a response refactor and the two captures stay
comparable. Feed the outputs to capture_artifact_baseline.py.

The planning LLM is stubbed with a fixed plan so the only nondeterminism left is the
music provider itself — otherwise a re-planned slideshow would change the video's
duration and swamp the signal we are actually looking for.

    python3 EdennCode/Scripts/run_artifact_capture_job.py --pipeline multi_image --out /tmp/mi.json
    python3 EdennCode/Scripts/run_artifact_capture_job.py --pipeline video_music --out /tmp/vm.json

REAL, PAID provider calls. Storage must be configured (URLs are what get probed).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from EdennCode.env import load_env  # noqa: E402

load_env()

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from EdennCode.Deployment.api_common import ApiContext  # noqa: E402
from EdennCode.Deployment.settings import DeploymentSettings  # noqa: E402
from EdennCode.Deployment.storage import create_storage_service  # noqa: E402


MULTI_IMAGE_PLAN = {
    "video_title": "City Pulse Frames",
    "music_title": "Neon Skyline",
    "video_description": "A bright three-frame reveal that rises into an energetic payoff.",
    "image_order": [1, 2, 3],
    "storyline_summary": "A three-frame lifestyle reveal with a clear rhythmic lift.",
    "overall_mood": "uplifting",
    "target_bpm": 120,
    "primary_instruments": ["drums", "bass", "synth"],
    "music_prompt_summary": "Punchy upbeat pop with a strong pulse and bright lift.",
    "image_beats": [
        {"image_index": 1, "label": "Hook", "role": "setup", "emotion": "curious",
         "description": "Open on the establishing frame.",
         "transition_hint": "Cut on the downbeat."},
        {"image_index": 2, "label": "Build", "role": "develop", "emotion": "engaged",
         "description": "Carry momentum into the middle frame.",
         "transition_hint": "Cut on the next downbeat."},
        {"image_index": 3, "label": "Lift", "role": "payoff", "emotion": "excited",
         "description": "Land on the reveal frame.",
         "transition_hint": "Hold through the phrase ending."},
    ],
    "music_sections": [
        {"section_id": "hook", "label": "Hook", "image_indices": [1],
         "objective": "Open with a clear pulse.", "energy_start": 0.5, "energy_end": 0.6,
         "instrumentation_focus": ["drums", "bass"], "lyric_lines": ["Feel the city wake"]},
        {"section_id": "build", "label": "Build", "image_indices": [2],
         "objective": "Grow the energy toward the lift.", "energy_start": 0.6, "energy_end": 0.7,
         "instrumentation_focus": ["drums", "synth"], "lyric_lines": ["Feel the rhythm rise"]},
        {"section_id": "lift", "label": "Lift", "image_indices": [3],
         "objective": "Push to a brighter payoff.", "energy_start": 0.7, "energy_end": 0.8,
         "instrumentation_focus": ["synth", "claps"], "lyric_lines": ["Hold the moment high"]},
    ],
}


class _FixedPlanClient:
    """Planning LLM stub: one fixed plan, so the slideshow is byte-stable."""

    async def complete_messages(self, _messages, json_schema=None):
        return (
            MULTI_IMAGE_PLAN,
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


def _build_context(tmp_dir: Path, *, multi_image: bool) -> ApiContext:
    settings = DeploymentSettings.from_env()
    settings.workdir = tmp_dir / "jobs"  # type: ignore[misc]
    storage = create_storage_service(settings)
    if not getattr(storage, "enabled", False):
        raise SystemExit(
            "Storage is disabled. Set AZURE_STORAGE_CONNECTION_STRING — the capture "
            "probes the URLs the response returns."
        )

    from EdennCode.Deployment.multi_image_workflows import MultiImageGenerationOrchestrator
    from EdennCode.Deployment.workflows import VideoGenerationOrchestrator

    return ApiContext(
        settings=settings,
        storage=storage,
        workflow=VideoGenerationOrchestrator(),
        alignment_workflow=MagicMock(),
        audio_creative_edit_workflow=MagicMock(),
        logger=MagicMock(),
        multi_image_workflow=(
            MultiImageGenerationOrchestrator() if multi_image else MagicMock()
        ),
    )


def run_multi_image(*, modelspec: str, tmp_dir: Path) -> dict:
    from EdennCode.Deployment.api_multi_image_generation import (
        create_multi_image_generation_router,
    )
    from EdennCode.WorkflowFactory.VideoMusicWorkflow.PipelineDataModel.pipeline_datamodel import (
        Language, VideoCategory,
    )
    from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.UserIntentUnderstandingStage.user_prompt_preprocessor import (
        UserPromptPreprocessorResult,
    )
    from EdennCode.TestSuites.helpers.paths import MULTI_IMAGE_DESIGN_IMAGES_DIR

    images_dir = tmp_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    sources = sorted(p for p in MULTI_IMAGE_DESIGN_IMAGES_DIR.iterdir() if p.is_file())[:3]
    image_paths = []
    for index, source in enumerate(sources, start=1):
        copied = images_dir / f"frame_{index}{source.suffix.lower()}"
        shutil.copy2(source, copied)
        image_paths.append(copied)

    preprocess = UserPromptPreprocessorResult(
        was_transformed=False,
        detected_include_vocals=False,
        transformed_prompt="Create an uplifting instrumental electronic track.",
        detected_references=[],
        detected_language=Language.EN,
        detected_category=VideoCategory.DEFAULT,
        detected_vocal_gender="unknown",
        detected_vocal_language="",
        reasoning="Artifact capture fixture.",
    )

    context = _build_context(tmp_dir, multi_image=True)
    app = FastAPI()
    app.include_router(create_multi_image_generation_router(context))

    stage = (
        "EdennCode.WorkflowFactory.MultiImageWorkflow.Stages"
        ".MultiImageE2EGenerationStage.multi_image_generation_e2e_stage"
    )
    with patch(f"{stage}.build_azure_client", return_value=_FixedPlanClient()), patch(
        f"{stage}.UserPromptPreprocessorAgent.preprocess",
        new=AsyncMock(return_value=preprocess),
    ):
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/jobs/multi-image",
                data={
                    "user_prompt": "Create an uplifting instrumental electronic track.",
                    "modelspec": modelspec,
                    "align_to_beats": "true",
                },
                files=[
                    ("images", (p.name, p.read_bytes(), "image/jpeg")) for p in image_paths
                ],
            )
    if response.status_code != 200:
        raise SystemExit(f"multi-image job failed: {response.status_code} {response.text}")
    return response.json()


def run_video_music(*, modelspec: str, tmp_dir: Path, video: Path) -> dict:
    from EdennCode.Deployment.api_video_generation import create_video_generation_router

    context = _build_context(tmp_dir, multi_image=False)
    app = FastAPI()
    app.include_router(create_video_generation_router(context))

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/jobs/video",
            data={
                "user_prompt": "Create a steady instrumental electronic track. No vocals.",
                "include_vocals": "false",
                "modelspec": modelspec,
                "music_volume": "0.8",
                "preserve_original_audio": "false",
                "compression_flag": "false",
            },
            files={"video": (video.name, video.read_bytes(), "video/mp4")},
        )
    if response.status_code != 200:
        raise SystemExit(f"video-music job failed: {response.status_code} {response.text}")
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline", choices=("multi_image", "video_music"), required=True)
    parser.add_argument("--modelspec", default="edenn_basic")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--video",
        type=Path,
        default=REPO_ROOT / "EdennCode/TestSuites/assets/smoke/videos"
        / "sample_clip.mp4",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="artifact-capture-") as tmp:
        tmp_dir = Path(tmp)
        if args.pipeline == "multi_image":
            payload = run_multi_image(modelspec=args.modelspec, tmp_dir=tmp_dir)
        else:
            payload = run_video_music(
                modelspec=args.modelspec, tmp_dir=tmp_dir, video=args.video
            )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
