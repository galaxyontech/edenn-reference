from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import sentry_sdk
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from EdennCode.Deployment.api_common import (
    ApiContext,
    ErrorDetail,
    JobStatus,
    LyricsTimestampModel,
    TokenUsageCountsResponse,
    cleanup_temp_dir,
    download_public_file_to_disk,
    edenn_error_to_http_exception,
    guess_audio_content_type,
    guess_image_content_type,
    parse_url_list_json,
    prepare_audio_for_provider_b_vocal_clone,
    probe_audio_metrics,
    resolve_media_source_to_disk,
    resolve_optional_media_source_to_disk,
    sanitize_filename,
    service_version,
    write_upload_to_disk,
)
from EdennCode.Deployment.audio_edit_workflows import AudioCreativeEditResult
from EdennCode.Deployment.auth.middleware import get_principal, resolve_user_id
from EdennCode.exceptions import EdennApiError, EdennError

VALID_CREATIVE_EDIT_MODEL_SPECS = {"edenn_enhanced", "edenn_studio"}
LEGACY_MODEL_MAP = {
    "provider_c": "edenn_studio",
}
VALID_STUDIO_MODES = {"simple", "custom"}


class CreativeEditPromptResponse(BaseModel):
    title: str | None = None
    edit_intent_summary: str | None = None
    visual_style_summary: str | None = None
    edit_prompt: str | None = None
    style_prompt: str | None = None
    lyrics_prompt: str | None = None


class CreativeSceneModel(BaseModel):
    scene_index: int
    start_timestamp: float
    end_timestamp: float
    visual_summary: str
    key_actions: str
    mood: str


class CreativeVisualAnalysisResponse(BaseModel):
    input_type: str
    summary: str = ""
    overall_mood: str = ""
    visual_style: str = ""
    creative_direction: str = ""
    key_elements: List[str] = Field(default_factory=list)
    scenes: List[CreativeSceneModel] = Field(default_factory=list)


class CreativeEditTokenBreakdownResponse(BaseModel):
    user_prompt_preprocessor: TokenUsageCountsResponse = Field(default_factory=TokenUsageCountsResponse)
    visual_analysis: TokenUsageCountsResponse = Field(default_factory=TokenUsageCountsResponse)
    creative_edit_prompt: TokenUsageCountsResponse = Field(default_factory=TokenUsageCountsResponse)


class AudioCreativeEditResponse(BaseModel):
    job_id: str
    status: str = Field(default=JobStatus.COMPLETED)
    version: str = Field(default_factory=service_version)
    source_audio_blob: Optional[str] = None
    source_audio_url: Optional[str] = None
    edited_audio_blob: Optional[str] = None
    edited_audio_url: Optional[str] = None
    # Duration (seconds) + byte size of the complete edited audio track.
    edited_audio_duration_s: Optional[float] = None
    edited_audio_size_bytes: Optional[int] = None
    secondary_edited_audio_blob: Optional[str] = None
    secondary_edited_audio_url: Optional[str] = None
    secondary_edited_audio_duration_s: Optional[float] = None
    secondary_edited_audio_size_bytes: Optional[int] = None
    thumbnail_blob: Optional[str] = None
    thumbnail_url: Optional[str] = None
    visual_analysis: CreativeVisualAnalysisResponse
    creative_edit_prompt: CreativeEditPromptResponse
    lyrics_timestamps: List[LyricsTimestampModel] = Field(default_factory=list)
    include_vocals: bool = Field(default=False)
    vocal_gender: str = Field(default="female")
    modelspec: str = Field(default="edenn_enhanced")
    user_requested_language: str = Field(default="")
    vocal_id_used: Optional[str] = None
    token_usage: Optional[int] = None
    raw_token_usage: Optional[TokenUsageCountsResponse] = None
    token_usage_breakdown: Optional[CreativeEditTokenBreakdownResponse] = None
    job_received_timestamp: Optional[int] = None
    job_finished_timestamp: Optional[int] = None


