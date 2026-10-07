from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.async_pipeline_v2.api import create_async_pipeline_v2_router
from EdennCode.Deployment.async_pipeline_v2.models import (
    AsyncV2Artifact,
    AsyncV2Job,
    AsyncV2JobEvent,
    AsyncV2StageRun,
    AsyncV2Task,
    JobStatus,
    StageStatus,
    TaskEnvelope,
    TaskStatus,
    new_id,
)
from EdennCode.Deployment.async_pipeline_v2.queue_names import namespaced_queue_name
from EdennCode.Deployment.async_pipeline_v2.queue.postgres_queue import PostgresTaskQueue
from EdennCode.Deployment.async_pipeline_v2.repositories import AsyncPipelineV2Repository
from EdennCode.Deployment.async_pipeline_v2.stages.video_music_split import (
    AnalysisAndPlanningStageOutput,
    ProviderCandidateGenerationStageOutput,
    SelectionRankingRemixFinalizeStageInput,
    SelectionRankingRemixFinalizeStageOutput,
    VideoMusicAnalysisAndPlanningStage,
    VideoMusicSelectionRankingRemixFinalizeStage,
    VideoMusicProviderCandidateGenerationStage,
    analysis_output_to_json,
    candidate_output_to_json,
)
from EdennCode.Deployment.async_pipeline_v2.worker_main import build_split_workers
from EdennCode.Deployment.async_pipeline_v2.workers.split_stage_workers import (
    AnalysisAndPlanningWorker,
    BasicMusicGenerationWorker,
    EnhancedMusicGenerationWorker,
    ProviderCandidateGenerationWorker,
    SelectionRankingRemixFinalizeWorker,
    StageTaskResult,
    StudioMusicGenerationWorker,
    VideoPreprocessWorker,
    VideoMusicSplitStageRuntime,
    _SplitStageWorkerBase,
    provider_queue_name_for_modelspec,
)
from EdennCode.Deployment.postgres_wrapper import PostgresClient
from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Deployment.storage import create_storage_service
from EdennCode.Deployment.workflows import VideoGenerationResult
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata import VideoMetadata
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicGenerationStage.music_generation_stage import (
    MusicGenerationStage,
    MusicGenertionModelEnum,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.music_matching_stage import (
    MusicMatchingStageOutput,
)
from EdennCode.Util.MediaUtils.ffmpeg_utils import resolve_ffmpeg_binary
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoPreprocessStage.media_preprocess_stage import (
    PreprocessStage,
)
from EdennCode.env import load_env


# 20.6s / 320x568 (portrait) / 2.3MB — must satisfy the source-video input
# guardrails (>15s, <=150s, <=300MB) because these tests exercise guarded paths.
SMOKE_VIDEO = Path(
    "EdennCode/TestSuites/assets/smoke/videos/Videos2026-04-08_104426_347.mp4"
)
# 5.2s — deliberately below the 15s minimum, for guardrail rejection tests.
SMOKE_SHORT_VIDEO = Path(
    "EdennCode/TestSuites/assets/smoke/videos/"
    "sample_clip.mp4"
)


def _postgres_env_available() -> bool:
    load_env()
    return bool(
        os.getenv("DATABASE_URL")
        or (os.getenv("PGHOST") and os.getenv("PGDATABASE") and os.getenv("PGUSER"))
    )


def _storage_env_available() -> bool:
    load_env()
    provider = (os.getenv("MEDIA_STORAGE_PROVIDER") or "azure").strip().lower()
    if provider in {"china_cos", "cos", "tencent_cos", "tencent-cos"}:
        return bool(
            os.getenv("COS_SECRET_ID")
            and os.getenv("COS_SECRET_KEY")
            and os.getenv("COS_REGION")
            and os.getenv("COS_VIDEO_BUCKET")
            and os.getenv("COS_AUDIO_BUCKET")
            and os.getenv("COS_IMAGE_BUCKET")
        )
    return bool(
        os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        or os.getenv("AZURE_STORAGE_ACCOUNT_URL")
    )


def _real_provider_env_available(modelspec: str) -> bool:
    load_env()
    if modelspec == "edenn_basic":
        return bool(os.getenv("PROVIDER_A_API_KEY"))
    if modelspec == "edenn_enhanced":
        return bool(
            os.getenv("EDENN_ENHANCED_PROVIDER_B_API_KEY")
            or os.getenv("PROVIDER_B_API_KEY")
            or os.getenv("PROVIDER_B_API_KEY_1")
            or os.getenv("PROVIDER_B_API_KEY_2")
        )
    if modelspec == "edenn_studio":
        return bool(os.getenv("PROVIDER_C_API_KEY"))
    return False


def _save_split_payload(label: str, payload: dict) -> None:
    output_dir = (os.getenv("SAVE_ASYNC_V2_REAL_PROVIDER_PAYLOADS_DIR") or "").strip()
    if not output_dir:
        return
    target_dir = Path(output_dir).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"async_v2_split_real_provider_{label}_{int(time.time())}.json"
    target_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class _QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return None


@contextmanager
def _permit_loopback_asset_url(base_url: str):
    """Let the SSRF guard accept THIS fixture server's own origin, only.

    ``assert_public_asset_url`` (api_common.py:174) rejects loopback literals —
    correct in production, and fatal for a test whose whole point is to serve a
    real file over a real local HTTP server. So rather than switch the guard
    off, allow exactly the origin the fixture just bound and hand every other
    URL to the real implementation: an SSRF regression in the code under test
    still fails these tests.
    """
    from EdennCode.Deployment import api_common

    real = api_common.assert_public_asset_url

    def _guard(url: str, *, asset_label: str = "asset") -> str:
        if (url or "").startswith(base_url):
            return url
        return real(url, asset_label=asset_label)

    api_common.assert_public_asset_url = _guard
    try:
        yield
    finally:
        api_common.assert_public_asset_url = real


@contextmanager
def _serve_directory(directory: Path):
    handler = partial(_QuietStaticHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        with _permit_loopback_asset_url(base_url):
            yield base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _make_long_high_resolution_video(source: Path, destination: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required for long high-resolution split integration tests.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-stream_loop",
        "3",
        "-i",
        str(source),
        "-t",
        "20",
        "-vf",
        "scale=1920:1920",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(destination),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)


class _MonitoringPostgresTaskQueue:
    def __init__(self, delegate: PostgresTaskQueue) -> None:
        self.delegate = delegate
        self.transitions: list[dict[str, object]] = []

    def ensure_schema(self) -> None:
        self.delegate.ensure_schema()

    def _record(self, action: str, task: AsyncV2Task | None) -> None:
        if task is None:
            self.transitions.append({"action": action, "status": None})
            return
        self.transitions.append(
            {
                "action": action,
                "task_id": task.task_id,
                "queue_name": task.queue_name,
                "task_type": task.task_type,
                "job_id": task.job_id,
                "status": task.status,
                "attempt": task.attempt,
                "lease_owner": task.lease_owner,
            }
        )

    def enqueue(self, envelope: TaskEnvelope, **kwargs) -> str:
        task_id = self.delegate.enqueue(envelope, **kwargs)
        if kwargs.get("client") is not None:
            self._record(
                "enqueue",
                AsyncV2Task(
                    task_id=envelope.task_id,
                    job_id=envelope.job_id,
                    queue_name=envelope.queue_name,
                    task_type=envelope.task_type,
                    status=TaskStatus.QUEUED,
                    payload_json=envelope.payload_json,
                    priority=envelope.priority,
                    max_attempts=envelope.max_attempts,
                    idempotency_key=envelope.idempotency_key,
                    not_before=envelope.not_before,
                ),
            )
        else:
            self._record("enqueue", self.delegate.get_task(task_id))
        return task_id

    def get_task(self, task_id: str) -> AsyncV2Task | None:
        return self.delegate.get_task(task_id)

    def lease(self, **kwargs) -> AsyncV2Task | None:
        task = self.delegate.lease(**kwargs)
        self._record("lease", task)
        return task

    def heartbeat(self, **kwargs) -> AsyncV2Task:
        task = self.delegate.heartbeat(**kwargs)
        self._record("heartbeat", task)
        return task

    def complete(self, **kwargs) -> AsyncV2Task:
        task = self.delegate.complete(**kwargs)
        self._record("complete", task)
        return task

    def fail(self, **kwargs) -> AsyncV2Task:
        task = self.delegate.fail(**kwargs)
        self._record("fail", task)
        return task

    def requeue_expired_leases(self, **kwargs):
        return self.delegate.requeue_expired_leases(**kwargs)

    def cancel_job_tasks(self, **kwargs):
        return self.delegate.cancel_job_tasks(**kwargs)


class _RecordingStorage:
    enabled = True

    def __init__(self) -> None:
        self.upload_calls: list[dict] = []

    def upload_path(
        self,
        *,
        container: str,
        path: Path,
        blob_name: str | None = None,
        content_type: str | None = None,
    ):
        self.upload_calls.append(
            {
                "container": container,
                "path": path,
                "blob_name": blob_name,
                "content_type": content_type,
            }
        )
        return blob_name or path.name

    def generate_sas_url(
        self,
        *,
        container: str,
        blob_name: str,
        ttl_minutes: int | None = None,
        require_signed: bool = False,
    ):
        return f"https://storage.test/{container}/{blob_name}?sig=fake"


class _MemoryRepository:
    def __init__(self) -> None:
        self.jobs: dict[str, AsyncV2Job] = {}
        self.artifacts: dict[str, AsyncV2Artifact] = {}
        self.events: list[AsyncV2JobEvent] = []
        self.stage_runs: dict[str, AsyncV2StageRun] = {}

    def ensure_schema(self) -> None:
        return None

    @contextmanager
    def transaction(self):
        yield self

    def create_job(self, **kwargs):
        job = AsyncV2Job(
            job_id=kwargs["job_id"],
            job_type=kwargs["job_type"],
            status=kwargs.get("status") or JobStatus.QUEUED,
            request_json=kwargs["request_json"],
            session_id=kwargs.get("session_id"),
            creator_user_id=kwargs.get("creator_user_id"),
            priority=kwargs.get("priority") or 0,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self.jobs[job.job_id] = job
        return job

    def get_job(self, job_id: str):
        return self.jobs.get(job_id)

    def update_job_status(self, job_id: str, **kwargs):
        job = self.jobs[job_id]
        updated = replace(
            job,
            status=kwargs["status"],
            current_stage=kwargs.get("current_stage", job.current_stage),
            progress_percent=kwargs.get("progress_percent", job.progress_percent),
            result_json=kwargs.get("result_json", job.result_json),
            error_json=kwargs.get("error_json", job.error_json),
            updated_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc) if kwargs.get("finished") else job.finished_at,
        )
        self.jobs[job_id] = updated
        return updated

    def update_job_status_checked(self, job_id: str, **kwargs):
        job = self.jobs[job_id]
        if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED):
            return job, False
        return self.update_job_status(job_id, **kwargs), True

    def start_stage_run(self, **kwargs):
        stage_run = AsyncV2StageRun(
            stage_run_id=kwargs.get("stage_run_id") or new_id("stage_run"),
            job_id=kwargs["job_id"],
            task_id=kwargs.get("task_id"),
            stage_name=kwargs["stage_name"],
            attempt=kwargs.get("attempt") or 1,
            status=StageStatus.STARTED,
            input_json=kwargs.get("input_json") or {},
            started_at=datetime.now(timezone.utc),
        )
        self.stage_runs[stage_run.stage_run_id] = stage_run
        return stage_run

    def update_stage_run(self, stage_run_id: str, **kwargs):
        stage_run = self.stage_runs[stage_run_id]
        updated = replace(
            stage_run,
            status=kwargs.get("status", stage_run.status),
            output_json=kwargs.get("output_json", stage_run.output_json),
            error_json=kwargs.get("error_json", stage_run.error_json),
            heartbeat_at=datetime.now(timezone.utc) if kwargs.get("heartbeat") else stage_run.heartbeat_at,
            finished_at=datetime.now(timezone.utc) if kwargs.get("finished") else stage_run.finished_at,
        )
        self.stage_runs[stage_run_id] = updated
        return updated

    def add_artifact(self, **kwargs):
        artifact = AsyncV2Artifact(
            artifact_id=kwargs["artifact_id"],
            job_id=kwargs["job_id"],
            artifact_type=kwargs["artifact_type"],
            role=kwargs.get("role"),
            container=kwargs.get("container"),
            blob_name=kwargs.get("blob_name"),
            url=kwargs.get("url"),
            content_type=kwargs.get("content_type"),
            local_path=kwargs.get("local_path"),
            metadata_json=kwargs.get("metadata_json") or {},
            payload_json=kwargs.get("payload_json"),
            created_at=datetime.now(timezone.utc),
        )
        self.artifacts[artifact.artifact_id] = artifact
        return artifact

    def get_artifact(self, artifact_id: str):
        return self.artifacts.get(artifact_id)

    def list_artifacts(self, job_id: str):
        return [artifact for artifact in self.artifacts.values() if artifact.job_id == job_id]

    def add_event(self, **kwargs):
        kwargs.pop("client", None)
        event = AsyncV2JobEvent(
            event_id=kwargs.get("event_id") or new_id("event"),
            job_id=kwargs["job_id"],
            event_type=kwargs["event_type"],
            stage_name=kwargs.get("stage_name"),
            message=kwargs.get("message"),
            payload_json=kwargs.get("payload_json") or {},
            created_at=datetime.now(timezone.utc),
        )
        self.events.append(event)
        return event

    def list_events(self, job_id: str):
        return [event for event in self.events if event.job_id == job_id]


class _MemoryQueue:
    def __init__(self) -> None:
        self.tasks: dict[str, AsyncV2Task] = {}
        self.heartbeat_calls: list[dict] = []

    def ensure_schema(self) -> None:
        return None

    def enqueue(self, envelope: TaskEnvelope, *, client=None) -> str:
        existing = next(
            (
                task
                for task in self.tasks.values()
                if envelope.idempotency_key and task.idempotency_key == envelope.idempotency_key
            ),
            None,
        )
        if existing is not None:
            return existing.task_id
        task = AsyncV2Task(
            task_id=envelope.task_id,
            job_id=envelope.job_id,
            queue_name=envelope.queue_name,
            task_type=envelope.task_type,
            status=TaskStatus.QUEUED,
            payload_json=envelope.payload_json,
            priority=envelope.priority,
            max_attempts=envelope.max_attempts,
            idempotency_key=envelope.idempotency_key,
            not_before=envelope.not_before,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self.tasks[task.task_id] = task
        return task.task_id

    def lease(self, *, queue_name: str, worker_id: str, lease_seconds: int):
        for task in self.tasks.values():
            if task.queue_name == queue_name and task.status == TaskStatus.QUEUED:
                leased = replace(
                    task,
                    status=TaskStatus.LEASED,
                    lease_owner=worker_id,
                    attempt=task.attempt + 1,
                    updated_at=datetime.now(timezone.utc),
                )
                self.tasks[task.task_id] = leased
                return leased
        return None

    def complete(self, *, task_id: str, worker_id: str, client=None):
        task = self.tasks[task_id]
        completed = replace(
            task,
            status=TaskStatus.COMPLETED,
            updated_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )
        self.tasks[task_id] = completed
        return completed

    def heartbeat(self, *, task_id: str, worker_id: str, lease_seconds: int):
        task = self.tasks[task_id]
        if task.status != TaskStatus.LEASED or task.lease_owner != worker_id:
            raise KeyError(task_id)
        self.heartbeat_calls.append(
            {
                "task_id": task_id,
                "worker_id": worker_id,
                "lease_seconds": lease_seconds,
            }
        )
        updated = replace(
            task,
            updated_at=datetime.now(timezone.utc),
        )
        self.tasks[task_id] = updated
        return updated

    def fail(self, *, task_id: str, worker_id: str, error: dict, retry: bool, backoff_seconds: int, client=None):
        task = self.tasks[task_id]
        status = TaskStatus.QUEUED if retry and task.attempt < task.max_attempts else TaskStatus.FAILED
        failed = replace(
            task,
            status=status,
            last_error_json=error,
            updated_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc) if status == TaskStatus.FAILED else None,
        )
        self.tasks[task_id] = failed
        return failed

    def get_task(self, task_id: str):
        return self.tasks.get(task_id)

    def cancel_job_tasks(self, *, job_id: str) -> int:
        return 0


class _TransactionalHandoffWorker(_SplitStageWorkerBase):
    """Minimal split worker used to test persistence handoff mechanics only."""

    stage_name = "transactional_handoff"
    task_type = "transactional_handoff"
    queue_name = "transactional-handoff"

    def __init__(self, *args, next_task: TaskEnvelope | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.next_task = next_task

    async def _run_stage(self, *, job: AsyncV2Job, task: AsyncV2Task) -> StageTaskResult:
        return StageTaskResult(
            output_json={"handoff": "ok", "attempt": task.attempt},
            next_task=self.next_task,
            job_status=JobStatus.QUEUED,
            progress_percent=55,
        )


def test_provider_queue_name_for_modelspec_routes_to_isolated_queues() -> None:
    assert provider_queue_name_for_modelspec("edenn_basic") == "music-basic"
    assert provider_queue_name_for_modelspec("edenn_enhanced") == "music-enhanced"
    assert provider_queue_name_for_modelspec("edenn_studio") == "music-studio"
    assert provider_queue_name_for_modelspec("unknown") == "music-basic"


def test_json_artifact_records_db_payload_and_loads_without_local_file(tmp_path: Path) -> None:
    job_id = "job_json_payload_local"
    repo = _MemoryRepository()
    queue = _MemoryQueue()
    repo.create_job(
        job_id=job_id,
        job_type="video_music",
        request_json={"source_video_artifact_id": f"{job_id}:source_video:input"},
    )
    worker = _TransactionalHandoffWorker(
        repository=repo,  # type: ignore[arg-type]
        queue=queue,  # type: ignore[arg-type]
        runtime=SimpleNamespace(),  # type: ignore[arg-type]
        settings=SimpleNamespace(workdir=tmp_path),
        worker_id="json-payload-test",
    )
    payload = {
        "job_id": job_id,
        "stage": "analysis",
        "nested": {"scene_count": 2, "modelspec": "edenn_basic"},
    }

    artifact = worker._record_json_artifact(
        job_id=job_id,
        artifact_id=f"{job_id}:analysis_plan:v1",
        artifact_type="analysis_plan",
        role="primary",
        payload=payload,
    )
    Path(artifact.local_path).unlink()

    assert artifact.payload_json == payload
    assert artifact.metadata_json["payload_stored_in_db"] is True
    assert worker._load_json_artifact(artifact) == payload


def test_async_json_artifact_loader_uses_db_payload_without_local_file_or_url(tmp_path: Path) -> None:
    job_id = "job_json_payload_async"
    repo = _MemoryRepository()
    queue = _MemoryQueue()
    payload = {
        "job_id": job_id,
        "provider_name": "provider_c",
        "task_id": "provider_task_123",
    }
    artifact = repo.add_artifact(
        artifact_id=f"{job_id}:provider_task:primary_generation",
        job_id=job_id,
        artifact_type="provider_task",
        role="primary_generation",
        content_type="application/json",
        metadata_json={"payload_stored_in_db": True},
        payload_json=payload,
    )
    worker = _TransactionalHandoffWorker(
        repository=repo,  # type: ignore[arg-type]
        queue=queue,  # type: ignore[arg-type]
        runtime=SimpleNamespace(),  # type: ignore[arg-type]
        settings=SimpleNamespace(workdir=tmp_path),
        worker_id="json-payload-async-test",
    )

    assert asyncio.run(worker._load_json_artifact_async(artifact)) == payload


@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1" or not _postgres_env_available(),
    reason="Set RUN_ASYNC_V2_POSTGRES_INTEGRATION=1 with Postgres env to run.",
)
def test_postgres_split_worker_success_handoff_commits_next_task_and_current_completion() -> None:
    suffix = uuid.uuid4().hex
    job_id = f"test_async_v2_split_handoff_job_{suffix}"
    current_task_id = f"{job_id}:transactional-handoff"
    next_task_id = f"{job_id}:next-stage"
    worker_id = f"worker_split_handoff_{suffix}"
    repo = AsyncPipelineV2Repository()
    queue = PostgresTaskQueue()
    repo.ensure_schema()

    try:
        repo.create_job(
            job_id=job_id,
            job_type="video_music",
            request_json={
                "source_video_artifact_id": f"{job_id}:source_video:input",
                "modelspec": "edenn_basic",
                "mode": "split",
            },
            priority=6,
        )
        queue.enqueue(
            TaskEnvelope(
                task_id=current_task_id,
                job_id=job_id,
                queue_name="transactional-handoff",
                task_type="transactional_handoff",
                payload_json={},
                priority=6,
                max_attempts=3,
                idempotency_key=f"{job_id}:transactional-handoff:v1",
            )
        )
        worker = _TransactionalHandoffWorker(
            repository=repo,
            queue=queue,
            runtime=SimpleNamespace(),
            settings=SimpleNamespace(workdir="/tmp"),
            worker_id=worker_id,
            lease_seconds=120,
            next_task=TaskEnvelope(
                task_id=next_task_id,
                job_id=job_id,
                queue_name="next-stage",
                task_type="next_stage",
                payload_json={"from_task_id": current_task_id},
                priority=6,
                max_attempts=3,
                idempotency_key=f"{job_id}:next-stage:v1",
            ),
        )

        processed = asyncio.run(worker.process_one())

        assert processed is not None
        assert processed.status == TaskStatus.COMPLETED
        current_task = queue.get_task(current_task_id)
        next_task = queue.get_task(next_task_id)
        assert current_task is not None
        assert current_task.status == TaskStatus.COMPLETED
        assert next_task is not None
        assert next_task.status == TaskStatus.QUEUED
        status_view = repo.build_status_view(job_id)
        assert status_view["status"] == JobStatus.QUEUED
        assert status_view["current_stage"] == "transactional_handoff"
        assert status_view["progress_percent"] == 55
        assert status_view["stages"][0]["status"] == StageStatus.COMPLETED
        event_types = [event.event_type for event in repo.list_events(job_id)]
        assert event_types[0] == "stage.started"
        assert sorted(event_types[1:]) == [
            "stage.completed",
            "stage.metrics",
            "task.enqueued",
        ]
    finally:
        with PostgresClient.from_env() as client:
            client.run_sql("DELETE FROM async_v2_jobs WHERE job_id = %s", params=[job_id])


@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1" or not _postgres_env_available(),
    reason="Set RUN_ASYNC_V2_POSTGRES_INTEGRATION=1 with Postgres env to run.",
)
def test_postgres_split_worker_handoff_failure_rolls_back_next_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid.uuid4().hex
    job_id = f"test_async_v2_split_handoff_rollback_job_{suffix}"
    current_task_id = f"{job_id}:transactional-handoff"
    next_task_id = f"{job_id}:next-stage"
    worker_id = f"worker_split_handoff_rollback_{suffix}"
    repo = AsyncPipelineV2Repository()
    queue = PostgresTaskQueue()
    repo.ensure_schema()

    try:
        repo.create_job(
            job_id=job_id,
            job_type="video_music",
            request_json={
                "source_video_artifact_id": f"{job_id}:source_video:input",
                "modelspec": "edenn_basic",
                "mode": "split",
            },
            priority=6,
        )
        queue.enqueue(
            TaskEnvelope(
                task_id=current_task_id,
                job_id=job_id,
                queue_name="transactional-handoff",
                task_type="transactional_handoff",
                payload_json={},
                priority=6,
                max_attempts=3,
                idempotency_key=f"{job_id}:transactional-handoff:v1",
            )
        )
        original_complete = queue.complete

        def complete_then_raise(*, task_id: str, worker_id: str, client=None):
            original_complete(task_id=task_id, worker_id=worker_id, client=client)
            raise RuntimeError("forced handoff completion failure")

        monkeypatch.setattr(queue, "complete", complete_then_raise)
        worker = _TransactionalHandoffWorker(
            repository=repo,
            queue=queue,
            runtime=SimpleNamespace(),
            settings=SimpleNamespace(workdir="/tmp"),
            worker_id=worker_id,
            lease_seconds=120,
            retry_backoff_seconds=0,
            next_task=TaskEnvelope(
                task_id=next_task_id,
                job_id=job_id,
                queue_name="next-stage",
                task_type="next_stage",
                payload_json={"from_task_id": current_task_id},
                priority=6,
                max_attempts=3,
                idempotency_key=f"{job_id}:next-stage:v1",
            ),
        )

        processed = asyncio.run(worker.process_one())

        assert processed is not None
        assert processed.status == TaskStatus.QUEUED
        assert queue.get_task(next_task_id) is None
        current_task = queue.get_task(current_task_id)
        assert current_task is not None
        assert current_task.status == TaskStatus.QUEUED
        assert current_task.last_error_json is not None
        assert current_task.last_error_json["type"] == "RuntimeError"
        stage_runs = repo.list_stage_runs(job_id)
        assert len(stage_runs) == 1
        assert stage_runs[0].status == StageStatus.FAILED
        event_types = [event.event_type for event in repo.list_events(job_id)]
        assert event_types == ["stage.started", "stage.failed"]
        job = repo.get_job(job_id)
        assert job is not None
        assert job.status == JobStatus.QUEUED
    finally:
        with PostgresClient.from_env() as client:
            client.run_sql("DELETE FROM async_v2_jobs WHERE job_id = %s", params=[job_id])


