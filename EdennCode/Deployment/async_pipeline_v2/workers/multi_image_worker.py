"""Async pipeline v2 monolith worker for multi-image music generation.

Mirrors ``VideoMusicMonolithWorker``'s production hardening (lease heartbeat,
retry/dead-letter with ``max_attempts=1`` to avoid double-billing paid providers,
sanitized error payloads, deterministic output artifacts for URL refresh) but
consumes N source-image artifacts (+ optional vocal sample) instead of one source
video. It reuses ``MultiImageGenerationOrchestrator.run()`` unchanged — that
orchestrator already runs OpenCV preprocessing and ffmpeg assembly via
``asyncio.to_thread``, so CPU/IO-bound work stays off the event loop.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, List, Optional
from uuid import uuid4

from EdennCode.Deployment.api_common import (
    cleanup_temp_dir,
    download_public_file_to_disk,
    guess_audio_content_type,
    guess_image_content_type,
    prepare_audio_for_provider_b_vocal_clone,
    probe_audio_metrics,
)
from EdennCode.Deployment.api_multi_image_generation import (
    MULTI_IMAGE_MAX_IMAGES,
    MultiImageJobResponse,
    _assemble_multi_image_response,
    _probe_video_metadata,
)
from EdennCode.Deployment.api_video_generation import _provider_neutral_blob_name
from EdennCode.Deployment.auth.usage_recorder import record_v2_job_usage
from EdennCode.Deployment.async_pipeline_v2.result_paths import (
    result_audio_url,
    result_video_url,
)
from EdennCode.Deployment.async_pipeline_v2.workers.monolith_worker import (
    _compress_thumbnail_under_limit,
    _generate_thumbnail_from_video,
)
from EdennCode.Deployment.async_pipeline_v2.models import (
    AsyncV2Artifact,
    AsyncV2Task,
    JobStatus,
    StageStatus,
)
from EdennCode.Deployment.async_pipeline_v2.artifact_service import upload_path_with_retry
from EdennCode.Deployment.async_pipeline_v2.queue.postgres_queue import PostgresTaskQueue
from EdennCode.Deployment.async_pipeline_v2.repositories import AsyncPipelineV2Repository
from EdennCode.Deployment.async_pipeline_v2.worker_heartbeat import StageLeaseHeartbeat
from EdennCode.Deployment.error_codes import public_error_payload

logger = logging.getLogger(__name__)

STAGE_NAME = "multi_image_monolith"

# Defense-in-depth ceiling mirroring the API contract's image cap
# (MULTI_IMAGE_MAX_IMAGES). The submit endpoints already reject out-of-range
# counts; this guards against a job reaching the worker by any other path, since
# synchronous per-image download + blob upload is a timeout/DoS vector.
DEFAULT_MAX_IMAGES = MULTI_IMAGE_MAX_IMAGES

# Input materialization (image/vocal download) happens BEFORE any paid provider
# call, so a transient blob/network blip is safe to retry without double-billing
# risk — generation is separately protected by the job-level max_attempts=1. A
# fresh SAS is re-minted on each attempt so an expiring/racey signature can't
# wedge the whole job.
INPUT_DOWNLOAD_MAX_ATTEMPTS = 3
INPUT_DOWNLOAD_BACKOFF_S = 0.75


def _artifact_suffix(artifact: AsyncV2Artifact, default: str = ".png") -> str:
    metadata = artifact.metadata_json or {}
    name = metadata.get("source_filename") or artifact.blob_name or ""
    suffix = Path(str(name)).suffix.lower()
    if suffix and len(suffix) <= 16:
        return suffix
    return default


class MultiImageMonolithWorker:
    def __init__(
        self,
        *,
        repository: AsyncPipelineV2Repository,
        queue: PostgresTaskQueue,
        orchestrator: Any,
        settings: Any,
        storage: Any = None,
        worker_id: Optional[str] = None,
        queue_name: str = "multi-image-pipeline",
        lease_seconds: int = 900,
        plan_cache: Any = None,
    ) -> None:
        self.repository = repository
        self.queue = queue
        self.orchestrator = orchestrator  # MultiImageGenerationOrchestrator
        self.settings = settings
        self.storage = storage
        self.worker_id = worker_id or f"worker-multi-image-{uuid4().hex}"
        self.queue_name = queue_name
        self.lease_seconds = lease_seconds
        self.workdir = Path(getattr(settings, "workdir", None) or "/tmp")
        # Optional inline plan cache (safe: stores plan JSON, no SAS URLs). None disables.
        self.plan_cache = plan_cache

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
        job_dir = self.workdir / task.job_id
        try:
            job = self.repository.get_job(task.job_id)
            if job is None:
                raise KeyError(f"Job not found for task {task.task_id}: {task.job_id}")

            # Completed-job guard (belt to the reaper's suspenders): a requeued
            # orphan of an already-COMPLETED job must never re-run the paid music
            # generation. Close the task; the billed result already exists.
            if (getattr(job, "status", None) == JobStatus.COMPLETED
                    and getattr(job, "result_json", None) is not None):
                self.repository.add_event(
                    job_id=task.job_id,
                    event_type="task.skipped",
                    stage_name=STAGE_NAME,
                    message="Task skipped because its job is already completed.",
                    payload_json={"task_id": task.task_id},
                )
                return self.queue.complete(
                    task_id=task.task_id, worker_id=self.worker_id
                )

            request = job.request_json or {}
            image_artifact_ids = (
                task.payload_json.get("image_artifact_ids")
                or request.get("image_artifact_ids")
                or []
            )
            if not image_artifact_ids:
                raise ValueError("multi_image_monolith task requires image_artifact_ids.")
            if len(image_artifact_ids) > DEFAULT_MAX_IMAGES:
                raise ValueError(
                    f"Too many images ({len(image_artifact_ids)}); max {DEFAULT_MAX_IMAGES}."
                )
            vocal_artifact_id = (
                task.payload_json.get("vocal_sample_artifact_id")
                or request.get("vocal_sample_artifact_id")
            )

            stage_run = self.repository.start_stage_run(
                job_id=task.job_id,
                task_id=task.task_id,
                stage_name=STAGE_NAME,
                attempt=task.attempt,
                input_json={
                    "task_type": task.task_type,
                    "image_artifact_ids": list(image_artifact_ids),
                    "vocal_sample_artifact_id": vocal_artifact_id,
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
                message="Multi-image music workflow started.",
                payload_json={
                    "task_id": task.task_id,
                    "attempt": task.attempt,
                    "image_count": len(image_artifact_ids),
                },
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
                images_dir = job_dir / "source" / "images"
                output_video_path = job_dir / "output" / "multi_image_story.mp4"
                await self._download_images(image_artifact_ids, images_dir)

                prepared_vocal_path: Optional[Path] = None
                if vocal_artifact_id:
                    vocal_dir = job_dir / "source" / "vocal"
                    vocal_path = await self._download_artifact(vocal_artifact_id, vocal_dir)
                    # Re-run the ProviderB vocal-clone prep (edenn_enhanced) off-loop.
                    prepared_vocal_path = await asyncio.to_thread(
                        prepare_audio_for_provider_b_vocal_clone,
                        source_audio_path=vocal_path,
                        destination_dir=vocal_dir,
                    )

                result = await self.orchestrator.run(
                    folder_path=images_dir,
                    output_path=output_video_path,
                    user_prompt=str(request.get("user_prompt") or ""),
                    include_vocals=bool(request.get("include_vocals", False)),
                    vocal_gender=request.get("vocal_gender") or None,
                    align_to_beats=bool(request.get("align_to_beats", True)),
                    lyrics_language=request.get("lyrics_language") or None,
                    modelspec=str(request.get("modelspec") or "edenn_basic"),
                    per_image_duration=float(request.get("per_image_duration", 3.0) or 3.0),
                    # Preserve an explicit 0.0 (silence): a truthiness `or 1.0` would wrongly
                    # promote a caller's 0 volume back to full.
                    music_volume=(
                        float(request["music_volume"])
                        if request.get("music_volume") is not None
                        else 1.0
                    ),
                    vocal_id=request.get("vocal_id") or None,
                    vocal_sample_path=prepared_vocal_path,
                    # Default ON (parity with the submit endpoints): a job whose
                    # request predates the field still gets the watermark.
                    water_mark=bool(request.get("water_mark", True)),
                    audio_output_format=request.get("audio_output_format") or None,
                    user_lyrics_prompt=request.get("user_lyrics_prompt") or None,
                    # Transition spec is parsed+validated at submit (v2 api); a job
                    # enqueued before this field existed falls back to hard cuts.
                    transition_mode=str(request.get("transition_mode") or "none"),
                    transitions=request.get("transitions") or None,
                    # Either a single blend length or a per-boundary list, as
                    # resolved at submit; None (legacy/absent) keeps the 0.4 default.
                    transition_duration_s=(
                        request.get("transition_duration_s")
                        if request.get("transition_duration_s") is not None
                        else 0.4
                    ),
                    fixed_image_order=(
                        str(request.get("image_order") or "auto") == "fixed"
                    ),
                    # Explicit per-image seconds (validated at submit); when set,
                    # timing is exact and beat alignment is skipped.
                    per_image_durations=(
                        [float(v) for v in request["per_image_durations"]]
                        if request.get("per_image_durations")
                        else None
                    ),
                    plan_cache=self.plan_cache,
                )
                response = self._persist_result_assets_and_build_response(
                    job_id=task.job_id, result=result
                )
                result_json = response.model_dump(mode="json")

            # Atomic terminal bookkeeping (parity with the video monolith): the
            # stage run, job status, completion event and task completion commit
            # or roll back together, so a crash between writes can never strand
            # the job non-terminal while its task is completed (or vice versa).
            with self.repository.transaction() as client:
                self.repository.update_stage_run(
                    stage_run.stage_run_id,
                    status=StageStatus.COMPLETED,
                    output_json={
                        "video_url": result_video_url(result_json),
                        "audio_url": result_audio_url(result_json),
                        "result_artifact_types": [
                            "complete_audio",
                            "matched_audio",
                            "output_video",
                            "thumbnail",
                        ],
                    },
                    finished=True,
                    client=client,
                )
                _, completed_applied = self.repository.update_job_status_checked(
                    task.job_id,
                    status=JobStatus.COMPLETED,
                    current_stage=STAGE_NAME,
                    progress_percent=100,
                    result_json=result_json,
                    error_json=None,
                    finished=True,
                    client=client,
                )
                if completed_applied:
                    self.repository.add_event(
                        job_id=task.job_id,
                        event_type="job.completed",
                        stage_name=STAGE_NAME,
                        message="Multi-image music workflow completed.",
                        payload_json={
                            "video_url": result_video_url(result_json),
                            "audio_url": result_audio_url(result_json),
                        },
                        client=client,
                    )
                else:
                    logger.warning(
                        "job %s already terminal; completed write skipped", task.job_id
                    )
                completed_task = self.queue.complete(
                    task_id=task.task_id, worker_id=self.worker_id, client=client
                )
            if completed_applied:
                record_v2_job_usage(
                    job, status="completed", endpoint="/api/v2/jobs/multi-image-music",
                    result=result, result_json=result_json,
                )
            return completed_task
        except Exception as exc:
            # Client-facing error: generic catalog text only — never the raw
            # exception string, which can name the upstream provider/model.
            # retryable comes from the error-code catalog: permanent failures
            # (content policy, bad input) must not tell the user to retry, and
            # must not be retried internally — every attempt bills the provider.
            error_json = public_error_payload(exc)
            retryable = bool(error_json.get("retryable", True))
            internal_error = {
                "message": str(exc),
                "type": type(exc).__name__,
                "retryable": retryable,
            }
            logger.exception(
                "multi_image_monolith task failed: job=%s task=%s",
                task.job_id,
                task.task_id,
            )
            # Atomic failure bookkeeping (parity with the video monolith): the
            # task's terminal state and the job's terminal (or requeued) state
            # commit or roll back together, so a crash can never strand the job
            # non-terminal while its task is dead-lettered.
            with self.repository.transaction() as client:
                if stage_run is not None:
                    self.repository.update_stage_run(
                        stage_run.stage_run_id,
                        status=StageStatus.FAILED,
                        error_json=error_json,
                        finished=True,
                        client=client,
                    )
                failed_task = self.queue.fail(
                    task_id=task.task_id,
                    worker_id=self.worker_id,
                    error=internal_error,
                    retry=retryable,
                    backoff_seconds=30,
                    client=client,
                )
                if failed_task.status in {"dead_lettered", "failed"}:
                    _, failed_applied = self.repository.update_job_status_checked(
                        task.job_id,
                        status=JobStatus.FAILED,
                        current_stage=STAGE_NAME,
                        error_json=error_json,
                        finished=True,
                        client=client,
                    )
                    if failed_applied:
                        self.repository.add_event(
                            job_id=task.job_id,
                            event_type="job.failed",
                            stage_name=STAGE_NAME,
                            message="Multi-image music workflow failed.",
                            payload_json=error_json,
                            client=client,
                        )
                        record_v2_job_usage(
                            job, status="failed", endpoint="/api/v2/jobs/multi-image-music",
                        )
                else:
                    self.repository.update_job_status(
                        task.job_id,
                        status=JobStatus.QUEUED,
                        current_stage=STAGE_NAME,
                        error_json=error_json,
                        client=client,
                    )
            return failed_task
        finally:
            cleanup_temp_dir(job_dir, logger=logger)

    # ------------------------------------------------------------------
    # Input materialization
    # ------------------------------------------------------------------
    async def _download_images(self, artifact_ids: List[str], images_dir: Path) -> None:
        images_dir.mkdir(parents=True, exist_ok=True)
        for idx, artifact_id in enumerate(artifact_ids):
            artifact = self.repository.get_artifact(str(artifact_id))
            if artifact is None:
                raise KeyError(f"Source image artifact not found: {artifact_id}")
            dest = images_dir / f"image_{idx + 1:03d}{_artifact_suffix(artifact, '.png')}"
            await self._materialize_artifact(artifact, dest)

    async def _download_artifact(self, artifact_id: str, dest_dir: Path) -> Path:
        artifact = self.repository.get_artifact(str(artifact_id))
        if artifact is None:
            raise KeyError(f"Artifact not found: {artifact_id}")
        dest_dir.mkdir(parents=True, exist_ok=True)
        filename = (artifact.metadata_json or {}).get("source_filename") or (
            f"asset{_artifact_suffix(artifact, '.m4a')}"
        )
        return await self._materialize_artifact(artifact, dest_dir / filename)

    async def _materialize_artifact(self, artifact: AsyncV2Artifact, dest: Path) -> Path:
        """Resolve an input artifact to a local file (parity with resolve_source_video).

        Prefers the on-host local_path (tests / same-node), otherwise re-mints a
        FRESH SAS from container+blob when storage is available (avoids the stored
        SAS-expiry 404 trap), falling back to the durable URL recorded at staging.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        local = Path(artifact.local_path) if artifact.local_path else None
        if local and local.exists():
            data = await asyncio.to_thread(local.read_bytes)
            await asyncio.to_thread(dest.write_bytes, data)
            return dest

        from urllib.parse import urlparse as _urlparse

        last_exc: Optional[Exception] = None
        for attempt in range(1, INPUT_DOWNLOAD_MAX_ATTEMPTS + 1):
            # Re-mint a FRESH SAS every attempt (an expiring/racey signature is a
            # common transient cause, so a stale URL must not be reused on retry).
            url = None
            if (
                self.storage is not None
                and artifact.container
                and artifact.blob_name
                and hasattr(self.storage, "generate_sas_url")
            ):
                # require_signed: never hand the downloader an unsigned URL for a
                # private container (would 403). Returns None if the account key is
                # unavailable, in which case we fall back to the durable staged URL.
                url = self.storage.generate_sas_url(
                    container=artifact.container,
                    blob_name=artifact.blob_name,
                    require_signed=True,
                )
            if not url:
                url = artifact.url or (artifact.metadata_json or {}).get("source_url")
            if not url:
                # A missing URL is a hard config error, not a transient blip.
                raise ValueError(
                    f"Cannot materialize artifact {artifact.artifact_id}: no local_path or durable URL."
                )
            _host = _urlparse(str(url)).netloc
            try:
                result = await download_public_file_to_disk(
                    url=str(url), destination=dest, asset_label="input asset"
                )
                if attempt > 1:
                    logger.info(
                        "multi_image _materialize artifact=%s recovered on attempt %d/%d (host=%s)",
                        artifact.artifact_id, attempt, INPUT_DOWNLOAD_MAX_ATTEMPTS, _host,
                    )
                return result
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "multi_image _materialize download attempt %d/%d FAILED "
                    "artifact=%s host=%s signed=%s: %s",
                    attempt, INPUT_DOWNLOAD_MAX_ATTEMPTS, artifact.artifact_id, _host,
                    ("sig=" in str(url) or "sig%3D" in str(url)), exc,
                )
                if attempt < INPUT_DOWNLOAD_MAX_ATTEMPTS:
                    await asyncio.sleep(INPUT_DOWNLOAD_BACKOFF_S * attempt)
        logger.error(
            "multi_image _materialize download FAILED after %d attempts artifact=%s: %s",
            INPUT_DOWNLOAD_MAX_ATTEMPTS, artifact.artifact_id, last_exc,
        )
        raise last_exc if last_exc is not None else RuntimeError(
            f"multi_image _materialize exhausted retries for {artifact.artifact_id}"
        )

    # ------------------------------------------------------------------
    # Output persistence + response building (reuses the unified assembler)
    # ------------------------------------------------------------------
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
            upload_blob = upload_path_with_retry(
                self.storage,
                container=container,
                path=path,
                blob_name=_provider_neutral_blob_name(
                    job_id=job_id, folder=folder, label=label, source_path=path
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
                "source_filename": path.name,
                "size_bytes": path.stat().st_size if path.exists() else None,
                "uploaded": bool(upload_blob),
            },
        )
        return upload_blob, upload_url

    def _persist_result_assets_and_build_response(
        self, *, job_id: str, result: Any
    ) -> MultiImageJobResponse:
        audio_container = getattr(self.settings, "audio_container_name", "generated-audio")
        output_container = getattr(self.settings, "output_container", "generated-media")

        # Only the generated track is surfaced; alternate takes are not uploaded.
        track_paths = list(result.full_track_paths)
        complete_track_path = track_paths[0] if track_paths else None
        complete_audio_url: Optional[str] = None
        complete_audio_duration_s, complete_audio_size_bytes = probe_audio_metrics(
            complete_track_path
        )
        if complete_track_path is not None:
            _, complete_audio_url = self._record_output_artifact(
                job_id=job_id,
                artifact_type="complete_audio",
                role="primary",
                path=complete_track_path,
                container=audio_container,
                folder="audio/complete",
                label="complete_audio",
                content_type=guess_audio_content_type(complete_track_path),
            )

        # audio_* is the windowed slice muxed into the slideshow — the part the
        # viewer hears. Recorded as a distinct ``matched_audio`` artifact so GET
        # re-signs audio_url from it. Falls back to the full track when the whole
        # track fits the video (no distinct window), so audio_url ==
        # complete_audio_url in that degenerate case.
        matched_path = getattr(result, "matched_music_path", None)
        if matched_path is not None:
            _, audio_url = self._record_output_artifact(
                job_id=job_id,
                artifact_type="matched_audio",
                role="primary",
                path=matched_path,
                container=audio_container,
                folder="audio/window",
                label="matched_audio",
                content_type=guess_audio_content_type(matched_path),
            )
            audio_duration_s, audio_size_bytes = probe_audio_metrics(matched_path)
        else:
            audio_url = complete_audio_url
            audio_duration_s, audio_size_bytes = (
                complete_audio_duration_s,
                complete_audio_size_bytes,
            )

        _, video_url = self._record_output_artifact(
            job_id=job_id,
            artifact_type="output_video",
            role="final",
            path=result.final_video_path,
            container=output_container,
            folder="video/multi_image",
            label="slideshow",
            content_type="video/mp4",
        )

        # Preview thumbnail from the assembled slideshow (best-effort — a thumbnail
        # failure must never fail the job). Mirrors the video-gen worker: pick a
        # representative frame, keep it under the size budget, upload as a
        # "thumbnail" artifact so GET re-signs its URL like the other media.
        thumbnail_url: Optional[str] = None
        try:
            thumbnail_path = _generate_thumbnail_from_video(
                result.final_video_path, result.final_video_path.parent
            )
            if thumbnail_path and thumbnail_path.exists():
                thumbnail_path = _compress_thumbnail_under_limit(
                    thumbnail_path, thumbnail_path.parent
                )
            if thumbnail_path and thumbnail_path.exists():
                _, thumbnail_url = self._record_output_artifact(
                    job_id=job_id,
                    artifact_type="thumbnail",
                    role="thumbnail",
                    path=thumbnail_path,
                    container=output_container,
                    folder="thumbnail",
                    label="thumbnail",
                    content_type=guess_image_content_type(thumbnail_path),
                )
        except Exception:
            logger.warning(
                "Multi-image thumbnail generation failed for job %s; continuing without one.",
                job_id,
                exc_info=True,
            )

        return _assemble_multi_image_response(
            job_id=job_id,
            result=result,
            audio_url=audio_url,
            audio_duration_s=audio_duration_s,
            audio_size_bytes=audio_size_bytes,
            complete_audio_url=complete_audio_url,
            complete_audio_duration_s=complete_audio_duration_s,
            complete_audio_size_bytes=complete_audio_size_bytes,
            video_url=video_url,
            geometry=_probe_video_metadata(result.final_video_path),
            thumbnail_url=thumbnail_url,
        )