def _normalize_creative_prompt(raw_value: Any) -> CreativeEditPromptResponse:
    if not isinstance(raw_value, dict):
        return CreativeEditPromptResponse()
    return CreativeEditPromptResponse(
        title=raw_value.get("title"),
        edit_intent_summary=raw_value.get("edit_intent_summary"),
        visual_style_summary=raw_value.get("visual_style_summary"),
        edit_prompt=raw_value.get("edit_prompt"),
        style_prompt=raw_value.get("style_prompt"),
        lyrics_prompt=raw_value.get("lyrics_prompt"),
    )


def _normalize_visual_analysis(raw_value: Any) -> CreativeVisualAnalysisResponse:
    scenes: List[CreativeSceneModel] = []
    raw_scenes = getattr(raw_value, "scenes", []) or []
    for scene in raw_scenes:
        scenes.append(
            CreativeSceneModel(
                scene_index=getattr(scene, "scene_index", 0),
                start_timestamp=getattr(scene, "start_timestamp", 0.0),
                end_timestamp=getattr(scene, "end_timestamp", 0.0),
                visual_summary=getattr(scene, "visual_summary", ""),
                key_actions=getattr(scene, "key_actions", ""),
                mood=getattr(scene, "mood", ""),
            )
        )
    key_elements = getattr(raw_value, "key_elements", []) or []
    if not isinstance(key_elements, list):
        key_elements = []
    return CreativeVisualAnalysisResponse(
        input_type=getattr(raw_value, "input_type", "none"),
        summary=getattr(raw_value, "summary", "") or "",
        overall_mood=getattr(raw_value, "overall_mood", "") or "",
        visual_style=getattr(raw_value, "visual_style", "") or "",
        creative_direction=getattr(raw_value, "creative_direction", "") or "",
        key_elements=[str(item) for item in key_elements if item is not None],
        scenes=scenes,
    )


def _normalize_counts(raw_value: Any) -> TokenUsageCountsResponse:
    if not isinstance(raw_value, dict):
        return TokenUsageCountsResponse()
    return TokenUsageCountsResponse(
        prompt_tokens=int(raw_value.get("prompt_tokens", 0) or 0),
        completion_tokens=int(raw_value.get("completion_tokens", 0) or 0),
        total_tokens=int(raw_value.get("total_tokens", 0) or 0),
    )


def _normalize_breakdown(raw_value: Any) -> CreativeEditTokenBreakdownResponse:
    if not isinstance(raw_value, dict):
        return CreativeEditTokenBreakdownResponse()
    return CreativeEditTokenBreakdownResponse(
        user_prompt_preprocessor=_normalize_counts(raw_value.get("user_prompt_preprocessor")),
        visual_analysis=_normalize_counts(raw_value.get("visual_analysis")),
        creative_edit_prompt=_normalize_counts(raw_value.get("creative_edit_prompt")),
    )


def _normalize_studio_mode(raw_value: Optional[str]) -> str:
    normalized = (raw_value or "simple").strip().lower() or "simple"
    if normalized not in VALID_STUDIO_MODES:
        allowed = ", ".join(sorted(VALID_STUDIO_MODES))
        raise EdennApiError(
            f"Invalid studio_mode '{normalized}'. Allowed values: {allowed}.",
            public_message="Invalid studio mode.",
            status_code=400,
            component="api",
            operation="normalize_studio_mode",
        )
    return normalized


def _normalize_studio_weight(
    *,
    field_name: str,
    raw_value: Optional[float],
) -> Optional[float]:
    if raw_value is None:
        return None
    value = float(raw_value)
    if value < 0.0 or value > 1.0:
        raise EdennApiError(
            f"{field_name} must be between 0.00 and 1.00.",
            public_message=f"{field_name} must be between 0.00 and 1.00.",
            status_code=400,
            component="api",
            operation="normalize_studio_weight",
        )
    hundredths = round(value * 100)
    if abs((value * 100) - hundredths) > 1e-9:
        raise EdennApiError(
            f"{field_name} must use 0.01 increments.",
            public_message=f"{field_name} must use 0.01 increments.",
            status_code=400,
            component="api",
            operation="normalize_studio_weight",
        )
    return hundredths / 100.0