@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1" or not _postgres_env_available(),
    reason="Set RUN_ASYNC_V2_POSTGRES_INTEGRATION=1 with Postgres env to run.",
)
def test_postgres_json_artifact_payload_roundtrips_without_local_file(tmp_path: Path) -> None:
    suffix = uuid.uuid4().hex
    job_id = f"test_async_v2_json_payload_job_{suffix}"
    artifact_id = f"{job_id}:analysis_plan:v1"
    payload = {
        "job_id": job_id,
        "source_video_artifact_id": f"{job_id}:source_video:input",
        "effective_modelspec": "edenn_basic",
        "scenes": [{"scene_index": 0, "start_timestamp": 0, "end_timestamp": 3}],
    }
    repo = AsyncPipelineV2Repository()
    queue = PostgresTaskQueue()
    repo.ensure_schema()

    try:
        repo.create_job(
            job_id=job_id,
            job_type="video_music",
            request_json={
                "source_video_artifact_id": f"{job_id}:source_video:input",
                "modelspec": "edenn_basic",
                "mode": "split",
            },
        )
        repo.add_artifact(
            artifact_id=artifact_id,
            job_id=job_id,
            artifact_type="analysis_plan",
            role="primary",
            content_type="application/json",
            metadata_json={"payload_stored_in_db": True},
            payload_json=payload,
        )
        artifact = repo.get_artifact(artifact_id)
        assert artifact is not None
        worker = _TransactionalHandoffWorker(
            repository=repo,
            queue=queue,
            runtime=SimpleNamespace(),
            settings=SimpleNamespace(workdir=tmp_path),
            worker_id=f"json_payload_roundtrip_{suffix}",
        )

        assert asyncio.run(worker._load_json_artifact_async(artifact)) == payload
        status_view = repo.build_status_view(job_id)
        assert status_view["artifacts"][0]["payload_available"] is True
        assert "payload_json" not in status_view["artifacts"][0]["metadata"]
    finally:
        with PostgresClient.from_env() as client:
            client.run_sql("DELETE FROM async_v2_jobs WHERE job_id = %s", params=[job_id])


