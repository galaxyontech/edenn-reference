"""Worker that renders an approved sound-effects plan onto its video.

This is the third time the same gap has been closed, and it was the last one
open. Narration was first: the agentic tool enqueued ``voiceover`` tasks and no
deployed process consumed them, so they sat in the queue forever while the API
reported them queued. Restyling was second, and went unnoticed longer because a
worker class existed — just no role that ran it. Sound effects had neither.

Nothing about the code looks wrong when this happens. The tool is right, the
queue is right, the workflow is right; the defect lives in the space between
them, and the standalone deployment hides it completely by completing jobs
in-process.

The rendering decisions are NOT here. They live in
``AgenticAudio.tools.sfx_render``, which the local design server calls too,
because two copies of "did the user's plan choose these moments or did the
engine" is how the plan card quietly becomes a suggestion box on one deployment
and not the other.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from EdennCode.Deployment.api_common import download_public_file_to_disk
from EdennCode.Deployment.api_video_generation import _provider_neutral_blob_name
from EdennCode.Deployment.async_pipeline_v2.artifact_service import (
    upload_path_with_retry,
)
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
from EdennCode.Deployment.error_codes import public_error_payload


logger = logging.getLogger(__name__)

STAGE_NAME = "video_sfx"


class VideoSfxWorker:
    """Consumes the ``video_sfx`` task the agentic ``generate_sfx`` tool enqueues.

    The task is enqueued with ``max_attempts=1`` and that is deliberate: every
    effect in a bed is its own paid generation, so a queue-level retry of a run
    that failed halfway pays for the effects that already succeeded a second
    time. A failure here ends the job rather than re-entering it.
    """

    def __init__(
        self,
        *,
        repository: AsyncPipelineV2Repository,
        queue: PostgresTaskQueue,
        settings: Any,
        storage: Any = None,
        worker_id: Optional[str] = None,
        queue_name: str = "sfx-pipeline",
        lease_seconds: int = 900,
    ) -> None:
        self.repository = repository
        self.queue = queue
        self.settings = settings
        self.storage = storage
        self.worker_id = worker_id or f"worker-sfx-{uuid4().hex}"
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

            request = job.request_json or {}
            source_artifact_id = (
                task.payload_json.get("source_video_artifact_id")
                or request.get("source_video_artifact_id")
            )
            if not source_artifact_id:
                raise ValueError("video_sfx task requires source_video_artifact_id.")
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
                    "planned_events": len(request.get("events") or []),
                    "spotting": request.get("sfx_spotting"),
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
                message="Sound-effect render started.",
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
                )

            self.repository.update_stage_run(
                stage_run.stage_run_id,
                status=StageStatus.COMPLETED,
                output_json={
                    "video_url": result_json.get("video_url"),
                    "audio_url": result_json.get("audio_url"),
                    "result_artifact_types": ["sfx_audio", "sfx_video"],
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
                    message="Sound-effect render completed.",
                    payload_json={
                        "video_url": result_json.get("video_url"),
                        "audio_url": result_json.get("audio_url"),
                        "events": len(result_json.get("rendered_events") or []),
                    },
                )
            return self.queue.complete(task_id=task.task_id, worker_id=self.worker_id)
        except Exception as exc:
            # Client-facing error: generic catalog text only — never the raw
            # exception string, which can name the upstream provider/model.
            error_json = public_error_payload(exc)
            retryable = bool(error_json.get("retryable", True))
            internal_error = {
                "message": str(exc),
                "type": type(exc).__name__,
                "retryable": retryable,
            }
            logger.exception("video_sfx task %s failed: %s", task.task_id, exc)
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
                message="Sound-effect render failed.",
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
                    message="Sound-effect render failed permanently.",
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

    def _vendor_read_url(self, artifact: AsyncV2Artifact) -> str:
        """A URL the video-conditioned engine can fetch the clip from.

        Minted FRESH from the artifact's blob coordinates, never read off the
        row: the stored URL is a signed link stamped when the artifact was
        created, and a render can run long after that signature expired — an
        expired handoff quietly downgrades the engine to its text route with
        nothing failing. A signed, expiring, read-only link is also exactly
        the right thing to hand a third party, and the reason this never falls
        back to anything we host: our own tokens must never ride in a URL
        given to a vendor. A stored URL is trusted only when it carries no
        signature at all (a plain public link has nothing to expire). Absent
        both, the text route carries the whole plan — a quality decision, not
        an outage.
        """
        container = getattr(artifact, "container", None)
        blob_name = getattr(artifact, "blob_name", None)
        if (
            container
            and blob_name
            and self.storage is not None
            and getattr(self.storage, "enabled", False)
            and hasattr(self.storage, "generate_sas_url")
        ):
            try:
                fresh = self.storage.generate_sas_url(
                    container=container, blob_name=blob_name
                )
                if fresh:
                    return str(fresh)
            except Exception:  # noqa: BLE001 — degrade to the text route
                logger.warning("could not mint a fresh source read URL", exc_info=True)
        url = str(getattr(artifact, "url", "") or "")
        if url.startswith(("http://", "https://")) and "?" not in url:
            return url
        return ""

    async def _resolve_reuse_audio(
        self, refs: Any, *, workdir: Path
    ) -> dict[str, str]:
        """Turn kept-effect references back into files on THIS container.

        A kept effect arrives as the URL its render was published under —
        which is the only form that means anything across a fleet, since a
        filesystem path belongs to whichever replica wrote it. URLs are
        downloaded next to the run; a local path from an older session passes
        through when it happens to exist here. Anything unresolvable is left
        for the shared render's existence check, which counts it out loud —
        a redo that quietly costs the whole bed again is the defect this
        entire path exists to prevent.
        """
        resolved: dict[str, str] = {}
        for event_id, ref in (refs or {}).items():
            text = str(ref or "")
            if not text:
                continue
            if text.startswith(("http://", "https://")):
                destination = workdir / "reuse" / f"{event_id}.audio"
                try:
                    await download_public_file_to_disk(
                        url=text, destination=destination, asset_label="audio"
                    )
                    resolved[str(event_id)] = str(destination)
                except Exception:  # noqa: BLE001 — regenerate rather than fail
                    logger.warning(
                        "kept effect %s could not be fetched; it will be "
                        "generated again", event_id,
                    )
            else:
                resolved[str(event_id)] = text
        return resolved

    async def _run(
        self,
        *,
        job_id: str,
        request: dict[str, Any],
        source_artifact: AsyncV2Artifact,
    ) -> dict[str, Any]:
        from EdennCode.EdennAgent.AgenticAudio.tools.sfx_render import (
            published_manifest,
            render_sfx,
        )

        source_video_path = await self.source_preparation.resolve_source_video(
            source_artifact
        )
        workdir = self.source_preparation.workdir(job_id, "sfx")
        workdir.mkdir(parents=True, exist_ok=True)

        reuse_refs = request.get("reuse_event_audio") or {}
        out = await render_sfx(
            video_path=Path(source_video_path),
            source_public_url=self._vendor_read_url(source_artifact),
            events=request.get("events") or [],
            summary=str(request.get("sfx_summary") or ""),
            ambience=str(request.get("sfx_ambience") or ""),
            spotting=request.get("sfx_spotting"),
            # Same choice the standalone honours: watch the footage, or write
            # the effects from the plan's prompts.
            route=str(request.get("agentic_sfx_route") or "auto"),
            reuse_event_audio=await self._resolve_reuse_audio(
                reuse_refs, workdir=workdir
            ),
            run_dir=workdir / "run",
            log=logger.info,
        )

        # Per-event audio goes to durable storage and the result carries the
        # URL, never the path: a path is meaningless to the next replica and
        # serving it to a client is a server-path leak. A kept effect keeps
        # the URL it was FIRST published under — re-uploading it would mint a
        # fresh URL that makes a reused sound indistinguishable from a
        # regenerated one.
        reused_urls = {
            str(event_id): str(ref)
            for event_id, ref in reuse_refs.items()
            if str(event_id) in out.reused_event_ids
            and str(ref or "").startswith(("http://", "https://"))
        }

        def _publish_event_audio(event_id: str, path: Path) -> Optional[str]:
            if not (self.storage is not None and getattr(self.storage, "enabled", False)):
                return None
            blob = upload_path_with_retry(
                self.storage,
                container=getattr(self.settings, "audio_container_name", "generated-audio"),
                path=path,
                blob_name=_provider_neutral_blob_name(
                    job_id=job_id,
                    folder="audio/sfx/events",
                    label=f"sfx_event_{event_id}",
                    source_path=path,
                ),
                content_type="audio/wav",
            )
            if blob and hasattr(self.storage, "generate_sas_url"):
                return self.storage.generate_sas_url(
                    container=getattr(self.settings, "audio_container_name", "generated-audio"),
                    blob_name=blob,
                )
            return None

        rendered_events = published_manifest(
            out.rendered_events,
            publish=_publish_event_audio,
            known_urls=reused_urls,
        )

        audio_blob, audio_url = self._record_output_artifact(
            job_id=job_id,
            artifact_type="sfx_audio",
            role="primary",
            path=out.mixed_audio_path,
            container=getattr(self.settings, "audio_container_name", "generated-audio"),
            folder="audio/sfx",
            label="sfx_audio",
            content_type="audio/wav",
        )
        video_blob, video_url = self._record_output_artifact(
            job_id=job_id,
            artifact_type="sfx_video",
            role="final",
            path=out.final_video_path,
            container=getattr(self.settings, "output_container", "generated-media"),
            folder="video/sfx",
            label="sfx_video",
            content_type="video/mp4",
        )

        return {
            "status": "completed",
            # Same contract the standalone returns, because the session hydrates
            # both the same way. `complete_audio_url` is the bed on its own.
            "audio_url": audio_url or video_url,
            "complete_audio_url": audio_url or video_url,
            "audio_blob": audio_blob,
            "video_url": video_url,
            "video_blob": video_blob,
            "agentic_sfx_id": request.get("agentic_sfx_id"),
            "spotting": out.spotting,
            "rendered_events": rendered_events,
            "reused_event_ids": list(out.reused_event_ids),
            # Measured at render, where the files are local. The read path
            # judges from these numbers and probes nothing.
            "take_signals": dict(out.take_signals or {}),
            # Did the engine watch the footage, or read a description of
            # it? The session is entitled to know which it paid for.
            "watched_the_video": bool(out.watched_the_video),
            "not_watched_reason": str(out.not_watched_reason or ""),
            "placeholder": False,
        }

    def _record_output_artifact(
        self,
        *,
        job_id: str,
        artifact_type: str,
        role: str,
        path: Optional[Path],
        container: str,
        folder: str,
        label: str,
        content_type: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """Record one output, uploading it when storage is configured.

        Returns ``(None, None)`` for an output this run did not produce — a
        planned render with no ambience bed makes a video and no standalone
        audio track, and inventing an artifact row for a file that is not there
        is how a URL that 404s reaches a player.
        """
        if path is None or not Path(path).exists():
            return None, None

        artifact_id = f"{job_id}:{artifact_type}:{role}"
        existing = self.repository.get_artifact(artifact_id)
        if existing is not None:
            return existing.blob_name, existing.url

        upload_blob = None
        upload_url = None
        if self.storage is not None and getattr(self.storage, "enabled", False):
            upload_blob = upload_path_with_retry(
                self.storage,
                container=container,
                path=Path(path),
                blob_name=_provider_neutral_blob_name(
                    job_id=job_id,
                    folder=folder,
                    label=label,
                    source_path=Path(path),
                ),
                content_type=content_type,
            )
            if upload_blob and hasattr(self.storage, "generate_sas_url"):
                upload_url = self.storage.generate_sas_url(
                    container=container, blob_name=upload_blob
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
                "source_filename": Path(path).name,
                "size_bytes": Path(path).stat().st_size,
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


async def run_video_sfx_worker_loop(
    *,
    worker: VideoSfxWorker,
    once: bool = False,
    poll_interval_seconds: float = 2.0,
) -> None:
    while True:
        task = await worker.process_one()
        if once:
            return
        if task is None:
            await asyncio.sleep(poll_interval_seconds)


__all__ = ["VideoSfxWorker", "run_video_sfx_worker_loop"]