class MultiImagePlanCache:
    """Duck-typed plan cache backed by the async-v2 CacheService.

    Stores the raw planning-stage plan JSON INLINE (never a SAS URL), so a cache
    hit can never 404 the way the compress cache does — mirrors the safe
    understanding-cache pattern. Pure optimization: all methods swallow errors.
    """

    KIND = "multi_image_plan"

    def __init__(self, cache_service: Any, *, ttl_days: int = 14) -> None:
        self._cache = cache_service
        self._ttl_days = ttl_days

    def get(self, key: str) -> Optional[dict]:
        try:
            entry = self._cache.get(key, expected_kind=self.KIND)
        except Exception:
            return None
        if entry is None:
            return None
        plan = (getattr(entry, "payload_json", None) or {}).get("plan")
        return plan if isinstance(plan, dict) else None

    def put(self, key: str, plan: dict) -> None:
        try:
            _entry, token = self._cache.get_or_lease(
                key, kind=self.KIND, content_sha=key, key_version=self.KIND
            )
            if token is None:
                return
            self._cache.complete_lease(
                key,
                token,
                kind=self.KIND,
                payload_json={"plan": plan},
                content_sha=key,
                key_version=self.KIND,
                size_bytes=None,
                ttl_days=self._ttl_days,
            )
        except Exception:
            return


__all__ = ["MultiImageMonolithWorker", "MultiImagePlanCache", "STAGE_NAME"]
