from __future__ import annotations

from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from EdennCode.Deployment.api_common import (
    ApiContext,
    cleanup_temp_dir,
    edenn_error_to_http_exception,
    guess_audio_content_type,
    prepare_audio_for_provider_b_vocal_clone,
    resolve_optional_media_source_to_disk,
    service_version,
)
from EdennCode.Deployment.vocal_clone_workflows import VocalCloneResult
from EdennCode.exceptions import EdennError


class VocalCloneResponse(BaseModel):
    job_id: str
    status: str = Field(default="completed")
    vocal_sample_blob: Optional[str] = None
    vocal_sample_url: Optional[str] = None
    vocal_id: str
    version: str = Field(default_factory=service_version)


def create_vocal_clone_router(context: ApiContext) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/api/v1/jobs/vocal-clone",
        response_model=VocalCloneResponse,
        summary="Upload or reference a vocal sample and receive a reusable vocal ID.",
    )
    async def create_vocal_clone_job(
        *,
        vocal_sample: UploadFile | None = File(None, description="Vocal sample audio file."),
        vocal_sample_url: Optional[str] = Form(None),
    ) -> VocalCloneResponse:
        if context.vocal_clone_workflow is None:
            raise HTTPException(status_code=500, detail="Vocal clone workflow is not configured.")

        job_id = uuid4().hex
        job_dir = context.settings.workdir / job_id
        source_dir = job_dir / "source" / "vocal"

        try:
            source_audio_path = await resolve_optional_media_source_to_disk(
                upload=vocal_sample,
                remote_url=vocal_sample_url,
                destination_dir=source_dir,
                fallback_filename="vocal_sample.m4a",
                asset_label="vocal sample",
            )
            if source_audio_path is None:
                raise HTTPException(
                    status_code=400,
                    detail="Provide exactly one vocal_sample or vocal_sample_url.",
                )
            prepared_audio_path = prepare_audio_for_provider_b_vocal_clone(
                source_audio_path=source_audio_path,
                destination_dir=source_dir,
            )
            result: VocalCloneResult = await context.vocal_clone_workflow.run(
                source_audio_path=prepared_audio_path,
            )

            vocal_sample_blob = vocal_sample_url_value = None
            if context.storage.enabled:
                vocal_sample_blob = context.storage.upload_path(
                    container=context.settings.upload_container,
                    path=prepared_audio_path,
                    blob_name=f"jobs/{job_id}/vocal-clone/{prepared_audio_path.name}",
                    content_type=guess_audio_content_type(prepared_audio_path),
                )
                if vocal_sample_blob:
                    vocal_sample_url_value = context.storage.generate_sas_url(
                        container=context.settings.upload_container,
                        blob_name=vocal_sample_blob,
                        require_signed=True,
                    )

            return VocalCloneResponse(
                job_id=job_id,
                vocal_sample_blob=vocal_sample_blob,
                vocal_sample_url=vocal_sample_url_value,
                vocal_id=result.vocal_id,
            )
        except HTTPException:
            raise
        except EdennError as exc:
            context.logger.exception("Vocal clone job %s failed: %s", job_id, exc.to_dict())
            raise edenn_error_to_http_exception(exc) from exc
        except Exception as exc:
            context.logger.exception("Vocal clone job %s failed: %s", job_id, exc)
            raise HTTPException(status_code=500, detail="Unexpected server error.") from exc
        finally:
            cleanup_temp_dir(job_dir, logger=context.logger)

    return router


__all__ = ["VocalCloneResponse", "create_vocal_clone_router"]
