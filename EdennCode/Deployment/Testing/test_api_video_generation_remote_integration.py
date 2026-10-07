from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.api_common import ApiContext
from EdennCode.Deployment.api_video_generation import (
    _async_job_store,
    create_video_generation_router,
)
from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Deployment.storage import create_storage_service
from EdennCode.Deployment.workflows import VideoGenerationOrchestrator
from EdennCode.TestSuites.helpers.integration import (
    handle_remote_failure,
    handle_remote_http_failure,
    require_any_remote_env,
    require_remote_media_storage_env,
    require_remote_env,
    run_with_remote_rate_limit_retry,
)
from EdennCode.TestSuites.helpers.remote_vocal_sample import (
    remote_vocal_sample_form_data,
    remote_vocal_sample_upload_tuple,
    require_remote_vocal_sample,
)
from EdennCode.TestSuites.helpers.paths import SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH


def _build_video_api_app() -> FastAPI:
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
        alignment_workflow=MagicMock(),
        audio_creative_edit_workflow=MagicMock(),
        logger=MagicMock(),
    )
    app = FastAPI()
    app.include_router(create_video_generation_router(context))
    return app


def _save_remote_payload_if_requested(case_id: str, payload: dict) -> None:
    output_dir = (os.getenv("SAVE_REMOTE_VIDEO_API_PAYLOADS_DIR") or "").strip()
    if not output_dir:
        return

    target_dir = Path(output_dir).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{case_id}.json"
    target_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _poll_async_video_music_job(
    client: TestClient,
    job_id: str,
    *,
    timeout_s: float = 900.0,
    interval_s: float = 2.0,
) -> dict:
    deadline = time.monotonic() + timeout_s
    last_payload: dict | None = None

    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/async_video_music_gen/{job_id}")
        handle_remote_http_failure(response)
        assert response.status_code == 200, response.text
        payload = response.json()
        last_payload = payload
        if payload["status"] in {"completed", "failed"}:
            return payload
        time.sleep(interval_s)

    raise AssertionError(
        f"Async video music job {job_id} did not finish within {timeout_s}s; "
        f"last payload={last_payload}"
    )


