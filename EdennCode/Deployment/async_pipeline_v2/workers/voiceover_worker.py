from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from EdennCode.Deployment.api_video_generation import _provider_neutral_blob_name
from EdennCode.Deployment.error_codes import public_error_payload
from EdennCode.Deployment.async_pipeline_v2.models import (
    AsyncV2Task,
    JobStatus,
    StageStatus,
)
from EdennCode.Deployment.async_pipeline_v2.artifact_service import upload_path_with_retry
from EdennCode.Deployment.async_pipeline_v2.queue.postgres_queue import PostgresTaskQueue
from EdennCode.Deployment.async_pipeline_v2.repositories import AsyncPipelineV2Repository
from EdennCode.Deployment.async_pipeline_v2.worker_heartbeat import StageLeaseHeartbeat


logger = logging.getLogger(__name__)

STAGE_NAME = "voiceover"


class DefaultVoiceoverSynthesizer:
    """Real TTS synthesizer backed by ``AzureModelGatewayTTS4OMini`` (lazy, off-thread)."""

    def __init__(self) -> None:
        self._tts: Any = None

    async def synthesize(
        self,
        *,
        script: str,
        voice: str,
        instructions: str,
        speed: float,
        out_path: Path,
    ) -> Path:
        if self._tts is None:
            from EdennCode.ModelFactory.VoiceOverModelFactory.model_gateway_base_model import (
                AzureModelGatewayTTS4OMini,
            )

            self._tts = AzureModelGatewayTTS4OMini()
        return await asyncio.to_thread(
            self._tts.send_request_streaming,
            instructions,
            speed,
            script,
            voice,
            out_path,
        )


