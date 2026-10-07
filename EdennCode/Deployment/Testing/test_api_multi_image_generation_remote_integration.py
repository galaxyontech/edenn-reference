import os
import re
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.api_common import ApiContext
from EdennCode.Deployment.api_multi_image_generation import create_multi_image_generation_router
from EdennCode.Deployment.multi_image_workflows import MultiImageGenerationOrchestrator
from EdennCode.Util.MediaUtils import get_image_dimensions
from EdennCode.WorkflowFactory.VideoMusicWorkflow.PipelineDataModel.pipeline_datamodel import (
    Language,
    VideoCategory,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.UserIntentUnderstandingStage.user_prompt_preprocessor import (
    UserPromptPreprocessorResult,
)
from EdennCode.TestSuites.helpers.integration import (
    handle_remote_failure,
    handle_remote_http_failure,
    require_any_remote_env,
    require_remote_env,
    run_with_remote_rate_limit_retry,
)
from EdennCode.TestSuites.helpers.remote_vocal_sample import (
    remote_vocal_sample_form_data,
    remote_vocal_sample_upload_tuple,
    require_remote_vocal_sample,
)
from EdennCode.TestSuites.helpers.paths import MULTI_IMAGE_DESIGN_IMAGES_DIR


class _DisabledStorage:
    enabled = False


class _FakeAzureClient:
    async def complete_messages(self, _messages, json_schema=None):
        return (
            {
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
                    {
                        "image_index": 1,
                        "label": "Hook",
                        "role": "setup",
                        "emotion": "curious",
                        "description": "Open on the establishing frame.",
                        "transition_hint": "Cut on the downbeat.",
                    },
                    {
                        "image_index": 2,
                        "label": "Build",
                        "role": "develop",
                        "emotion": "engaged",
                        "description": "Carry momentum into the middle frame.",
                        "transition_hint": "Cut on the next downbeat.",
                    },
                    {
                        "image_index": 3,
                        "label": "Lift",
                        "role": "payoff",
                        "emotion": "excited",
                        "description": "Land on the reveal frame.",
                        "transition_hint": "Hold through the phrase ending.",
                    },
                ],
                "music_sections": [
                    {
                        "section_id": "intro",
                        "label": "Intro",
                        "image_indices": [1],
                        "objective": "Establish the beat immediately.",
                        "energy_start": 0.35,
                        "energy_end": 0.6,
                        "instrumentation_focus": ["drums", "bass"],
                        "lyric_lines": ["Step into the light"],
                    },
                    {
                        "section_id": "build",
                        "label": "Build",
                        "image_indices": [2],
                        "objective": "Grow the energy toward the lift.",
                        "energy_start": 0.6,
                        "energy_end": 0.7,
                        "instrumentation_focus": ["drums", "synth"],
                        "lyric_lines": ["Feel the rhythm rise"],
                    },
                    {
                        "section_id": "lift",
                        "label": "Lift",
                        "image_indices": [3],
                        "objective": "Push to a brighter payoff.",
                        "energy_start": 0.7,
                        "energy_end": 0.8,
                        "instrumentation_focus": ["synth", "claps"],
                        "lyric_lines": ["Hold the moment high"],
                    },
                ],
            },
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


def _require_provider_env(modelspec: str) -> None:
    if modelspec == "edenn_basic":
        require_remote_env("PROVIDER_A_API_KEY")
        return
    if modelspec == "edenn_enhanced":
        # The ProviderB client reads a numbered key pool (PROVIDER_B_API_KEY_N, any N,
        # gaps allowed) with the base/explicit keys as fallback. Include every
        # numbered key present so a local .env using e.g. PROVIDER_B_API_KEY_3.. is
        # detected instead of skipped.
        numbered_keys = sorted(
            name for name in os.environ if re.fullmatch(r"PROVIDER_B_API_KEY_\d+", name)
        )
        require_any_remote_env(
            "EDENN_ENHANCED_PROVIDER_B_API_KEY",
            "PROVIDER_B_API_KEY",
            *numbered_keys,
        )
        return
    if modelspec == "edenn_studio":
        require_remote_env("PROVIDER_C_API_KEY")
        return
    raise AssertionError(f"Unexpected modelspec: {modelspec}")


def _copy_example_images(destination: Path, limit: int = 3) -> list[Path]:
    source_images = sorted(path for path in MULTI_IMAGE_DESIGN_IMAGES_DIR.iterdir() if path.is_file())
    if len(source_images) < limit:
        raise RuntimeError("Multi-image example folder does not contain enough images.")
    destination.mkdir(parents=True, exist_ok=True)
    copied_paths: list[Path] = []
    for idx, source in enumerate(source_images[:limit], start=1):
        copied = destination / f"frame_{idx}{source.suffix.lower()}"
        shutil.copy2(source, copied)
        copied_paths.append(copied)
    return copied_paths


@pytest.mark.remote_integration
@pytest.mark.parametrize(
    ("modelspec", "user_prompt", "expect_vocals"),
    [
        ("edenn_basic", "Create an uplifting instrumental electronic track.", False),
        ("edenn_basic", "Create an uplifting female vocal anthem in English.", True),
        ("edenn_enhanced", "Create an uplifting instrumental electronic track.", False),
        ("edenn_enhanced", "Create an uplifting female vocal anthem in English.", True),
        ("edenn_studio", "Create an uplifting instrumental electronic track.", False),
        ("edenn_studio", "Create an uplifting female vocal anthem in English.", True),
    ],
    ids=[
        "edenn_basic_instrumental",
        "edenn_basic_vocals",
        "edenn_enhanced_instrumental",
        "edenn_enhanced_vocals",
        "edenn_studio_instrumental",
        "edenn_studio_vocals",
    ],
)
def test_multi_image_api_remote_branch_matrix(
    modelspec: str,
    user_prompt: str,
    expect_vocals: bool,
) -> None:
    _require_provider_env(modelspec)
    preprocess_result = UserPromptPreprocessorResult(
        was_transformed=False,
        detected_include_vocals=expect_vocals,
        transformed_prompt=user_prompt,
        detected_references=[],
        detected_language=Language.EN,
        detected_category=VideoCategory.DEFAULT,
        detected_vocal_gender="female" if expect_vocals else "unknown",
        detected_vocal_language="EN" if expect_vocals else "",
        reasoning="Remote API integration fixture.",
    )

    def _run() -> None:
        with tempfile.TemporaryDirectory(prefix="api-multi-image-remote-") as tmp:
            tmp_dir = Path(tmp)
            images_dir = tmp_dir / "images"
            image_paths = _copy_example_images(images_dir)
            expected_compression = any(
                max(get_image_dimensions(path)) > 1280 for path in image_paths
            )
            settings = SimpleNamespace(
                workdir=tmp_dir / "jobs",
                music_volume=1.0,
                audio_container_name="audio",
                output_container="videos",
            )
            context = ApiContext(
                settings=settings,
                storage=_DisabledStorage(),
                workflow=MagicMock(),
                alignment_workflow=MagicMock(),
                audio_creative_edit_workflow=MagicMock(),
                logger=MagicMock(),
                multi_image_workflow=MultiImageGenerationOrchestrator(),
            )
            app = FastAPI()
            app.include_router(create_multi_image_generation_router(context))

            try:
                with patch(
                    "EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.MultiImageE2EGenerationStage.multi_image_generation_e2e_stage.build_azure_client",
                    return_value=_FakeAzureClient(),
                ), patch(
                    "EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.MultiImageE2EGenerationStage.multi_image_generation_e2e_stage.UserPromptPreprocessorAgent.preprocess",
                    new=AsyncMock(return_value=preprocess_result),
                ):
                    with TestClient(app) as client:
                        response = client.post(
                            "/api/v1/jobs/multi-image",
                            data={
                                "user_prompt": user_prompt,
                                "modelspec": modelspec,
                                "align_to_beats": "true",
                            },
                            files=[
                                (
                                    "images",
                                    (path.name, path.read_bytes(), "image/jpeg"),
                                )
                                for path in image_paths
                            ],
                        )
            except Exception as exc:
                handle_remote_failure(exc)
                raise

            handle_remote_http_failure(response)
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["modelspec"] == modelspec
            assert payload["video_title"] == "City Pulse Frames"
            # WS4: song-style music_title threaded end-to-end, distinct from video_title.
            assert payload["music_title"] == "Neon Skyline"
            assert payload["music_title"] != payload["video_title"]
            assert payload["video_description"]
            assert payload["user_requested_language"] == Language.EN
            assert payload["full_tracks"]
            assert payload["section_timeline"]
            assert payload["compression_applied"] is expected_compression

            # WS1: complete-music duration (s) + byte size. Storage is disabled in
            # this fixture, so metrics are probed from the local files before cleanup.
            assert payload["audio_size_bytes"] and payload["audio_size_bytes"] > 0
            assert payload["audio_duration_s"] and payload["audio_duration_s"] > 0
            for track in payload["full_tracks"]:
                assert track["size_bytes"] and track["size_bytes"] > 0
                assert track["duration_s"] and track["duration_s"] > 0

            # WS2: assembled slideshow geometry + length for frontend layout.
            video_metadata = payload["video_metadata"]
            assert video_metadata["width"] and video_metadata["width"] > 0
            assert video_metadata["height"] and video_metadata["height"] > 0
            assert video_metadata["duration_s"] and video_metadata["duration_s"] > 0
            # video-music key aliases mirror the typed values.
            assert video_metadata["duration"] == video_metadata["duration_s"]

            # Schema unification: multi-image response is a superset of VideoJobResponse.
            # audio_* is the windowed slice muxed into the slideshow (the part the
            # viewer hears); complete_* / full_tracks[0] is the full primary track.
            # The window is never longer than the full track, and complete_* mirrors
            # full_tracks[0].
            assert payload["complete_audio_duration_s"] and payload["complete_audio_duration_s"] > 0
            assert payload["complete_audio_size_bytes"] and payload["complete_audio_size_bytes"] > 0
            assert payload["audio_duration_s"] <= payload["complete_audio_duration_s"] + 0.1
            assert payload["audio_size_bytes"] <= payload["complete_audio_size_bytes"]
            assert payload["complete_audio_duration_s"] == payload["full_tracks"][0]["duration_s"]
            # The windowed clip is no longer than the assembled slideshow.
            assert payload["audio_duration_s"] <= payload["video_metadata"]["duration_s"] + 1.0
            # video_summary mirrors the flat titles (video-response shape).
            assert payload["video_summary"]["video_title"] == payload["video_title"]
            assert payload["video_summary"]["music_title"] == payload["music_title"]
            # primary_full_lyrics_timestamps mirrors lyrics_timestamps.
            assert payload["primary_full_lyrics_timestamps"] == payload["lyrics_timestamps"]
            # Genuinely video-only concepts stay N/A for multi-image.
            assert payload["scenes"] == []
            assert payload["critical_warning"] is None
            assert payload["provider_audio_id"] is None
            # Planner token usage IS surfaced (multi-image runs an LLM planning
            # stage); the mocked planner reports total_tokens=2.
            assert payload["token_usage"] == 2
            if expect_vocals:
                assert payload["include_vocals"] is True
                assert payload["vocal_gender"] == "female"
                assert payload["lyrics_language"] == "EN"
                assert payload["lyrics_timestamps"]
            else:
                assert payload["include_vocals"] is False

    run_with_remote_rate_limit_retry(
        _run,
        operation_name=(
            "test_multi_image_api_remote_branch_matrix"
            f"[{modelspec}_{'vocals' if expect_vocals else 'instrumental'}]"
        ),
    )


@pytest.mark.remote_integration
def test_multi_image_api_remote_contract_with_vocal_clone() -> None:
    _require_provider_env("edenn_enhanced")
    require_remote_vocal_sample()
    preprocess_result = UserPromptPreprocessorResult(
        was_transformed=False,
        detected_include_vocals=True,
        transformed_prompt="Create an uplifting female vocal anthem in English.",
        detected_references=[],
        detected_language=Language.EN,
        detected_category=VideoCategory.DEFAULT,
        detected_vocal_gender="female",
        detected_vocal_language="EN",
        reasoning="Remote API integration fixture.",
    )

    def _run() -> None:
        with tempfile.TemporaryDirectory(prefix="api-multi-image-remote-vocal-clone-") as tmp:
            tmp_dir = Path(tmp)
            images_dir = tmp_dir / "images"
            image_paths = _copy_example_images(images_dir)
            settings = SimpleNamespace(
                workdir=tmp_dir / "jobs",
                music_volume=1.0,
                audio_container_name="audio",
                output_container="videos",
            )
            context = ApiContext(
                settings=settings,
                storage=_DisabledStorage(),
                workflow=MagicMock(),
                alignment_workflow=MagicMock(),
                audio_creative_edit_workflow=MagicMock(),
                logger=MagicMock(),
                multi_image_workflow=MultiImageGenerationOrchestrator(),
            )
            app = FastAPI()
            app.include_router(create_multi_image_generation_router(context))

            try:
                with patch(
                    "EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.MultiImageE2EGenerationStage.multi_image_generation_e2e_stage.build_azure_client",
                    return_value=_FakeAzureClient(),
                ), patch(
                    "EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.MultiImageE2EGenerationStage.multi_image_generation_e2e_stage.UserPromptPreprocessorAgent.preprocess",
                    new=AsyncMock(return_value=preprocess_result),
                ):
                    with TestClient(app) as client:
                        files = [
                            (
                                "images",
                                (path.name, path.read_bytes(), "image/jpeg"),
                            )
                            for path in image_paths
                        ]
                        vocal_upload = remote_vocal_sample_upload_tuple()
                        if vocal_upload is not None:
                            files.append(vocal_upload)
                        response = client.post(
                            "/api/v1/jobs/multi-image",
                            data={
                                "user_prompt": "Create an uplifting female vocal anthem in English.",
                                "modelspec": "edenn_enhanced",
                                "align_to_beats": "true",
                                **remote_vocal_sample_form_data(),
                            },
                            files=files,
                        )
            except Exception as exc:
                handle_remote_failure(exc)
                raise

            handle_remote_http_failure(response)
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["modelspec"] == "edenn_enhanced"
            assert payload["include_vocals"] is True
            assert payload["full_tracks"]
            assert payload["vocal_id_used"]

    run_with_remote_rate_limit_retry(
        _run,
        operation_name="test_multi_image_api_remote_contract_with_vocal_clone",
    )