@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1" or not _postgres_env_available(),
    reason="Set RUN_ASYNC_V2_POSTGRES_INTEGRATION=1 with Postgres env to run.",
)
def test_postgres_record_json_artifact_persists_payload_column(tmp_path: Path) -> None:
    suffix = uuid.uuid4().hex
    job_id = f"test_async_v2_record_json_payload_job_{suffix}"
    artifact_id = f"{job_id}:video_preprocess:v1"
    payload = {
        "job_id": job_id,
        "prepared_source_video_artifact_id": f"{job_id}:source_video:input",
        "video_metadata": {"duration": 12.5, "width": 360, "height": 360},
        "compression": {"requested": False, "applied": False},
    }
    repo = AsyncPipelineV2Repository()
    queue = PostgresTaskQueue()
    repo.ensure_schema()

    try:
        repo.create_job(
            job_id=job_id,
            job_type="video_music",
            request_json={
                "source_video_artifact_id": f"{job_id}:source_video:input",
                "modelspec": "edenn_basic",
                "mode": "split",
            },
        )
        worker = _TransactionalHandoffWorker(
            repository=repo,
            queue=queue,
            runtime=SimpleNamespace(),
            settings=SimpleNamespace(workdir=tmp_path),
            worker_id=f"record_json_payload_{suffix}",
        )

        recorded = worker._record_json_artifact(
            job_id=job_id,
            artifact_id=artifact_id,
            artifact_type="video_preprocess",
            role="primary",
            payload=payload,
        )
        Path(recorded.local_path).unlink()
        reloaded = repo.get_artifact(artifact_id)
        assert reloaded is not None
        assert reloaded.payload_json == payload
        assert asyncio.run(worker._load_json_artifact_async(reloaded)) == payload
    finally:
        with PostgresClient.from_env() as client:
            client.run_sql("DELETE FROM async_v2_jobs WHERE job_id = %s", params=[job_id])


def test_video_preprocess_worker_records_artifact_and_enqueues_analysis(tmp_path: Path) -> None:
    source_path = tmp_path / SMOKE_VIDEO.name
    source_path.write_bytes(SMOKE_VIDEO.read_bytes())
    job_id = "job_split_preprocess"
    source_artifact_id = f"{job_id}:source_video:input"
    repo = _MemoryRepository()
    queue = _MemoryQueue()
    storage = _RecordingStorage()
    settings = SimpleNamespace(
        workdir=tmp_path,
        upload_container="user-uploads",
        output_container="generated-media",
        audio_container_name="generated-audio",
    )
    request_json = {
        "source_video_artifact_id": source_artifact_id,
        "modelspec": "edenn_basic",
        "user_prompt": "upbeat",
        "mode": "split",
        "compression_flag": True,
        "compression_max_height": 128,
        "max_attempts": 2,
    }
    repo.create_job(
        job_id=job_id,
        job_type="video_music",
        request_json=request_json,
        status=JobStatus.QUEUED,
        priority=5,
    )
    repo.add_artifact(
        artifact_id=source_artifact_id,
        job_id=job_id,
        artifact_type="source_video",
        role="input",
        container="user-uploads",
        blob_name=f"jobs/{job_id}/input/source_video.mp4",
        url="https://storage.test/user-uploads/source_video.mp4?sig=fake",
        content_type="video/mp4",
        local_path=str(source_path.resolve()),
        metadata_json={"source_filename": source_path.name},
    )
    queue.enqueue(
        TaskEnvelope(
            task_id=f"{job_id}:video-preprocess",
            job_id=job_id,
            queue_name="video-preprocess",
            task_type="video_preprocess",
            payload_json={"source_video_artifact_id": source_artifact_id},
            priority=5,
            max_attempts=2,
            idempotency_key=f"{job_id}:video-preprocess:v1",
        )
    )
    runtime = SimpleNamespace(
        workflow=SimpleNamespace(video_asset_preprocess=PreprocessStage())
    )
    worker = VideoPreprocessWorker(
        repository=repo,  # type: ignore[arg-type]
        queue=queue,  # type: ignore[arg-type]
        runtime=runtime,  # type: ignore[arg-type]
        settings=settings,
        storage=storage,
        worker_id="video-preprocess-test",
        lease_seconds=60,
    )

    processed = asyncio.run(worker.process_one())

    assert processed is not None
    assert processed.status == TaskStatus.COMPLETED
    assert queue.heartbeat_calls == [
        {
            "task_id": f"{job_id}:video-preprocess",
            "worker_id": "video-preprocess-test",
            "lease_seconds": 60,
        }
    ]
    assert next(iter(repo.stage_runs.values())).heartbeat_at is not None
    preprocess_artifact = repo.get_artifact(f"{job_id}:video_preprocess:v1")
    compressed_artifact = repo.get_artifact(f"{job_id}:source_video:compressed_input")
    assert preprocess_artifact is not None
    assert compressed_artifact is not None
    assert compressed_artifact.role == "compressed_input"
    preprocess_payload = json.loads(Path(preprocess_artifact.local_path).read_text())
    assert preprocess_payload["prepared_source_video_artifact_id"] == compressed_artifact.artifact_id
    assert preprocess_payload["video_metadata"]["height"] == 128
    assert preprocess_payload["compression"]["applied"] is True

    analysis_task = queue.tasks[f"{job_id}:analysis-and-planning"]
    assert analysis_task.queue_name == "analysis-and-planning"
    assert analysis_task.task_type == "analysis_and_planning"
    assert analysis_task.payload_json["source_video_artifact_id"] == compressed_artifact.artifact_id
    assert analysis_task.payload_json["video_preprocess_artifact_id"] == preprocess_artifact.artifact_id
    assert repo.jobs[job_id].current_stage == "video_preprocess"
    assert repo.jobs[job_id].progress_percent == 25
    assert [event.event_type for event in repo.list_events(job_id)] == [
        "stage.started",
        "artifact.created",
        "artifact.created",
        "task.enqueued",
        "stage.completed",
        "stage.metrics",
    ]


def _preprocess_worker_for_source(
    tmp_path: Path, source_path: Path, *, max_attempts: int = 2
):
    """Build a video-preprocess worker wired to a job whose source is source_path."""
    job_id = "job_split_guardrail"
    source_artifact_id = f"{job_id}:source_video:input"
    repo = _MemoryRepository()
    queue = _MemoryQueue()
    settings = SimpleNamespace(
        workdir=tmp_path,
        upload_container="user-uploads",
        output_container="generated-media",
        audio_container_name="generated-audio",
    )
    repo.create_job(
        job_id=job_id,
        job_type="video_music",
        request_json={
            "source_video_artifact_id": source_artifact_id,
            "modelspec": "edenn_basic",
            "user_prompt": "upbeat",
            "mode": "split",
            "max_attempts": max_attempts,
        },
        status=JobStatus.QUEUED,
        priority=5,
    )
    repo.add_artifact(
        artifact_id=source_artifact_id,
        job_id=job_id,
        artifact_type="source_video",
        role="input",
        container="user-uploads",
        blob_name=f"jobs/{job_id}/input/source_video.mp4",
        url="https://storage.test/user-uploads/source_video.mp4?sig=fake",
        content_type="video/mp4",
        local_path=str(source_path.resolve()),
        metadata_json={"source_filename": source_path.name},
    )
    queue.enqueue(
        TaskEnvelope(
            task_id=f"{job_id}:video-preprocess",
            job_id=job_id,
            queue_name="video-preprocess",
            task_type="video_preprocess",
            payload_json={"source_video_artifact_id": source_artifact_id},
            priority=5,
            max_attempts=max_attempts,
        )
    )
    runtime = SimpleNamespace(
        workflow=SimpleNamespace(video_asset_preprocess=PreprocessStage())
    )
    worker = VideoPreprocessWorker(
        repository=repo,  # type: ignore[arg-type]
        queue=queue,  # type: ignore[arg-type]
        runtime=runtime,  # type: ignore[arg-type]
        settings=settings,
        storage=_RecordingStorage(),
        worker_id="video-preprocess-test",
        lease_seconds=60,
    )
    return worker, repo, queue, job_id


def test_video_preprocess_worker_rejects_too_short_source_with_10007(
    tmp_path: Path,
) -> None:
    # The 5.2s clip violates the >15s minimum; even with retries budgeted the
    # guardrail failure is permanent (retryable: false) and nothing downstream
    # runs — no compression artifact, no analysis task.
    source_path = tmp_path / SMOKE_SHORT_VIDEO.name
    source_path.write_bytes(SMOKE_SHORT_VIDEO.read_bytes())
    worker, repo, queue, job_id = _preprocess_worker_for_source(tmp_path, source_path)

    processed = asyncio.run(worker.process_one())

    assert processed is not None
    assert processed.status == TaskStatus.FAILED
    job = repo.jobs[job_id]
    assert job.status == JobStatus.FAILED
    assert job.error_json["error_code"] == 10007
    assert job.error_json["retryable"] is False
    assert repo.get_artifact(f"{job_id}:source_video:compressed_input") is None
    assert repo.get_artifact(f"{job_id}:video_preprocess:v1") is None
    assert f"{job_id}:analysis-and-planning" not in queue.tasks


def test_video_preprocess_worker_rejects_oversize_source_with_10005(
    tmp_path: Path,
) -> None:
    # Size is validated before any ffprobe/ffmpeg work, so a sparse >300MB file
    # is rejected as too large without ever being decoded.
    source_path = tmp_path / "huge_source.mp4"
    with source_path.open("wb") as handle:
        handle.truncate(300 * 1024 * 1024 + 1)
    worker, repo, queue, job_id = _preprocess_worker_for_source(tmp_path, source_path)

    processed = asyncio.run(worker.process_one())

    assert processed is not None
    assert processed.status == TaskStatus.FAILED
    job = repo.jobs[job_id]
    assert job.status == JobStatus.FAILED
    assert job.error_json["error_code"] == 10005
    assert job.error_json["retryable"] is False
    assert f"{job_id}:analysis-and-planning" not in queue.tasks


def test_provider_worker_records_provider_task_before_polling_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / SMOKE_VIDEO.name
    source_path.write_bytes(SMOKE_VIDEO.read_bytes())
    audio_path = tmp_path / "candidate.wav"
    audio_path.write_bytes(b"RIFF....WAVEfmt candidate")
    job_id = "job_provider_task_state"
    source_artifact_id = f"{job_id}:source_video:input"
    analysis_artifact_id = f"{job_id}:analysis_plan:v1"
    repo = _MemoryRepository()
    queue = _MemoryQueue()
    storage = _RecordingStorage()
    settings = SimpleNamespace(
        workdir=tmp_path,
        upload_container="user-uploads",
        output_container="generated-media",
        audio_container_name="generated-audio",
    )
    request_json = {
        "source_video_artifact_id": source_artifact_id,
        "modelspec": "edenn_studio",
        "user_prompt": "cinematic",
        "mode": "split",
        "max_attempts": 2,
    }
    repo.create_job(
        job_id=job_id,
        job_type="video_music",
        request_json=request_json,
        status=JobStatus.QUEUED,
        priority=8,
    )
    repo.add_artifact(
        artifact_id=source_artifact_id,
        job_id=job_id,
        artifact_type="source_video",
        role="input",
        content_type="video/mp4",
        local_path=str(source_path.resolve()),
        metadata_json={"source_filename": source_path.name},
    )
    video_metadata = VideoMetadata.from_file(source_path)
    analysis_payload = {
        "job_id": job_id,
        "source_video_artifact_id": source_artifact_id,
        "video_metadata": video_metadata.to_dict(),
        "scenes": [],
        "video_summary": {},
        "video_title": "Provider task state test",
        "video_description": "",
        "music_description": {"style_prompt": "cinematic", "lyrics_prompt": "short hook"},
        "effective_modelspec": "edenn_studio",
        "include_vocals": True,
        "vocal_gender": "female",
        "user_requested_language": "en",
        "detected_category": "test",
        "overall_mood": "cinematic",
        "thumbnail_path": None,
        "token_usage_breakdown": {},
        "token_usage": {},
        "job_received_timestamp": 123,
    }
    analysis_path = tmp_path / "analysis.json"
    analysis_path.write_text(json.dumps(analysis_payload), encoding="utf-8")
    repo.add_artifact(
        artifact_id=analysis_artifact_id,
        job_id=job_id,
        artifact_type="analysis_plan",
        role="primary",
        content_type="application/json",
        local_path=str(analysis_path),
        metadata_json={"size_bytes": analysis_path.stat().st_size},
    )
    queue.enqueue(
        TaskEnvelope(
            task_id=f"{job_id}:provider-candidate-generation",
            job_id=job_id,
            queue_name="provider-candidate-generation",
            task_type="provider_candidate_generation",
            payload_json={
                "source_video_artifact_id": source_artifact_id,
                "analysis_artifact_id": analysis_artifact_id,
                "model_spec": "edenn_studio",
            },
            priority=8,
            max_attempts=2,
            idempotency_key=f"{job_id}:provider-candidate-generation:v1",
        )
    )

    async def fake_run(self, stage_input):
        assert stage_input.resume_provider_task_id is None
        maybe = stage_input.provider_task_recorder(
            {
                "provider_name": "provider_c",
                "operation": "generate",
                "role": "primary_generation",
                "task_id": "provider_c_task_123",
                "base_used": "https://provider-c.test",
            }
        )
        if hasattr(maybe, "__await__"):
            await maybe
        return ProviderCandidateGenerationStageOutput(
            job_id=stage_input.job_id,
            model_spec="edenn_studio",
            candidate_audio_path=audio_path,
            complete_audio_path=audio_path,
            secondary_complete_audio_path=None,
            primary_full_lyrics="",
            provider_task_id="provider_c_task_123",
            generation_api_call_count=2,
        )

    monkeypatch.setattr(VideoMusicProviderCandidateGenerationStage, "run", fake_run)
    worker = ProviderCandidateGenerationWorker(
        repository=repo,  # type: ignore[arg-type]
        queue=queue,  # type: ignore[arg-type]
        runtime=SimpleNamespace(),  # type: ignore[arg-type]
        settings=settings,
        storage=storage,
        worker_id="provider-task-test",
        lease_seconds=60,
    )

    processed = asyncio.run(worker.process_one())

    assert processed is not None
    assert processed.status == TaskStatus.COMPLETED
    provider_task_artifact = repo.get_artifact(
        f"{job_id}:provider_task:primary_generation"
    )
    candidate_metadata_artifact = repo.get_artifact(
        f"{job_id}:candidate_metadata:primary"
    )
    assert provider_task_artifact is not None
    assert candidate_metadata_artifact is not None
    provider_task_payload = json.loads(Path(provider_task_artifact.local_path).read_text())
    candidate_payload = json.loads(Path(candidate_metadata_artifact.local_path).read_text())
    assert provider_task_payload["provider_name"] == "provider_c"
    assert provider_task_payload["task_id"] == "provider_c_task_123"
    assert provider_task_payload["metadata"]["base_used"] == "https://provider-c.test"
    assert candidate_payload["provider_task_id"] == "provider_c_task_123"
    assert candidate_payload["provider_task_artifact_id"] == provider_task_artifact.artifact_id
    assert candidate_payload["generation_api_call_count"] == 2
    artifact_events = [
        event
        for event in repo.list_events(job_id)
        if event.event_type == "artifact.created"
    ]
    assert [event.payload_json["artifact_id"] for event in artifact_events] == [
        provider_task_artifact.artifact_id,
        f"{job_id}:candidate_audio:primary",
        candidate_metadata_artifact.artifact_id,
    ]


