from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.api_common import ApiContext
from EdennCode.Deployment.api_vocal_clone import create_vocal_clone_router
from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Deployment.storage import create_storage_service
from EdennCode.Deployment.vocal_clone_workflows import VocalCloneOrchestrator
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


def _build_vocal_clone_api_app() -> FastAPI:
    settings = DeploymentSettings.from_env()
    storage = create_storage_service(settings)
    workflow = VocalCloneOrchestrator()
    context = ApiContext(
        settings=settings,
        storage=storage,
        workflow=MagicMock(),
        alignment_workflow=MagicMock(),
        audio_creative_edit_workflow=MagicMock(),
        logger=MagicMock(),
        vocal_clone_workflow=workflow,
    )
    app = FastAPI()
    app.include_router(create_vocal_clone_router(context))
    return app


@pytest.mark.remote_integration
def test_vocal_clone_api_remote_contract() -> None:
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

    def _run() -> None:
        try:
            app = _build_vocal_clone_api_app()
            with TestClient(app) as client:
                form_data = remote_vocal_sample_form_data()
                files = {}
                vocal_upload = remote_vocal_sample_upload_tuple()
                if vocal_upload is not None:
                    files[vocal_upload[0]] = vocal_upload[1]
                response = client.post(
                    "/api/v1/jobs/vocal-clone",
                    data=form_data,
                    files=files or None,
                )
            handle_remote_http_failure(response)
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["vocal_id"]
            assert payload["job_id"]
        except Exception as exc:
            handle_remote_failure(exc)
            raise

    run_with_remote_rate_limit_retry(
        _run,
        operation_name="test_vocal_clone_api_remote_contract",
    )
