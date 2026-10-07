from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import cv2

from EdennCode.Deployment.api_common import (
    guess_audio_content_type,
    guess_image_content_type,
    probe_audio_metrics,
)
from EdennCode.Deployment.api_video_generation import (
    VideoJobAssets,
    VideoJobResponse,
    _provider_neutral_blob_name,
    build_video_job_response,
)
from EdennCode.Deployment.auth.usage_recorder import record_v2_job_usage
from EdennCode.Deployment.async_pipeline_v2.models import (
    AsyncV2Artifact,
    AsyncV2Task,
    JobStatus,
    StageStatus,
)
from EdennCode.Deployment.error_codes import public_error_payload
from EdennCode.Deployment.async_pipeline_v2.artifact_service import upload_path_with_retry
from EdennCode.Deployment.async_pipeline_v2.input_guardrails import (
    MAX_SOURCE_VIDEO_BYTES,
    validate_source_video_file,
)
from EdennCode.Deployment.async_pipeline_v2.result_paths import (
    result_audio_url,
    result_video_url,
)
from EdennCode.Deployment.async_pipeline_v2.queue.postgres_queue import PostgresTaskQueue
from EdennCode.Deployment.async_pipeline_v2.repositories import AsyncPipelineV2Repository
from EdennCode.Deployment.async_pipeline_v2.video_source_preparation import (
    VideoSourcePreparationService,
)
from EdennCode.Deployment.async_pipeline_v2.worker_heartbeat import StageLeaseHeartbeat
from EdennCode.Deployment.async_pipeline_v2.callback_delivery import deliver_job_callback
from EdennCode.Deployment.async_pipeline_v2.workers.loop_guard import run_consumer_loop
from EdennCode.Deployment.recommendation_persistence import RecommendationAssetIds
from EdennCode.Deployment.workflows import VideoGenerationOrchestrator, VideoGenerationResult
from EdennCode.Util.MediaUtils import resolve_ffmpeg_binary


logger = logging.getLogger(__name__)


# Thumbnails are small preview images, not deliverables, so cap their on-disk size.
# A full-resolution single frame can be hundreds of KB; we downscale the longest side
# and step webp quality down until the encoded image fits the budget.
THUMBNAIL_MAX_BYTES = 100_000
THUMBNAIL_MAX_DIM = 720
_THUMBNAIL_WEBP_QUALITIES = (85, 70, 55, 40, 28, 18, 10)


def _resize_to_max_dim(frame, max_dim: int):
    height, width = frame.shape[:2]
    longest = max(height, width)
    if longest <= max_dim:
        return frame
    scale = max_dim / float(longest)
    new_size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
    return cv2.resize(frame, new_size, interpolation=cv2.INTER_AREA)


def _encode_thumbnail_under_limit(
    frame, destination: Path, *, max_bytes: int = THUMBNAIL_MAX_BYTES, max_dim: int = THUMBNAIL_MAX_DIM
) -> Optional[Path]:
    """Encode a frame as webp under max_bytes.

    Steps quality down at each candidate size, and if a high-entropy frame still does
    not fit, steps the longest side down (720 -> 512 -> ... -> 160). At 160px even pure
    noise is well under the budget, so this converges for any real input. The smallest
    attempt is written if, pathologically, nothing fits.
    """

    last_buffer = None
    for dim in (max_dim, 512, 360, 240, 160):
        resized = _resize_to_max_dim(frame, dim)
        for quality in _THUMBNAIL_WEBP_QUALITIES:
            ok, buffer = cv2.imencode(".webp", resized, [int(cv2.IMWRITE_WEBP_QUALITY), quality])
            if not ok:
                continue
            last_buffer = buffer
            if buffer.nbytes <= max_bytes:
                destination.write_bytes(buffer.tobytes())
                return destination
    if last_buffer is not None:
        destination.write_bytes(last_buffer.tobytes())
        return destination
    return None


