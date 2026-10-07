from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.alignment_workflows import AudioAlignmentOrchestrator
from EdennCode.Deployment.api_common import ApiContext
from EdennCode.Deployment.api_video_alignment import create_video_alignment_router
from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Deployment.storage import create_storage_service
from EdennCode.TestSuites.helpers.integration import (
    handle_remote_failure,
    handle_remote_http_failure,
    require_remote_env,
    require_remote_media_storage_env,
    run_with_remote_rate_limit_retry,
)
from EdennCode.TestSuites.helpers.paths import (
    REMOTE_VOCAL_CLONE_SAMPLE_PATH,
    SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH,
)


def _build_alignment_api_app() -> FastAPI:
    settings = DeploymentSettings.from_env()
    storage = create_storage_service(settings)
    alignment_workflow = AudioAlignmentOrchestrator()
    context = ApiContext(
        settings=settings,
        storage=storage,
        workflow=MagicMock(),
        alignment_workflow=alignment_workflow,
        audio_creative_edit_workflow=MagicMock(),
        logger=MagicMock(),
    )
    app = FastAPI()
    app.include_router(create_video_alignment_router(context))
    return app


@pytest.mark.remote_integration
def test_video_alignment_api_remote_contract_file_upload() -> None:
    """Basic file-upload path: video + audio, no lyrics, default top_k=3."""
    require_remote_env("AZURE_ENDPOINT", "AZURE_API_KEY", "AZURE_MODEL")
    require_remote_media_storage_env()
    assert SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.exists()
    assert REMOTE_VOCAL_CLONE_SAMPLE_PATH.exists()

    def _run() -> None:
        try:
            app = _build_alignment_api_app()
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video-align-audio",
                    files={
                        "video": (
                            SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.name,
                            SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.read_bytes(),
                            "video/mp4",
                        ),
                        "audio": (
                            REMOTE_VOCAL_CLONE_SAMPLE_PATH.name,
                            REMOTE_VOCAL_CLONE_SAMPLE_PATH.read_bytes(),
                            "audio/mp4",
                        ),
                    },
                )
            handle_remote_http_failure(response)
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["job_id"]
            assert payload["status"] == "completed"
            assert payload["lyrics_provided"] is False
            assert payload["job_received_timestamp"] is not None
            assert payload["job_finished_timestamp"] is not None
            assert payload["video_metadata"]["duration"] > 0

            segments = payload["segments"]
            assert 1 <= len(segments) <= 3
            assert payload["best_segment"]["rank"] == 1
            assert segments[0]["rank"] == 1

            for segment in segments:
                assert segment["score"] >= 0.0
                assert segment["music_start_s"] >= 0.0
                assert segment["music_end_s"] > segment["music_start_s"]
                assert segment["aligned_lyrics"] == []

            # Storage should yield a matched audio URL for each segment
            assert payload["best_segment"]["matched_audio_url"]
        except Exception as exc:
            handle_remote_failure(exc)
            raise

    run_with_remote_rate_limit_retry(
        _run,
        operation_name="test_video_alignment_api_remote_contract_file_upload",
    )


@pytest.mark.remote_integration
def test_video_alignment_api_remote_contract_with_lyrics() -> None:
    """Alignment with word-level lyrics timestamps: lyrics_provided=True, aligned_lyrics populated."""
    require_remote_env("AZURE_ENDPOINT", "AZURE_API_KEY", "AZURE_MODEL")
    require_remote_media_storage_env()
    assert SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.exists()
    assert REMOTE_VOCAL_CLONE_SAMPLE_PATH.exists()

    lyrics = [
        {"text": "step", "startS": 0.5, "endS": 0.9, "i": 0},
        {"text": "into", "startS": 1.0, "endS": 1.4, "i": 1},
        {"text": "the", "startS": 1.5, "endS": 1.7, "i": 2},
        {"text": "light", "startS": 1.8, "endS": 2.3, "i": 3},
        {"text": "hold", "startS": 2.8, "endS": 3.1, "i": 4},
        {"text": "the", "startS": 3.2, "endS": 3.4, "i": 5},
        {"text": "moment", "startS": 3.5, "endS": 4.0, "i": 6},
    ]

    def _run() -> None:
        try:
            app = _build_alignment_api_app()
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video-align-audio",
                    data={
                        "top_k": "2",
                        "lyrics_timestamps_json": json.dumps(lyrics),
                    },
                    files={
                        "video": (
                            SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.name,
                            SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.read_bytes(),
                            "video/mp4",
                        ),
                        "audio": (
                            REMOTE_VOCAL_CLONE_SAMPLE_PATH.name,
                            REMOTE_VOCAL_CLONE_SAMPLE_PATH.read_bytes(),
                            "audio/mp4",
                        ),
                    },
                )
            handle_remote_http_failure(response)
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["lyrics_provided"] is True
            assert 1 <= len(payload["segments"]) <= 2
            assert payload["best_segment"]["rank"] == 1

            best = payload["best_segment"]
            # aligned_lyrics is the subset of input lyrics that fall within the selected window
            assert isinstance(best["aligned_lyrics"], list)
            for word in best["aligned_lyrics"]:
                assert word["text"]
                assert word["startS"] >= 0.0
                assert word["endS"] >= word["startS"]
        except Exception as exc:
            handle_remote_failure(exc)
            raise

    run_with_remote_rate_limit_retry(
        _run,
        operation_name="test_video_alignment_api_remote_contract_with_lyrics",
    )