@pytest.mark.parametrize(
    ("worker_cls", "modelspec", "queue_name"),
    [
        (BasicMusicGenerationWorker, "edenn_basic", "music-basic"),
        (EnhancedMusicGenerationWorker, "edenn_enhanced", "music-enhanced"),
        (StudioMusicGenerationWorker, "edenn_studio", "music-studio"),
    ],
    ids=["basic-provider-video-free", "enhanced-provider-video-free", "studio-provider-video-free"],
)
def test_provider_workers_do_not_resolve_source_video(
    worker_cls,
    modelspec: str,
    queue_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_path = tmp_path / f"{modelspec}_candidate.wav"
    audio_path.write_bytes(b"RIFF....WAVEfmt candidate")
    job_id = f"job_{modelspec}_provider_video_free"
    source_artifact_id = f"{job_id}:source_video:input"
    analysis_artifact_id = f"{job_id}:analysis_plan:v1"
    missing_source_path = tmp_path / "source-video-was-not-downloaded.mp4"
    repo = _MemoryRepository()
    queue = _MemoryQueue()
    storage = _RecordingStorage()
    settings = SimpleNamespace(
        workdir=tmp_path,
        upload_container="user-uploads",
        output_container="generated-media",
        audio_container_name="generated-audio",
    )
    repo.create_job(
        job_id=job_id,
        job_type="video_music",
        request_json={
            "source_video_artifact_id": source_artifact_id,
            "modelspec": modelspec,
            "user_prompt": "video-free provider generation",
            "mode": "split",
            "max_attempts": 2,
        },
        status=JobStatus.QUEUED,
        priority=7,
    )
    repo.add_artifact(
        artifact_id=source_artifact_id,
        job_id=job_id,
        artifact_type="source_video",
        role="input",
        content_type="video/mp4",
        local_path=str(missing_source_path),
        metadata_json={"source_filename": missing_source_path.name},
    )
    repo.add_artifact(
        artifact_id=analysis_artifact_id,
        job_id=job_id,
        artifact_type="analysis_plan",
        role="primary",
        content_type="application/json",
        payload_json={
            "job_id": job_id,
            "source_video_artifact_id": source_artifact_id,
            "video_metadata": {
                "path": str(missing_source_path),
                "duration": 12.5,
                "size_bytes": 12345,
                "width": 640,
                "height": 360,
                "fps": 24.0,
                "video_codec": "h264",
                "video_bit_rate": None,
                "has_audio": True,
                "audio_codec": "aac",
                "audio_channels": 2,
                "audio_sample_rate": 44100,
                "audio_bit_rate": None,
                "audio_activity": [],
            },
            "scenes": [],
            "video_summary": {},
            "video_title": "Provider video-free test",
            "video_description": "",
            "music_description": {"style_prompt": "cinematic", "lyrics_prompt": "short hook"},
            "effective_modelspec": modelspec,
            "include_vocals": modelspec != "edenn_basic",
            "vocal_gender": "female",
            "user_requested_language": "en",
            "detected_category": "test",
            "overall_mood": "cinematic",
            "thumbnail_path": None,
            "token_usage_breakdown": {},
            "token_usage": {},
            "job_received_timestamp": 123,
        },
        metadata_json={"payload_stored_in_db": True},
    )
    queue.enqueue(
        TaskEnvelope(
            task_id=f"{job_id}:provider-candidate-generation",
            job_id=job_id,
            queue_name=queue_name,
            task_type="provider_candidate_generation",
            payload_json={
                "source_video_artifact_id": source_artifact_id,
                "analysis_artifact_id": analysis_artifact_id,
                "model_spec": modelspec,
            },
            priority=7,
            max_attempts=2,
            idempotency_key=f"{job_id}:provider-candidate-generation:v1",
        )
    )

    async def fail_resolve_source_video(*_args, **_kwargs):
        raise AssertionError("provider workers must not resolve source video bytes")

    async def fake_run(self, stage_input):
        assert stage_input.source_video_path.name == "metadata_only_source_video.unavailable"
        assert not stage_input.source_video_path.exists()
        assert stage_input.analysis.video_metadata.path == stage_input.source_video_path.resolve()
        assert stage_input.analysis.video_metadata.duration == 12.5
        assert stage_input.analysis.video_metadata.size_bytes == 12345
        maybe = stage_input.provider_task_recorder(
            {
                "provider_name": f"{modelspec}-provider",
                "operation": "generate",
                "role": "primary_generation",
                "task_id": f"{modelspec}_task_123",
            }
        )
        if hasattr(maybe, "__await__"):
            await maybe
        return ProviderCandidateGenerationStageOutput(
            job_id=stage_input.job_id,
            model_spec=modelspec,
            candidate_audio_path=audio_path,
            complete_audio_path=audio_path if modelspec != "edenn_basic" else None,
            secondary_complete_audio_path=None,
            primary_full_lyrics="",
            provider_task_id=f"{modelspec}_task_123",
        )

    monkeypatch.setattr(
        ProviderCandidateGenerationWorker,
        "_resolve_source_video",
        fail_resolve_source_video,
    )
    monkeypatch.setattr(VideoMusicProviderCandidateGenerationStage, "run", fake_run)
    worker = worker_cls(
        repository=repo,  # type: ignore[arg-type]
        queue=queue,  # type: ignore[arg-type]
        runtime=SimpleNamespace(),  # type: ignore[arg-type]
        settings=settings,
        storage=storage,
        worker_id=f"{modelspec}-provider-video-free-test",
        lease_seconds=60,
    )

    processed = asyncio.run(worker.process_one())

    assert processed is not None
    assert processed.status == TaskStatus.COMPLETED
    assert repo.get_artifact(f"{job_id}:candidate_audio:primary") is not None
    assert repo.get_artifact(f"{job_id}:candidate_metadata:primary") is not None
    next_task = queue.tasks[f"{job_id}:selection-ranking-remix-finalize"]
    assert next_task.queue_name == "selection-ranking-remix-finalize"
    assert next_task.payload_json["source_video_artifact_id"] == source_artifact_id
    provider_task_artifact = repo.get_artifact(f"{job_id}:provider_task:primary_generation")
    assert provider_task_artifact is not None
    assert provider_task_artifact.payload_json["task_id"] == f"{modelspec}_task_123"


def _app_client(settings, storage, repo, queue) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_async_pipeline_v2_router(
            SimpleNamespace(settings=settings, storage=storage),  # type: ignore[arg-type]
            repository=repo,
            queue=queue,
        )
    )
    return TestClient(app)


def _split_runtime(settings, storage) -> VideoMusicSplitStageRuntime:
    return VideoMusicSplitStageRuntime(
        storage_service=storage,
        llm_image_container=settings.llm_image_container,
        llm_image_sas_ttl_minutes=settings.llm_image_sas_ttl_minutes,
        llm_image_cleanup_delay_seconds=settings.llm_image_cleanup_delay_seconds,
    )


def _post_split_job(client: TestClient, *, modelspec: str) -> dict:
    with SMOKE_VIDEO.open("rb") as handle:
        asset_response = client.post(
            "/api/v2/assets/video",
            files={"video": (f"split-{modelspec}-{uuid.uuid4().hex}.mp4", handle, "video/mp4")},
            data={
                "creator_user_id": f"async_v2_split_{modelspec}",
                "session_id": f"async_v2_split_session_{modelspec}",
            },
        )
    assert asset_response.status_code == 200, asset_response.text
    asset_payload = asset_response.json()
    job_response = client.post(
        "/api/v2/jobs/video-music",
        json={
            "source_video_artifact_id": asset_payload["artifact_id"],
            "modelspec": modelspec,
            "user_prompt": (
                "Create a concise upbeat female vocal pop hook that matches "
                "the pacing of this short social video."
            ),
            "preserve_original_audio": False,
            "music_volume": 0.75,
            "mode": "split",
            "priority": 7,
            "max_attempts": 2,
        },
    )
    assert job_response.status_code == 200, job_response.text
    return {
        "asset": asset_payload,
        "job": job_response.json(),
    }


async def _process_one(worker) -> str:
    result = await worker.process_one()
    assert result is not None
    return result.status


def _build_worker(worker_cls, *, repo, queue, settings, storage, suffix: str):
    return worker_cls(
        repository=repo,
        queue=queue,
        runtime=_split_runtime(settings, storage),
        settings=settings,
        storage=storage,
        worker_id=f"{worker_cls.stage_name}_{suffix}_{uuid.uuid4().hex}",
        lease_seconds=2400,
    )


def _build_role_workers(role: str, *, repo, queue, settings, storage, suffix: str):
    return build_split_workers(
        role=role,
        repository=repo,
        queue=queue,
        runtime=_split_runtime(settings, storage),
        settings=settings,
        storage=storage,
        worker_id=f"{role}_{suffix}_{uuid.uuid4().hex}",
        lease_seconds=2400,
    )


def _provider_worker_class_for_modelspec(modelspec: str):
    if modelspec == "edenn_studio":
        return StudioMusicGenerationWorker
    if modelspec == "edenn_enhanced":
        return EnhancedMusicGenerationWorker
    return BasicMusicGenerationWorker


def _run_split_job(repo, queue, settings, storage, *, modelspec: str, allow_provider_retry: bool = True) -> None:
    preprocess_worker = _build_worker(
        VideoPreprocessWorker,
        repo=repo,
        queue=queue,
        settings=settings,
        storage=storage,
        suffix="single",
    )
    assert asyncio.run(_process_one(preprocess_worker)) == TaskStatus.COMPLETED

    analysis_worker = _build_worker(
        AnalysisAndPlanningWorker,
        repo=repo,
        queue=queue,
        settings=settings,
        storage=storage,
        suffix="single",
    )
    assert asyncio.run(_process_one(analysis_worker)) == TaskStatus.COMPLETED

    provider_worker_cls = _provider_worker_class_for_modelspec(modelspec)
    provider_worker = _build_worker(
        provider_worker_cls,
        repo=repo,
        queue=queue,
        settings=settings,
        storage=storage,
        suffix="single",
    )
    provider_status = None
    for _ in range(3 if allow_provider_retry else 1):
        provider_status = asyncio.run(_process_one(provider_worker))
        if provider_status == TaskStatus.COMPLETED:
            break
        if provider_status == TaskStatus.QUEUED:
            time.sleep(max(35, getattr(provider_worker, "retry_backoff_seconds", 30) + 5))
            continue
        break
    assert provider_status == TaskStatus.COMPLETED

    final_worker = _build_worker(
        SelectionRankingRemixFinalizeWorker,
        repo=repo,
        queue=queue,
        settings=settings,
        storage=storage,
        suffix="single",
    )
    assert asyncio.run(_process_one(final_worker)) == TaskStatus.COMPLETED