class VoiceoverWorker:
    """Worker that TTS-es an approved voice-over script into narration audio.

    Consumes the ``voiceover`` task enqueued by the agentic
    ``generate_voiceover`` tool. The TTS step is injectable (``synthesizer``) so
    tests stay hermetic. Composing the narration onto the original video with the
    music layer is a later sub-iteration (compose_mix).
    """

    def __init__(
        self,
        *,
        repository: AsyncPipelineV2Repository,
        queue: PostgresTaskQueue,
        settings: Any,
        storage: Any = None,
        synthesizer: Any = None,
        worker_id: Optional[str] = None,
        queue_name: str = "voiceover-pipeline",
        lease_seconds: int = 600,
    ) -> None:
        self.repository = repository
        self.queue = queue
        self.settings = settings
        self.storage = storage
        self.synthesizer = synthesizer or DefaultVoiceoverSynthesizer()
        self.worker_id = worker_id or f"worker-voiceover-{uuid4().hex}"
        self.queue_name = queue_name
        self.lease_seconds = lease_seconds

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
            script = str(request.get("script") or "").strip()
            if not script:
                raise ValueError("voiceover task requires a non-empty script.")

            stage_run = self.repository.start_stage_run(
                job_id=task.job_id,
                task_id=task.task_id,
                stage_name=STAGE_NAME,
                attempt=task.attempt,
                input_json={"task_type": task.task_type, "voice_id": request.get("voice_id")},
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
                message="Voice-over synthesis started.",
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
                result_json = await self._run(job_id=task.job_id, request=request)

            self.repository.update_stage_run(
                stage_run.stage_run_id,
                status=StageStatus.COMPLETED,
                output_json={"audio_url": result_json.get("audio_url")},
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
                    message="Voice-over synthesis completed.",
                    payload_json={"audio_url": result_json.get("audio_url")},
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
            internal_error = {"message": str(exc), "type": type(exc).__name__, "retryable": retryable}
            logger.exception("voiceover task %s failed: %s", task.task_id, exc)
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
                message="Voice-over synthesis failed.",
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
            else:
                self.repository.update_job_status(
                    task.job_id,
                    status=JobStatus.QUEUED,
                    current_stage=STAGE_NAME,
                    error_json=error_json,
                )
            return failed_task

    async def _run(self, *, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        workdir = (
            Path(getattr(self.settings, "workdir", tempfile.gettempdir()))
            / "agentic_voiceover"
            / job_id
        )
        workdir.mkdir(parents=True, exist_ok=True)
        out_path = workdir / "voiceover.wav"

        # A timed plan is rendered line by line and placed against the picture.
        # This path used to flat-synthesize the concatenated script and return no
        # placements, so every start time, delivery direction and deliberate
        # silence the director chose was discarded here — the plan was real
        # everywhere except where it mattered. The rendering itself is shared
        # with the local design server so the two cannot drift apart again.
        segments = [
            s for s in (request.get("segments") or [])
            if isinstance(s, dict) and str(s.get("text") or "").strip()
        ]
        realized: Optional[list[dict[str, Any]]] = None
        if segments:
            from EdennCode.EdennAgent.AgenticAudio.tools.narration_render import (
                render_segmented_narration,
            )

            realized = await render_segmented_narration(
                segments=segments,
                synthesize=self.synthesizer.synthesize,
                voice=str(request.get("tts_voice") or "shimmer"),
                base_instructions=str(request.get("tts_instructions") or ""),
                default_speed=float(request.get("speed") or 1.0),
                workdir=workdir / "lines",
                out_path=out_path,
                video_duration_s=float(request.get("video_duration_s") or 0.0),
                cuts=[float(c) for c in (request.get("cuts") or [])],
                avoid_windows=[list(w) for w in (request.get("speech_windows") or [])],
                log=logger.info,
            )
        if realized is None:
            # No usable timed plan (or none given): the flat script is the
            # honest fallback, not a silent failure.
            await self.synthesizer.synthesize(
                script=str(request.get("script") or ""),
                voice=str(request.get("tts_voice") or "shimmer"),
                instructions=str(request.get("tts_instructions") or ""),
                speed=float(request.get("speed") or 1.0),
                out_path=out_path,
            )

        audio_blob = audio_url = None
        if self.storage is not None and getattr(self.storage, "enabled", False):
            container = getattr(self.settings, "audio_container_name", "generated-audio")
            audio_blob = upload_path_with_retry(
                self.storage,
                container=container,
                path=out_path,
                blob_name=_provider_neutral_blob_name(
                    job_id=job_id,
                    folder="audio/voiceover",
                    label="voiceover",
                    source_path=out_path,
                ),
                content_type="audio/wav",
            )
            self.repository.add_artifact(
                artifact_id=f"{job_id}:voiceover_audio:primary",
                job_id=job_id,
                artifact_type="voiceover_audio",
                role="primary",
                container=container if audio_blob else None,
                blob_name=audio_blob,
                url=(
                    self.storage.generate_sas_url(container=container, blob_name=audio_blob)
                    if audio_blob and hasattr(self.storage, "generate_sas_url")
                    else None
                ),
                content_type="audio/wav",
                local_path=str(out_path),
                metadata_json={"voice_id": request.get("voice_id")},
            )
            if audio_blob and hasattr(self.storage, "generate_sas_url"):
                audio_url = self.storage.generate_sas_url(
                    container=container, blob_name=audio_blob
                )

        return {
            "status": "completed",
            "audio_url": audio_url,
            "audio_blob": audio_blob,
            "voice_id": request.get("voice_id"),
            "language": request.get("language"),
            "agentic_voiceover_id": request.get("agentic_voiceover_id"),
            # The REALIZED placements. Without these the layer never resolves its
            # timed plan, so the mix cannot duck per line and the alignment
            # report has nothing to measure.
            **({"segments": realized} if realized else {}),
        }


async def run_voiceover_worker_loop(
    *,
    worker: VoiceoverWorker,
    once: bool = False,
    poll_interval_seconds: float = 2.0,
) -> None:
    while True:
        task = await worker.process_one()
        if once:
            return
        if task is None:
            await asyncio.sleep(poll_interval_seconds)


__all__ = ["VoiceoverWorker", "DefaultVoiceoverSynthesizer", "run_voiceover_worker_loop"]