def _compress_thumbnail_under_limit(
    source_path: Path, output_dir: Path, *, max_bytes: int = THUMBNAIL_MAX_BYTES
) -> Path:
    """Return a webp copy of `source_path` under max_bytes.

    Applied to the final thumbnail regardless of how it was produced (video frame
    extraction or scene segmentation), so the uploaded preview is always small. If the
    source is already small enough it is returned unchanged; if it cannot be read, the
    original is returned so the pipeline never fails over a thumbnail.
    """

    try:
        if source_path.exists() and source_path.stat().st_size <= max_bytes:
            return source_path
        image = cv2.imread(str(source_path), cv2.IMREAD_COLOR)
        if image is None:
            return source_path
        output_dir.mkdir(parents=True, exist_ok=True)
        destination = output_dir / f"{source_path.stem}_lt100k.webp"
        result = _encode_thumbnail_under_limit(image, destination, max_bytes=max_bytes)
        return result if result is not None else source_path
    except Exception:
        logger.warning("Thumbnail compression failed for %s; using original.", source_path, exc_info=True)
        return source_path


# A frame this uniform is treated as blank (e.g. a black fade-in frame) and skipped.
_THUMBNAIL_MIN_CONTENT_STD = 12.0


def _pick_representative_frame(video_path: Path):
    """Return the earliest non-black frame, matching the v1 thumbnail behavior.

    v1 (scene segmentation) thumbnails are the first non-black frame from the start
    of the video. To keep v2 consistent — the split finalize worker falls back to
    this when the scene-segmentation thumbnail file isn't local — sample from the
    front going forward and return the first frame with meaningful variance (i.e.
    skip a black fade-in, but otherwise take the opening frame). Falls back to the
    highest-variance sample if every sample is dark. Returns None only if no frame
    can be read.
    """

    capture = cv2.VideoCapture(str(video_path))
    try:
        total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        positions = (
            [
                int(total * f)
                for f in (0.0, 0.02, 0.05, 0.1, 0.15, 0.25, 0.35, 0.5, 0.65, 0.8)
            ]
            if total > 1
            else [0]
        )
        best = None
        best_std = -1.0
        for pos in positions:
            if total > 1:
                capture.set(cv2.CAP_PROP_POS_FRAMES, min(total - 1, max(0, pos)))
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            std = float(frame.std())
            if std > best_std:
                best_std, best = std, frame
            if std >= _THUMBNAIL_MIN_CONTENT_STD:
                return frame  # earliest non-black frame; matches v1
        return best
    finally:
        capture.release()


