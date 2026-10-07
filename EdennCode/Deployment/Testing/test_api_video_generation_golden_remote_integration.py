from __future__ import annotations

import json
import os
import shutil
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.api_common import ApiContext
from EdennCode.Deployment.api_video_generation import create_video_generation_router
from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Deployment.storage import create_storage_service
from EdennCode.Deployment.workflows import VideoGenerationOrchestrator
from EdennCode.TestSuites.helpers.integration import (
    handle_remote_failure,
    handle_remote_http_failure,
    require_any_remote_env,
    require_remote_env,
    require_remote_media_storage_env,
    run_with_remote_rate_limit_retry,
)
from EdennCode.TestSuites.helpers.remote_vocal_sample import (
    remote_vocal_sample_form_data,
    remote_vocal_sample_upload_tuple,
    require_remote_vocal_sample,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_CASES_PATH = REPO_ROOT / "EdennCode" / "TestSuites" / "golden" / "video_generation_cases.json"
VIDEO_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}


def _load_golden_cases() -> list[dict[str, Any]]:
    return json.loads(GOLDEN_CASES_PATH.read_text(encoding="utf-8"))


GOLDEN_CASES = _load_golden_cases()


def _build_video_api_app() -> tuple[FastAPI, DeploymentSettings, Any]:
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
    return app, settings, storage


def _save_remote_payload_if_requested(case_id: str, payload: dict[str, Any]) -> None:
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


def _require_case_env(case: dict[str, Any]) -> None:
    require_remote_env("AZURE_ENDPOINT", "AZURE_API_KEY", "AZURE_MODEL")
    require_remote_media_storage_env()

    provider = case["required_remote_provider"]
    if provider == "edenn_basic":
        require_remote_env("PROVIDER_A_API_KEY")
    elif provider == "edenn_enhanced":
        require_any_remote_env(
            "EDENN_ENHANCED_PROVIDER_B_API_KEY",
            "PROVIDER_B_API_KEY",
            "PROVIDER_B_API_KEY_1",
            "PROVIDER_B_API_KEY_2",
        )
    elif provider == "edenn_studio":
        require_remote_env("PROVIDER_C_API_KEY")
    else:
        raise AssertionError(f"Unsupported golden remote provider: {provider}")

    if case.get("vocal_sample") == "remote_helper":
        require_remote_vocal_sample()


def _resolved_video_path(case: dict[str, Any]) -> Path:
    video_path = REPO_ROOT / case["video_path"]
    assert video_path.exists(), f"Golden video fixture is missing: {video_path}"
    assert video_path.stat().st_size > 1024, (
        "Golden video fixture appears to be a Git LFS pointer or empty file: "
        f"{video_path}"
    )
    return video_path


def _video_upload_tuple(video_path: Path) -> tuple[str, bytes, str]:
    content_type = VIDEO_CONTENT_TYPES.get(video_path.suffix.lower(), "video/mp4")
    return (video_path.name, video_path.read_bytes(), content_type)


def _stage_video_url(
    *,
    settings: DeploymentSettings,
    storage: Any,
    case_id: str,
    video_path: Path,
) -> tuple[str, str]:
    blob_name = (
        "remote-golden-inputs/video-generation/"
        f"{case_id}/{uuid4().hex}-{video_path.name}"
    )
    uploaded_blob = storage.upload_path(
        container=settings.upload_container,
        path=video_path,
        blob_name=blob_name,
        content_type=VIDEO_CONTENT_TYPES.get(video_path.suffix.lower(), "video/mp4"),
    )
    assert uploaded_blob, "Golden video_url staging upload did not return a blob name."
    staged_url = storage.generate_sas_url(
        container=settings.upload_container,
        blob_name=uploaded_blob,
    )
    assert staged_url, "Golden video_url staging did not return a downloadable URL."
    return uploaded_blob, staged_url


def _assert_nonempty_url(payload: dict[str, Any], field_name: str) -> None:
    value = payload.get(field_name)
    assert isinstance(value, str) and value.startswith(("http://", "https://")), (
        f"{field_name} should be a downloadable URL, got {value!r}"
    )