async def _resolve_optional_video_source(
    *,
    video: UploadFile | None,
    video_url: Optional[str],
    destination_dir: Path,
) -> Optional[Path]:
    upload_provided = video is not None
    url_provided = bool((video_url or "").strip())
    if not upload_provided and not url_provided:
        return None
    return await resolve_media_source_to_disk(
        upload=video,
        remote_url=video_url,
        destination_dir=destination_dir,
        fallback_filename="visual.mp4",
        asset_label="video",
    )


async def _resolve_optional_image_sources(
    *,
    images: Optional[List[UploadFile]],
    image_urls_json: Optional[str],
    destination_dir: Path,
) -> List[Path]:
    uploads = [item for item in (images or []) if item is not None]
    url_list = parse_url_list_json(image_urls_json, field_name="image_urls_json")
    if uploads and url_list:
        raise EdennApiError(
            "Provide either image uploads or image_urls_json, not both.",
            public_message="Provide image uploads or image URLs, not both.",
            status_code=400,
            component="api",
            operation="resolve_image_sources",
        )
    resolved: List[Path] = []
    if uploads:
        destination_dir.mkdir(parents=True, exist_ok=True)
        for idx, image in enumerate(uploads):
            filename = sanitize_filename(image.filename or f"image_{idx + 1}.png")
            resolved.append(await write_upload_to_disk(image, destination_dir / filename))
        return resolved
    if url_list:
        destination_dir.mkdir(parents=True, exist_ok=True)
        for idx, remote_url in enumerate(url_list):
            destination = destination_dir / f"image_{idx + 1}_{sanitize_filename(Path(remote_url).name or 'remote.png')}"
            resolved.append(
                await download_public_file_to_disk(
                    url=remote_url,
                    destination=destination,
                    asset_label="image",
                )
            )
    return resolved


