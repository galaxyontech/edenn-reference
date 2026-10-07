from __future__ import annotations

import io
import math
import struct
import wave
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.api_audio_creative_edit import create_audio_creative_edit_router
from EdennCode.Deployment.api_common import ApiContext
from EdennCode.TestSuites.helpers.paths import MULTI_IMAGE_DESIGN_IMAGES_DIR
from EdennCode.Deployment.audio_edit_workflows import AudioCreativeEditOrchestrator
from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Deployment.storage import create_storage_service
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


def _build_audio_creative_edit_api_app() -> FastAPI:
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
        workflow=MagicMock(),
        alignment_workflow=MagicMock(),
        audio_creative_edit_workflow=workflow,
        logger=MagicMock(),
    )
    app = FastAPI()
    app.include_router(create_audio_creative_edit_router(context))
    return app


def _make_melodic_wav_bytes(duration_s: float = 12.0, sample_rate: int = 16000) -> bytes:
    note_sequence = [261.63, 293.66, 329.63, 392.00, 440.00, 392.00, 329.63, 293.66]
    frame_count = int(duration_s * sample_rate)
    note_frame_count = max(1, sample_rate // 2)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        for frame_index in range(frame_count):
            frequency = note_sequence[(frame_index // note_frame_count) % len(note_sequence)]
            note_progress = (frame_index % note_frame_count) / note_frame_count
            envelope = 1.0
            if note_progress < 0.08:
                envelope = note_progress / 0.08
            elif note_progress > 0.85:
                envelope = max(0.0, (1.0 - note_progress) / 0.15)
            sample = envelope * (
                0.55 * math.sin(2.0 * math.pi * frequency * frame_index / sample_rate)
                + 0.22 * math.sin(2.0 * math.pi * frequency * 2.0 * frame_index / sample_rate)
                + 0.10 * math.sin(2.0 * math.pi * (frequency / 2.0) * frame_index / sample_rate)
            )
            wav_file.writeframesraw(struct.pack("<h", int(sample * 32767.0 * 0.6)))
    return buffer.getvalue()


@pytest.mark.remote_integration
@pytest.mark.parametrize(
    ("modelspec", "user_prompt", "expect_vocals", "patch_provider_c", "patch_enhanced"),
    [
        (
            "edenn_enhanced",
            "Turn this into a bright female vocal pop cover with clear lyrics.",
            True,
            True,
            False,
        ),
        (
            "edenn_studio",
            "Turn this into a cinematic instrumental cover with a wide dramatic lift.",
            False,
            False,
            True,
        ),
    ],
    ids=[
        "edenn_enhanced",
        "edenn_studio",
    ],
)
def test_audio_creative_edit_api_remote_contract(
    modelspec: str,
    user_prompt: str,
    expect_vocals: bool,
    patch_provider_c: bool,
    patch_enhanced: bool,
) -> None:
    require_remote_env(
        "AZURE_ENDPOINT",
        "AZURE_API_KEY",
        "AZURE_MODEL",
    )
    require_remote_media_storage_env()
    if modelspec == "edenn_enhanced":
        require_any_remote_env(
            "EDENN_ENHANCED_PROVIDER_B_API_KEY",
            "PROVIDER_B_API_KEY",
            "PROVIDER_B_API_KEY_1",
            "PROVIDER_B_API_KEY_2",
        )
    elif modelspec == "edenn_studio":
        require_remote_env("PROVIDER_C_API_KEY")

    audio_bytes = _make_melodic_wav_bytes()

    def _run() -> None:
        try:
            with ExitStack() as stack:
                if patch_provider_c:
                    stack.enter_context(
                        patch(
                            "EdennCode.WorkflowFactory.AudioCreativeEditWorkflow.audio_creative_edit_workflow.ProviderCApi",
                            return_value=MagicMock(),
                        )
                    )
                if patch_enhanced:
                    stack.enter_context(
                        patch(
                            "EdennCode.WorkflowFactory.AudioCreativeEditWorkflow.audio_creative_edit_workflow.build_edenn_enhanced_music_provider",
                            return_value=MagicMock(),
                        )
                    )
                app = _build_audio_creative_edit_api_app()
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/jobs/audio-creative-edit",
                        data={
                            "user_prompt": user_prompt,
                            "modelspec": modelspec,
                        },
                        files={
                            "audio": (
                                "source.wav",
                                audio_bytes,
                                "audio/wav",
                            )
                        },
                    )
            handle_remote_http_failure(response)
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["modelspec"] == modelspec
            assert payload["source_audio_url"]
            assert payload["edited_audio_url"]
            # Complete edited-track duration (s) + byte size.
            assert payload["edited_audio_duration_s"] and payload["edited_audio_duration_s"] > 0
            assert payload["edited_audio_size_bytes"] and payload["edited_audio_size_bytes"] > 0
            assert payload["creative_edit_prompt"]
            assert payload["visual_analysis"]["input_type"] == "none"
            assert payload["include_vocals"] is expect_vocals
            if expect_vocals:
                assert payload["lyrics_timestamps"]
                assert payload["vocal_gender"]
                assert payload["user_requested_language"]
        except Exception as exc:
            handle_remote_failure(exc)
            raise

    run_with_remote_rate_limit_retry(
        _run,
        operation_name=f"test_audio_creative_edit_api_remote_contract[{modelspec}]",
    )


@pytest.mark.remote_integration
def test_audio_creative_edit_api_remote_contract_with_vocal_clone() -> None:
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

    audio_bytes = _make_melodic_wav_bytes()

    def _run() -> None:
        try:
            app = _build_audio_creative_edit_api_app()
            with TestClient(app) as client:
                form_data = {
                    "user_prompt": "Turn this into a bright female vocal pop cover with clear lyrics.",
                    "modelspec": "edenn_enhanced",
                    **remote_vocal_sample_form_data(),
                }
                files = {
                    "audio": (
                        "source.wav",
                        audio_bytes,
                        "audio/wav",
                    )
                }
                vocal_upload = remote_vocal_sample_upload_tuple()
                if vocal_upload is not None:
                    files[vocal_upload[0]] = vocal_upload[1]
                response = client.post(
                    "/api/v1/jobs/audio-creative-edit",
                    data=form_data,
                    files=files,
                )
            handle_remote_http_failure(response)
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["modelspec"] == "edenn_enhanced"
            assert payload["include_vocals"] is True
            assert payload["edited_audio_url"]
            assert payload["vocal_id_used"]
        except Exception as exc:
            handle_remote_failure(exc)
            raise

    run_with_remote_rate_limit_retry(
        _run,
        operation_name="test_audio_creative_edit_api_remote_contract_with_vocal_clone",
    )


@pytest.mark.remote_integration
def test_audio_creative_edit_api_remote_contract_with_image_context() -> None:
    """edenn_enhanced with a visual image input: visual_analysis.input_type should be 'images'."""
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

    image_paths = sorted(p for p in MULTI_IMAGE_DESIGN_IMAGES_DIR.iterdir() if p.is_file())
    assert image_paths, f"No images found in {MULTI_IMAGE_DESIGN_IMAGES_DIR}"
    image_path = image_paths[0]
    audio_bytes = _make_melodic_wav_bytes()

    def _run() -> None:
        try:
            app = _build_audio_creative_edit_api_app()
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/audio-creative-edit",
                    data={
                        "user_prompt": (
                            "Turn this into a bright upbeat pop track "
                            "that captures the mood of the image."
                        ),
                        "modelspec": "edenn_enhanced",
                    },
                    files={
                        "audio": ("source.wav", audio_bytes, "audio/wav"),
                        "images": (image_path.name, image_path.read_bytes(), "image/jpeg"),
                    },
                )
            handle_remote_http_failure(response)
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["modelspec"] == "edenn_enhanced"
            assert payload["edited_audio_url"]
            assert payload["source_audio_url"]
            assert payload["creative_edit_prompt"]
            assert payload["visual_analysis"]["input_type"] == "images"
        except Exception as exc:
            handle_remote_failure(exc)
            raise

    run_with_remote_rate_limit_retry(
        _run,
        operation_name="test_audio_creative_edit_api_remote_contract_with_image_context",
    )


@pytest.mark.remote_integration
def test_audio_creative_edit_api_remote_contract_studio_custom_mode() -> None:
    """edenn_studio with studio_mode='custom' and explicit style/audio weights succeeds."""
    require_remote_env(
        "AZURE_ENDPOINT",
        "AZURE_API_KEY",
        "AZURE_MODEL",
        "PROVIDER_C_API_KEY",
    )
    require_remote_media_storage_env()

    audio_bytes = _make_melodic_wav_bytes()

    def _run() -> None:
        try:
            with patch(
                "EdennCode.WorkflowFactory.AudioCreativeEditWorkflow.audio_creative_edit_workflow.build_edenn_enhanced_music_provider",
                return_value=MagicMock(),
            ):
                app = _build_audio_creative_edit_api_app()
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/jobs/audio-creative-edit",
                        data={
                            "user_prompt": "A dramatic cinematic orchestral cover with no vocals.",
                            "modelspec": "edenn_studio",
                            "studio_mode": "custom",
                            "studio_style_weight": "0.7",
                            "studio_audio_weight": "0.5",
                            "studio_weirdness_constraint": "0.3",
                        },
                        files={
                            "audio": ("source.wav", audio_bytes, "audio/wav"),
                        },
                    )
            handle_remote_http_failure(response)
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["modelspec"] == "edenn_studio"
            assert payload["edited_audio_url"]
            assert payload["source_audio_url"]
            assert payload["creative_edit_prompt"]
        except Exception as exc:
            handle_remote_failure(exc)
            raise

    run_with_remote_rate_limit_retry(
        _run,
        operation_name="test_audio_creative_edit_api_remote_contract_studio_custom_mode",
    )