def _assert_golden_response(case: dict[str, Any], payload: dict[str, Any]) -> None:
    expected = case["expected"]
    assert payload["status"] == "completed"
    assert payload["modelspec"] == expected["modelspec"]
    assert payload["include_vocals"] is expected["include_vocals"]
    assert payload["job_id"]
    assert payload["video_id"]
    assert payload["creative_id"]
    assert payload["primary_music_id"]
    assert payload["selected_music_id"]
    assert payload["alignment_id"]

    metadata = payload["video_metadata"]
    assert metadata["duration"] >= expected["min_duration_s"]
    assert metadata["width"] > 0
    assert metadata["height"] > 0

    assert payload["video_summary"]
    assert payload["music_prompt"]
    music_prompt = payload["music_prompt"]
    assert any(
        (music_prompt.get(key) or "").strip()
        for key in ("style_prompt", "lyrics_prompt", "global_music_prompt")
    )

    if expected["requires_audio_url"]:
        _assert_nonempty_url(payload, "audio_url")
    if expected["requires_video_url"]:
        _assert_nonempty_url(payload, "video_url")
    if expected["requires_complete_audio_url"]:
        _assert_nonempty_url(payload, "complete_audio_url")
    else:
        assert payload.get("complete_audio_url") is None

    if expected["requires_primary_full_lyrics"]:
        assert payload["primary_full_lyrics"].strip()
    else:
        assert payload["primary_full_lyrics"] is None

    if expected["requires_primary_word_level_lyrics"]:
        assert payload["primary_full_word_level_lyrics_timestamps"]
    else:
        assert payload["primary_full_word_level_lyrics_timestamps"] == []

    if expected["requires_vocal_id_used"]:
        assert payload["vocal_id_used"]
    else:
        assert payload["vocal_id_used"] is None

    assert payload["matching"]["used_track"] == expected["matching_used_track"]

    expected_lyrics_phrase = expected.get("music_prompt_lyrics_contains")
    if expected_lyrics_phrase:
        assert expected_lyrics_phrase in (music_prompt.get("lyrics_prompt") or "")


@pytest.mark.remote_integration
@pytest.mark.parametrize(
    "case",
    GOLDEN_CASES,
    ids=[case["case_id"] for case in GOLDEN_CASES],
)
def test_video_generation_api_remote_golden_case(case: dict[str, Any]) -> None:
    """Run one manifest-defined real media API example as a golden contract test."""

    _require_case_env(case)
    video_path = _resolved_video_path(case)
    case_id = case["case_id"]

    def _run() -> None:
        staged_blob: str | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="api-video-golden-remote-") as tmp:
                local_temp_dir = (
                    "temp_video_workdir/"
                    f"test_api_video_generation_golden_{case_id}_{Path(tmp).name}"
                )
                local_temp_root = Path.cwd() / local_temp_dir

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
                    patch_providers = case.get("patch_providers", {})
                    if patch_providers.get("provider_c"):
                        stack.enter_context(
                            patch(
                                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.ProviderCApi",
                                return_value=MagicMock(),
                            )
                        )
                    if patch_providers.get("basic"):
                        stack.enter_context(
                            patch(
                                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.build_music_provider",
                                return_value=MagicMock(),
                            )
                        )
                    if patch_providers.get("enhanced"):
                        stack.enter_context(
                            patch(
                                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.build_edenn_enhanced_music_provider",
                                return_value=MagicMock(),
                            )
                        )

                    app, settings, storage = _build_video_api_app()
                    form_data = dict(case["form"])
                    files: dict[str, tuple[str, bytes, str]] = {}
                    input_source = case["input_source"]

                    if input_source == "upload":
                        files["video"] = _video_upload_tuple(video_path)
                    elif input_source == "staged_video_url":
                        staged_blob, staged_url = _stage_video_url(
                            settings=settings,
                            storage=storage,
                            case_id=case_id,
                            video_path=video_path,
                        )
                        form_data["video_url"] = staged_url
                    else:
                        raise AssertionError(f"Unsupported golden input source: {input_source}")

                    if case.get("vocal_sample") == "remote_helper":
                        form_data.update(remote_vocal_sample_form_data())
                        vocal_upload = remote_vocal_sample_upload_tuple()
                        if vocal_upload is not None:
                            files[vocal_upload[0]] = vocal_upload[1]

                    with TestClient(app) as client:
                        response = client.post(
                            "/api/v1/jobs/video",
                            data=form_data,
                            files=files or None,
                        )

                try:
                    handle_remote_http_failure(response)
                    assert response.status_code == 200, response.text
                    payload = response.json()
                    _save_remote_payload_if_requested(case_id, payload)
                    _assert_golden_response(case, payload)
                finally:
                    if staged_blob:
                        try:
                            storage.delete_blob(
                                container=settings.upload_container,
                                blob_name=staged_blob,
                            )
                        except Exception:
                            pass
                    if local_temp_root.exists():
                        shutil.rmtree(local_temp_root, ignore_errors=True)
        except Exception as exc:
            handle_remote_failure(exc)
            raise

    run_with_remote_rate_limit_retry(
        _run,
        operation_name=f"test_video_generation_api_remote_golden_case[{case_id}]",
    )