def create_audio_creative_edit_router(context: ApiContext) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/api/v1/jobs/audio-creative-edit",
        response_model=AudioCreativeEditResponse,
        summary="Upload source audio and optionally visuals to generate a creative audio edit.",
    )
    async def create_audio_creative_edit_job(
        *,
        request: Request,
        audio: UploadFile | None = File(None, description="Source audio file."),
        audio_url: Optional[str] = Form(None),
        user_prompt: str = Form(""),
        modelspec: str = Form("edenn_enhanced"),
        studio_mode: str = Form("simple"),
        studio_style_weight: Optional[float] = Form(None),
        studio_audio_weight: Optional[float] = Form(None),
        studio_weirdness_constraint: Optional[float] = Form(None),
        video: UploadFile | None = File(None, description="Optional visual video file."),
        video_url: Optional[str] = Form(None),
        images: Optional[List[UploadFile]] = File(None, description="Optional visual image files."),
        image_urls_json: Optional[str] = Form(None),
        vocal_id: Optional[str] = Form(None),
        vocal_sample: UploadFile | None = File(None, description="Optional vocal sample for edenn_enhanced vocal cloning."),
        vocal_sample_url: Optional[str] = Form(None),
        user_id: Optional[str] = Form(
            None,
            description="Optional caller-supplied user identifier (overridden by "
                        "the API key's user in enforce mode).",
        ),
    ) -> AudioCreativeEditResponse:
        user_id = resolve_user_id(request, user_id)
        job_id = uuid4().hex
        job_dir = context.settings.workdir / job_id
        source_dir = job_dir / "source"
        source_audio_dir = source_dir / "audio"
        visual_video_dir = source_dir / "video"
        visual_images_dir = source_dir / "images"
        vocal_source_dir = source_dir / "vocal"

        source_audio_path = await resolve_media_source_to_disk(
            upload=audio,
            remote_url=audio_url,
            destination_dir=source_audio_dir,
            fallback_filename="source_audio.wav",
            asset_label="audio",
        )

        try:
            requested_modelspec_raw = (modelspec or "edenn_enhanced").strip().lower() or "edenn_enhanced"
            requested_modelspec = LEGACY_MODEL_MAP.get(requested_modelspec_raw, requested_modelspec_raw)
            if requested_modelspec not in VALID_CREATIVE_EDIT_MODEL_SPECS:
                allowed = ", ".join(sorted(VALID_CREATIVE_EDIT_MODEL_SPECS))
                raise EdennApiError(
                    f"Invalid modelspec '{requested_modelspec}'. Allowed values: {allowed}.",
                    public_message=f"Invalid modelspec '{requested_modelspec}'. Allowed values: {allowed}.",
                    status_code=400,
                    component="api",
                    operation="validate_modelspec",
                )
            vocal_sample_path = await resolve_optional_media_source_to_disk(
                upload=vocal_sample,
                remote_url=vocal_sample_url,
                destination_dir=vocal_source_dir,
                fallback_filename="vocal_sample.m4a",
                asset_label="vocal sample",
            )
            normalized_vocal_id = (vocal_id or "").strip() or None
            if normalized_vocal_id and vocal_sample_path is not None:
                raise HTTPException(
                    status_code=400,
                    detail="Provide either vocal_id or vocal_sample/vocal_sample_url, not both.",
                )
            if requested_modelspec != "edenn_enhanced" and (
                normalized_vocal_id or vocal_sample_path is not None
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Vocal clone inputs are only supported for modelspec=edenn_enhanced.",
                )
            prepared_vocal_sample_path = (
                prepare_audio_for_provider_b_vocal_clone(
                    source_audio_path=vocal_sample_path,
                    destination_dir=vocal_source_dir,
                )
                if vocal_sample_path is not None
                else None
            )
            normalized_studio_mode = _normalize_studio_mode(studio_mode)
            normalized_studio_style_weight = _normalize_studio_weight(
                field_name="studio_style_weight",
                raw_value=studio_style_weight,
            )
            normalized_studio_audio_weight = _normalize_studio_weight(
                field_name="studio_audio_weight",
                raw_value=studio_audio_weight,
            )
            normalized_studio_weirdness_constraint = _normalize_studio_weight(
                field_name="studio_weirdness_constraint",
                raw_value=studio_weirdness_constraint,
            )
            studio_custom_mode = normalized_studio_mode == "custom"
            has_studio_tuning = any(
                value is not None
                for value in (
                    normalized_studio_style_weight,
                    normalized_studio_audio_weight,
                    normalized_studio_weirdness_constraint,
                )
            )
            if requested_modelspec != "edenn_studio":
                if normalized_studio_mode != "simple" or has_studio_tuning:
                    raise EdennApiError(
                        "Studio-specific generation settings require modelspec=edenn_studio.",
                        public_message="Studio-specific generation settings require modelspec=edenn_studio.",
                        status_code=400,
                        component="api",
                        operation="validate_studio_options",
                    )
            elif not studio_custom_mode and has_studio_tuning:
                raise EdennApiError(
                    "Studio influence weights require studio_mode=custom.",
                    public_message="Studio influence weights require studio_mode=custom.",
                    status_code=400,
                    component="api",
                    operation="validate_studio_options",
                )

            visual_video_path = await _resolve_optional_video_source(
                video=video,
                video_url=video_url,
                destination_dir=visual_video_dir,
            )
            visual_image_paths = await _resolve_optional_image_sources(
                images=images,
                image_urls_json=image_urls_json,
                destination_dir=visual_images_dir,
            )
            if visual_video_path and visual_image_paths:
                raise EdennApiError(
                    "Provide either video/video_url or images/image_urls_json, not both.",
                    public_message="Provide one visual source type at a time.",
                    status_code=400,
                    component="api",
                    operation="resolve_visual_sources",
                )

            source_audio_blob = source_audio_url_value = None
            source_audio_provider_url: Optional[str] = None
            if context.storage.enabled:
                source_audio_blob = context.storage.upload_path(
                    container=context.settings.upload_container,
                    path=source_audio_path,
                    blob_name=f"jobs/{job_id}/source-audio/{source_audio_path.name}",
                    content_type=guess_audio_content_type(source_audio_path),
                )
                if source_audio_blob:
                    source_audio_url_value = context.storage.generate_sas_url(
                        container=context.settings.upload_container,
                        blob_name=source_audio_blob,
                        require_signed=True,
                    )
                    source_audio_provider_url = source_audio_url_value
            if requested_modelspec == "edenn_studio" and not source_audio_provider_url:
                if (audio_url or "").strip():
                    source_audio_provider_url = (audio_url or "").strip()
                else:
                    raise EdennApiError(
                        "ProviderC creative edit requires a remotely accessible source audio URL.",
                        public_message="This request needs storage-enabled audio staging.",
                        status_code=500,
                        component="api",
                        operation="prepare_provider_c_source_audio",
                    )

            result: AudioCreativeEditResult = await context.audio_creative_edit_workflow.run(
                source_audio_path=source_audio_path,
                user_prompt=user_prompt,
                modelspec=requested_modelspec,
                source_audio_provider_url=source_audio_provider_url,
                video_path=visual_video_path,
                image_paths=visual_image_paths,
                vocal_id=normalized_vocal_id,
                vocal_sample_path=prepared_vocal_sample_path,
                provider_c_custom_mode=studio_custom_mode,
                provider_c_style_weight=normalized_studio_style_weight,
                provider_c_audio_weight=normalized_studio_audio_weight,
                provider_c_weirdness_constraint=normalized_studio_weirdness_constraint,
            )

            edited_audio_blob = edited_audio_url = None
            secondary_edited_audio_blob = secondary_edited_audio_url = None
            thumbnail_blob = thumbnail_url = None
            if context.storage.enabled:
                edited_audio_blob = context.storage.upload_path(
                    container=context.settings.audio_container_name,
                    path=result.edited_audio_path,
                    blob_name=f"jobs/{job_id}/audio-edit/{result.edited_audio_path.name}",
                    content_type=guess_audio_content_type(result.edited_audio_path),
                )
                if edited_audio_blob:
                    edited_audio_url = context.storage.generate_sas_url(
                        container=context.settings.audio_container_name,
                        blob_name=edited_audio_blob,
                    )
                if result.secondary_edited_audio_path and result.secondary_edited_audio_path.exists():
                    secondary_edited_audio_blob = context.storage.upload_path(
                        container=context.settings.audio_container_name,
                        path=result.secondary_edited_audio_path,
                        blob_name=f"jobs/{job_id}/audio-edit/secondary/{result.secondary_edited_audio_path.name}",
                        content_type=guess_audio_content_type(result.secondary_edited_audio_path),
                    )
                    if secondary_edited_audio_blob:
                        secondary_edited_audio_url = context.storage.generate_sas_url(
                            container=context.settings.audio_container_name,
                            blob_name=secondary_edited_audio_blob,
                        )
                thumbnail_path = getattr(result.visual_analysis, "thumbnail_path", None)
                if thumbnail_path and Path(thumbnail_path).exists():
                    thumbnail_blob = context.storage.upload_path(
                        container=context.settings.output_container,
                        path=Path(thumbnail_path),
                        blob_name=f"jobs/{job_id}/creative-edit/thumbnail/{Path(thumbnail_path).name}",
                        content_type=guess_image_content_type(thumbnail_path),
                    )
                    if thumbnail_blob:
                        thumbnail_url = context.storage.generate_sas_url(
                            container=context.settings.output_container,
                            blob_name=thumbnail_blob,
                        )

            # Probe the complete edited track(s) from the local files before cleanup.
            edited_audio_duration_s, edited_audio_size_bytes = probe_audio_metrics(
                result.edited_audio_path
            )
            _secondary_path = result.secondary_edited_audio_path
            secondary_edited_audio_duration_s, secondary_edited_audio_size_bytes = (
                probe_audio_metrics(
                    _secondary_path
                    if _secondary_path and _secondary_path.exists()
                    else None
                )
            )

            raw_usage = _normalize_counts(result.token_usage)
            if context.usage_recorder is not None:
                latency_ms = None
                if result.job_finished_timestamp and result.job_received_timestamp:
                    latency_ms = (
                        result.job_finished_timestamp - result.job_received_timestamp
                    ) * 1000
                context.usage_recorder.record_job(
                    job_id=job_id,
                    endpoint="/api/v1/jobs/audio-creative-edit",
                    status="completed",
                    principal=get_principal(request),
                    model_spec=result.used_music_model_spec,
                    token_usage=result.token_usage,
                    cost_metadata=None,  # endpoint computes no cost today
                    latency_ms=latency_ms,
                )
            return AudioCreativeEditResponse(
                job_id=job_id,
                source_audio_blob=source_audio_blob,
                source_audio_url=source_audio_url_value,
                edited_audio_blob=edited_audio_blob,
                edited_audio_url=edited_audio_url,
                edited_audio_duration_s=edited_audio_duration_s,
                edited_audio_size_bytes=edited_audio_size_bytes,
                secondary_edited_audio_blob=secondary_edited_audio_blob,
                secondary_edited_audio_url=secondary_edited_audio_url,
                secondary_edited_audio_duration_s=secondary_edited_audio_duration_s,
                secondary_edited_audio_size_bytes=secondary_edited_audio_size_bytes,
                thumbnail_blob=thumbnail_blob,
                thumbnail_url=thumbnail_url,
                visual_analysis=_normalize_visual_analysis(result.visual_analysis),
                creative_edit_prompt=_normalize_creative_prompt(result.creative_edit_prompt),
                lyrics_timestamps=[
                    LyricsTimestampModel(
                        text=item.text,
                        startS=float(item.startS),
                        endS=float(item.endS),
                        i=item.i,
                    )
                    for item in result.lyrics_timestamps
                ],
                include_vocals=result.include_vocals,
                vocal_gender=result.vocal_gender,
                modelspec=result.used_music_model_spec,
                user_requested_language=result.user_requested_language,
                vocal_id_used=result.vocal_id_used,
                token_usage=raw_usage.total_tokens,
                raw_token_usage=raw_usage,
                token_usage_breakdown=_normalize_breakdown(result.token_usage_breakdown),
                job_received_timestamp=result.job_received_timestamp,
                job_finished_timestamp=result.job_finished_timestamp,
            )
        except HTTPException:
            raise
        except EdennError as exc:
            context.logger.exception("Audio creative edit job %s failed: %s", job_id, exc.to_dict())
            raise edenn_error_to_http_exception(exc) from exc
        except Exception as exc:
            context.logger.exception("Audio creative edit job %s failed: %s", job_id, exc)
            with sentry_sdk.new_scope() as scope:
                scope.set_tag("edenn_error_code", 90001)
                scope.set_tag("job_id", job_id)
                sentry_sdk.capture_exception(exc)
            raise HTTPException(
                status_code=500,
                detail=ErrorDetail(
                    error_code=90001,
                    message="An unexpected error occurred. Please try again later.",
                    retryable=True,
                ).model_dump(),
            ) from exc
        finally:
            cleanup_temp_dir(job_dir, logger=context.logger)

    return router
