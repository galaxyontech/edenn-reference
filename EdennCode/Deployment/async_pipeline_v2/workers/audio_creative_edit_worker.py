from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from EdennCode.Deployment.api_common import (
    download_public_file_to_disk,
    guess_audio_content_type,
    probe_audio_metrics,
)
from EdennCode.Deployment.api_video_generation import _provider_neutral_blob_name
from EdennCode.Deployment.error_codes import public_error_payload
from EdennCode.Deployment.async_pipeline_v2.models import (
    AsyncV2Artifact,
    AsyncV2Task,
    JobStatus,
    StageStatus,
)
from EdennCode.Deployment.async_pipeline_v2.queue.postgres_queue import PostgresTaskQueue
from EdennCode.Deployment.async_pipeline_v2.repositories import AsyncPipelineV2Repository
from EdennCode.Deployment.async_pipeline_v2.video_source_preparation import (
    VideoSourcePreparationService,
)
from EdennCode.Deployment.async_pipeline_v2.worker_heartbeat import StageLeaseHeartbeat
from EdennCode.Util.MediaUtils.ffmpeg_utils import overlay_music_on_video


logger = logging.getLogger(__name__)

STAGE_NAME = "audio_creative_edit"


class AudioCreativeEditWorker:
    """Worker that restyles an existing track (audio-to-audio) onto its video.

    Consumes the ``audio_creative_edit`` task enqueued by the agentic
    ``edit_audio(creative_edit)`` path: it downloads the parent track + resolves
    the source video, runs the (injectable) ``AudioCreativeEditOrchestrator`` to
    reinterpret the audio, then re-muxes the edited audio onto the source video.
    The job result mirrors the video_music contract (``audio_url``/``video_url``)
    so agentic candidate hydration is unchanged.
    """

    def __init__(
        self,
        *,
        repository: AsyncPipelineV2Repository,
        queue: PostgresTaskQueue,
        orchestrator: Any,
        settings: Any,
        storage: Any = None,
        worker_id: Optional[str] = None,
        queue_name: str = "audio-creative-edit-pipeline",
        lease_seconds: int = 900,
    ) -> None:
        self.repository = repository
        self.queue = queue
        self.orchestrator = orchestrator
        self.settings = settings
        self.storage = storage
        self.worker_id = worker_id or f"worker-creative-edit-{uuid4().hex}"
        self.queue_name = queue_name
        self.lease_seconds = lease_seconds
        self.source_preparation = VideoSourcePreparationService(
            repository=repository,
            settings=settings,
            storage=storage,
            stage_name=STAGE_NAME,
            diagnostic_logger=logger,
        )

    async def process_one(self) -> Optional[AsyncV2Task]:
        task = self.queue.lease(
            queue_name=self.queue_name,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if task is None:
            return None

        stage_run = None
        try:
            job = self.repository.get_job(task.job_id)
            if job is None:
                raise KeyError(f"Job not found for task {task.task_id}: {task.job_id}")

            request = job.request_json
            source_artifact_id = (
                task.payload_json.get("source_video_artifact_id")
                or request.get("source_video_artifact_id")
            )
            if not source_artifact_id:
                raise ValueError("audio_creative_edit task requires source_video_artifact_id.")
            source_audio_url = request.get("source_audio_url")
            if not source_audio_url:
                raise ValueError("audio_creative_edit task requires source_audio_url.")

            source_artifact = self.repository.get_artifact(str(source_artifact_id))
            if source_artifact is None:
                raise KeyError(f"Source video artifact not found: {source_artifact_id}")

            stage_run = self.repository.start_stage_run(
                job_id=task.job_id,
                task_id=task.task_id,
                stage_name=STAGE_NAME,
                attempt=task.attempt,
                input_json={
                    "task_type": task.task_type,
                    "source_video_artifact_id": source_artifact.artifact_id,
                    "request": request,
                },
            )
            self.repository.update_job_status(
                task.job_id,
                status=JobStatus.PROCESSING,
                current_stage=STAGE_NAME,
                progress_percent=20,
            )
            self.repository.add_event(
                job_id=task.job_id,
                event_type="stage.started",
                stage_name=STAGE_NAME,
                message="Audio creative edit started.",
                payload_json={"task_id": task.task_id, "attempt": task.attempt},
            )

            async with StageLeaseHeartbeat(
                queue=self.queue,
                repository=self.repository,
                task_id=task.task_id,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
                stage_run_id=stage_run.stage_run_id,
                diagnostic_logger=logger,
            ):
                result_json = await self._run(
                    job_id=task.job_id,
                    request=request,
                    source_artifact=source_artifact,
                    source_audio_url=str(source_audio_url),
                )

            self.repository.update_stage_run(
                stage_run.stage_run_id,
                status=StageStatus.COMPLETED,
                output_json={
                    "video_url": result_json.get("video_url"),
                    "audio_url": result_json.get("audio_url"),
                    "result_artifact_types": ["edited_audio", "remixed_video"],
                },
                finished=True,
            )
            _, completed_applied = self.repository.update_job_status_checked(
                task.job_id,
                status=JobStatus.COMPLETED,
                current_stage=STAGE_NAME,
                progress_percent=100,
                result_json=result_json,
                error_json=None,
                finished=True,
            )
            if completed_applied:
                self.repository.add_event(
                    job_id=task.job_id,
                    event_type="job.completed",
                    stage_name=STAGE_NAME,
                    message="Audio creative edit completed.",
                    payload_json={
                        "video_url": result_json.get("video_url"),
                        "audio_url": result_json.get("audio_url"),
                    },
                )
            return self.queue.complete(task_id=task.task_id, worker_id=self.worker_id)
        except Exception as exc:
            # Client-facing error: generic catalog text only — never the raw
            # exception string, which can name the upstream provider/model.
            # retryable comes from the error-code catalog: permanent failures
            # must not tell the user to retry, and must not be retried internally.
            error_json = public_error_payload(exc)
            retryable = bool(error_json.get("retryable", True))
            # Internal-only detail for the queue's retry/dead-letter bookkeeping
            # (never surfaced through the job status/events API).
            internal_error = {
                "message": str(exc),
                "type": type(exc).__name__,
                "retryable": retryable,
            }
            logger.exception("audio_creative_edit task %s failed: %s", task.task_id, exc)
            if stage_run is not None:
                self.repository.update_stage_run(
                    stage_run.stage_run_id,
                    status=StageStatus.FAILED,
                    error_json=error_json,
                    finished=True,
                )
            self.repository.add_event(
                job_id=task.job_id,
                event_type="stage.failed",
                stage_name=STAGE_NAME,
                message="Audio creative edit failed.",
                payload_json={"task_id": task.task_id, "error": error_json},
            )
            failed_task = self.queue.fail(
                task_id=task.task_id,
                worker_id=self.worker_id,
                error=internal_error,
                retry=retryable,
                backoff_seconds=30,
            )
            if failed_task.status in {"dead_lettered", "failed"}:
                self.repository.update_job_status(
                    task.job_id,
                    status=JobStatus.FAILED,
                    current_stage=STAGE_NAME,
                    error_json=error_json,
                    finished=True,
                )
                self.repository.add_event(
                    job_id=task.job_id,
                    event_type="job.failed",
                    stage_name=STAGE_NAME,
                    message="Audio creative edit failed permanently.",
                    payload_json={"task_id": task.task_id, "error": error_json},
                )
            else:
                self.repository.update_job_status(
                    task.job_id,
                    status=JobStatus.QUEUED,
                    current_stage=STAGE_NAME,
                    error_json=error_json,
                )
            return failed_task

    async def _run(
        self,
        *,
        job_id: str,
        request: dict[str, Any],
        source_artifact: AsyncV2Artifact,
        source_audio_url: str,
    ) -> dict[str, Any]:
        source_video_path = await self.source_preparation.resolve_source_video(source_artifact)
        workdir = self.source_preparation.workdir(job_id, "creative_edit")
        workdir.mkdir(parents=True, exist_ok=True)

        source_audio_path = workdir / "source_audio.audio"
        await download_public_file_to_disk(
            url=source_audio_url, destination=source_audio_path, asset_label="audio"
        )

        result = await self.orchestrator.run(
            source_audio_path=source_audio_path,
            user_prompt=str(request.get("user_prompt") or ""),
            modelspec=str(request.get("modelspec") or "edenn_enhanced"),
            video_path=Path(source_video_path),
        )

        edited_audio_path = Path(result.edited_audio_path)
        remixed_video_path = workdir / "remixed_video.mp4"
        overlay_music_on_video(
            Path(source_video_path),
            edited_audio_path,
            remixed_video_path,
            music_volume=float(request.get("music_volume", 1.0) or 1.0),
            preserve_original_audio=bool(request.get("preserve_original_audio", False)),
        )

        audio_blob, audio_url = self._record_output_artifact(
            job_id=job_id,
            artifact_type="edited_audio",
            role="primary",
            path=edited_audio_path,
            container=getattr(self.settings, "audio_container_name", "generated-audio"),
            folder="audio/creative_edit",
            label="edited_audio",
            content_type=guess_audio_content_type(edited_audio_path),
        )
        edited_audio_duration_s, edited_audio_size_bytes = probe_audio_metrics(
            edited_audio_path
        )
        secondary_audio_url = None
        secondary_edited_audio_duration_s = secondary_edited_audio_size_bytes = None
        secondary_path = getattr(result, "secondary_edited_audio_path", None)
        if secondary_path and Path(secondary_path).exists():
            secondary_edited_audio_duration_s, secondary_edited_audio_size_bytes = (
                probe_audio_metrics(Path(secondary_path))
            )
            _, secondary_audio_url = self._record_output_artifact(
                job_id=job_id,
                artifact_type="secondary_edited_audio",
                role="secondary",
                path=Path(secondary_path),
                container=getattr(self.settings, "audio_container_name", "generated-audio"),
                folder="audio/creative_edit/secondary",
                label="secondary_edited_audio",
                content_type=guess_audio_content_type(Path(secondary_path)),
            )
        video_blob, video_url = self._record_output_artifact(
            job_id=job_id,
            artifact_type="remixed_video",
            role="final",
            path=remixed_video_path,
            container=getattr(self.settings, "output_container", "generated-media"),
            folder="video/creative_edit",
            label="remixed_video",
            content_type="video/mp4",
        )

        return {
            "status": "completed",
            "audio_url": audio_url,
            "audio_blob": audio_blob,
            "edited_audio_duration_s": edited_audio_duration_s,
            "edited_audio_size_bytes": edited_audio_size_bytes,
            "secondary_edited_audio_url": secondary_audio_url,
            "secondary_edited_audio_duration_s": secondary_edited_audio_duration_s,
            "secondary_edited_audio_size_bytes": secondary_edited_audio_size_bytes,
            "video_url": video_url,
            "video_blob": video_blob,
            "modelspec": getattr(result, "used_music_model_spec", None)
            or str(request.get("modelspec") or ""),
            "include_vocals": bool(getattr(result, "include_vocals", False)),
            "vocal_gender": getattr(result, "vocal_gender", None),
            "creative_edit_prompt": getattr(result, "creative_edit_prompt", None),
            "agentic_edit_kind": request.get("agentic_edit_kind"),
            "agentic_parent_candidate_id": request.get("agentic_parent_candidate_id"),
        }

    def _record_output_artifact(
        self,
        *,
        job_id: str,
        artifact_type: str,
        role: str,
        path: Path,
        container: str,
        folder: str,
        label: str,
        content_type: str,
    ) -> tuple[Optional[str], Optional[str]]:
        artifact_id = f"{job_id}:{artifact_type}:{role}"
        existing = self.repository.get_artifact(artifact_id)
        if existing is not None:
            return existing.blob_name, existing.url

        upload_blob = None
        upload_url = None
        if self.storage is not None and getattr(self.storage, "enabled", False):
            upload_blob = self.storage.upload_path(
                container=container,
                path=path,
                blob_name=_provider_neutral_blob_name(
                    job_id=job_id,
                    folder=folder,
                    label=label,
                    source_path=path,
                ),
                content_type=content_type,
            )
            if upload_blob and hasattr(self.storage, "generate_sas_url"):
                upload_url = self.storage.generate_sas_url(
                    container=container,
                    blob_name=upload_blob,
                )

        self.repository.add_artifact(
            artifact_id=artifact_id,
            job_id=job_id,
            artifact_type=artifact_type,
            role=role,
            container=container if upload_blob else None,
            blob_name=upload_blob,
            url=upload_url,
            content_type=content_type,
            local_path=str(path),
            metadata_json={
                "source_filename": path.name,
                "size_bytes": path.stat().st_size if path.exists() else None,
                "uploaded": bool(upload_blob),
            },
        )
        self.repository.add_event(
            job_id=job_id,
            event_type="artifact.created",
            stage_name=STAGE_NAME,
            message=f"{artifact_type} artifact recorded.",
            payload_json={
                "artifact_id": artifact_id,
                "artifact_type": artifact_type,
                "role": role,
                "blob_name": upload_blob,
                "url": upload_url,
            },
        )
        return upload_blob, upload_url


async def run_creative_edit_worker_loop(
    *,
    worker: AudioCreativeEditWorker,
    once: bool = False,
    poll_interval_seconds: float = 2.0,
) -> None:
    while True:
        task = await worker.process_one()
        if once:
            return
        if task is None:
            await asyncio.sleep(poll_interval_seconds)


__all__ = ["AudioCreativeEditWorker", "run_creative_edit_worker_loop"]