def test_split_finalize_generates_thumbnail_url_when_analysis_thumbnail_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = "job_split_missing_thumbnail"
    repo = _MemoryRepository()
    queue = _MemoryQueue()
    storage = _RecordingStorage()
    settings = SimpleNamespace(
        workdir=tmp_path,
        upload_container="user-uploads",
        output_container="voicestorage",
        audio_container_name="audio",
        llm_image_container="user-uploads",
        llm_image_sas_ttl_minutes=5,
        llm_image_cleanup_delay_seconds=0,
    )
    source_path = tmp_path / "source_video.mp4"
    source_path.write_bytes(SMOKE_VIDEO.read_bytes())
    matched_audio = tmp_path / "matched_audio.wav"
    matched_audio.write_bytes(b"RIFF....WAVEfmt matched")
    remixed_video = tmp_path / "remixed_video.mp4"
    remixed_video.write_bytes(source_path.read_bytes())

    source_artifact_id = f"{job_id}:source_video:input"
    analysis_artifact_id = f"{job_id}:analysis_plan:v1"
    candidate_audio_artifact_id = f"{job_id}:candidate_audio:primary"
    candidate_metadata_artifact_id = f"{job_id}:candidate_metadata:primary"

    request_json = {
        "source_video_artifact_id": source_artifact_id,
        "modelspec": "edenn_basic",
        "user_prompt": "local split thumbnail regression",
        "mode": "split",
        "preserve_original_audio": False,
        "music_volume": 0.75,
        "max_attempts": 1,
    }
    repo.create_job(
        job_id=job_id,
        job_type="video_music",
        request_json=request_json,
        status=JobStatus.QUEUED,
    )
    repo.add_artifact(
        artifact_id=source_artifact_id,
        job_id=job_id,
        artifact_type="source_video",
        role="input",
        container="voicestorage",
        blob_name=f"jobs/{job_id}/input/source_video.mp4",
        url="https://storage.test/source_video.mp4?sig=fake",
        content_type="video/mp4",
        local_path=str(source_path),
        metadata_json={"source_filename": source_path.name},
    )
    analysis = AnalysisAndPlanningStageOutput(
        job_id=job_id,
        source_video_artifact_id=source_artifact_id,
        video_metadata=VideoMetadata.from_file(source_path),
        scenes=[],
        video_summary={},
        video_title="Split thumbnail regression",
        video_description="",
        music_prompt={"style_prompt": "upbeat instrumental"},
        effective_modelspec="edenn_basic",
        include_vocals=False,
        vocal_gender="unknown",
        user_requested_language="en",
        detected_category="test",
        overall_mood="upbeat",
        thumbnail_path=tmp_path / "missing_analysis_thumbnail.webp",
        token_usage_breakdown={},
        token_usage={},
        job_received_timestamp=123,
    )
    repo.add_artifact(
        artifact_id=analysis_artifact_id,
        job_id=job_id,
        artifact_type="analysis_plan",
        role="primary",
        content_type="application/json",
        payload_json=analysis_output_to_json(analysis),
        metadata_json={"payload_stored_in_db": True},
    )
    repo.add_artifact(
        artifact_id=candidate_audio_artifact_id,
        job_id=job_id,
        artifact_type="candidate_audio",
        role="primary",
        content_type="audio/wav",
        local_path=str(matched_audio),
    )
    candidate = ProviderCandidateGenerationStageOutput(
        job_id=job_id,
        model_spec="edenn_basic",
        candidate_audio_path=matched_audio,
        complete_audio_path=None,
        secondary_complete_audio_path=None,
        primary_full_lyrics=None,
    )
    repo.add_artifact(
        artifact_id=candidate_metadata_artifact_id,
        job_id=job_id,
        artifact_type="candidate_metadata",
        role="primary",
        content_type="application/json",
        payload_json=candidate_output_to_json(candidate),
        metadata_json={"payload_stored_in_db": True},
    )
    queue.enqueue(
        TaskEnvelope(
            task_id=f"{job_id}:selection-ranking-remix-finalize",
            job_id=job_id,
            queue_name="selection-ranking-remix-finalize",
            task_type="selection_ranking_remix_finalize",
            payload_json={
                "source_video_artifact_id": source_artifact_id,
                "analysis_artifact_id": analysis_artifact_id,
                "candidate_metadata_artifact_id": candidate_metadata_artifact_id,
                "candidate_audio_artifact_id": candidate_audio_artifact_id,
            },
            max_attempts=1,
            idempotency_key=f"{job_id}:selection-ranking-remix-finalize:v1",
        )
    )

    async def fake_finalize_run(self, stage_input):
        assert not stage_input.analysis.thumbnail_path.exists()
        return SelectionRankingRemixFinalizeStageOutput(
            job_id=stage_input.job_id,
            result=VideoGenerationResult(
                video_metadata=VideoMetadata.from_file(stage_input.source_video_path),
                scenes=[],
                video_summary={},
                video_title="Split thumbnail regression",
                video_description="",
                music_prompt={"style_prompt": "upbeat instrumental"},
                music_prompt_in_chinese={},
                generated_music_path=matched_audio,
                complete_generated_music_path=None,
                secondary_complete_generated_music_path=None,
                remixed_video_path=remixed_video,
                include_vocals=False,
                vocal_gender="unknown",
                lyrics_timestamps=[],
                word_level_lyrics_timestamps=[],
                thumbnail_path=stage_input.analysis.thumbnail_path,
                used_music_model_spec="edenn_basic",
            ),
        )

    monkeypatch.setattr(
        VideoMusicSelectionRankingRemixFinalizeStage,
        "run",
        fake_finalize_run,
    )
    worker = _build_worker(
        SelectionRankingRemixFinalizeWorker,
        repo=repo,
        queue=queue,
        settings=settings,
        storage=storage,
        suffix="thumbnail",
    )

    assert asyncio.run(_process_one(worker)) == TaskStatus.COMPLETED

    result = repo.get_job(job_id).result_json
    expected_thumbnail_blob = f"jobs/{job_id}/thumbnail/thumbnail.webp"
    thumbnail_url = result["video_metadata"]["thumbnail_url"]
    assert thumbnail_url == (
        f"https://storage.test/voicestorage/jobs/{job_id}/thumbnail/thumbnail.webp?sig=fake"
    )
    thumbnail_artifact = repo.get_artifact(f"{job_id}:thumbnail:thumbnail")
    assert thumbnail_artifact is not None
    assert thumbnail_artifact.url == thumbnail_url
    thumbnail_upload = next(
        call for call in storage.upload_calls if call["blob_name"] == expected_thumbnail_blob
    )
    assert thumbnail_upload["content_type"] == "image/webp"
    assert thumbnail_upload["path"].exists()


class _PublicStatusRepository(_MemoryRepository):
    """``_MemoryRepository`` plus the status-view methods the public API uses."""

    def list_stage_runs(self, job_id: str):
        return [
            stage_run
            for stage_run in self.stage_runs.values()
            if stage_run.job_id == job_id
        ]

    def build_status_view(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)

        def _iso(value):
            return value.isoformat() if value else None

        return {
            "job_id": job.job_id,
            "job_type": job.job_type,
            "status": job.status,
            "current_stage": job.current_stage,
            "progress_percent": job.progress_percent,
            "priority": job.priority,
            "request": job.request_json,
            "result": job.result_json,
            "error": job.error_json,
            "created_at": _iso(job.created_at),
            "updated_at": _iso(job.updated_at),
            "finished_at": _iso(job.finished_at),
            "stages": [
                {
                    "name": stage.stage_name,
                    "status": stage.status,
                    "output": stage.output_json,
                    "error": stage.error_json,
                }
                for stage in self.list_stage_runs(job_id)
            ],
            "artifacts": [
                {
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "role": artifact.role,
                    "container": artifact.container,
                    "blob_name": artifact.blob_name,
                    "url": artifact.url,
                    "content_type": artifact.content_type,
                    "metadata": artifact.metadata_json,
                }
                for artifact in self.list_artifacts(job_id)
            ],
        }


def _make_silent_wav(path: Path, *, seconds: float = 1.0) -> None:
    import struct
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    frame_rate = 16000
    frame_count = int(frame_rate * seconds)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(frame_rate)
        wav_file.writeframes(struct.pack("<" + "h" * frame_count, *([0] * frame_count)))


def _assert_public_status_contract(payload: dict) -> None:
    """The public job status must expose fetchable media URLs and never leak
    internal storage details (blob keys, container/blob_name, stages, artifacts).
    """
    assert payload["status"] == JobStatus.COMPLETED, payload
    assert payload["progress_percent"] == 100.0, payload
    assert "stages" not in payload, payload
    assert "artifacts" not in payload, payload
    assert "request" not in payload, payload
    assert "events_url" not in payload, payload

    result = payload["result"]
    assert isinstance(result, dict), payload
    video_metadata = result["video_metadata"]
    audio_metadata = result["audio_metadata"]
    for block_name, block, field in (
        ("video_metadata", video_metadata, "video_url"),
        ("video_metadata", video_metadata, "thumbnail_url"),
        ("audio_metadata", audio_metadata, "audio_url"),
    ):
        value = block.get(field)
        assert isinstance(value, str) and value.strip(), {
            "missing_field": f"{block_name}.{field}",
            "result": result,
        }
    assert "container" not in result, result
    assert "blob_name" not in result, result

    def _walk(value) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                assert "blob" not in key.lower(), (key, payload)
                _walk(child)
        elif isinstance(value, list):
            for item in value:
                _walk(item)

    _walk(payload)


def test_split_video_url_e2e_public_status_exposes_thumbnail_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """End-to-end split-mode job submitted with a live ``video_url`` input.

    Exercises the real API ingestion, queue hand-offs across every split
    worker, the thumbnail-generation fallback, and the public status
    serialization. The heavy LLM analysis, provider generation, and remix
    stages are faked so the pipeline runs without Azure/ProviderB credentials,
    which keeps the test deterministic and runnable in GitHub CI.
    """
    assert SMOKE_VIDEO.exists()
    repo = _PublicStatusRepository()
    queue = _MemoryQueue()
    storage = _RecordingStorage()
    settings = SimpleNamespace(
        workdir=tmp_path / "workdir",
        upload_container="user-uploads",
        output_container="voicestorage",
        audio_container_name="audio",
        llm_image_container="user-uploads",
        llm_image_sas_ttl_minutes=5,
        llm_image_cleanup_delay_seconds=0,
        music_volume=1.0,
    )
    client = _app_client(settings, storage, repo, queue)

    candidate_audio = tmp_path / "candidate_audio.wav"
    _make_silent_wav(candidate_audio)

    async def fake_analysis_run(self, stage_input):
        return AnalysisAndPlanningStageOutput(
            job_id=stage_input.job_id,
            source_video_artifact_id=stage_input.source_video_artifact_id,
            video_metadata=VideoMetadata.from_file(stage_input.source_video_path),
            scenes=[],
            video_summary={},
            video_title="City Walk Moments",
            video_description="",
            music_prompt={"style_prompt": "upbeat instrumental electronic"},
            effective_modelspec="edenn_basic",
            include_vocals=False,
            vocal_gender="unknown",
            user_requested_language="en",
            detected_category="default",
            overall_mood="upbeat",
            # Point at a non-existent path so the finalize worker exercises the
            # thumbnail-generation fallback (the production thumbnail fix).
            thumbnail_path=stage_input.source_video_path.parent
            / "analysis_thumbnail_missing.webp",
            token_usage_breakdown={},
            token_usage={},
            job_received_timestamp=123,
        )

    async def fake_provider_run(self, stage_input):
        return ProviderCandidateGenerationStageOutput(
            job_id=stage_input.job_id,
            model_spec="edenn_basic",
            candidate_audio_path=candidate_audio,
            complete_audio_path=None,
            secondary_complete_audio_path=None,
            primary_full_lyrics=None,
        )

    async def fake_finalize_run(self, stage_input):
        return SelectionRankingRemixFinalizeStageOutput(
            job_id=stage_input.job_id,
            result=VideoGenerationResult(
                video_metadata=VideoMetadata.from_file(stage_input.source_video_path),
                scenes=[],
                video_summary={},
                video_title="City Walk Moments",
                video_description="",
                music_prompt={"style_prompt": "upbeat instrumental electronic"},
                music_prompt_in_chinese={},
                generated_music_path=candidate_audio,
                complete_generated_music_path=None,
                secondary_complete_generated_music_path=None,
                remixed_video_path=stage_input.source_video_path,
                include_vocals=False,
                vocal_gender="unknown",
                lyrics_timestamps=[],
                word_level_lyrics_timestamps=[],
                thumbnail_path=stage_input.analysis.thumbnail_path,
                used_music_model_spec="edenn_basic",
            ),
        )

    monkeypatch.setattr(VideoMusicAnalysisAndPlanningStage, "run", fake_analysis_run)
    monkeypatch.setattr(
        VideoMusicProviderCandidateGenerationStage, "run", fake_provider_run
    )
    monkeypatch.setattr(
        VideoMusicSelectionRankingRemixFinalizeStage, "run", fake_finalize_run
    )

    with _serve_directory(SMOKE_VIDEO.parent) as base_url:
        video_url = f"{base_url}/{SMOKE_VIDEO.name}"
        created = client.post(
            "/api/v2/jobs/video-music",
            json={
                "video_url": video_url,
                "modelspec": "edenn_basic",
                "user_prompt": (
                    "Create a short upbeat instrumental electronic background "
                    "track for this video. No vocals."
                ),
                "preserve_original_audio": False,
                "music_volume": 1.0,
                "mode": "split",
                "max_attempts": 1,
            },
        )
        assert created.status_code == 200, created.text
        created_payload = created.json()
        job_id = created_payload["job_id"]
        assert created_payload["status"] == JobStatus.QUEUED

        # Drive preprocess -> analysis -> provider -> finalize. The preprocess
        # worker downloads the live video_url, so run inside the server context.
        _run_split_job(
            repo,
            queue,
            settings,
            storage,
            modelspec="edenn_basic",
            allow_provider_retry=False,
        )

    status = client.get(f"/api/v2/jobs/{job_id}")
    assert status.status_code == 200, status.text
    payload = status.json()

    _assert_public_status_contract(payload)

    # The request is not echoed on read; the live-url ingestion wiring is asserted
    # on the stored row instead.
    stored_request = repo.get_job(job_id).request_json
    assert stored_request["requested_source_video_url"] == video_url
    assert stored_request["source_video_artifact_id"].endswith(":source_video:input")
    assert payload["result"]["video_metadata"]["thumbnail_url"].endswith(".webp?sig=fake")

    print("ACTUAL_SPLIT_E2E_PUBLIC_STATUS=" + json.dumps(payload, indent=2))


async def _drain_stage_concurrently(
    worker_cls,
    *,
    expected_count: int,
    repo,
    queue,
    settings,
    storage,
    suffix: str,
    allow_retry: bool = False,
) -> None:
    completed = 0
    batches = 0
    max_batches = expected_count * (5 if allow_retry else 4)
    observed: list[dict[str, object]] = []
    while completed < expected_count and batches < max_batches:
        remaining = expected_count - completed
        workers = [
            _build_worker(
                worker_cls,
                repo=repo,
                queue=queue,
                settings=settings,
                storage=storage,
                suffix=suffix,
            )
            for _ in range(remaining)
        ]
        results = await asyncio.gather(*(worker.process_one() for worker in workers))
        batches += 1
        made_progress = False
        retry_seen = False
        for result in results:
            if result is None:
                observed.append({"batch": batches, "task_id": None, "status": None})
                continue
            observed.append(
                {
                    "batch": batches,
                    "task_id": result.task_id,
                    "status": result.status,
                    "attempt": result.attempt,
                    "last_error": result.last_error_json,
                }
            )
            made_progress = True
            if result.status == TaskStatus.COMPLETED:
                completed += 1
            elif result.status == TaskStatus.QUEUED:
                retry_seen = True
            else:
                raise AssertionError(f"{worker_cls.stage_name} returned {result.status}: {result}")
        if completed >= expected_count:
            return
        if retry_seen:
            await asyncio.sleep(35)
        elif made_progress:
            await asyncio.sleep(1)
        else:
            await asyncio.sleep(5)
    raise AssertionError(
        f"Only completed {completed}/{expected_count} for {worker_cls.stage_name}; "
        f"observed={observed}"
    )


