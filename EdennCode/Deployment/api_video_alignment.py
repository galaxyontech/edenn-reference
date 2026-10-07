from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import sentry_sdk
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from EdennCode.Deployment.alignment_workflows import AudioAlignmentResult
from EdennCode.Deployment.api_common import (
    ApiContext,
    ErrorDetail,
    JobStatus,
    LyricsTimestampModel,
    cleanup_temp_dir,
    edenn_error_to_http_exception,
    guess_audio_content_type,
    parse_lyrics_timestamps_json,
    resolve_media_source_to_disk,
    service_version,
)
from EdennCode.exceptions import EdennApiError, EdennError


class AlignmentSegmentResponse(BaseModel):
    rank: int
    music_start_s: float
    music_end_s: float
    score: float
    matched_audio_blob: Optional[str] = None
    matched_audio_url: Optional[str] = None
    aligned_lyrics: List[LyricsTimestampModel] = Field(default_factory=list)
    details: Dict[str, Any] = Field(default_factory=dict)


class VideoAudioAlignmentJobResponse(BaseModel):
    job_id: str
    status: str = Field(default=JobStatus.COMPLETED)
    version: str = Field(default_factory=service_version)
    video_metadata: Dict[str, Any]
    lyrics_provided: bool = False
    best_segment: AlignmentSegmentResponse
    segments: List[AlignmentSegmentResponse]
    job_received_timestamp: Optional[int] = None
    job_finished_timestamp: Optional[int] = None


def create_video_alignment_router(context: ApiContext) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/api/v1/jobs/video-align-audio",
        response_model=VideoAudioAlignmentJobResponse,
        summary="Align an existing audio source to a video and return the top matching segments.",
    )
    async def align_video_audio_job(
        *,
        video: UploadFile | None = File(None, description="Video file (mp4, mov, etc.)."),
        video_url: Optional[str] = Form(None),
        audio: UploadFile | None = File(None, description="Audio file (wav, mp3, etc.)."),
        audio_url: Optional[str] = Form(None),
        lyrics_timestamps_json: Optional[str] = Form(None),
        top_k: int = Form(3),
    ) -> VideoAudioAlignmentJobResponse:
        job_id = uuid4().hex
        job_dir = context.settings.workdir / job_id
        source_dir = job_dir / "source"
        received_timestamp = int(time.time())

        try:
            if top_k < 1 or top_k > 5:
                raise EdennApiError(
                    f"top_k must be between 1 and 5. Received: {top_k}",
                    public_message="top_k must be between 1 and 5.",
                    status_code=400,
                    component="api",
                    operation="align_video_audio_job",
                )

            local_video_path = await resolve_media_source_to_disk(
                upload=video,
                remote_url=video_url,
                destination_dir=source_dir,
                fallback_filename="input_video",
                asset_label="video",
            )
            local_audio_path = await resolve_media_source_to_disk(
                upload=audio,
                remote_url=audio_url,
                destination_dir=source_dir,
                fallback_filename="input_audio",
                asset_label="audio",
            )
            lyrics_timestamps = parse_lyrics_timestamps_json(lyrics_timestamps_json)

            result: AudioAlignmentResult = await context.alignment_workflow.run(
                video_path=local_video_path,
                audio_path=local_audio_path,
                lyrics_timestamps=lyrics_timestamps,
                top_k=top_k,
            )

            response_segments: List[AlignmentSegmentResponse] = []
            for segment in result.segments:
                matched_audio_blob = matched_audio_url = None
                if context.storage.enabled and segment.rendered_audio_path.exists():
                    matched_audio_blob = context.storage.upload_path(
                        container=context.settings.audio_container_name,
                        path=segment.rendered_audio_path,
                        blob_name=(
                            f"jobs/{job_id}/alignment/segments/"
                            f"rank_{segment.rank}/{segment.rendered_audio_path.name}"
                        ),
                        content_type=guess_audio_content_type(segment.rendered_audio_path),
                    )
                    if matched_audio_blob:
                        matched_audio_url = context.storage.generate_sas_url(
                            container=context.settings.audio_container_name,
                            blob_name=matched_audio_blob,
                        )

                response_segments.append(
                    AlignmentSegmentResponse(
                        rank=segment.rank,
                        music_start_s=segment.music_start_s,
                        music_end_s=segment.music_end_s,
                        score=segment.score,
                        matched_audio_blob=matched_audio_blob,
                        matched_audio_url=matched_audio_url,
                        aligned_lyrics=[
                            LyricsTimestampModel(
                                text=getattr(word, "text", ""),
                                startS=float(getattr(word, "startS", 0.0)),
                                endS=float(getattr(word, "endS", 0.0)),
                                i=getattr(word, "i", None),
                            )
                            for word in (segment.aligned_lyrics or [])
                        ],
                        details=dict(segment.details),
                    )
                )

            if not response_segments:
                raise EdennApiError(
                    "Alignment workflow returned no ranked segments.",
                    public_message="The audio could not be aligned to the video.",
                    status_code=500,
                    component="api",
                    operation="align_video_audio_job",
                )

            return VideoAudioAlignmentJobResponse(
                job_id=job_id,
                video_metadata=result.video_metadata.to_dict(),
                lyrics_provided=result.lyrics_provided,
                best_segment=response_segments[0],
                segments=response_segments,
                job_received_timestamp=received_timestamp,
                job_finished_timestamp=int(time.time()),
            )
        except HTTPException:
            raise
        except EdennError as exc:
            context.logger.exception(
                "Alignment job %s failed with typed Edenn error: %s",
                job_id,
                exc.to_dict(),
            )
            raise edenn_error_to_http_exception(exc) from exc
        except Exception as exc:
            context.logger.exception("Alignment job %s failed: %s", job_id, exc)
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


__all__ = [
    "AlignmentSegmentResponse",
    "VideoAudioAlignmentJobResponse",
    "create_video_alignment_router",
]