def _generate_thumbnail_from_video(video_path: Path, output_dir: Path) -> Optional[Path]:
    if not video_path.exists():
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    thumbnail_path = output_dir / f"{video_path.stem}_thumbnail.webp"

    frame = _pick_representative_frame(video_path)
    if frame is not None:
        result = _encode_thumbnail_under_limit(frame, thumbnail_path)
        if result is not None:
            return result

    # Last resort: let ffmpeg extract a single frame (downscaled), then enforce budget.
    try:
        subprocess.run(
            [
                resolve_ffmpeg_binary(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-an",
                "-vf",
                f"scale='if(gt(iw,ih),min({THUMBNAIL_MAX_DIM},iw),-2)':"
                f"'if(gt(iw,ih),-2,min({THUMBNAIL_MAX_DIM},ih))'",
                "-quality",
                "80",
                str(thumbnail_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if thumbnail_path.exists() and thumbnail_path.stat().st_size > 0:
            return _compress_thumbnail_under_limit(thumbnail_path, output_dir)
    except Exception:
        logger.warning("ffmpeg thumbnail extraction failed for %s", video_path, exc_info=True)
    return None


def _asset_id_from_request(request: dict[str, Any], name: str, fallback: str) -> str:
    value = (request.get(name) or "").strip() if isinstance(request.get(name), str) else ""
    return value or fallback


class VideoMusicMonolithWorker:
    """Worker that executes the full video-music workflow in one leased task.

    The monolith path intentionally keeps the established business workflow
    intact: after resolving and optionally compressing the source video, it
    delegates to `VideoGenerationOrchestrator.run()` for analysis, provider
    generation, matching, remixing, and response construction. Shared
    source-video preparation is isolated behind `VideoSourcePreparationService`
    so future split workers can reuse the same durable artifact behavior
    without changing the orchestration logic here.
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
        queue_name: str = "video-music-pipeline",
        lease_seconds: int = 900,
    ) -> None:
        self.repository = repository
        self.queue = queue
        self.orchestrator = orchestrator
        self.settings = settings
        self.storage = storage
        self.worker_id = worker_id or f"worker-monolith-{uuid4().hex}"
        self.queue_name = queue_name
        self.lease_seconds = lease_seconds
        self.source_preparation = VideoSourcePreparationService(
            repository=repository,
            settings=settings,
            storage=storage,
            stage_name="video_music_monolith",
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
            job = self.repository.get_job(task.job_id)
            if job is None:
                raise KeyError(f"Job not found for task {task.task_id}: {task.job_id}")

            # Completed-job guard (belt to the reaper's suspenders): if this task's
            # job already COMPLETED — e.g. a requeued orphan whose original worker
            # recovered and finished the job in the meantime — never re-run the paid
            # generation. Close the task; the billed result and its callback already
            # exist (so do NOT re-fire the webhook).
            if (getattr(job, "status", None) == JobStatus.COMPLETED
                    and getattr(job, "result_json", None) is not None):
                self.repository.add_event(
                    job_id=task.job_id,
                    event_type="task.skipped",
                    stage_name="video_music_monolith",
                    message="Task skipped because its job is already completed.",
                    payload_json={"task_id": task.task_id},
                )
                return self.queue.complete(
                    task_id=task.task_id, worker_id=self.worker_id
                )

            source_artifact_id = (
                task.payload_json.get("source_video_artifact_id")
                or job.request_json.get("source_video_artifact_id")
            )
            if not source_artifact_id:
                raise ValueError("video_music_monolith task requires source_video_artifact_id.")

            source_artifact = self.repository.get_artifact(str(source_artifact_id))
            if source_artifact is None:
                raise KeyError(f"Source video artifact not found: {source_artifact_id}")

            stage_run = self.repository.start_stage_run(
                job_id=task.job_id,
                task_id=task.task_id,
                stage_name="video_music_monolith",
                attempt=task.attempt,
                input_json={
                    "task_type": task.task_type,
                    "source_video_artifact_id": source_artifact.artifact_id,
                    "request": job.request_json,
                },
            )
            self.repository.update_job_status(
                task.job_id,
                status=JobStatus.PROCESSING,
                current_stage="video_music_monolith",
                progress_percent=20,
            )
            self.repository.add_event(
                job_id=task.job_id,
                event_type="stage.started",
                stage_name="video_music_monolith",
                message="Monolithic video music workflow started.",
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
                source_video_path = await self._resolve_source_video(source_artifact)
                # Input guardrails on the ORIGINAL resolved file, before
                # compression: violations fail the job with their specific
                # error code (10005/10006/10007) without burning ffmpeg CPU.
                await asyncio.to_thread(validate_source_video_file, source_video_path)
                (
                    workflow_source_video_path,
                    workflow_source_artifact,
                    compression_info,
                ) = await self._prepare_source_video_for_workflow(
                    job_id=task.job_id,
                    request=job.request_json,
                    source_artifact=source_artifact,
                    source_video_path=source_video_path,
                )
                result = await self._run_workflow(
                    job_id=task.job_id,
                    request=job.request_json,
                    source_video_path=workflow_source_video_path,
                )
                response = self._persist_result_assets_and_build_response(
                    job_id=task.job_id,
                    request=job.request_json,
                    source_artifact=workflow_source_artifact,
                    source_video_path=workflow_source_video_path,
                    result=result,
                )
                result_json = response.model_dump(mode="json")

            # Atomic terminal commit: stage completion, job status (+result_json),
            # the job.completed event, and task completion succeed or roll back
            # together, so a crash can never strand the job with the result lost.
            with self.repository.transaction() as client:
                self.repository.update_stage_run(
                    stage_run.stage_run_id,
                    status=StageStatus.COMPLETED,
                    output_json={
                        "video_url": result_video_url(result_json),
                        "audio_url": result_audio_url(result_json),
                        "result_artifact_types": [
                            "matched_audio",
                            "complete_audio",
                            "secondary_complete_audio",
                            "remixed_video",
                            "thumbnail",
                        ],
                        "compression": compression_info,
                    },
                    finished=True,
                    client=client,
                )
                _, completed_applied = self.repository.update_job_status_checked(
                    task.job_id,
                    status=JobStatus.COMPLETED,
                    current_stage="video_music_monolith",
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
                        stage_name="video_music_monolith",
                        message="Monolithic video music workflow completed.",
                        payload_json={
                            "video_url": result_video_url(result_json),
                            "audio_url": result_audio_url(result_json),
                            "compression": compression_info,
                        },
                        client=client,
                    )
                else:
                    logger.warning(
                        "job %s already terminal; completed write skipped", task.job_id
                    )
                completed_task = self.queue.complete(
                    task_id=task.task_id,
                    worker_id=self.worker_id,
                    client=client,
                )
            # Best-effort client webhook AFTER the job is committed complete —
            # only when this worker's terminal write actually landed.
            if completed_applied:
                record_v2_job_usage(
                    job, status="completed", endpoint="/api/v2/jobs/video-music",
                    result=result, result_json=result_json,
                )
                await deliver_job_callback(
                    job, status=JobStatus.COMPLETED,
                    result_json=result_json, error_json=None, logger=logger,
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
            # Internal-only detail for the queue's retry/dead-letter bookkeeping
            # (never surfaced through the job status/events API).
            internal_error = {
                "message": str(exc),
                "type": type(exc).__name__,
                "retryable": retryable,
            }
            logger.exception("video_music_monolith task %s failed: %s", task.task_id, exc)
            # Atomic failure bookkeeping: the task's terminal state and the job's
            # terminal (or requeued) state commit or roll back together, so a crash
            # can never strand the job non-terminal while its task is dead-lettered.
            # A lost-lease KeyError from fail() propagates out to roll back cleanly;
            # the loop guard catches it and continues.
            deliver_failed = False
            with self.repository.transaction() as client:
                if stage_run is not None:
                    self.repository.update_stage_run(
                        stage_run.stage_run_id,
                        status=StageStatus.FAILED,
                        error_json=error_json,
                        finished=True,
                        client=client,
                    )
                self.repository.add_event(
                    job_id=task.job_id,
                    event_type="stage.failed",
                    stage_name="video_music_monolith",
                    message="Monolithic video music workflow failed.",
                    payload_json={"task_id": task.task_id, "error": error_json},
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
                        current_stage="video_music_monolith",
                        error_json=error_json,
                        finished=True,
                        client=client,
                    )
                    if failed_applied:
                        self.repository.add_event(
                            job_id=task.job_id,
                            event_type="job.failed",
                            stage_name="video_music_monolith",
                            message="Monolithic video music workflow failed permanently.",
                            payload_json={"task_id": task.task_id, "error": error_json},
                            client=client,
                        )
                    deliver_failed = failed_applied
                else:
                    self.repository.update_job_status(
                        task.job_id,
                        status=JobStatus.QUEUED,
                        current_stage="video_music_monolith",
                        error_json=error_json,
                        client=client,
                    )
            # Best-effort client webhook only on PERMANENT failure, AFTER commit.
            if deliver_failed:
                record_v2_job_usage(
                    job, status="failed", endpoint="/api/v2/jobs/video-music",
                )
                await deliver_job_callback(
                    job, status=JobStatus.FAILED,
                    result_json=None, error_json=error_json, logger=logger,
                )
            return failed_task

    async def _resolve_source_video(self, artifact: AsyncV2Artifact) -> Path:
        # Bound the download by the input-size guardrail so an oversized
        # video_url source aborts with 10005 instead of filling worker disk.
        return await self.source_preparation.resolve_source_video(
            artifact, max_bytes=MAX_SOURCE_VIDEO_BYTES
        )

    async def _prepare_source_video_for_workflow(
        self,
        *,
        job_id: str,
        request: dict[str, Any],
        source_artifact: AsyncV2Artifact,
        source_video_path: Path,
    ) -> tuple[Path, AsyncV2Artifact, dict[str, Any]]:
        prepared = await self.source_preparation.prepare_for_workflow(
            job_id=job_id,
            request=request,
            source_artifact=source_artifact,
            source_video_path=source_video_path,
        )
        return prepared.path, prepared.artifact, prepared.compression_info

    async def _run_workflow(
        self,
        *,
        job_id: str,
        request: dict[str, Any],
        source_video_path: Path,
    ) -> VideoGenerationResult:
        asset_ids = RecommendationAssetIds.create(job_id=job_id)
        video_id = _asset_id_from_request(request, "video_id", asset_ids.video_id)
        creative_id = _asset_id_from_request(request, "creative_id", asset_ids.creative_id)
        primary_music_id = _asset_id_from_request(
            request,
            "primary_music_id",
            asset_ids.primary_music_id,
        )
        secondary_music_id = request.get("secondary_music_id") or asset_ids.secondary_music_id
        selected_music_id = _asset_id_from_request(
            request,
            "selected_music_id",
            primary_music_id,
        )
        alignment_id = _asset_id_from_request(request, "alignment_id", asset_ids.alignment_id)

        return await self.orchestrator.run(
            video_path=source_video_path,
            preserve_original_audio=bool(request.get("preserve_original_audio", False)),
            music_volume=float(request.get("music_volume", 1.0) or 1.0),
            water_mark=bool(request.get("water_mark", False)),
            include_vocals=bool(request.get("include_vocals", False)),
            vocal_gender=str(request.get("vocal_gender") or "female"),
            user_prompt=str(request.get("user_prompt") or ""),
            verbose_instruction=bool(request.get("verbose_instruction", False)),
            music_style_prompt=request.get("music_style_prompt") or None,
            lyrics_prompt=request.get("lyrics_prompt") or None,
            language=request.get("language") or None,
            modelspec=str(request.get("modelspec") or request.get("music_model_spec") or "edenn_basic"),
            audio_output_format=request.get("audio_output_format") or None,
            vocal_id=request.get("vocal_id") or None,
            vocal_sample_path=(
                Path(request["vocal_sample_path"])
                if request.get("vocal_sample_path")
                else None
            ),
            job_id=job_id,
            video_id=video_id,
            creative_id=creative_id,
            primary_music_id=primary_music_id,
            secondary_music_id=secondary_music_id,
            selected_music_id=selected_music_id,
            alignment_id=alignment_id,
        )

    def _persist_result_assets_and_build_response(
        self,
        *,
        job_id: str,
        request: dict[str, Any],
        source_artifact: AsyncV2Artifact,
        source_video_path: Path,
        result: VideoGenerationResult,
    ) -> VideoJobResponse:
        upload_blob = source_artifact.blob_name
        upload_url = source_artifact.url
        audio_blob = audio_url = None
        complete_audio_blob = complete_audio_url = None
        secondary_complete_audio_blob = secondary_complete_audio_url = None
        video_blob = video_url = None
        thumbnail_blob = thumbnail_url = None

        audio_blob, audio_url = self._record_output_artifact(
            job_id=job_id,
            artifact_type="matched_audio",
            role="primary",
            path=Path(result.generated_music_path),
            container=getattr(self.settings, "audio_container_name"),
            folder="audio",
            label="matched_audio",
            content_type=guess_audio_content_type(result.generated_music_path),
        )

        complete_music_path = getattr(result, "complete_generated_music_path", None)
        if complete_music_path and Path(complete_music_path).exists():
            complete_audio_blob, complete_audio_url = self._record_output_artifact(
                job_id=job_id,
                artifact_type="complete_audio",
                role="primary",
                path=Path(complete_music_path),
                container=getattr(self.settings, "audio_container_name"),
                folder="audio/complete",
                label="complete_audio",
                content_type=guess_audio_content_type(complete_music_path),
            )

        secondary_complete_music_path = getattr(
            result,
            "secondary_complete_generated_music_path",
            None,
        )
        if secondary_complete_music_path and Path(secondary_complete_music_path).exists():
            secondary_complete_audio_blob, secondary_complete_audio_url = self._record_output_artifact(
                job_id=job_id,
                artifact_type="secondary_complete_audio",
                role="secondary",
                path=Path(secondary_complete_music_path),
                container=getattr(self.settings, "audio_container_name"),
                folder="audio/complete/secondary",
                label="secondary_audio",
                content_type=guess_audio_content_type(secondary_complete_music_path),
            )

        # Probe the tracks from local files (before cleanup).
        audio_duration_s, audio_size_bytes = probe_audio_metrics(
            Path(result.generated_music_path)
        )
        complete_audio_duration_s, complete_audio_size_bytes = probe_audio_metrics(
            Path(complete_music_path)
            if complete_music_path and Path(complete_music_path).exists()
            else None
        )

        video_blob, video_url = self._record_output_artifact(
            job_id=job_id,
            artifact_type="remixed_video",
            role="final",
            path=Path(result.remixed_video_path),
            container=getattr(self.settings, "output_container"),
            folder="video",
            label="remixed_video",
            content_type="video/mp4",
        )

        thumbnail_path = getattr(result, "thumbnail_path", None)
        thumbnail_path = Path(thumbnail_path) if thumbnail_path else None
        if thumbnail_path is None or not thumbnail_path.exists():
            thumbnail_path = _generate_thumbnail_from_video(
                source_video_path,
                source_video_path.parent,
            )
        if thumbnail_path and thumbnail_path.exists():
            # Guarantee the uploaded preview is under the size budget regardless of
            # whether it came from scene segmentation or frame extraction.
            thumbnail_path = _compress_thumbnail_under_limit(
                thumbnail_path, thumbnail_path.parent
            )
        if thumbnail_path and thumbnail_path.exists():
            thumbnail_blob, thumbnail_url = self._record_output_artifact(
                job_id=job_id,
                artifact_type="thumbnail",
                role="thumbnail",
                path=thumbnail_path,
                container=getattr(self.settings, "output_container"),
                folder="thumbnail",
                label="thumbnail",
                content_type=guess_image_content_type(thumbnail_path),
            )

        return build_video_job_response(
            job_id=job_id,
            result=result,
            requested_modelspec=str(request.get("modelspec") or "edenn_basic"),
            assets=VideoJobAssets(
                audio_url=audio_url,
                audio_duration_s=audio_duration_s,
                audio_size_bytes=audio_size_bytes,
                complete_audio_url=complete_audio_url,
                complete_audio_duration_s=complete_audio_duration_s,
                complete_audio_size_bytes=complete_audio_size_bytes,
                video_url=video_url,
                thumbnail_url=thumbnail_url,
            ),
        )

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
            stage_name="video_music_monolith",
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


async def run_worker_loop(
    *,
    worker: VideoMusicMonolithWorker,
    once: bool = False,
    poll_interval_seconds: float = 2.0,
    shutdown_event: Optional[asyncio.Event] = None,
) -> None:
    await run_consumer_loop(
        process_one=worker.process_one,
        once=once,
        poll_interval_seconds=poll_interval_seconds,
        shutdown_event=shutdown_event,
        logger=logger,
        label=f"video-monolith:{getattr(worker, 'queue_name', 'worker')}",
    )


__all__ = ["VideoMusicMonolithWorker", "run_worker_loop"]