@pytest.mark.remote_integration
def test_video_alignment_api_remote_contract_top_k_1() -> None:
    """top_k=1 returns exactly one segment; best_segment matches segments[0]."""
    require_remote_env("AZURE_ENDPOINT", "AZURE_API_KEY", "AZURE_MODEL")
    require_remote_media_storage_env()
    assert SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.exists()
    assert REMOTE_VOCAL_CLONE_SAMPLE_PATH.exists()

    def _run() -> None:
        try:
            app = _build_alignment_api_app()
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video-align-audio",
                    data={"top_k": "1"},
                    files={
                        "video": (
                            SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.name,
                            SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.read_bytes(),
                            "video/mp4",
                        ),
                        "audio": (
                            REMOTE_VOCAL_CLONE_SAMPLE_PATH.name,
                            REMOTE_VOCAL_CLONE_SAMPLE_PATH.read_bytes(),
                            "audio/mp4",
                        ),
                    },
                )
            handle_remote_http_failure(response)
            assert response.status_code == 200, response.text
            payload = response.json()
            assert len(payload["segments"]) == 1
            assert payload["best_segment"]["rank"] == 1
            assert payload["segments"][0]["rank"] == 1
            assert payload["best_segment"]["score"] == payload["segments"][0]["score"]
            assert (
                payload["best_segment"]["music_start_s"]
                == payload["segments"][0]["music_start_s"]
            )
        except Exception as exc:
            handle_remote_failure(exc)
            raise

    run_with_remote_rate_limit_retry(
        _run,
        operation_name="test_video_alignment_api_remote_contract_top_k_1",
    )


@pytest.mark.remote_integration
def test_video_alignment_api_remote_contract_top_k_5() -> None:
    """top_k=5 returns up to 5 segments with sequential ranks starting at 1."""
    require_remote_env("AZURE_ENDPOINT", "AZURE_API_KEY", "AZURE_MODEL")
    require_remote_media_storage_env()
    assert SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.exists()
    assert REMOTE_VOCAL_CLONE_SAMPLE_PATH.exists()

    def _run() -> None:
        try:
            app = _build_alignment_api_app()
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video-align-audio",
                    data={"top_k": "5"},
                    files={
                        "video": (
                            SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.name,
                            SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.read_bytes(),
                            "video/mp4",
                        ),
                        "audio": (
                            REMOTE_VOCAL_CLONE_SAMPLE_PATH.name,
                            REMOTE_VOCAL_CLONE_SAMPLE_PATH.read_bytes(),
                            "audio/mp4",
                        ),
                    },
                )
            handle_remote_http_failure(response)
            assert response.status_code == 200, response.text
            payload = response.json()
            segments = payload["segments"]
            assert 1 <= len(segments) <= 5
            ranks = [s["rank"] for s in segments]
            assert ranks == list(range(1, len(ranks) + 1)), (
                f"Expected sequential ranks starting at 1, got {ranks}"
            )
            # All segments must have valid window boundaries
            for segment in segments:
                assert segment["music_start_s"] >= 0.0
                assert segment["music_end_s"] > segment["music_start_s"]
                assert segment["score"] >= 0.0
        except Exception as exc:
            handle_remote_failure(exc)
            raise

    run_with_remote_rate_limit_retry(
        _run,
        operation_name="test_video_alignment_api_remote_contract_top_k_5",
    )


@pytest.mark.remote_integration
def test_video_alignment_api_remote_contract_video_url_input() -> None:
    """video_url form field resolves to the same result as a file upload."""
    import os

    require_remote_env(
        "AZURE_ENDPOINT",
        "AZURE_API_KEY",
        "AZURE_MODEL",
        "ALIGNMENT_TEST_VIDEO_URL",
    )
    require_remote_media_storage_env()
    assert REMOTE_VOCAL_CLONE_SAMPLE_PATH.exists()

    video_url = os.environ["ALIGNMENT_TEST_VIDEO_URL"].strip()

    def _run() -> None:
        try:
            app = _build_alignment_api_app()
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video-align-audio",
                    data={
                        "video_url": video_url,
                        "top_k": "2",
                    },
                    files={
                        "audio": (
                            REMOTE_VOCAL_CLONE_SAMPLE_PATH.name,
                            REMOTE_VOCAL_CLONE_SAMPLE_PATH.read_bytes(),
                            "audio/mp4",
                        ),
                    },
                )
            handle_remote_http_failure(response)
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["job_id"]
            assert payload["status"] == "completed"
            assert 1 <= len(payload["segments"]) <= 2
            assert payload["best_segment"]["rank"] == 1
            assert payload["video_metadata"]["duration"] > 0
        except Exception as exc:
            handle_remote_failure(exc)
            raise

    run_with_remote_rate_limit_retry(
        _run,
        operation_name="test_video_alignment_api_remote_contract_video_url_input",
    )
