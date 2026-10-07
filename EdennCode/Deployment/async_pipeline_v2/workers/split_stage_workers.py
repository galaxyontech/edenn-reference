from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from EdennCode.Deployment.api_common import (
    download_public_file_to_disk,
    guess_audio_content_type,
)
from EdennCode.Deployment.api_video_generation import _provider_neutral_blob_name
from EdennCode.Deployment.auth.usage_recorder import record_v2_job_usage
from EdennCode.Deployment.error_codes import public_error_payload
from EdennCode.Deployment.async_pipeline_v2.result_paths import (
    result_audio_url,
    result_video_url,
)
from EdennCode.Deployment.async_pipeline_v2.models import (
    AsyncV2Artifact,
    AsyncV2Job,
    AsyncV2Task,
    JobStatus,
    StageStatus,
    TaskEnvelope,
    TaskStatus,
    utc_now,
)
from EdennCode.Deployment.async_pipeline_v2.queue_names import namespaced_queue_name
from EdennCode.Deployment.async_pipeline_v2.queue.postgres_queue import PostgresTaskQueue
from EdennCode.Deployment.async_pipeline_v2.repositories import AsyncPipelineV2Repository
from EdennCode.Deployment.async_pipeline_v2.stages.video_music_split import (
    AnalysisAndPlanningStageInput,
    ProviderCandidateGenerationStageInput,
    SelectionRankingRemixFinalizeStageInput,
    VideoPreprocessStageInput,
    VideoMusicAnalysisAndPlanningStage,
    VideoMusicPreprocessStage,
    VideoMusicProviderCandidateGenerationStage,
    VideoMusicSelectionRankingRemixFinalizeStage,
    VideoMusicSplitStageRuntime,
    analysis_output_from_json,
    analysis_output_to_json,
    candidate_output_from_json,
    candidate_output_to_json,
    preprocess_output_from_json,
    preprocess_output_to_json,
)
from EdennCode.Deployment.async_pipeline_v2.artifact_service import sha256_file
from EdennCode.Deployment.async_pipeline_v2.cache_service import (
    KV_COMPRESS,
    compression_cache_key,
)
from EdennCode.Deployment.async_pipeline_v2.input_guardrails import (
    MAX_SOURCE_VIDEO_BYTES,
    validate_source_video_file,
)
from EdennCode.Deployment.async_pipeline_v2.video_source_preparation import (
    VideoSourcePreparationService,
    coerce_request_bool,
)
from EdennCode.Deployment.async_pipeline_v2.worker_heartbeat import StageLeaseHeartbeat
from EdennCode.Deployment.async_pipeline_v2.callback_delivery import deliver_job_callback
from EdennCode.Deployment.async_pipeline_v2.workers.loop_guard import run_consumer_loop
from EdennCode.Deployment.async_pipeline_v2.workers.monolith_worker import (
    VideoMusicMonolithWorker,
)


logger = logging.getLogger(__name__)