@pytest.mark.remote_integration
@pytest.mark.parametrize(
    (
        "case_id",
        "modelspec",
        "include_vocals",
        "user_prompt",
        "patch_provider_c",
        "patch_basic",
        "patch_enhanced",
        "expected_modelspec",
    ),
    [
        (
            "edenn_basic_instrumental",
            "edenn_basic",
            False,
            "Create a steady instrumental electronic track for this video.",
            True,
            False,
            True,
            "edenn_basic",
        ),
        (
            "edenn_basic_vocals",
            "edenn_basic",
            True,
            "Create a modern female vocal pop song with clear lyrics for this video.",
            True,
            False,
            True,
            "edenn_basic",
        ),
        (
            "edenn_enhanced_instrumental",
            "edenn_enhanced",
            False,
            "Create a steady instrumental electronic track for this video. No vocals.",
            True,
            False,
            True,
            "edenn_basic",
        ),
        (
            "edenn_enhanced_vocals",
            "edenn_enhanced",
            True,
            "Create a modern female vocal pop song with clear lyrics for this video.",
            True,
            True,
            False,
            "edenn_enhanced",
        ),
        (
            "edenn_studio_instrumental",
            "edenn_studio",
            False,
            "Create a steady instrumental cinematic track for this video. No vocals.",
            True,
            False,
            True,
            "edenn_basic",
        ),
        (
            "edenn_studio_vocals",
            "edenn_studio",
            True,
            "Create a cinematic female vocal anthem with lyrics for this video.",
            False,
            True,
            True,
            "edenn_studio",
        ),
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
def test_video_generation_api_remote_contract(
    case_id: str,
    modelspec: str,
    include_vocals: bool,
    user_prompt: str,
    patch_provider_c: bool,
    patch_basic: bool,
    patch_enhanced: bool,
    expected_modelspec: str,
) -> None:
    """Verify each public video modelspec path returns the stable remote API contract."""

    require_remote_env(
        "AZURE_ENDPOINT",
        "AZURE_API_KEY",
        "AZURE_MODEL",
    )
    require_remote_media_storage_env()
    if expected_modelspec == "edenn_basic":
        require_remote_env("PROVIDER_A_API_KEY")
    elif expected_modelspec == "edenn_enhanced":
        require_any_remote_env(
            "EDENN_ENHANCED_PROVIDER_B_API_KEY",
            "PROVIDER_B_API_KEY",
            "PROVIDER_B_API_KEY_1",
            "PROVIDER_B_API_KEY_2",
        )
    elif expected_modelspec == "edenn_studio":
        require_remote_env("PROVIDER_C_API_KEY")

    assert SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.exists()

    def _run() -> None:
        try:
            with tempfile.TemporaryDirectory(prefix="api-video-remote-") as tmp:
                local_temp_dir = (
                    "temp_video_workdir/"
                    f"test_api_video_generation_remote_{modelspec}_{Path(tmp).name}"
                )
                local_temp_root = Path.cwd() / local_temp_dir
                app = _build_video_api_app()
                with ExitStack() as stack:
                    stack.enter_context(
                        patch.dict(
                            "os.environ",
                            {
                                "USE_LOCAL_TEMP_DIR": "true",
                                "LOCAL_TEMP_DIR": local_temp_dir,
                            },
                            clear=False,
                        )
                    )
                    if patch_provider_c:
                        stack.enter_context(
                            patch(
                                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.ProviderCApi",
                                return_value=MagicMock(),
                            )
                        )
                    if patch_basic:
                        stack.enter_context(
                            patch(
                                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.build_music_provider",
                                return_value=MagicMock(),
                            )
                        )
                    if patch_enhanced:
                        stack.enter_context(
                            patch(
                                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.build_edenn_enhanced_music_provider",
                                return_value=MagicMock(),
                            )
                        )

                    with TestClient(app) as client:
                        response = client.post(
                            "/api/v1/jobs/video",
                            data={
                                "user_prompt": user_prompt,
                                "include_vocals": str(include_vocals).lower(),
                                "modelspec": modelspec,
                            },
                            files={
                                "video": (
                                    SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.name,
                                    SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.read_bytes(),
                                    "video/mp4",
                                )
                            },
                        )
                try:
                    handle_remote_http_failure(response)
                    assert response.status_code == 200, response.text
                    payload = response.json()
                    _save_remote_payload_if_requested(case_id, payload)
                    assert payload["modelspec"] == expected_modelspec
                    assert payload["video_metadata"]["duration"] > 18.0
                    assert payload["video_summary"]
                    assert payload["video_summary"]["video_title"].strip()
                    assert payload["video_summary"]["video_description"].strip()
                    # Song-style music_title surfaced at the top level, sourced from
                    # the video_summary understanding dict.
                    assert payload["video_summary"]["music_title"].strip()
                    assert payload["music_title"].strip()
                    assert payload["music_title"] == payload["video_summary"]["music_title"].strip()
                    assert payload["music_prompt"]
                    assert payload["video_url"]
                    assert payload["audio_url"]
                    assert payload["include_vocals"] is include_vocals
                    assert isinstance(payload["matching"], dict)
                    assert "used_track" in payload["matching"]
                    if include_vocals:
                        assert payload["lyrics_timestamps"] is not None
                        assert payload["word_level_lyrics_timestamps"] is not None
                    if expected_modelspec in {"edenn_enhanced", "edenn_studio"}:
                        assert payload["complete_audio_url"]
                        # Complete-music duration (s) + byte size for the primary track.
                        assert payload["complete_audio_duration_s"] and payload["complete_audio_duration_s"] > 0
                        assert payload["complete_audio_size_bytes"] and payload["complete_audio_size_bytes"] > 0
                        assert payload["secondary_complete_audio_duration_s"] is None
                        assert payload["secondary_complete_audio_size_bytes"] is None
                        assert payload["primary_full_lyrics"]
                        assert payload["primary_full_lyrics"].strip()
                        assert payload["primary_full_lyrics_timestamps"]
                        assert payload["primary_full_word_level_lyrics_timestamps"]
                        assert payload["matching"]["used_track"] == "primary"
                        assert payload["secondary_complete_audio_url"] is None
                        assert payload["secondary_full_lyrics"] is None
                        assert payload["secondary_full_lyrics_timestamps"] == []
                        assert payload["secondary_full_word_level_lyrics_timestamps"] == []
                    else:
                        assert payload["primary_full_lyrics"] is None
                        assert payload["primary_full_lyrics_timestamps"] == []
                        assert payload["primary_full_word_level_lyrics_timestamps"] == []
                        assert payload["secondary_full_lyrics"] is None
                        assert payload["secondary_full_lyrics_timestamps"] == []
                        assert payload["secondary_full_word_level_lyrics_timestamps"] == []
                        assert payload["matching"]["used_track"] is None
                finally:
                    if local_temp_root.exists():
                        import shutil

                        shutil.rmtree(local_temp_root, ignore_errors=True)
        except Exception as exc:
            handle_remote_failure(exc)
            raise

    run_with_remote_rate_limit_retry(
        _run,
        operation_name=f"test_video_generation_api_remote_contract[{case_id}]",
    )


@pytest.mark.remote_integration
@pytest.mark.parametrize(
    (
        "case_id",
        "modelspec",
        "music_style_prompt",
        "lyrics_prompt",
        "patch_provider_c",
        "patch_basic",
        "patch_enhanced",
        "expected_modelspec",
        "expected_include_vocals",
        "expected_lyrics_phrase",
    ),
    [
        (
            "verbose_edenn_enhanced_vocals_spanish_lyrics",
            "edenn_enhanced",
            "Spanish female vocal pop, warm and cinematic, with clear chorus vocals.",
            "El coro debe incluir exactamente la frase: brilla mi corazon.",
            True,
            True,
            False,
            "edenn_enhanced",
            True,
            "brilla mi corazon",
        ),
        (
            "verbose_edenn_enhanced_vocals_style_only",
            "edenn_enhanced",
            "Mandarin female vocal pop with a bright festival feeling and clear lyrics.",
            None,
            True,
            True,
            False,
            "edenn_enhanced",
            True,
            None,
        ),
        (
            "verbose_edenn_studio_vocals_japanese_lyrics",
            "edenn_studio",
            "Japanese female vocal cinematic pop, emotional and uplifting.",
            "サビに「夜明けの光」を必ず入れる。",
            False,
            True,
            True,
            "edenn_studio",
            True,
            "夜明けの光",
        ),
        (
            "verbose_edenn_enhanced_instrumental_no_lyrics",
            "edenn_enhanced",
            "Instrumental electronic underscore only, no vocals, no lyrics.",
            None,
            True,
            False,
            True,
            "edenn_basic",
            False,
            None,
        ),
    ],
    ids=[
        "verbose_edenn_enhanced_vocals_spanish_lyrics",
        "verbose_edenn_enhanced_vocals_style_only",
        "verbose_edenn_studio_vocals_japanese_lyrics",
        "verbose_edenn_enhanced_instrumental_no_lyrics",
    ],
)
def test_video_generation_api_remote_verbose_instruction_contract(
    case_id: str,
    modelspec: str,
    music_style_prompt: str,
    lyrics_prompt: str | None,
    patch_provider_c: bool,
    patch_basic: bool,
    patch_enhanced: bool,
    expected_modelspec: str,
    expected_include_vocals: bool,
    expected_lyrics_phrase: str | None,
) -> None:
    """Verify verbose split-prompt requests preserve style and lyric guidance remotely."""

    require_remote_env(
        "AZURE_ENDPOINT",
        "AZURE_API_KEY",
        "AZURE_MODEL",
    )
    require_remote_media_storage_env()
    if expected_modelspec == "edenn_basic":
        require_remote_env("PROVIDER_A_API_KEY")
    elif expected_modelspec == "edenn_enhanced":
        require_any_remote_env(
            "EDENN_ENHANCED_PROVIDER_B_API_KEY",
            "PROVIDER_B_API_KEY",
            "PROVIDER_B_API_KEY_1",
            "PROVIDER_B_API_KEY_2",
        )
    elif expected_modelspec == "edenn_studio":
        require_remote_env("PROVIDER_C_API_KEY")

    assert SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.exists()

    def _run() -> None:
        try:
            with tempfile.TemporaryDirectory(prefix="api-video-remote-verbose-") as tmp:
                local_temp_dir = (
                    "temp_video_workdir/"
                    f"test_api_video_generation_remote_{case_id}_{Path(tmp).name}"
                )
                local_temp_root = Path.cwd() / local_temp_dir
                app = _build_video_api_app()
                with ExitStack() as stack:
                    stack.enter_context(
                        patch.dict(
                            "os.environ",
                            {
                                "USE_LOCAL_TEMP_DIR": "true",
                                "LOCAL_TEMP_DIR": local_temp_dir,
                            },
                            clear=False,
                        )
                    )
                    if patch_provider_c:
                        stack.enter_context(
                            patch(
                                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.ProviderCApi",
                                return_value=MagicMock(),
                            )
                        )
                    if patch_basic:
                        stack.enter_context(
                            patch(
                                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.build_music_provider",
                                return_value=MagicMock(),
                            )
                        )
                    if patch_enhanced:
                        stack.enter_context(
                            patch(
                                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.build_edenn_enhanced_music_provider",
                                return_value=MagicMock(),
                            )
                        )

                    form_data = {
                        "verbose_instruction": "true",
                        "music_style_prompt": music_style_prompt,
                        "modelspec": modelspec,
                    }
                    if lyrics_prompt is not None:
                        form_data["lyrics_prompt"] = lyrics_prompt

                    with TestClient(app) as client:
                        response = client.post(
                            "/api/v1/jobs/video",
                            data=form_data,
                            files={
                                "video": (
                                    SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.name,
                                    SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.read_bytes(),
                                    "video/mp4",
                                )
                            },
                        )
                try:
                    handle_remote_http_failure(response)
                    assert response.status_code == 200, response.text
                    payload = response.json()
                    _save_remote_payload_if_requested(case_id, payload)
                    assert payload["modelspec"] == expected_modelspec
                    assert payload["include_vocals"] is expected_include_vocals
                    assert payload["video_summary"]["video_title"].strip()
                    assert payload["video_summary"]["video_description"].strip()
                    assert payload["audio_url"]
                    assert payload["video_url"]
                    music_prompt = payload["music_prompt"]
                    assert music_prompt
                    if expected_modelspec == "edenn_basic":
                        assert music_prompt["global_music_prompt"].strip()
                        assert music_prompt["lyrics_prompt"] is None
                        assert payload["primary_full_lyrics"] is None
                    else:
                        assert music_prompt["style_prompt"].strip()
                        assert music_prompt["lyrics_prompt"].strip()
                        assert payload["complete_audio_url"]
                        assert payload["primary_full_lyrics"].strip()
                        if expected_lyrics_phrase:
                            assert expected_lyrics_phrase in music_prompt["lyrics_prompt"]
                finally:
                    if local_temp_root.exists():
                        import shutil

                        shutil.rmtree(local_temp_root, ignore_errors=True)
        except Exception as exc:
            handle_remote_failure(exc)
            raise

    run_with_remote_rate_limit_retry(
        _run,
        operation_name=f"test_video_generation_api_remote_verbose_instruction_contract[{case_id}]",
    )


@pytest.mark.remote_integration
def test_video_generation_api_remote_contract_with_vocal_clone() -> None:
    """Verify enhanced video generation accepts a real vocal clone sample remotely."""

    require_remote_env(
        "AZURE_ENDPOINT",
        "AZURE_API_KEY",
        "AZURE_MODEL",
    )
    require_remote_media_storage_env()
    require_any_remote_env(
        "EDENN_ENHANCED_PROVIDER_B_API_KEY",
        "PROVIDER_B_API_KEY",
        "PROVIDER_B_API_KEY_1",
        "PROVIDER_B_API_KEY_2",
    )
    require_remote_vocal_sample()
    assert SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.exists()

    def _run() -> None:
        try:
            with tempfile.TemporaryDirectory(prefix="api-video-remote-vocal-clone-") as tmp:
                local_temp_dir = (
                    "temp_video_workdir/"
                    f"test_api_video_generation_remote_vocal_clone_{Path(tmp).name}"
                )
                local_temp_root = Path.cwd() / local_temp_dir
                app = _build_video_api_app()
                with ExitStack() as stack:
                    stack.enter_context(
                        patch.dict(
                            "os.environ",
                            {
                                "USE_LOCAL_TEMP_DIR": "true",
                                "LOCAL_TEMP_DIR": local_temp_dir,
                            },
                            clear=False,
                        )
                    )
                    stack.enter_context(
                        patch(
                            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.ProviderCApi",
                            return_value=MagicMock(),
                        )
                    )
                    stack.enter_context(
                        patch(
                            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.build_music_provider",
                            return_value=MagicMock(),
                        )
                    )

                    with TestClient(app) as client:
                        form_data = {
                            "user_prompt": "Create a modern female vocal pop song with clear lyrics for this video.",
                            "include_vocals": "true",
                            "modelspec": "edenn_enhanced",
                            **remote_vocal_sample_form_data(),
                        }
                        files = {
                            "video": (
                                SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.name,
                                SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.read_bytes(),
                                "video/mp4",
                            )
                        }
                        vocal_upload = remote_vocal_sample_upload_tuple()
                        if vocal_upload is not None:
                            files[vocal_upload[0]] = vocal_upload[1]
                        response = client.post(
                            "/api/v1/jobs/video",
                            data=form_data,
                            files=files,
                        )
                try:
                    handle_remote_http_failure(response)
                    assert response.status_code == 200, response.text
                    payload = response.json()
                    assert payload["modelspec"] == "edenn_enhanced"
                    assert payload["include_vocals"] is True
                    assert payload["audio_url"]
                    assert payload["complete_audio_url"]
                    assert payload["vocal_id_used"]
                finally:
                    if local_temp_root.exists():
                        import shutil

                        shutil.rmtree(local_temp_root, ignore_errors=True)
        except Exception as exc:
            handle_remote_failure(exc)
            raise

    run_with_remote_rate_limit_retry(
        _run,
        operation_name="test_video_generation_api_remote_contract_with_vocal_clone",
    )


@pytest.mark.remote_integration
def test_async_video_generation_api_remote_contract_edenn_basic() -> None:
    """Submit a real video through the async route and poll for the final result."""

    require_remote_env(
        "AZURE_ENDPOINT",
        "AZURE_API_KEY",
        "AZURE_MODEL",
        "PROVIDER_A_API_KEY",
    )
    require_remote_media_storage_env()
    assert SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.exists()

    case_id = "async_edenn_basic_instrumental"

    def _run() -> None:
        job_id: str | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="api-video-remote-async-") as tmp:
                local_temp_dir = (
                    "temp_video_workdir/"
                    f"test_api_video_generation_remote_async_{Path(tmp).name}"
                )
                local_temp_root = Path.cwd() / local_temp_dir
                app = _build_video_api_app()
                try:
                    with ExitStack() as stack:
                        stack.enter_context(
                            patch.dict(
                                "os.environ",
                                {
                                    "USE_LOCAL_TEMP_DIR": "true",
                                    "LOCAL_TEMP_DIR": local_temp_dir,
                                },
                                clear=False,
                            )
                        )
                        stack.enter_context(
                            patch(
                                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.ProviderCApi",
                                return_value=MagicMock(),
                            )
                        )
                        stack.enter_context(
                            patch(
                                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.build_edenn_enhanced_music_provider",
                                return_value=MagicMock(),
                            )
                        )

                        with TestClient(app) as client:
                            accepted_response = client.post(
                                "/api/v1/jobs/async_video_music_gen",
                                data={
                                    "user_prompt": (
                                        "Create a steady instrumental electronic track "
                                        "for this video. No vocals."
                                    ),
                                    "include_vocals": "false",
                                    "modelspec": "edenn_basic",
                                },
                                files={
                                    "video": (
                                        SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.name,
                                        SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.read_bytes(),
                                        "video/mp4",
                                    )
                                },
                            )
                            handle_remote_http_failure(accepted_response)
                            assert accepted_response.status_code == 200, accepted_response.text
                            accepted_payload = accepted_response.json()
                            assert accepted_payload["status"] == "pending"
                            job_id = accepted_payload["job_id"]

                            status_payload = _poll_async_video_music_job(client, job_id)

                    _save_remote_payload_if_requested(
                        case_id,
                        {
                            "accepted": accepted_payload,
                            "status": status_payload,
                        },
                    )

                    assert status_payload["job_id"] == job_id
                    assert status_payload["status"] == "completed", status_payload.get("error")
                    assert status_payload["error"] is None
                    result = status_payload["result"]
                    assert result
                    assert result["job_id"] == job_id
                    assert result["status"] == "completed"
                    assert result["modelspec"] == "edenn_basic"
                    assert result["include_vocals"] is False
                    assert result["video_metadata"]["duration"] > 18.0
                    assert result["video_summary"]
                    assert result["music_prompt"]
                    assert result["video_url"]
                    assert result["audio_url"]
                    assert isinstance(result["matching"], dict)
                    assert result["matching"]["used_track"] is None
                finally:
                    if job_id:
                        _async_job_store.pop(job_id, None)
                    if local_temp_root.exists():
                        import shutil

                        shutil.rmtree(local_temp_root, ignore_errors=True)
        except Exception as exc:
            handle_remote_failure(exc)
            raise

    run_with_remote_rate_limit_retry(
        _run,
        operation_name="test_async_video_generation_api_remote_contract_edenn_basic",
    )