async def _drain_provider_stage_concurrently(
    *,
    modelspecs: list[str],
    repo,
    queue,
    settings,
    storage,
    suffix: str,
) -> None:
    pending_queues = {
        namespaced_queue_name(
            provider_queue_name_for_modelspec(modelspec),
            settings=settings,
        )
        for modelspec in modelspecs
    }
    batches = 0
    max_batches = len(pending_queues) * 5
    while pending_queues and batches < max_batches:
        workers = [
            worker
            for worker in _build_role_workers(
                "worker-provider",
                repo=repo,
                queue=queue,
                settings=settings,
                storage=storage,
                suffix=suffix,
            )
            if worker.queue_name in pending_queues
        ]
        results = await asyncio.gather(*(worker.process_one() for worker in workers))
        batches += 1
        made_progress = False
        retry_seen = False
        for worker, result in zip(workers, results):
            if result is None:
                continue
            made_progress = True
            if result.status == TaskStatus.COMPLETED:
                pending_queues.discard(worker.queue_name)
                continue
            if result.status == TaskStatus.QUEUED:
                retry_seen = True
                continue
            raise AssertionError(
                f"{worker.queue_name} provider worker returned {result.status}: {result}"
            )
        if pending_queues and retry_seen:
            await asyncio.sleep(65)
        elif pending_queues and made_progress:
            await asyncio.sleep(1)
        elif pending_queues:
            await asyncio.sleep(5)
    if pending_queues:
        raise AssertionError(
            f"Provider worker role did not complete queues: {sorted(pending_queues)}"
        )


def _collect_uploaded(repo, job_ids: list[str]) -> list[tuple[str, str]]:
    uploaded: list[tuple[str, str]] = []
    for job_id in job_ids:
        try:
            artifacts = repo.list_artifacts(job_id)
        except Exception:
            continue
        for artifact in artifacts:
            if artifact.container and artifact.blob_name:
                uploaded.append((artifact.container, artifact.blob_name))
    return uploaded


def _cleanup(repo, storage, *, job_ids: list[str]) -> None:
    seen = set()
    for container, blob_name in _collect_uploaded(repo, job_ids):
        if (container, blob_name) in seen:
            continue
        seen.add((container, blob_name))
        if hasattr(storage, "delete_blob"):
            storage.delete_blob(container=container, blob_name=blob_name)
    with PostgresClient.from_env() as client:
        for job_id in job_ids:
            client.run_sql("DELETE FROM async_v2_jobs WHERE job_id = %s", params=[job_id])


@pytest.mark.parametrize(
    "modelspec",
    ["edenn_basic", "edenn_enhanced", "edenn_studio"],
    ids=["split-real-provider_a-basic", "split-real-provider_b-enhanced", "split-real-provider_c-studio"],
)
@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_REAL_PROVIDER_INTEGRATION") != "1"
    or os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1"
    or os.getenv("RUN_ASYNC_V2_STORAGE_INTEGRATION") != "1"
    or not _postgres_env_available()
    or not _storage_env_available(),
    reason=(
        "Set RUN_ASYNC_V2_REAL_PROVIDER_INTEGRATION=1, "
        "RUN_ASYNC_V2_POSTGRES_INTEGRATION=1, and "
        "RUN_ASYNC_V2_STORAGE_INTEGRATION=1."
    ),
)
def test_real_provider_split_pipeline_from_api_for_modelspec(
    modelspec: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _real_provider_env_available(modelspec):
        pytest.skip(f"Real provider env is not configured for {modelspec}.")

    monkeypatch.setenv("USE_LOCAL_TEMP_DIR", "true")
    monkeypatch.setenv("LOCAL_TEMP_DIR", str(tmp_path / f"split_{modelspec}"))
    settings = DeploymentSettings.from_env()
    storage = create_storage_service(settings)
    repo = AsyncPipelineV2Repository()
    queue = PostgresTaskQueue()
    client = _app_client(settings, storage, repo, queue)
    created = _post_split_job(client, modelspec=modelspec)
    job_ids = [created["asset"]["job_id"], created["job"]["job_id"]]

    try:
        _run_split_job(repo, queue, settings, storage, modelspec=modelspec)
        status_view = repo.build_status_view(created["job"]["job_id"])
        _save_split_payload(modelspec, status_view)

        assert status_view["status"] == JobStatus.COMPLETED, status_view
        assert [stage["name"] for stage in status_view["stages"]] == [
            "video_preprocess",
            "analysis_and_planning",
            "provider_candidate_generation",
            "selection_ranking_remix_finalize",
        ]
        provider_events = [
            event
            for event in repo.list_events(created["job"]["job_id"])
            if event.event_type == "task.enqueued"
        ]
        assert provider_events[0].payload_json["queue_name"] == provider_queue_name_for_modelspec(modelspec)
        result = status_view["result"]
        assert result["request_metadata"]["modelspec"] == modelspec
        assert result["video_metadata"]["video_url"], result
        assert result["audio_metadata"]["audio_url"], result
        assert result["request_metadata"]["include_vocals"] is True
        assert result["video_metadata"]["geometry"]["duration"] > 0
        artifact_types = {artifact["artifact_type"] for artifact in status_view["artifacts"]}
        assert {
            "source_video",
            "video_preprocess",
            "analysis_plan",
            "candidate_audio",
            "candidate_metadata",
            "matched_audio",
            "remixed_video",
            "final_result",
        }.issubset(artifact_types)
        if modelspec in {"edenn_enhanced", "edenn_studio"}:
            assert result["audio_metadata"]["complete_audio_url"], result
    finally:
        _cleanup(repo, storage, job_ids=job_ids)


@pytest.mark.parametrize(
    "modelspec",
    ["edenn_enhanced", "edenn_studio"],
    ids=["split-url-long-highres-enhanced", "split-url-long-highres-studio"],
)
@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_REAL_PROVIDER_INTEGRATION") != "1"
    or os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1"
    or os.getenv("RUN_ASYNC_V2_STORAGE_INTEGRATION") != "1"
    or not _postgres_env_available()
    or not _storage_env_available(),
    reason=(
        "Set RUN_ASYNC_V2_REAL_PROVIDER_INTEGRATION=1, "
        "RUN_ASYNC_V2_POSTGRES_INTEGRATION=1, and "
        "RUN_ASYNC_V2_STORAGE_INTEGRATION=1."
    ),
)
def test_real_provider_split_pipeline_video_url_long_highres_for_modelspec(
    modelspec: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _real_provider_env_available(modelspec):
        pytest.skip(f"Real provider env is not configured for {modelspec}.")
    assert SMOKE_VIDEO.exists()

    served_dir = tmp_path / "served"
    source_video = served_dir / f"split-url-long-highres-{modelspec}.mp4"
    _make_long_high_resolution_video(SMOKE_VIDEO, source_video)

    suffix = uuid.uuid4().hex
    queue_namespace = f"pytest-split-url-highres-{modelspec}-{suffix}"
    monkeypatch.setenv("USE_LOCAL_TEMP_DIR", "true")
    monkeypatch.setenv("LOCAL_TEMP_DIR", str(tmp_path / f"split_url_highres_{modelspec}"))
    monkeypatch.setenv("ASYNC_V2_QUEUE_NAMESPACE", queue_namespace)

    settings = DeploymentSettings.from_env()
    storage = create_storage_service(settings)
    repo = AsyncPipelineV2Repository()
    queue = _MonitoringPostgresTaskQueue(PostgresTaskQueue())
    client = _app_client(settings, storage, repo, queue)  # type: ignore[arg-type]
    job_ids: list[str] = []

    try:
        with _serve_directory(served_dir) as base_url:
            video_url = f"{base_url}/{source_video.name}"
            response = client.post(
                "/api/v2/jobs/video-music",
                data={
                    "video_url": video_url,
                    "mode": "split",
                    "modelspec": modelspec,
                    # New v2 contract: freestanding lyrics_prompt keys the
                    # vocal path; no verbose flag, no music_style_prompt.
                    "user_prompt": (
                        "Create bright pop music that follows the pacing of this "
                        "long high-resolution video smoke test."
                    ),
                    "lyrics_prompt": (
                        "Write short original English female vocal lyrics with a "
                        "clear hook for a generated test song."
                    ),
                    "preserve_original_audio": "false",
                    "music_volume": "0.75",
                    "compression_flag": "true",
                    "compression_max_height": "1280",
                    "priority": "8",
                    "max_attempts": "2",
                    "user_id": f"async_v2_split_url_highres_{modelspec}",
                    "session_id": f"async_v2_split_url_highres_session_{suffix}",
                },
            )
            assert response.status_code == 200, response.text
            job_payload = response.json()
            job_id = job_payload["job_id"]
            job_ids.append(job_id)

            initial_task = queue.get_task(job_payload["task_id"])
            assert initial_task is not None
            assert initial_task.status == TaskStatus.QUEUED
            assert initial_task.queue_name == namespaced_queue_name(
                "video-preprocess",
                settings=settings,
            )
            assert initial_task.task_type == "video_preprocess"

            _run_split_job(
                repo,
                queue,  # type: ignore[arg-type]
                settings,
                storage,
                modelspec=modelspec,
            )

        status_view = repo.build_status_view(job_id)
        _save_split_payload(f"url_long_highres_{modelspec}", status_view)
        assert status_view["status"] == JobStatus.COMPLETED, status_view
        assert [stage["name"] for stage in status_view["stages"]] == [
            "video_preprocess",
            "analysis_and_planning",
            "provider_candidate_generation",
            "selection_ranking_remix_finalize",
        ]

        result = status_view["result"]
        assert result["request_metadata"]["modelspec"] == modelspec
        assert result["video_metadata"]["video_url"], result
        assert result["audio_metadata"]["audio_url"], result
        assert result["audio_metadata"]["complete_audio_url"], result
        assert result["request_metadata"]["include_vocals"] is True
        assert result["video_metadata"]["geometry"]["duration"] >= 19
        assert result["video_metadata"]["geometry"]["height"] <= 1280

        artifacts = status_view["artifacts"]
        artifact_types = {artifact["artifact_type"] for artifact in artifacts}
        assert {
            "source_video",
            "video_preprocess",
            "analysis_plan",
            "candidate_audio",
            "candidate_metadata",
            "matched_audio",
            "remixed_video",
            "final_result",
        }.issubset(artifact_types)

        source_by_role = {
            artifact["role"]: artifact
            for artifact in artifacts
            if artifact["artifact_type"] == "source_video"
        }
        input_source = source_by_role["input"]
        assert input_source["url"] == video_url
        assert input_source["metadata"]["source_url"] == video_url
        compressed_source = source_by_role["compressed_input"]
        assert compressed_source["metadata"]["compression_applied"] is True
        compression = compressed_source["metadata"]["compression"]
        assert compression["requested"] is True
        assert compression["applied"] is True
        assert compression["max_height"] == 1280
        assert compressed_source["metadata"]["source_video_metadata"]["height"] == 1920
        assert compressed_source["metadata"]["output_video_metadata"]["height"] <= 1280

        expected_queues = [
            namespaced_queue_name("video-preprocess", settings=settings),
            namespaced_queue_name("analysis-and-planning", settings=settings),
            namespaced_queue_name(provider_queue_name_for_modelspec(modelspec), settings=settings),
            namespaced_queue_name("selection-ranking-remix-finalize", settings=settings),
        ]
        transitions = [
            (transition["action"], transition["status"], transition.get("queue_name"))
            for transition in queue.transitions
        ]
        for queue_name in expected_queues:
            assert ("enqueue", TaskStatus.QUEUED, queue_name) in transitions
            assert ("lease", TaskStatus.LEASED, queue_name) in transitions
            assert ("complete", TaskStatus.COMPLETED, queue_name) in transitions
    finally:
        _cleanup(repo, storage, job_ids=job_ids)


@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_REAL_PROVIDER_CONCURRENT") != "1"
    or os.getenv("RUN_ASYNC_V2_REAL_PROVIDER_INTEGRATION") != "1"
    or os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1"
    or os.getenv("RUN_ASYNC_V2_STORAGE_INTEGRATION") != "1"
    or not _postgres_env_available()
    or not _storage_env_available(),
    reason=(
        "Set RUN_ASYNC_V2_REAL_PROVIDER_CONCURRENT=1 plus the real provider, "
        "Postgres, and storage integration flags."
    ),
)
def test_real_provider_split_pipeline_three_concurrent_requests_from_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modelspecs = ["edenn_basic", "edenn_enhanced", "edenn_studio"]
    missing = [modelspec for modelspec in modelspecs if not _real_provider_env_available(modelspec)]
    if missing:
        pytest.skip(f"Real provider env is not configured for: {', '.join(missing)}.")

    monkeypatch.setenv("USE_LOCAL_TEMP_DIR", "true")
    monkeypatch.setenv("LOCAL_TEMP_DIR", str(tmp_path / "split_concurrent"))
    monkeypatch.setenv("ASYNC_V2_QUEUE_NAMESPACE", f"pytest-{uuid.uuid4().hex}")
    settings = DeploymentSettings.from_env()
    storage = create_storage_service(settings)
    repo = AsyncPipelineV2Repository()
    queue = PostgresTaskQueue()
    job_ids: list[str] = []

    try:
        def post_model(modelspec: str) -> dict:
            return _post_split_job(
                _app_client(settings, storage, repo, queue),
                modelspec=modelspec,
            )

        with ThreadPoolExecutor(max_workers=3) as executor:
            created_jobs = list(executor.map(post_model, modelspecs))
        for created in created_jobs:
            job_ids.extend([created["asset"]["job_id"], created["job"]["job_id"]])
        preprocess_task_ids = [created["job"]["task_id"] for created in created_jobs]
        preprocess_tasks = [queue.get_task(task_id) for task_id in preprocess_task_ids]
        preprocess_task_snapshot = [
            {
                "task_id": task_id,
                "status": task.status if task is not None else None,
                "queue_name": task.queue_name if task is not None else None,
                "job_id": task.job_id if task is not None else None,
            }
            for task_id, task in zip(preprocess_task_ids, preprocess_tasks)
        ]
        assert len(set(preprocess_task_ids)) == 3, preprocess_task_snapshot
        assert all(task is not None for task in preprocess_tasks), preprocess_task_snapshot
        assert all(task.status == TaskStatus.QUEUED for task in preprocess_tasks if task), (
            preprocess_task_snapshot
        )
        expected_preprocess_queue = namespaced_queue_name(
            "video-preprocess",
            settings=settings,
        )
        assert all(
            task.queue_name == expected_preprocess_queue
            for task in preprocess_tasks
            if task
        ), preprocess_task_snapshot

        asyncio.run(
            _drain_stage_concurrently(
                VideoPreprocessWorker,
                expected_count=3,
                repo=repo,
                queue=queue,
                settings=settings,
                storage=storage,
                suffix="concurrent_preprocess",
            )
        )
        asyncio.run(
            _drain_stage_concurrently(
                AnalysisAndPlanningWorker,
                expected_count=3,
                repo=repo,
                queue=queue,
                settings=settings,
                storage=storage,
                suffix="concurrent_analysis",
            )
        )
        asyncio.run(
            _drain_provider_stage_concurrently(
                modelspecs=modelspecs,
                repo=repo,
                queue=queue,
                settings=settings,
                storage=storage,
                suffix="concurrent_provider",
            )
        )
        asyncio.run(
            _drain_stage_concurrently(
                SelectionRankingRemixFinalizeWorker,
                expected_count=3,
                repo=repo,
                queue=queue,
                settings=settings,
                storage=storage,
                suffix="concurrent_final",
            )
        )

        status_views = {
            model: repo.build_status_view(created["job"]["job_id"])
            for model, created in zip(modelspecs, created_jobs)
        }
        _save_split_payload("concurrent_3", status_views)
        for modelspec, status_view in status_views.items():
            assert status_view["status"] == JobStatus.COMPLETED, status_view
            result = status_view["result"]
            assert result["request_metadata"]["modelspec"] == modelspec
            assert result["video_metadata"]["video_url"], result
            assert result["audio_metadata"]["audio_url"], result
    finally:
        _cleanup(repo, storage, job_ids=job_ids)