def _cache_layer_enabled(env_name: str) -> bool:
    """Whether a specific cache layer is enabled, independent of the others.

    The shared cache object is constructed when any layer is on, so each integration
    point checks its own flag to decide whether to actually use it.
    """

    return (os.getenv(env_name) or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def provider_queue_name_for_modelspec(modelspec: str) -> str:
    normalized = (modelspec or "").strip().lower()
    if normalized == "edenn_studio":
        return "music-studio"
    if normalized == "edenn_enhanced":
        return "music-enhanced"
    return "music-basic"


def provider_stage_max_attempts(requested: int, modelspec: str) -> int:
    """Attempt budget for the provider_candidate_generation task.

    Studio/enhanced record the provider task id (with its creating key and
    base) durably before polling and a retry resumes the PRIMARY generation
    poll-only, so the big-ticket submission is never re-paid — without the
    second attempt the recorded task id is unrecoverable and a
    slow-but-successful generation is thrown away (a prod generation once
    completed 8.5s after the single attempt gave up). The basic tier has no
    resume support, so it keeps the requested count: a retry there would
    re-submit and pay again.

    Known residual spend on the retry (bounded, small relative to a lost
    primary): extension tasks and enhanced vocal clones are re-submitted, and
    a failure in the accept->record gap leaves no artifact to resume.
    """
    normalized = (modelspec or "").strip().lower()
    if normalized in {"edenn_studio", "edenn_enhanced"}:
        return max(2, requested)
    return requested


@dataclass(frozen=True)
class StageTaskResult:
    output_json: dict[str, Any]
    next_task: Optional[TaskEnvelope] = None
    job_status: str = JobStatus.QUEUED
    progress_percent: float = 0.0
    result_json: Optional[dict[str, Any]] = None
    finished: bool = False


def _json_blob_name(*, job_id: str, artifact_type: str, role: str) -> str:
    safe_type = artifact_type.replace(":", "_").replace("/", "_")
    safe_role = role.replace(":", "_").replace("/", "_")
    return f"jobs/{job_id}/stage_artifacts/{safe_type}_{safe_role}.json"


def _provider_task_artifact_id(*, job_id: str, role: str) -> str:
    safe_role = role.replace(":", "_").replace("/", "_")
    return f"{job_id}:provider_task:{safe_role}"


class _SplitStageWorkerBase:
    """Base class for one durable split-stage queue worker.

    Each subclass owns exactly one stage-level task type and queue. The base
    class handles leasing, status/event bookkeeping, retry transitions, and
    idempotent next-task enqueueing. Stage subclasses should keep `_run_stage`
    focused on their business boundary so queue mechanics do not leak into
    workflow logic.
    """

    stage_name: str
    task_type: str
    queue_name: str
    retry_backoff_seconds: int = 30
    # Number of tasks one replica processes concurrently. Provider stages are
    # I/O-bound on async provider polling, so a single replica can hold many
    # in-flight generations at once; raising this collapses provider-queue wait
    # under bursts. CPU-bound media stages keep this at 1 because asyncio gives
    # them no real parallelism and concurrent ffmpeg/cv2 work would only contend.
    max_concurrency: int = 1

    def __init__(
        self,
        *,
        repository: AsyncPipelineV2Repository,
        queue: PostgresTaskQueue,
        runtime: VideoMusicSplitStageRuntime,
        settings: Any,
        storage: Any = None,
        worker_id: Optional[str] = None,
        lease_seconds: int = 900,
        queue_name: Optional[str] = None,
        retry_backoff_seconds: Optional[int] = None,
        cache: Any = None,
    ) -> None:
        self.repository = repository
        self.queue = queue
        self.runtime = runtime
        self.settings = settings
        self.storage = storage
        # Optional content-addressed cache (CacheService). When present, stages may
        # reuse cross-job results (e.g. compressed source video) instead of redoing
        # CPU-heavy work. None keeps behavior identical to the uncached path.
        self.cache = cache
        self.base_queue_name = queue_name or self.queue_name
        self.queue_name = namespaced_queue_name(
            self.base_queue_name, settings=settings)
        if retry_backoff_seconds is not None:
            self.retry_backoff_seconds = retry_backoff_seconds
        self.worker_id = worker_id or f"{self.stage_name}-{uuid4().hex}"
        self.lease_seconds = lease_seconds
        self.source_preparation = VideoSourcePreparationService(
            repository=repository,
            settings=settings,
            storage=storage,
            stage_name=self.stage_name,
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
        job = None  # bound before the try so the failure path can read it safely
        try:
            if task.task_type != self.task_type:
                raise ValueError(
                    f"{self.stage_name} worker cannot process task_type={task.task_type}."
                )
            job = self.repository.get_job(task.job_id)
            if job is None:
                raise KeyError(
                    f"Job not found for task {task.task_id}: {task.job_id}")
            if job.status == JobStatus.CANCELED:
                self.repository.add_event(
                    job_id=task.job_id,
                    event_type="task.skipped",
                    stage_name=self.stage_name,
                    message="Task skipped because the job was already canceled.",
                    payload_json={"task_id": task.task_id},
                )
                self.queue.cancel_job_tasks(job_id=task.job_id)
                return self.queue.get_task(task.task_id)

            stage_run = self.repository.start_stage_run(
                job_id=task.job_id,
                task_id=task.task_id,
                stage_name=self.stage_name,
                attempt=task.attempt,
                input_json={
                    "task_type": task.task_type,
                    "payload": task.payload_json,
                    "request": job.request_json,
                },
            )
            self.repository.update_job_status(
                task.job_id,
                status=JobStatus.PROCESSING,
                current_stage=self.stage_name,
                progress_percent=max(1.0, float(job.progress_percent or 0.0)),
            )
            self.repository.add_event(
                job_id=task.job_id,
                event_type="stage.started",
                stage_name=self.stage_name,
                message=f"{self.stage_name} started.",
                payload_json={"task_id": task.task_id,
                              "attempt": task.attempt},
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
                stage_started = time.monotonic()
                result = await self._run_stage(job=job, task=task)
                stage_ms = (time.monotonic() - stage_started) * 1000.0
            committed, status_applied = self._commit_stage_success(
                task=task,
                stage_run_id=stage_run.stage_run_id,
                result=result,
            )
            self._emit_stage_metrics(
                job=job,
                task=task,
                stage_ms=stage_ms,
                finished=result.finished,
            )
            # Best-effort client webhook only when this stage finishes the whole
            # job (the finalize stage), AFTER the terminal state is committed —
            # and only when this worker's terminal write actually landed.
            if result.finished and status_applied:
                record_v2_job_usage(
                    job, status="completed", endpoint="/api/v2/jobs/video-music",
                    result_json=result.result_json,
                )
                await deliver_job_callback(
                    job, status=JobStatus.COMPLETED,
                    result_json=result.result_json, error_json=None, logger=logger,
                )
            return committed
        except Exception as exc:
            # Client-facing error: generic catalog text only — never the raw
            # exception string, which can name the upstream provider/model.
            # retryable comes from the error-code catalog: permanent failures
            # (content policy, bad input) must not tell the user to retry, and
            # must not be retried internally — every attempt bills the provider.
            error_json = public_error_payload(exc)
            retryable = bool(error_json.get("retryable", True))
            # Internal-only detail for the queue's retry/dead-letter bookkeeping
            # (never surfaced through the job status/events API).
            internal_error = {
                "message": str(exc),
                "type": type(exc).__name__,
                "retryable": retryable,
            }
            logger.exception("%s task %s failed: %s",
                             self.stage_name, task.task_id, exc)
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
                stage_name=self.stage_name,
                message=f"{self.stage_name} failed.",
                payload_json={"task_id": task.task_id, "error": error_json},
            )
            failed_task = self.queue.fail(
                task_id=task.task_id,
                worker_id=self.worker_id,
                error=internal_error,
                retry=retryable,
                backoff_seconds=self.retry_backoff_seconds,
            )
            if failed_task.status in {TaskStatus.DEAD_LETTERED, TaskStatus.FAILED}:
                _, failed_applied = self.repository.update_job_status_checked(
                    task.job_id,
                    status=JobStatus.FAILED,
                    current_stage=self.stage_name,
                    error_json=error_json,
                    finished=True,
                )
                if failed_applied:
                    self.repository.add_event(
                        job_id=task.job_id,
                        event_type="job.failed",
                        stage_name=self.stage_name,
                        message="Split-stage video music workflow failed permanently.",
                        payload_json={"task_id": task.task_id,
                                      "error": error_json},
                    )
                    # Best-effort client webhook only on PERMANENT failure — never
                    # on the retryable re-queue below, and never when the job was
                    # already terminal. error_json is already sanitized.
                    record_v2_job_usage(
                        job, status="failed", endpoint="/api/v2/jobs/video-music",
                    )
                    await deliver_job_callback(
                        job, status=JobStatus.FAILED,
                        result_json=None, error_json=error_json, logger=logger,
                    )
            else:
                self.repository.update_job_status(
                    task.job_id,
                    status=JobStatus.QUEUED,
                    current_stage=self.stage_name,
                    error_json=error_json,
                )
            return failed_task

    async def _run_stage(self, *, job: AsyncV2Job, task: AsyncV2Task) -> StageTaskResult:
        raise NotImplementedError

    def _commit_stage_success(
        self,
        *,
        task: AsyncV2Task,
        stage_run_id: str,
        result: StageTaskResult,
    ) -> tuple[AsyncV2Task, bool]:
        """Atomically publish successful split-stage state.

        The stage itself may do long-running work and create durable artifacts
        before this point. The handoff below must be all-or-nothing: enqueueing
        the next deterministic task, marking the current stage complete, moving
        the job forward, adding user-visible events, and completing the leased
        task are one transaction. If this commit fails, the next task is not
        left visible while the current task is retryable.

        Returns ``(completed_task, status_applied)``; ``status_applied`` is
        False when the job was already terminal and the status write no-op'd.
        """
        self.repository.ensure_schema()
        self.queue.ensure_schema()
        with self.repository.transaction() as client:
            if result.next_task is not None:
                self.queue.enqueue(result.next_task, client=client)
                self.repository.add_event(
                    job_id=task.job_id,
                    event_type="task.enqueued",
                    stage_name=self.stage_name,
                    message=(
                        "Next split-stage task enqueued. Task IDs are deterministic "
                        "per job/stage so retrying this handoff remains idempotent."
                    ),
                    payload_json={
                        "task_id": result.next_task.task_id,
                        "queue_name": result.next_task.queue_name,
                        "task_type": result.next_task.task_type,
                    },
                    client=client,
                )

            self.repository.update_stage_run(
                stage_run_id,
                status=StageStatus.COMPLETED,
                output_json=result.output_json,
                finished=True,
                client=client,
            )
            _, status_applied = self.repository.update_job_status_checked(
                task.job_id,
                status=result.job_status,
                current_stage=self.stage_name,
                progress_percent=result.progress_percent,
                result_json=result.result_json,
                error_json=None,
                finished=result.finished,
                client=client,
            )
            if status_applied:
                self.repository.add_event(
                    job_id=task.job_id,
                    event_type="stage.completed",
                    stage_name=self.stage_name,
                    message=f"{self.stage_name} completed.",
                    payload_json=result.output_json,
                    client=client,
                )
                if result.finished:
                    self.repository.add_event(
                        job_id=task.job_id,
                        event_type="job.completed",
                        stage_name=self.stage_name,
                        message="Split-stage video music workflow completed.",
                        payload_json={
                            "video_url": result_video_url(result.result_json),
                            "audio_url": result_audio_url(result.result_json),
                        },
                        client=client,
                    )
            else:
                logger.warning(
                    "job %s already terminal; %s status write skipped",
                    task.job_id, self.stage_name,
                )

            completed = self.queue.complete(
                task_id=task.task_id,
                worker_id=self.worker_id,
                client=client,
            )
            return completed, status_applied

    def _release_cache_lease(self, key: str, lease: str) -> None:
        """Best-effort release of a single-flight cache lease we no longer need."""

        if self.cache is None:
            return
        try:
            self.cache.release_lease(key, lease)
        except Exception:
            logger.debug("Failed to release cache lease %s.", key, exc_info=True)

    @staticmethod
    def _queue_wait_ms(task: AsyncV2Task) -> Optional[float]:
        """Milliseconds a task waited in queue after becoming eligible to lease.

        Computed entirely from DB timestamps on the leased row, so it is immune
        to worker/DB clock skew: `updated_at` is set to `now()` at lease time and
        `not_before` is when the task became eligible (enqueue time on the first
        attempt, or the backoff deadline on a retry). This is the split-mode
        regression signal (`g→prov` in the latency breakdown).
        """

        leased_at = task.updated_at
        eligible_at = task.not_before or task.created_at
        if not isinstance(leased_at, datetime) or not isinstance(eligible_at, datetime):
            return None
        return max(0.0, round((leased_at - eligible_at).total_seconds() * 1000.0, 1))

    def _emit_stage_metrics(
        self,
        *,
        job: AsyncV2Job,
        task: AsyncV2Task,
        stage_ms: float,
        finished: bool,
    ) -> None:
        """Record per-stage timing as a queryable `stage.metrics` event.

        Best-effort: telemetry must never fail a stage that already committed,
        so any error here is swallowed. Emits queue wait and stage work time for
        every stage, plus end-to-end `job_total_ms` on the terminal stage, so the
        latency breakdown can be read straight from the events table instead of a
        one-off stage-run join.
        """

        try:
            payload: dict[str, Any] = {
                "task_id": task.task_id,
                "task_type": task.task_type,
                "stage_name": self.stage_name,
                "attempt": task.attempt,
                "stage_ms": round(stage_ms, 1),
                "queue_wait_ms": self._queue_wait_ms(task),
            }
            if finished and isinstance(job.created_at, datetime):
                payload["job_total_ms"] = round(
                    (utc_now() - job.created_at).total_seconds() * 1000.0, 1
                )
            self.repository.add_event(
                job_id=task.job_id,
                event_type="stage.metrics",
                stage_name=self.stage_name,
                message=f"{self.stage_name} timing.",
                payload_json=payload,
            )
        except Exception:
            logger.debug(
                "Failed to emit stage.metrics for task %s.", task.task_id, exc_info=True
            )

    def _workdir(self, job_id: str, *parts: str) -> Path:
        return Path(getattr(self.settings, "workdir", tempfile.gettempdir())) / "async_pipeline_v2" / job_id / Path(*parts)

    def _queue_name(self, queue_name: str) -> str:
        return namespaced_queue_name(queue_name, settings=self.settings)

    async def _resolve_source_video(self, artifact: AsyncV2Artifact) -> Path:
        # Bound the download by the input-size guardrail so an oversized
        # video_url source aborts with 10005 instead of filling worker disk.
        return await self.source_preparation.resolve_source_video(
            artifact, max_bytes=MAX_SOURCE_VIDEO_BYTES
        )

    async def _resolve_file_artifact(self, artifact: AsyncV2Artifact, *, label: str) -> Path:
        local_path = Path(artifact.local_path).expanduser(
        ) if artifact.local_path else None
        if local_path and local_path.exists():
            return local_path.resolve()
        if artifact.url:
            # Restore the producer's filename, not the neutral blob name: the
            # tail-chop's already-trimmed check is a filename marker, and the
            # blob name strips it — a cross-replica re-download under the blob
            # name made finalize chop 6s of real music off an already-chopped
            # track.
            metadata = artifact.metadata_json or {}
            source_filename = Path(str(metadata.get("source_filename") or "")).name
            destination = self._workdir(
                artifact.job_id,
                "resolved_artifacts",
                source_filename
                or Path(artifact.blob_name or artifact.artifact_id).name,
            )
            return await download_public_file_to_disk(
                url=artifact.url,
                destination=destination,
                asset_label=label,
            )
        raise FileNotFoundError(
            f"Artifact {artifact.artifact_id} has no readable local_path or url."
        )

    def _record_json_artifact(
        self,
        *,
        job_id: str,
        artifact_id: str,
        artifact_type: str,
        role: str,
        payload: dict[str, Any],
    ) -> AsyncV2Artifact:
        existing = self.repository.get_artifact(artifact_id)
        if existing is not None:
            return existing

        path = self._workdir(job_id, "stage_artifacts",
                             f"{artifact_type}_{role}.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False,
                        indent=2), encoding="utf-8")

        container = None
        upload_blob = None
        upload_url = None
        if self.storage is not None and getattr(self.storage, "enabled", False):
            container = getattr(self.settings, "output_container", None) or getattr(
                self.settings, "upload_container", "user-uploads"
            )
            upload_blob = self.storage.upload_path(
                container=container,
                path=path,
                blob_name=_json_blob_name(
                    job_id=job_id,
                    artifact_type=artifact_type,
                    role=role,
                ),
                content_type="application/json",
            )
            if upload_blob and hasattr(self.storage, "generate_sas_url"):
                upload_url = self.storage.generate_sas_url(
                    container=container,
                    blob_name=upload_blob,
                )

        artifact = self.repository.add_artifact(
            artifact_id=artifact_id,
            job_id=job_id,
            artifact_type=artifact_type,
            role=role,
            container=container if upload_blob else None,
            blob_name=upload_blob,
            url=upload_url,
            content_type="application/json",
            local_path=str(path),
            metadata_json={
                "size_bytes": path.stat().st_size,
                "uploaded": bool(upload_blob),
                "payload_stored_in_db": True,
            },
            payload_json=payload,
        )
        self.repository.add_event(
            job_id=job_id,
            event_type="artifact.created",
            stage_name=self.stage_name,
            message=f"{artifact_type} artifact recorded.",
            payload_json={"artifact_id": artifact.artifact_id, "role": role},
        )
        return artifact

    def _load_json_artifact(self, artifact: AsyncV2Artifact) -> dict[str, Any]:
        local_path = Path(artifact.local_path).expanduser(
        ) if artifact.local_path else None
        if local_path and local_path.exists():
            return json.loads(local_path.read_text(encoding="utf-8"))
        if artifact.payload_json is not None:
            return dict(artifact.payload_json)
        legacy_payload = artifact.metadata_json.get("payload_json")
        if isinstance(legacy_payload, dict):
            return dict(legacy_payload)
        raise FileNotFoundError(
            f"JSON artifact {artifact.artifact_id} has no local file, DB payload, or URL."
        )

    async def _load_json_artifact_async(self, artifact: AsyncV2Artifact) -> dict[str, Any]:
        local_path = Path(artifact.local_path).expanduser(
        ) if artifact.local_path else None
        if local_path and local_path.exists():
            return json.loads(local_path.read_text(encoding="utf-8"))
        if artifact.payload_json is not None:
            return dict(artifact.payload_json)
        legacy_payload = artifact.metadata_json.get("payload_json")
        if isinstance(legacy_payload, dict):
            return dict(legacy_payload)
        resolved_path = await self._resolve_file_artifact(artifact, label="json artifact")
        return json.loads(resolved_path.read_text(encoding="utf-8"))

    def _record_audio_artifact(
        self,
        *,
        job_id: str,
        artifact_id: str,
        artifact_type: str,
        role: str,
        path: Path,
    ) -> AsyncV2Artifact:
        existing = self.repository.get_artifact(artifact_id)
        if existing is not None:
            return existing

        container = None
        upload_blob = None
        upload_url = None
        content_type = guess_audio_content_type(path)
        if self.storage is not None and getattr(self.storage, "enabled", False):
            container = getattr(self.settings, "audio_container_name")
            upload_blob = self.storage.upload_path(
                container=container,
                path=path,
                blob_name=_provider_neutral_blob_name(
                    job_id=job_id,
                    folder="audio/candidates",
                    label=f"{artifact_type}_{role}",
                    source_path=path,
                ),
                content_type=content_type,
            )
            if upload_blob and hasattr(self.storage, "generate_sas_url"):
                upload_url = self.storage.generate_sas_url(
                    container=container,
                    blob_name=upload_blob,
                )

        artifact = self.repository.add_artifact(
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
            stage_name=self.stage_name,
            message=f"{artifact_type} artifact recorded.",
            payload_json={
                "artifact_id": artifact.artifact_id,
                "role": role,
                "blob_name": upload_blob,
            },
        )
        return artifact

    def _find_artifact(self, job_id: str, artifact_type: str, role: Optional[str] = None) -> AsyncV2Artifact:
        for artifact in self.repository.list_artifacts(job_id):
            if artifact.artifact_type == artifact_type and (role is None or artifact.role == role):
                return artifact
        raise KeyError(
            f"Artifact not found for job {job_id}: {artifact_type}/{role or '*'}")

    def _optional_artifact(
        self,
        job_id: str,
        artifact_type: str,
        role: Optional[str] = None,
    ) -> Optional[AsyncV2Artifact]:
        for artifact in self.repository.list_artifacts(job_id):
            if artifact.artifact_type == artifact_type and (role is None or artifact.role == role):
                return artifact
        return None


class VideoPreprocessWorker(_SplitStageWorkerBase):
    """High-CPU split worker for source preparation and media metadata.

    This worker is the first split-stage task. It resolves the staged source
    artifact, optionally writes/reuses the compressed source artifact, runs the
    existing media preprocess component, stores the preprocess JSON artifact,
    and then enqueues analysis on the normal-CPU queue. It does not perform
    scene understanding or provider work.
    """

    stage_name = "video_preprocess"
    task_type = "video_preprocess"
    queue_name = "video-preprocess"

    async def _run_stage(self, *, job: AsyncV2Job, task: AsyncV2Task) -> StageTaskResult:
        source_artifact_id = (
            task.payload_json.get("source_video_artifact_id")
            or job.request_json.get("source_video_artifact_id")
        )
        if not source_artifact_id:
            raise ValueError(
                "video_preprocess requires source_video_artifact_id.")
        source_artifact = self.repository.get_artifact(str(source_artifact_id))
        if source_artifact is None:
            raise KeyError(
                f"Source video artifact not found: {source_artifact_id}")

        existing_preprocess = self.repository.get_artifact(
            f"{job.job_id}:video_preprocess:v1")
        if existing_preprocess is not None:
            preprocess_json = await self._load_json_artifact_async(existing_preprocess)
            prepared_source_artifact_id = str(
                preprocess_json.get("prepared_source_video_artifact_id")
                or preprocess_json.get("source_video_artifact_id")
                or source_artifact.artifact_id
            )
            prepared_source_artifact = self.repository.get_artifact(
                prepared_source_artifact_id)
            if prepared_source_artifact is None:
                raise KeyError(
                    f"Prepared source video artifact not found: {prepared_source_artifact_id}"
                )
            prepared_source_video_path = await self._resolve_source_video(
                prepared_source_artifact
            )
            preprocess_output = preprocess_output_from_json(
                preprocess_json,
                source_video_path=prepared_source_video_path,
                temp_folder=self._workdir(
                    job.job_id, "video_preprocess", "temp"),
            )
            next_task = self._analysis_task(
                job=job,
                task=task,
                source_video_artifact_id=prepared_source_artifact.artifact_id,
                video_preprocess_artifact_id=existing_preprocess.artifact_id,
            )
            return StageTaskResult(
                output_json={
                    "video_preprocess_artifact_id": existing_preprocess.artifact_id,
                    "source_video_artifact_id": prepared_source_artifact.artifact_id,
                    "duration": preprocess_output.video_metadata.duration,
                    "width": preprocess_output.video_metadata.width,
                    "height": preprocess_output.video_metadata.height,
                    "compression": preprocess_output.compression_info,
                    "reused": True,
                },
                next_task=next_task,
                job_status=JobStatus.QUEUED,
                progress_percent=25,
            )

        source_video_path = await self._resolve_source_video(source_artifact)
        # Input guardrails on the ORIGINAL resolved file, before hashing and
        # compression: a violation must fail the job with its specific error
        # code without burning ffmpeg/sha CPU first. ffprobe runs off-loop so
        # the lease heartbeat keeps beating.
        await asyncio.to_thread(validate_source_video_file, source_video_path)
        source_sha = self._effective_source_sha(source_artifact, source_video_path)
        prepared_source, compress_cache_state = await self._prepare_source_with_cache(
            job=job,
            source_artifact=source_artifact,
            source_video_path=source_video_path,
            source_sha=source_sha,
        )
        stage = VideoMusicPreprocessStage(self.runtime)
        stage_output = await stage.run(
            VideoPreprocessStageInput(
                job_id=job.job_id,
                request_json=job.request_json,
                source_video_artifact_id=source_artifact.artifact_id,
                prepared_source_video_artifact_id=prepared_source.artifact.artifact_id,
                source_video_path=prepared_source.path,
                compression_info=prepared_source.compression_info,
            )
        )
        preprocess_artifact = self._record_json_artifact(
            job_id=job.job_id,
            artifact_id=f"{job.job_id}:video_preprocess:v1",
            artifact_type="video_preprocess",
            role="primary",
            payload=preprocess_output_to_json(stage_output),
        )
        next_task = self._analysis_task(
            job=job,
            task=task,
            source_video_artifact_id=prepared_source.artifact.artifact_id,
            video_preprocess_artifact_id=preprocess_artifact.artifact_id,
            source_sha=source_sha,
        )
        return StageTaskResult(
            output_json={
                "video_preprocess_artifact_id": preprocess_artifact.artifact_id,
                "source_video_artifact_id": prepared_source.artifact.artifact_id,
                "duration": stage_output.video_metadata.duration,
                "width": stage_output.video_metadata.width,
                "height": stage_output.video_metadata.height,
                "compression": stage_output.compression_info,
                "compress_cache": compress_cache_state,
            },
            next_task=next_task,
            job_status=JobStatus.QUEUED,
            progress_percent=25,
        )

    def _effective_source_sha(
        self, source_artifact: AsyncV2Artifact, source_video_path: Path
    ) -> Optional[str]:
        """Source content sha for cache keys, computed on demand for video_url jobs.

        Staged assets already carry `sha256` in metadata. Jobs created directly from a
        `video_url` do not (the source is resolved lazily), so without this the cache
        would always bypass for real traffic. When a cache layer is enabled and the sha
        is missing, hash the resolved source file once (the worker already has it on
        disk). Returns None when no cache layer is enabled, so the hash cost is only
        paid when it can actually be used.
        """

        sha = (source_artifact.metadata_json or {}).get("sha256")
        if sha:
            return str(sha)
        if self.cache is None or not (
            _cache_layer_enabled("ASYNC_V2_COMPRESS_CACHE")
            or _cache_layer_enabled("ASYNC_V2_UNDERSTANDING_CACHE")
        ):
            return None
        try:
            return sha256_file(source_video_path)
        except Exception:
            logger.warning(
                "Failed to hash source video %s for cache key; skipping cache.",
                source_video_path,
                exc_info=True,
            )
            return None

    async def _prepare_source_with_cache(
        self,
        *,
        job: AsyncV2Job,
        source_artifact: AsyncV2Artifact,
        source_video_path: Path,
        source_sha: Optional[str],
    ) -> tuple[Any, str]:
        """Prepare the source video, reusing a cached compressed result when possible.

        Compression is a deterministic function of (source bytes, max_height), so the
        compressed output is content-addressed and shared across jobs. On a cache hit
        we pre-record this job's compressed-input artifact pointing at the already
        durable blob, which makes the source-preparation service reuse it instead of
        invoking ffmpeg. On a miss (with a single-flight lease) we compress as usual,
        then publish the result for the next job. The cache is a pure optimization:
        when it is absent, disabled, or unkeyable, behavior is identical to before.

        Returns the prepared source and a cache state for telemetry:
        `hit` | `miss` | `contended` | `bypass`.
        """

        async def _prepare() -> Any:
            return await self.source_preparation.prepare_for_workflow(
                job_id=job.job_id,
                request=job.request_json,
                source_artifact=source_artifact,
                source_video_path=source_video_path,
            )

        # Compression ON by default (must match prepare_for_workflow's default).
        requested = coerce_request_bool(job.request_json.get("compression_flag", True))
        if (
            self.cache is None
            or not _cache_layer_enabled("ASYNC_V2_COMPRESS_CACHE")
            or not requested
            or not source_sha
        ):
            return await _prepare(), "bypass"

        max_height = int(job.request_json.get("compression_max_height") or 1280)
        key = compression_cache_key(source_sha=str(source_sha), max_height=max_height)

        try:
            entry, lease = self.cache.get_or_lease(
                key,
                kind="compress",
                content_sha=str(source_sha),
                key_version=KV_COMPRESS,
            )
        except Exception:
            logger.warning(
                "Compression cache lookup failed for %s; computing uncached.",
                key,
                exc_info=True,
            )
            return await _prepare(), "bypass"

        if entry is not None:
            # Cache hit: stage the already-compressed blob as this job's compressed
            # input so prepare_for_workflow reuses it and skips ffmpeg entirely.
            try:
                self._record_compressed_artifact_from_cache(
                    job_id=job.job_id,
                    source_artifact_id=source_artifact.artifact_id,
                    payload=entry.payload_json,
                )
                prepared = await _prepare()
                return prepared, "hit"
            except Exception:
                logger.warning(
                    "Compression cache hit for %s could not be reused; recomputing.",
                    key,
                    exc_info=True,
                )
                return await _prepare(), "miss"

        if lease is None:
            # A concurrent worker holds the compute lease; produce a correct result
            # without caching this round.
            return await _prepare(), "contended"

        try:
            prepared = await _prepare()
        except Exception:
            # Free the key so the failure does not block caching for the lease TTL.
            self._release_cache_lease(key, lease)
            raise
        try:
            payload = self._compression_cache_payload(prepared)
            if payload is not None:
                self.cache.complete_lease(
                    key,
                    lease,
                    kind="compress",
                    payload_json=payload,
                    content_sha=str(source_sha),
                    key_version=KV_COMPRESS,
                    size_bytes=payload.get("size_bytes"),
                )
            else:
                self._release_cache_lease(key, lease)
        except Exception:
            logger.warning(
                "Failed to publish compression cache entry for %s.",
                key,
                exc_info=True,
            )
        return prepared, "miss"

    def _compression_cache_payload(self, prepared: Any) -> Optional[dict[str, Any]]:
        """Snapshot a prepared compressed artifact as a reusable cache payload.

        Only entries backed by a durable blob (container + blob_name) are cached: the
        cache is shared across worker hosts, so a local-only path would be unresolvable
        on a different replica and a hit would poison the job. Returns None when there
        is no blob, so the entry is simply not cached (split mode requires storage, so
        in production the compressed artifact always has a blob).
        """

        artifact = prepared.artifact
        metadata = dict(artifact.metadata_json or {})
        has_blob = bool(artifact.container and artifact.blob_name)
        if not has_blob:
            return None
        return {
            "artifact_type": artifact.artifact_type,
            "role": artifact.role,
            "container": artifact.container,
            "blob_name": artifact.blob_name,
            "url": artifact.url,
            "content_type": artifact.content_type,
            "local_path": str(artifact.local_path) if artifact.local_path else None,
            "metadata_json": metadata,
            "compression_info": dict(prepared.compression_info or {}),
            "size_bytes": metadata.get("size_bytes"),
        }

    def _record_compressed_artifact_from_cache(
        self, *, job_id: str, source_artifact_id: str, payload: dict[str, Any]
    ) -> AsyncV2Artifact:
        """Record this job's compressed-input artifact from a cache payload.

        Generates a fresh read URL from container/blob_name when storage is available
        so the reference does not depend on a possibly-expired stored SAS URL. Local
        path is preserved only if it still exists on this host (same-node cache);
        otherwise the artifact resolves via the fresh URL. The recorded metadata points
        `source_artifact_id` at THIS job's source (not the producing job's) so downstream
        lookups stay job-local.
        """

        artifact_id = f"{job_id}:source_video:compressed_input"
        existing = self.repository.get_artifact(artifact_id)
        if existing is not None:
            return existing

        container = payload.get("container")
        blob_name = payload.get("blob_name")
        url = payload.get("url")
        if container and blob_name and self.storage is not None and hasattr(
            self.storage, "generate_sas_url"
        ):
            try:
                url = self.storage.generate_sas_url(container=container, blob_name=blob_name)
            except Exception:
                logger.debug(
                    "Could not refresh SAS url for cached compressed blob %s/%s.",
                    container,
                    blob_name,
                    exc_info=True,
                )

        local_path = payload.get("local_path")
        if local_path and not Path(local_path).exists():
            local_path = None

        metadata = dict(payload.get("metadata_json") or {})
        metadata["reused_from_cache"] = True
        metadata["source_artifact_id"] = source_artifact_id

        return self.repository.add_artifact(
            artifact_id=artifact_id,
            job_id=job_id,
            artifact_type=payload.get("artifact_type") or "source_video",
            role=payload.get("role") or "compressed_input",
            container=container,
            blob_name=blob_name,
            url=url,
            content_type=payload.get("content_type") or "video/mp4",
            local_path=local_path,
            metadata_json=metadata,
        )

    def _analysis_task(
        self,
        *,
        job: AsyncV2Job,
        task: AsyncV2Task,
        source_video_artifact_id: str,
        video_preprocess_artifact_id: str,
        source_sha: Optional[str] = None,
    ) -> TaskEnvelope:
        max_attempts = int(job.request_json.get(
            "max_attempts") or task.max_attempts or 3)
        payload: dict[str, Any] = {
            "source_video_artifact_id": source_video_artifact_id,
            "video_preprocess_artifact_id": video_preprocess_artifact_id,
        }
        if source_sha:
            # Carry the original source content sha so the understanding cache can key
            # on it without re-resolving the original (the analysis worker only has the
            # prepared/compressed video locally).
            payload["source_sha"] = source_sha
        return TaskEnvelope(
            task_id=f"{job.job_id}:analysis-and-planning",
            job_id=job.job_id,
            queue_name=self._queue_name("analysis-and-planning"),
            task_type="analysis_and_planning",
            payload_json=payload,
            priority=job.priority,
            max_attempts=max_attempts,
            idempotency_key=f"{job.job_id}:analysis-and-planning:v1",
        )


class AnalysisAndPlanningWorker(_SplitStageWorkerBase):
    """Normal-CPU split worker for prompt, scene, and music planning."""

    stage_name = "analysis_and_planning"
    task_type = "analysis_and_planning"
    queue_name = "analysis-and-planning"

    async def _run_stage(self, *, job: AsyncV2Job, task: AsyncV2Task) -> StageTaskResult:
        source_artifact_id = (
            task.payload_json.get("source_video_artifact_id")
            or job.request_json.get("source_video_artifact_id")
        )
        if not source_artifact_id:
            raise ValueError(
                "analysis_and_planning requires source_video_artifact_id.")
        source_artifact = self.repository.get_artifact(str(source_artifact_id))
        if source_artifact is None:
            raise KeyError(
                f"Source video artifact not found: {source_artifact_id}")
        source_video_path = await self._resolve_source_video(source_artifact)
        preprocess = None
        video_preprocess_artifact_id = task.payload_json.get(
            "video_preprocess_artifact_id")
        if video_preprocess_artifact_id:
            video_preprocess_artifact = self.repository.get_artifact(
                str(video_preprocess_artifact_id)
            )
            if video_preprocess_artifact is None:
                raise KeyError(
                    f"Video preprocess artifact not found: {video_preprocess_artifact_id}"
                )
            preprocess = preprocess_output_from_json(
                await self._load_json_artifact_async(video_preprocess_artifact),
                source_video_path=source_video_path,
                temp_folder=self._workdir(
                    job.job_id, "analysis_and_planning", "preprocess"),
            )
        existing_analysis = self.repository.get_artifact(
            f"{job.job_id}:analysis_plan:v1")
        if existing_analysis is not None:
            analysis = analysis_output_from_json(
                await self._load_json_artifact_async(existing_analysis),
                source_video_path=source_video_path,
                temp_folder=self._workdir(
                    job.job_id, "analysis_and_planning", "temp"),
            )
            max_attempts = int(job.request_json.get(
                "max_attempts") or task.max_attempts or 3)
            next_task = TaskEnvelope(
                task_id=f"{job.job_id}:provider-candidate-generation",
                job_id=job.job_id,
                queue_name=self._queue_name(
                    provider_queue_name_for_modelspec(
                        analysis.effective_modelspec)
                ),
                task_type="provider_candidate_generation",
                payload_json={
                    "source_video_artifact_id": source_artifact.artifact_id,
                    "analysis_artifact_id": existing_analysis.artifact_id,
                    "model_spec": analysis.effective_modelspec,
                },
                priority=job.priority,
                max_attempts=provider_stage_max_attempts(
                    max_attempts, analysis.effective_modelspec),
                idempotency_key=f"{job.job_id}:provider-candidate-generation:v1",
            )
            return StageTaskResult(
                output_json={
                    "analysis_artifact_id": existing_analysis.artifact_id,
                    "scene_count": len(analysis.scenes),
                    "effective_modelspec": analysis.effective_modelspec,
                    "include_vocals": analysis.include_vocals,
                    "reused": True,
                },
                next_task=next_task,
                job_status=JobStatus.QUEUED,
                progress_percent=40,
            )

        understanding_cache = (
            self.cache
            if self.cache is not None and _cache_layer_enabled("ASYNC_V2_UNDERSTANDING_CACHE")
            else None
        )
        cache_source_sha = (
            self._original_source_sha(source_artifact, task=task)
            if understanding_cache is not None
            else None
        )
        cache_max_height = (
            self._cache_max_height(job.request_json)
            if understanding_cache is not None
            else None
        )
        stage = VideoMusicAnalysisAndPlanningStage(self.runtime)
        stage_output = await stage.run(
            AnalysisAndPlanningStageInput(
                job_id=job.job_id,
                request_json=job.request_json,
                source_video_artifact_id=source_artifact.artifact_id,
                source_video_path=source_video_path,
                preprocess=preprocess,
                understanding_cache=understanding_cache,
                cache_source_sha=cache_source_sha,
                cache_max_height=cache_max_height,
            )
        )
        analysis_json = analysis_output_to_json(stage_output)
        analysis_artifact = self._record_json_artifact(
            job_id=job.job_id,
            artifact_id=f"{job.job_id}:analysis_plan:v1",
            artifact_type="analysis_plan",
            role="primary",
            payload=analysis_json,
        )
        max_attempts = int(job.request_json.get(
            "max_attempts") or task.max_attempts or 3)
        next_task = TaskEnvelope(
            task_id=f"{job.job_id}:provider-candidate-generation",
            job_id=job.job_id,
            queue_name=self._queue_name(
                provider_queue_name_for_modelspec(
                    stage_output.effective_modelspec)
            ),
            task_type="provider_candidate_generation",
            payload_json={
                "source_video_artifact_id": source_artifact.artifact_id,
                "analysis_artifact_id": analysis_artifact.artifact_id,
                "model_spec": stage_output.effective_modelspec,
            },
            priority=job.priority,
            max_attempts=provider_stage_max_attempts(
                max_attempts, stage_output.effective_modelspec),
            idempotency_key=f"{job.job_id}:provider-candidate-generation:v1",
        )
        return StageTaskResult(
            output_json={
                "analysis_artifact_id": analysis_artifact.artifact_id,
                "scene_count": len(stage_output.scenes),
                "effective_modelspec": stage_output.effective_modelspec,
                "include_vocals": stage_output.include_vocals,
                "understanding_cache": stage_output.understanding_cache_state,
            },
            next_task=next_task,
            job_status=JobStatus.QUEUED,
            progress_percent=40,
        )

    def _original_source_sha(
        self, source_artifact: AsyncV2Artifact, *, task: Optional[AsyncV2Task] = None
    ) -> Optional[str]:
        """Resolve the original source video's content sha for cache keying.

        The analysis stage receives the prepared (possibly compressed) source artifact;
        the understanding cache is keyed on the ORIGINAL content sha plus max_height,
        which deterministically identify the prepared video. Preference order: the sha
        the preprocess stage propagated on the task payload (covers video_url jobs that
        had no staged sha), then this artifact's metadata, then the original artifact
        referenced by `source_artifact_id`.
        """

        if task is not None:
            propagated = task.payload_json.get("source_sha")
            if propagated:
                return str(propagated)
        metadata = source_artifact.metadata_json or {}
        sha = metadata.get("sha256")
        if sha:
            return str(sha)
        original_id = metadata.get("source_artifact_id")
        if original_id:
            original = self.repository.get_artifact(str(original_id))
            if original is not None:
                original_sha = (original.metadata_json or {}).get("sha256")
                if original_sha:
                    return str(original_sha)
        return None

    @staticmethod
    def _cache_max_height(request: dict[str, Any]) -> Optional[int]:
        """Max-height that identifies the prepared video, or None when uncompressed."""

        # Compression ON by default (must match prepare_for_workflow's default).
        if not coerce_request_bool(request.get("compression_flag", True)):
            return None
        return int(request.get("compression_max_height") or 1280)


class ProviderCandidateGenerationWorker(_SplitStageWorkerBase):
    """Provider split worker with durable task-ID persistence.

    Candidate generation is the only split stage that can spend money or create
    a new song outside our infrastructure. The worker records provider task IDs
    as `provider_task` artifacts as soon as the provider accepts a submission,
    before any long poll. Retries first reuse finished candidate artifacts, then
    pass an existing provider task ID back into the stage when the provider
    supports resume polling.
    """

    stage_name = "provider_candidate_generation"
    task_type = "provider_candidate_generation"
    queue_name = "provider-candidate-generation"
    # Provider generation is dominated by async provider polling, so one replica
    # can drain many queued jobs concurrently instead of serializing them behind
    # a single in-flight generation. This is the main lever against provider-queue
    # wait (the measured split-mode regression). Tunable via
    # ASYNC_V2_PROVIDER_CONCURRENCY in worker_main.
    max_concurrency: int = 8

    async def _run_stage(self, *, job: AsyncV2Job, task: AsyncV2Task) -> StageTaskResult:
        analysis_artifact_id = task.payload_json.get("analysis_artifact_id")
        source_artifact_id = task.payload_json.get("source_video_artifact_id")
        if not analysis_artifact_id or not source_artifact_id:
            raise ValueError(
                "provider_candidate_generation requires analysis and source artifacts.")
        analysis_artifact = self.repository.get_artifact(
            str(analysis_artifact_id))
        source_artifact = self.repository.get_artifact(str(source_artifact_id))
        if analysis_artifact is None:
            raise KeyError(
                f"Analysis artifact not found: {analysis_artifact_id}")
        if source_artifact is None:
            raise KeyError(
                f"Source video artifact not found: {source_artifact_id}")

        provider_temp_folder = self._workdir(
            job.job_id,
            "provider_candidate_generation",
            "temp",
        )
        provider_temp_folder.mkdir(parents=True, exist_ok=True)
        metadata_only_source_path = provider_temp_folder / \
            "metadata_only_source_video.unavailable"
        analysis = analysis_output_from_json(
            await self._load_json_artifact_async(analysis_artifact),
            source_video_path=metadata_only_source_path,
            temp_folder=provider_temp_folder,
        )
        expected_queue_name = self._queue_name(
            provider_queue_name_for_modelspec(analysis.effective_modelspec)
        )
        if (
            self.base_queue_name != "provider-candidate-generation"
            and self.queue_name != expected_queue_name
        ):
            raise ValueError(
                f"{self.stage_name} worker for {self.queue_name} cannot process "
                f"model_spec={analysis.effective_modelspec}."
            )
        existing_candidate_audio = self.repository.get_artifact(
            f"{job.job_id}:candidate_audio:primary")
        existing_candidate_metadata = self.repository.get_artifact(
            f"{job.job_id}:candidate_metadata:primary")
        if existing_candidate_audio is not None and existing_candidate_metadata is not None:
            max_attempts = int(job.request_json.get(
                "max_attempts") or task.max_attempts or 3)
            next_task = TaskEnvelope(
                task_id=f"{job.job_id}:selection-ranking-remix-finalize",
                job_id=job.job_id,
                queue_name=self._queue_name(
                    "selection-ranking-remix-finalize"),
                task_type="selection_ranking_remix_finalize",
                payload_json={
                    "source_video_artifact_id": source_artifact.artifact_id,
                    "analysis_artifact_id": analysis_artifact.artifact_id,
                    "candidate_metadata_artifact_id": existing_candidate_metadata.artifact_id,
                    "candidate_audio_artifact_id": existing_candidate_audio.artifact_id,
                },
                priority=job.priority,
                max_attempts=max_attempts,
                idempotency_key=f"{job.job_id}:selection-ranking-remix-finalize:v1",
            )
            return StageTaskResult(
                output_json={
                    "candidate_metadata_artifact_id": existing_candidate_metadata.artifact_id,
                    "candidate_audio_artifact_id": existing_candidate_audio.artifact_id,
                    "model_spec": analysis.effective_modelspec,
                    "reused": True,
                },
                next_task=next_task,
                job_status=JobStatus.QUEUED,
                progress_percent=75,
            )

        existing_provider_task = self._optional_artifact(
            job.job_id,
            artifact_type="provider_task",
            role="primary_generation",
        )
        resume_provider_task_id = None
        resume_provider_key_label = None
        resume_provider_base = None
        if existing_provider_task is not None:
            provider_task_payload = await self._load_json_artifact_async(existing_provider_task)
            resume_provider_task_id = provider_task_payload.get("task_id")
            task_metadata = provider_task_payload.get("metadata") or {}
            resume_provider_key_label = task_metadata.get("key_label") or None
            resume_provider_base = task_metadata.get("base_used") or None

        async def _record_provider_task(payload: dict[str, Any]) -> None:
            task_id = str(payload.get("task_id") or "").strip()
            if not task_id:
                return
            role = str(payload.get("role") or "primary_generation")
            artifact_id = _provider_task_artifact_id(
                job_id=job.job_id, role=role)
            record_payload = {
                "job_id": job.job_id,
                "stage_name": self.stage_name,
                "provider_name": payload.get("provider_name"),
                "model_spec": payload.get("model_spec") or analysis.effective_modelspec,
                "operation": payload.get("operation") or "generate",
                "role": role,
                "task_id": task_id,
                "attempt": task.attempt,
                "source_video_artifact_id": source_artifact.artifact_id,
                "analysis_artifact_id": analysis_artifact.artifact_id,
                "metadata": {
                    key: value
                    for key, value in payload.items()
                    if key
                    not in {
                        "provider_name",
                        "model_spec",
                        "operation",
                        "role",
                        "task_id",
                    }
                },
            }
            self._record_json_artifact(
                job_id=job.job_id,
                artifact_id=artifact_id,
                artifact_type="provider_task",
                role=role,
                payload=record_payload,
            )

        stage = VideoMusicProviderCandidateGenerationStage(self.runtime)
        candidate_output = await stage.run(
            ProviderCandidateGenerationStageInput(
                job_id=job.job_id,
                request_json=job.request_json,
                source_video_path=metadata_only_source_path,
                analysis=analysis,
                provider_task_recorder=_record_provider_task,
                resume_provider_task_id=(
                    str(resume_provider_task_id) if resume_provider_task_id else None
                ),
                resume_provider_key_label=(
                    str(resume_provider_key_label) if resume_provider_key_label else None
                ),
                resume_provider_base=(
                    str(resume_provider_base) if resume_provider_base else None
                ),
            )
        )
        candidate_audio_artifact = self._record_audio_artifact(
            job_id=job.job_id,
            artifact_id=f"{job.job_id}:candidate_audio:primary",
            artifact_type="candidate_audio",
            role="primary",
            path=candidate_output.candidate_audio_path,
        )
        candidate_json = candidate_output_to_json(candidate_output)
        candidate_json["candidate_audio_artifact_id"] = candidate_audio_artifact.artifact_id
        provider_task_artifact = self._optional_artifact(
            job.job_id,
            artifact_type="provider_task",
            role="primary_generation",
        )
        if provider_task_artifact is not None:
            candidate_json["provider_task_artifact_id"] = provider_task_artifact.artifact_id
        candidate_metadata_artifact = self._record_json_artifact(
            job_id=job.job_id,
            artifact_id=f"{job.job_id}:candidate_metadata:primary",
            artifact_type="candidate_metadata",
            role="primary",
            payload=candidate_json,
        )
        max_attempts = int(job.request_json.get(
            "max_attempts") or task.max_attempts or 3)
        next_task = TaskEnvelope(
            task_id=f"{job.job_id}:selection-ranking-remix-finalize",
            job_id=job.job_id,
            queue_name=self._queue_name("selection-ranking-remix-finalize"),
            task_type="selection_ranking_remix_finalize",
            payload_json={
                "source_video_artifact_id": source_artifact.artifact_id,
                "analysis_artifact_id": analysis_artifact.artifact_id,
                "candidate_metadata_artifact_id": candidate_metadata_artifact.artifact_id,
                "candidate_audio_artifact_id": candidate_audio_artifact.artifact_id,
            },
            priority=job.priority,
            max_attempts=max_attempts,
            idempotency_key=f"{job.job_id}:selection-ranking-remix-finalize:v1",
        )
        return StageTaskResult(
            output_json={
                "candidate_metadata_artifact_id": candidate_metadata_artifact.artifact_id,
                "candidate_audio_artifact_id": candidate_audio_artifact.artifact_id,
                "model_spec": candidate_output.model_spec,
                "provider_task_artifact_id": (
                    provider_task_artifact.artifact_id
                    if provider_task_artifact is not None
                    else None
                ),
                "critical_warning": candidate_output.critical_warning,
                "generation_api_call_count": candidate_output.generation_api_call_count,
            },
            next_task=next_task,
            job_status=JobStatus.QUEUED,
            progress_percent=75,
        )


class SelectionRankingRemixFinalizeWorker(_SplitStageWorkerBase):
    stage_name = "selection_ranking_remix_finalize"
    task_type = "selection_ranking_remix_finalize"
    queue_name = "selection-ranking-remix-finalize"

    async def _run_stage(self, *, job: AsyncV2Job, task: AsyncV2Task) -> StageTaskResult:
        source_artifact_id = task.payload_json.get("source_video_artifact_id")
        analysis_artifact_id = task.payload_json.get("analysis_artifact_id")
        candidate_metadata_artifact_id = task.payload_json.get(
            "candidate_metadata_artifact_id")
        candidate_audio_artifact_id = task.payload_json.get(
            "candidate_audio_artifact_id")
        if not all(
            [
                source_artifact_id,
                analysis_artifact_id,
                candidate_metadata_artifact_id,
                candidate_audio_artifact_id,
            ]
        ):
            raise ValueError(
                "selection_ranking_remix_finalize task payload is incomplete.")

        source_artifact = self.repository.get_artifact(str(source_artifact_id))
        analysis_artifact = self.repository.get_artifact(
            str(analysis_artifact_id))
        candidate_metadata_artifact = self.repository.get_artifact(
            str(candidate_metadata_artifact_id))
        candidate_audio_artifact = self.repository.get_artifact(
            str(candidate_audio_artifact_id))
        if source_artifact is None:
            raise KeyError(
                f"Source video artifact not found: {source_artifact_id}")
        if analysis_artifact is None:
            raise KeyError(
                f"Analysis artifact not found: {analysis_artifact_id}")
        if candidate_metadata_artifact is None:
            raise KeyError(
                f"Candidate metadata artifact not found: {candidate_metadata_artifact_id}")
        if candidate_audio_artifact is None:
            raise KeyError(
                f"Candidate audio artifact not found: {candidate_audio_artifact_id}")

        existing_result = job.result_json
        existing_final_artifact = self.repository.get_artifact(
            f"{job.job_id}:final_result:v1")
        if existing_result is not None and existing_final_artifact is not None:
            return StageTaskResult(
                output_json={
                    "final_result_artifact_id": existing_final_artifact.artifact_id,
                    "video_url": result_video_url(existing_result),
                    "audio_url": result_audio_url(existing_result),
                    "reused": True,
                },
                job_status=JobStatus.COMPLETED,
                progress_percent=100,
                result_json=existing_result,
                finished=True,
            )

        source_video_path = await self._resolve_source_video(source_artifact)
        candidate_audio_path = await self._resolve_file_artifact(
            candidate_audio_artifact,
            label="candidate audio",
        )
        analysis = analysis_output_from_json(
            await self._load_json_artifact_async(analysis_artifact),
            source_video_path=source_video_path,
            temp_folder=self._workdir(
                job.job_id, "selection_ranking_remix_finalize", "temp"),
        )
        candidate = candidate_output_from_json(
            await self._load_json_artifact_async(candidate_metadata_artifact),
            candidate_audio_path=candidate_audio_path,
        )
        stage = VideoMusicSelectionRankingRemixFinalizeStage(self.runtime)
        stage_output = await stage.run(
            SelectionRankingRemixFinalizeStageInput(
                job_id=job.job_id,
                request_json=job.request_json,
                source_video_path=source_video_path,
                analysis=analysis,
                candidate=candidate,
            )
        )

        response = VideoMusicMonolithWorker(
            repository=self.repository,
            queue=self.queue,
            orchestrator=None,
            settings=self.settings,
            storage=self.storage,
            worker_id=self.worker_id,
        )._persist_result_assets_and_build_response(
            job_id=job.job_id,
            request=job.request_json,
            source_artifact=source_artifact,
            source_video_path=source_video_path,
            result=stage_output.result,
        )
        result_json = response.model_dump(mode="json")
        final_result_artifact = self._record_json_artifact(
            job_id=job.job_id,
            artifact_id=f"{job.job_id}:final_result:v1",
            artifact_type="final_result",
            role="primary",
            payload=result_json,
        )
        return StageTaskResult(
            output_json={
                "final_result_artifact_id": final_result_artifact.artifact_id,
                "video_url": result_video_url(result_json),
                "audio_url": result_audio_url(result_json),
            },
            job_status=JobStatus.COMPLETED,
            progress_percent=100,
            result_json=result_json,
            finished=True,
        )


class BasicMusicGenerationWorker(ProviderCandidateGenerationWorker):
    queue_name = "music-basic"
    retry_backoff_seconds = 20


class EnhancedMusicGenerationWorker(ProviderCandidateGenerationWorker):
    queue_name = "music-enhanced"
    retry_backoff_seconds = 45


class StudioMusicGenerationWorker(ProviderCandidateGenerationWorker):
    queue_name = "music-studio"
    retry_backoff_seconds = 60


async def run_worker_loop(
    *,
    worker: _SplitStageWorkerBase,
    once: bool = False,
    poll_interval_seconds: float = 2.0,
    concurrency: Optional[int] = None,
    shutdown_event: Optional[asyncio.Event] = None,
) -> None:
    """Drain a split-stage queue, optionally processing several tasks at once.

    A single replica runs `concurrency` independent consumer loops against the
    same worker instance. Leasing uses `FOR UPDATE SKIP LOCKED`, so concurrent
    consumers always lease distinct tasks; completion/heartbeat/fail are keyed
    by `(task_id, lease_owner)`, so sharing one `worker_id` across consumers is
    safe. For I/O-bound provider stages this lets one replica hold many in-flight
    provider generations instead of serializing them, which is the primary fix
    for split-mode provider-queue wait. Defaults to the worker's
    `max_concurrency` (1 for CPU-bound media stages).
    """

    effective_concurrency = max(
        1, concurrency if concurrency is not None else getattr(worker, "max_concurrency", 1)
    )
    label = f"split:{getattr(worker, 'stage_name', 'worker')}"

    if once or effective_concurrency == 1:
        await run_consumer_loop(
            process_one=worker.process_one,
            once=once,
            poll_interval_seconds=poll_interval_seconds,
            shutdown_event=shutdown_event,
            logger=logger,
            label=label,
        )
        return

    await asyncio.gather(
        *(
            run_consumer_loop(
                process_one=worker.process_one,
                poll_interval_seconds=poll_interval_seconds,
                shutdown_event=shutdown_event,
                logger=logger,
                label=f"{label}#{index}",
            )
            for index in range(effective_concurrency)
        )
    )


__all__ = [
    "AnalysisAndPlanningWorker",
    "BasicMusicGenerationWorker",
    "EnhancedMusicGenerationWorker",
    "ProviderCandidateGenerationWorker",
    "SelectionRankingRemixFinalizeWorker",
    "StudioMusicGenerationWorker",
    "VideoPreprocessWorker",
    "VideoMusicSplitStageRuntime",
    "provider_queue_name_for_modelspec",
    "run_worker_loop",
]