# --- P1 stage/queue telemetry --------------------------------------------


class _RecordingRepository:
    """Minimal repository stub that captures add_event calls for telemetry tests."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def add_event(self, **kwargs) -> None:
        self.events.append(kwargs)


def _telemetry_worker(repository) -> BasicMusicGenerationWorker:
    return BasicMusicGenerationWorker(
        repository=repository,
        queue=SimpleNamespace(),
        runtime=SimpleNamespace(),
        settings=SimpleNamespace(workdir="/tmp"),
        storage=SimpleNamespace(enabled=False),
        worker_id="telemetry-test",
        lease_seconds=10,
    )


def _telemetry_task(**overrides) -> AsyncV2Task:
    base = dict(
        task_id="t1",
        job_id="job_x",
        queue_name="music-basic",
        task_type="provider_candidate_generation",
        status=TaskStatus.LEASED,
        payload_json={},
        attempt=1,
    )
    base.update(overrides)
    return AsyncV2Task(**base)


def _telemetry_job(created_at) -> AsyncV2Job:
    return AsyncV2Job(
        job_id="job_x",
        job_type="video_music",
        status=JobStatus.PROCESSING,
        request_json={},
        created_at=created_at,
    )


def test_queue_wait_ms_uses_db_timestamps() -> None:
    worker = _telemetry_worker(_RecordingRepository())
    eligible = datetime(2026, 6, 18, 11, 0, 0, tzinfo=timezone.utc)
    task = _telemetry_task(
        not_before=eligible,
        created_at=eligible,
        updated_at=eligible + timedelta(seconds=2.5),
    )
    assert worker._queue_wait_ms(task) == 2500.0


def test_queue_wait_ms_is_none_without_timestamps() -> None:
    worker = _telemetry_worker(_RecordingRepository())
    assert worker._queue_wait_ms(_telemetry_task()) is None


def test_queue_wait_ms_never_negative() -> None:
    worker = _telemetry_worker(_RecordingRepository())
    eligible = datetime(2026, 6, 18, 11, 0, 0, tzinfo=timezone.utc)
    task = _telemetry_task(
        not_before=eligible,
        updated_at=eligible - timedelta(seconds=1),  # clock weirdness must not go negative
    )
    assert worker._queue_wait_ms(task) == 0.0


def test_emit_stage_metrics_records_queryable_event() -> None:
    repo = _RecordingRepository()
    worker = _telemetry_worker(repo)
    eligible = datetime(2026, 6, 18, 11, 0, 0, tzinfo=timezone.utc)
    task = _telemetry_task(
        not_before=eligible,
        created_at=eligible,
        updated_at=eligible + timedelta(seconds=3),
    )
    job = _telemetry_job(datetime(2026, 6, 18, 10, 55, 0, tzinfo=timezone.utc))

    worker._emit_stage_metrics(job=job, task=task, stage_ms=1234.5, finished=True)

    assert len(repo.events) == 1
    event = repo.events[0]
    assert event["event_type"] == "stage.metrics"
    payload = event["payload_json"]
    assert payload["stage_name"] == "provider_candidate_generation"
    assert payload["stage_ms"] == 1234.5
    assert payload["queue_wait_ms"] == 3000.0
    assert payload["attempt"] == 1
    assert payload["job_total_ms"] > 0


def test_emit_stage_metrics_omits_job_total_until_finished() -> None:
    repo = _RecordingRepository()
    worker = _telemetry_worker(repo)
    job = _telemetry_job(datetime(2026, 6, 18, 10, 55, 0, tzinfo=timezone.utc))

    worker._emit_stage_metrics(job=job, task=_telemetry_task(), stage_ms=10.0, finished=False)

    assert "job_total_ms" not in repo.events[0]["payload_json"]


def test_emit_stage_metrics_swallows_repository_errors() -> None:
    class _BoomRepository:
        def add_event(self, **kwargs):
            raise RuntimeError("db down")

    worker = _telemetry_worker(_BoomRepository())
    # Telemetry must never fail a stage that already committed.
    worker._emit_stage_metrics(
        job=_telemetry_job(None),
        task=_telemetry_task(),
        stage_ms=1.0,
        finished=False,
    )


# --- Compression content cache (P1) ----------------------------------------


class _MemoryCacheEntry:
    def __init__(self, key, payload):
        self.cache_key = key
        self.payload_json = payload


class _MemoryCache:
    """In-memory stand-in for CacheService with single-flight semantics."""

    def __init__(self):
        self.ready: dict = {}
        self.pending: dict = {}

    def get(self, key):
        return self.ready.get(key)

    def get_or_lease(self, key, *, kind, content_sha=None, key_version=None, lease_seconds=300):
        if key in self.ready:
            return self.ready[key], None
        if key in self.pending:
            return None, None
        token = f"lease-{key}"
        self.pending[key] = token
        return None, token

    def complete_lease(self, key, token, *, kind, payload_json, content_sha=None,
                       key_version=None, size_bytes=None, ttl_days=None):
        if self.pending.get(key) != token:
            return None
        entry = _MemoryCacheEntry(key, payload_json)
        self.ready[key] = entry
        self.pending.pop(key, None)
        return entry


def _enqueue_preprocess_job(*, repo, queue, job_id, sha, source_path):
    source_artifact_id = f"{job_id}:source_video:input"
    request_json = {
        "source_video_artifact_id": source_artifact_id,
        "modelspec": "edenn_basic",
        "user_prompt": "upbeat",
        "mode": "split",
        "compression_flag": True,
        "compression_max_height": 128,
        "max_attempts": 2,
    }
    repo.create_job(job_id=job_id, job_type="video_music", request_json=request_json,
                    status=JobStatus.QUEUED, priority=5)
    # sha=None simulates a video_url job whose source artifact has no precomputed
    # content hash (only staged assets carry sha256).
    metadata = {"source_filename": source_path.name}
    if sha is not None:
        metadata["sha256"] = sha
    repo.add_artifact(
        artifact_id=source_artifact_id, job_id=job_id, artifact_type="source_video",
        role="input", container="user-uploads",
        blob_name=f"jobs/{job_id}/input/source_video.mp4",
        url="https://storage.test/user-uploads/source_video.mp4?sig=fake",
        content_type="video/mp4", local_path=str(source_path.resolve()),
        metadata_json=metadata,
    )
    queue.enqueue(TaskEnvelope(
        task_id=f"{job_id}:video-preprocess", job_id=job_id, queue_name="video-preprocess",
        task_type="video_preprocess",
        payload_json={"source_video_artifact_id": source_artifact_id},
        priority=5, max_attempts=2, idempotency_key=f"{job_id}:video-preprocess:v1",
    ))


def test_compression_content_cache_hit_skips_ffmpeg(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ASYNC_V2_COMPRESS_CACHE", "1")
    import EdennCode.Deployment.async_pipeline_v2.video_source_preparation as vsp

    real_compress = vsp.compress_video_to_max_height
    calls = {"n": 0}

    def counting_compress(*args, **kwargs):
        calls["n"] += 1
        return real_compress(*args, **kwargs)

    monkeypatch.setattr(vsp, "compress_video_to_max_height", counting_compress)

    source_path = tmp_path / SMOKE_VIDEO.name
    source_path.write_bytes(SMOKE_VIDEO.read_bytes())
    sha = "shared-content-sha-123"

    repo = _MemoryRepository()
    queue = _MemoryQueue()
    storage = _RecordingStorage()
    cache = _MemoryCache()
    settings = SimpleNamespace(
        workdir=tmp_path, upload_container="user-uploads",
        output_container="generated-media", audio_container_name="generated-audio",
    )
    runtime = SimpleNamespace(workflow=SimpleNamespace(video_asset_preprocess=PreprocessStage()))
    worker = VideoPreprocessWorker(
        repository=repo, queue=queue, runtime=runtime, settings=settings,  # type: ignore[arg-type]
        storage=storage, worker_id="cache-test", lease_seconds=60, cache=cache,
    )

    # Job A: cold cache -> real ffmpeg compression runs once, cache populated.
    _enqueue_preprocess_job(repo=repo, queue=queue, job_id="job_cache_a", sha=sha, source_path=source_path)
    a = asyncio.run(worker.process_one())
    assert a is not None and a.status == TaskStatus.COMPLETED
    assert calls["n"] == 1
    assert any(k.startswith("compress:") for k in cache.ready)

    # Job B: identical source content (same sha) -> cache hit, ffmpeg NOT re-run.
    _enqueue_preprocess_job(repo=repo, queue=queue, job_id="job_cache_b", sha=sha, source_path=source_path)
    b = asyncio.run(worker.process_one())
    assert b is not None and b.status == TaskStatus.COMPLETED
    assert calls["n"] == 1  # the whole point: compression was reused, not recomputed
    reused = repo.get_artifact("job_cache_b:source_video:compressed_input")
    assert reused is not None
    assert reused.metadata_json.get("reused_from_cache") is True


def test_compression_cache_disabled_recomputes(tmp_path: Path, monkeypatch) -> None:
    import EdennCode.Deployment.async_pipeline_v2.video_source_preparation as vsp

    real_compress = vsp.compress_video_to_max_height
    calls = {"n": 0}

    def counting_compress(*args, **kwargs):
        calls["n"] += 1
        return real_compress(*args, **kwargs)

    monkeypatch.setattr(vsp, "compress_video_to_max_height", counting_compress)

    source_path = tmp_path / SMOKE_VIDEO.name
    source_path.write_bytes(SMOKE_VIDEO.read_bytes())
    repo = _MemoryRepository()
    queue = _MemoryQueue()
    storage = _RecordingStorage()
    settings = SimpleNamespace(
        workdir=tmp_path, upload_container="user-uploads",
        output_container="generated-media", audio_container_name="generated-audio",
    )
    runtime = SimpleNamespace(workflow=SimpleNamespace(video_asset_preprocess=PreprocessStage()))
    worker = VideoPreprocessWorker(
        repository=repo, queue=queue, runtime=runtime, settings=settings,  # type: ignore[arg-type]
        storage=storage, worker_id="nocache-test", lease_seconds=60, cache=None,
    )
    for job_id in ("job_nc_a", "job_nc_b"):
        _enqueue_preprocess_job(repo=repo, queue=queue, job_id=job_id, sha="same", source_path=source_path)
        done = asyncio.run(worker.process_one())
        assert done is not None and done.status == TaskStatus.COMPLETED
    # No cache -> both jobs compress independently.
    assert calls["n"] == 2


def test_cache_keys_are_content_and_version_addressed() -> None:
    from EdennCode.Deployment.async_pipeline_v2.cache_service import (
        compression_cache_key,
        understanding_cache_key,
        KV_COMPRESS,
    )

    base = compression_cache_key(source_sha="abc", max_height=720)
    assert base == f"compress:{KV_COMPRESS}:abc:720"
    # different content or params -> different key
    assert compression_cache_key(source_sha="abc", max_height=480) != base
    assert compression_cache_key(source_sha="xyz", max_height=720) != base
    # understanding keyed on SOURCE sha (not prepared video), language-sensitive
    u_en = understanding_cache_key(source_sha="abc", max_height=720, language="en")
    assert understanding_cache_key(source_sha="abc", max_height=720, language="ja") != u_en
    assert understanding_cache_key(source_sha="abc", max_height=None, language="en").endswith(":none:en")


@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1" or not _postgres_env_available(),
    reason="Set RUN_ASYNC_V2_POSTGRES_INTEGRATION=1 with Postgres env to run.",
)
def test_cache_service_single_flight_round_trip_postgres() -> None:
    from EdennCode.Deployment.async_pipeline_v2.cache_service import CacheService

    cache = CacheService()
    key = f"compress:test:{uuid.uuid4().hex}:720"
    try:
        entry, lease = cache.get_or_lease(key, kind="compress", content_sha="x", key_version="c1")
        assert entry is None and lease is not None  # we won the compute lease

        # A concurrent caller must see the live pending lease, not a second lease.
        entry2, lease2 = cache.get_or_lease(key, kind="compress")
        assert entry2 is None and lease2 is None

        cache.complete_lease(
            key, lease, kind="compress",
            payload_json={"blob_name": "b", "container": "c"},
            content_sha="x", key_version="c1",
        )
        got = cache.get(key)
        assert got is not None
        assert got.status == "ready"
        assert got.payload_json["blob_name"] == "b"
        assert got.hit_count >= 1

        # Now it's a plain hit for everyone.
        hit, no_lease = cache.get_or_lease(key, kind="compress")
        assert hit is not None and no_lease is None
    finally:
        with PostgresClient.from_env() as client:
            client.run_sql("DELETE FROM async_v2_cache WHERE cache_key = %s", params=[key])


# --- Understanding content cache (P2) ---------------------------------------


def _understanding_fakes():
    from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.scene_segmentation_stage import (
        SceneSegmentationStageOutput,
    )
    from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.datamodel import (
        SceneUnderstanding,
    )
    from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoUnderstandingStage.video_understanding_page import (
        VideoUnderstandingStageOutput,
    )

    scene = SceneUnderstanding(
        scene_index=0, start_timestamp=0.0, end_timestamp=5.0,
        visual_summary="a car driving", key_actions="drives", mood="calm",
    )
    calls = {"scene": 0, "vu": 0}

    class _SceneStage:
        async def run(self, _inp):
            calls["scene"] += 1
            return SceneSegmentationStageOutput(
                scene_understanding_messages=[scene], token_usage={"total_tokens": 1},
                thumbnail_path=Path("/tmp/thumb.webp"),
            )

    class _VuStage:
        async def run(self, _inp):
            calls["vu"] += 1
            return VideoUnderstandingStageOutput(
                video_descriptions={"overall_mood": "calm"}, video_title="T",
                video_description="D", token_usage={"total_tokens": 2},
            )

    workflow = SimpleNamespace(scene_segmentation_stage=_SceneStage(), video_understanding_stage=_VuStage())
    return workflow, calls


def _resolve_understanding(stage, *, workflow, prep, cache, job_id, source_sha="sha-1", max_height=720):
    from EdennCode.Deployment.async_pipeline_v2.stages.video_music_split import (
        AnalysisAndPlanningStageInput,
    )

    stage_input = AnalysisAndPlanningStageInput(
        job_id="j", request_json={}, source_video_artifact_id="a",
        source_video_path=Path("/tmp/v.mp4"),
        understanding_cache=cache, cache_source_sha=source_sha, cache_max_height=max_height,
    )
    video_metadata = SimpleNamespace(path=Path("/tmp/v.mp4"), duration=5.0, fps=10.0)
    return asyncio.run(stage._resolve_understanding(
        workflow=workflow, stage_input=stage_input, prep=prep,
        video_metadata=video_metadata, job_id=job_id,
    ))


def test_understanding_cache_hit_skips_scene_and_video_understanding() -> None:
    workflow, calls = _understanding_fakes()
    cache = _MemoryCache()
    prep = SimpleNamespace(analysis_language="en", effective_language="en")
    stage = VideoMusicAnalysisAndPlanningStage(SimpleNamespace())

    # Miss: both expensive video stages run once; result is cached.
    r1 = _resolve_understanding(stage, workflow=workflow, prep=prep, cache=cache, job_id="j1")
    assert calls == {"scene": 1, "vu": 1}
    assert r1[7] == "miss"
    assert any(k.startswith("understanding:") for k in cache.ready)

    # Hit: same content + same analysis inputs -> neither video stage re-runs.
    r2 = _resolve_understanding(stage, workflow=workflow, prep=prep, cache=cache, job_id="j2")
    assert calls == {"scene": 1, "vu": 1}
    assert r2[7] == "hit"
    scenes = r2[0]
    assert len(scenes) == 1 and scenes[0].visual_summary == "a car driving"
    assert r2[6] is None  # thumbnail not cached on hit (finalize regenerates)


def test_understanding_cache_misses_on_different_analysis_inputs() -> None:
    workflow, calls = _understanding_fakes()
    cache = _MemoryCache()
    stage = VideoMusicAnalysisAndPlanningStage(SimpleNamespace())

    _resolve_understanding(stage, workflow=workflow,
                           prep=SimpleNamespace(analysis_language="en", effective_language="en"),
                           cache=cache, job_id="j1")
    # A newer prompt that resolves to a different language must NOT reuse understanding.
    _resolve_understanding(stage, workflow=workflow,
                           prep=SimpleNamespace(analysis_language="ja", effective_language="ja"),
                           cache=cache, job_id="j2")
    assert calls == {"scene": 2, "vu": 2}


def test_understanding_cache_disabled_bypasses() -> None:
    workflow, calls = _understanding_fakes()
    stage = VideoMusicAnalysisAndPlanningStage(SimpleNamespace())
    prep = SimpleNamespace(analysis_language="en", effective_language="en")

    r = _resolve_understanding(stage, workflow=workflow, prep=prep, cache=None, job_id="j1")
    assert calls == {"scene": 1, "vu": 1}
    assert r[7] == "bypass"


def test_compression_cache_computes_sha_for_video_url_jobs(tmp_path: Path, monkeypatch) -> None:
    """video_url jobs have no precomputed sha; the worker must hash on demand so the
    content cache still engages (otherwise the cache helps no real traffic)."""
    monkeypatch.setenv("ASYNC_V2_COMPRESS_CACHE", "1")
    import EdennCode.Deployment.async_pipeline_v2.video_source_preparation as vsp

    real_compress = vsp.compress_video_to_max_height
    calls = {"n": 0}

    def counting_compress(*args, **kwargs):
        calls["n"] += 1
        return real_compress(*args, **kwargs)

    monkeypatch.setattr(vsp, "compress_video_to_max_height", counting_compress)

    source_path = tmp_path / SMOKE_VIDEO.name
    source_path.write_bytes(SMOKE_VIDEO.read_bytes())

    repo = _MemoryRepository()
    queue = _MemoryQueue()
    storage = _RecordingStorage()
    cache = _MemoryCache()
    settings = SimpleNamespace(
        workdir=tmp_path, upload_container="user-uploads",
        output_container="generated-media", audio_container_name="generated-audio",
    )
    runtime = SimpleNamespace(workflow=SimpleNamespace(video_asset_preprocess=PreprocessStage()))
    worker = VideoPreprocessWorker(
        repository=repo, queue=queue, runtime=runtime, settings=settings,  # type: ignore[arg-type]
        storage=storage, worker_id="sha-test", lease_seconds=60, cache=cache,
    )

    # Both jobs reference a source artifact with NO sha256 (video_url style), same bytes.
    _enqueue_preprocess_job(repo=repo, queue=queue, job_id="job_url_a", sha=None, source_path=source_path)
    a = asyncio.run(worker.process_one())
    assert a is not None and a.status == TaskStatus.COMPLETED
    assert calls["n"] == 1
    # The worker hashed the file on demand and populated the content cache.
    keys = list(cache.ready)
    assert any(k.startswith("compress:") for k in keys), keys

    _enqueue_preprocess_job(repo=repo, queue=queue, job_id="job_url_b", sha=None, source_path=source_path)
    b = asyncio.run(worker.process_one())
    assert b is not None and b.status == TaskStatus.COMPLETED
    assert calls["n"] == 1  # cache hit despite no precomputed sha -> ffmpeg skipped

    # The understanding cache also gets the source sha via the analysis task payload.
    analysis_task = queue.tasks["job_url_a:analysis-and-planning"]
    assert analysis_task.payload_json.get("source_sha"), "preprocess must propagate sha"


def _write_test_tone(path: Path, *, duration_s: float) -> Path:
    subprocess.run(
        [
            resolve_ffmpeg_binary(), "-y", "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={duration_s}",
            "-ac", "2", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "192k",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@pytest.mark.parametrize("water_mark", [True, False])
def test_split_finalize_cuts_the_provider_tail_off_the_complete_track(tmp_path, water_mark):
    """The delivered full track loses the provider's trailing tag, always.

    Regression for job_1e3ed627: the provider returned a track shorter than the
    video, so the generation-time tail trim stood down to preserve coverage and
    the tag rode all the way out on complete_audio_url.
    """

    track = _write_test_tone(tmp_path / "instrumental.mp3", duration_s=20.0)
    matched = _write_test_tone(tmp_path / "matched.mp3", duration_s=20.0)

    video_metadata = VideoMetadata.from_file(SMOKE_VIDEO)
    video_metadata.duration = 60.0
    video_metadata.temp_folder = str(tmp_path)

    analysis = AnalysisAndPlanningStageOutput(
        job_id="job_split_tail",
        source_video_artifact_id="job_split_tail:source_video:input",
        video_metadata=video_metadata,
        scenes=[],
        video_summary={},
        video_title="Split tail regression",
        video_description="",
        music_prompt={"style_prompt": "playful instrumental"},
        effective_modelspec="edenn_enhanced",
        include_vocals=False,
        vocal_gender="unknown",
        user_requested_language="en",
        detected_category="test",
        overall_mood="playful",
        thumbnail_path=tmp_path / "thumb.webp",
        token_usage_breakdown={},
        token_usage={},
        job_received_timestamp=123,
    )
    candidate = ProviderCandidateGenerationStageOutput(
        job_id="job_split_tail",
        model_spec=MusicGenertionModelEnum.EDENN_ENHANCED,
        # Prod hands the same file out twice: the match source and the full track.
        candidate_audio_path=track,
        complete_audio_path=track,
        secondary_complete_audio_path=None,
        primary_full_lyrics=None,
    )

    aligned_inputs: list[Path] = []

    async def fake_matching_run(stage_input):
        aligned_inputs.append(Path(stage_input.local_music_path))
        return MusicMatchingStageOutput(
            reranked_music_outputs_path=matched,
            music_start_s=0.0,
            aligned_lyrics=[],
            used_track="primary",
        )

    async def fake_remix_run(_stage_input):
        return SimpleNamespace(remixed_video_path="remixed.mp4")

    runtime = SimpleNamespace(
        music_matching_stage=SimpleNamespace(run=fake_matching_run),
        workflow=SimpleNamespace(
            post_generation_remix=SimpleNamespace(run=fake_remix_run),
            music_generation_stage=MusicGenerationStage(
                provider_a_music_provider=MagicMock(),
                provider_c_music_provider=MagicMock(),
                provider_b_music_provider=MagicMock(),
            ),
        ),
    )
    stage = VideoMusicSelectionRankingRemixFinalizeStage(runtime)

    watermarked = tmp_path / "delivered_watermarked.mp3"
    with patch(
        "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicGenerationStage."
        "music_generation_stage.append_voice_watermark_to_audio",
        return_value=watermarked,
    ) as watermark:
        output = asyncio.run(
            stage.run(
                SelectionRankingRemixFinalizeStageInput(
                    job_id="job_split_tail",
                    request_json={"water_mark": water_mark, "job_received_timestamp": 123},
                    source_video_path=SMOKE_VIDEO,
                    analysis=analysis,
                    candidate=candidate,
                )
            )
        )

    result = output.result
    if water_mark:
        assert watermark.call_count == 1
        trimmed_path = watermark.call_args.args[0]
        assert result.complete_generated_music_path == watermarked
    else:
        watermark.assert_not_called()
        trimmed_path = result.complete_generated_music_path

    assert trimmed_path != track
    assert "_trimmed_tail6s" in trimmed_path.stem
    trimmed_s = float(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(trimmed_path)],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    )
    assert abs(trimmed_s - 14.0) < 0.3

    # Alignment ran on the clean track, so no window it could have picked
    # contains the provider's tail — the delivered video included.
    assert len(aligned_inputs) == 1
    assert aligned_inputs[0] != track
    assert "_trimmed_tail6s" in aligned_inputs[0].stem
    assert result.generated_music_path == matched

def test_resolve_file_artifact_keeps_the_producers_filename(tmp_path, monkeypatch):
    """A cross-replica re-download must not shed the tail-chop marker.

    The chop's already-trimmed check is a trailing filename marker; blob names
    are provider-neutral and drop it. Downloading under the blob name made the
    finalize stage chop 6 more seconds of real music off an already-chopped
    track whenever it ran on a different replica than the candidate stage.
    """

    worker = object.__new__(_SplitStageWorkerBase)
    worker._workdir = lambda _job_id, *parts: tmp_path.joinpath(*parts)

    captured: dict[str, Path] = {}

    async def fake_download(*, url, destination, asset_label):
        captured["destination"] = Path(destination)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"audio")
        return Path(destination)

    monkeypatch.setattr(
        "EdennCode.Deployment.async_pipeline_v2.workers.split_stage_workers."
        "download_public_file_to_disk",
        fake_download,
    )

    artifact = AsyncV2Artifact(
        artifact_id="job_x:candidate_audio:primary",
        job_id="job_x",
        artifact_type="candidate_audio",
        role="primary",
        container="audio",
        blob_name="jobs/job_x/audio/candidates/candidate_audio_primary.mp3",
        url="https://example.invalid/candidate_audio_primary.mp3",
        content_type="audio/mpeg",
        local_path="/nonexistent/instrumental_trimmed_tail6s.mp3",
        metadata_json={"source_filename": "instrumental_trimmed_tail6s.mp3"},
    )
    resolved = asyncio.run(worker._resolve_file_artifact(artifact, label="candidate audio"))
    assert resolved.name == "instrumental_trimmed_tail6s.mp3"
    assert captured["destination"].name == "instrumental_trimmed_tail6s.mp3"

    # Without the recorded source filename it still falls back to the blob name.
    artifact_no_meta = AsyncV2Artifact(
        artifact_id="job_x:candidate_audio:primary",
        job_id="job_x",
        artifact_type="candidate_audio",
        role="primary",
        container="audio",
        blob_name="jobs/job_x/audio/candidates/candidate_audio_primary.mp3",
        url="https://example.invalid/candidate_audio_primary.mp3",
        content_type="audio/mpeg",
        local_path=None,
        metadata_json={},
    )
    resolved = asyncio.run(
        worker._resolve_file_artifact(artifact_no_meta, label="candidate audio")
    )
    assert resolved.name == "candidate_audio_primary.mp3"
