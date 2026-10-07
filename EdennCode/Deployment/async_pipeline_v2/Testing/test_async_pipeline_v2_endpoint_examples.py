from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.async_pipeline_v2.api import create_async_pipeline_v2_router
from EdennCode.Deployment.async_pipeline_v2.artifact_service import ArtifactStagingService
from EdennCode.Deployment.async_pipeline_v2.models import (
    AsyncV2Task,
    JobStatus,
    TaskEnvelope,
    TaskStatus,
)
from EdennCode.Deployment.async_pipeline_v2.queue.postgres_queue import PostgresTaskQueue
from EdennCode.Deployment.async_pipeline_v2.queue_names import namespaced_queue_name
from EdennCode.Deployment.async_pipeline_v2.repositories import AsyncPipelineV2Repository
from EdennCode.Deployment.async_pipeline_v2.Testing.test_async_pipeline_v2_monolith_worker import (
    _FakeOrchestrator,
    _postgres_env_available,
    _real_provider_env_available,
    _storage_env_available,
)
from EdennCode.Deployment.async_pipeline_v2.Testing.test_async_pipeline_v2_split_stage_workers import (
    _MemoryQueue,
    _MemoryRepository,
    _RecordingStorage,
    _permit_loopback_asset_url,
)
from EdennCode.Deployment.async_pipeline_v2.workers.monolith_worker import (
    VideoMusicMonolithWorker,
)
from EdennCode.Deployment.postgres_wrapper import PostgresClient
from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Deployment.storage import create_storage_service
from EdennCode.Deployment.workflows import VideoGenerationOrchestrator


# 20.6s / 320x568 (portrait) / 2.3MB — must satisfy the source-video input
# guardrails (>15s, <=150s, <=300MB) because these tests exercise guarded paths.
SMOKE_VIDEO = Path(
    "EdennCode/TestSuites/assets/smoke/videos/Videos2026-04-08_104426_347.mp4"
)


class _StatusRepository(_MemoryRepository):
    def list_stage_runs(self, job_id: str):
        return [
            stage_run
            for stage_run in self.stage_runs.values()
            if stage_run.job_id == job_id
        ]

    def list_events(self, job_id: str, *, limit: int | None = None):
        events = [event for event in self.events if event.job_id == job_id]
        return events if limit is None else events[:limit]

    def build_status_view(self, job_id: str) -> dict:
        job = self.jobs[job_id]
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
                    "payload_available": artifact.payload_json is not None,
                }
                for artifact in self.list_artifacts(job_id)
            ],
        }


class _QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return None


class _MonitoringPostgresTaskQueue:
    def __init__(self, delegate: PostgresTaskQueue) -> None:
        self.delegate = delegate
        self.transitions: list[dict[str, object]] = []

    def _record(self, action: str, task: AsyncV2Task | None) -> None:
        if task is None:
            self.transitions.append({"action": action, "status": None})
            return
        self.transitions.append(
            {
                "action": action,
                "task_id": task.task_id,
                "queue_name": task.queue_name,
                "status": task.status,
                "attempt": task.attempt,
                "lease_owner": task.lease_owner,
            }
        )

    def enqueue(self, envelope: TaskEnvelope, **kwargs) -> str:
        task_id = self.delegate.enqueue(envelope, **kwargs)
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

    def cancel_job_tasks_if_unstarted(self, **kwargs):
        return self.delegate.cancel_job_tasks_if_unstarted(**kwargs)


@contextmanager
def _serve_directory(directory: Path) -> Iterator[str]:
    handler = partial(_QuietStaticHandler, directory=str(directory))
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    except PermissionError as exc:
        pytest.skip(f"Localhost HTTP server unavailable for video_url example: {exc}")
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


def _example_client(tmp_path: Path, *, storage=None):
    repo = _StatusRepository()
    queue = _MemoryQueue()
    storage = storage if storage is not None else _RecordingStorage()
    settings = SimpleNamespace(
        workdir=tmp_path / "workdir",
        upload_container="user-uploads",
        output_container="generated-media",
        audio_container_name="generated-audio",
    )
    context = SimpleNamespace(settings=settings, storage=storage)
    staging = ArtifactStagingService(
        repository=repo,  # type: ignore[arg-type]
        storage=storage,
        upload_container=settings.upload_container,
    )
    app = FastAPI()
    app.include_router(
        create_async_pipeline_v2_router(
            context,  # type: ignore[arg-type]
            repository=repo,  # type: ignore[arg-type]
            queue=queue,  # type: ignore[arg-type]
            artifact_staging=staging,
        )
    )
    return TestClient(app), repo, queue, storage, settings


def _process_monolith_job(
    *,
    repo: _StatusRepository,
    queue: _MemoryQueue,
    settings: SimpleNamespace,
    tmp_path: Path,
    queue_name: str = "video-music-pipeline",
):
    worker = VideoMusicMonolithWorker(
        repository=repo,  # type: ignore[arg-type]
        queue=queue,  # type: ignore[arg-type]
        orchestrator=_FakeOrchestrator(tmp_path / "orchestrator_outputs"),
        settings=settings,
        storage=_RecordingStorage(),
        worker_id="endpoint-example-worker",
        queue_name=queue_name,
        lease_seconds=30,
    )
    return asyncio.run(worker.process_one())


def _make_high_resolution_video(source: Path, destination: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required for the high-resolution endpoint integration test.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vf",
        "scale=1920:1920",
        "-t",
        "16",
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


@pytest.mark.parametrize("input_mode", ["video_upload", "video_url", "video_url_wins"])
def test_async_v2_video_music_endpoint_examples_complete_worker(
    input_mode: str,
    tmp_path: Path,
) -> None:
    assert SMOKE_VIDEO.exists()
    # The monolith path only exists as the storage-less local fallback now
    # (with storage enabled every job routes to the split pipeline), so this
    # worker E2E runs against a disabled storage.
    storage = _RecordingStorage()
    storage.enabled = False
    client, repo, queue, storage, settings = _example_client(tmp_path, storage=storage)
    data = {
        "mode": "monolith",
        "modelspec": "edenn_basic",
        "user_prompt": f"endpoint integration example for {input_mode}",
        "compression_flag": "false",
        "music_volume": "0.8",
        "max_attempts": "1",
        "user_id": f"example_user_{input_mode}",
    }

    files = None
    server_context = (
        _serve_directory(SMOKE_VIDEO.parent)
        if input_mode in {"video_url", "video_url_wins"}
        else None
    )
    with (server_context if server_context is not None else nullcontext("")) as base_url:
        if input_mode in {"video_url", "video_url_wins"}:
            data["video_url"] = f"{base_url}/{SMOKE_VIDEO.name}"
        if input_mode in {"video_upload", "video_url_wins"}:
            with SMOKE_VIDEO.open("rb") as handle:
                files = {"video": ("example-upload.mp4", handle, "video/mp4")}
                response = client.post(
                    "/api/v2/jobs/video-music",
                    data=data,
                    files=files,
                )
        else:
            response = client.post("/api/v2/jobs/video-music", data=data)

        assert response.status_code == 200, response.text
        accepted = response.json()
        task = queue.get_task(accepted["task_id"])
        assert task is not None
        assert task.queue_name == "video-music-pipeline"
        assert task.task_type == "video_music_monolith"
        assert task.payload_json["source_video_artifact_id"].endswith(
            ":source_video:input"
        )

        processed = _process_monolith_job(
            repo=repo,
            queue=queue,
            settings=settings,
            tmp_path=tmp_path / input_mode,
        )
        assert processed is not None
        assert processed.status == TaskStatus.COMPLETED

        status = repo.build_status_view(accepted["job_id"])
        assert status["status"] == JobStatus.COMPLETED
        assert status["result"]["video_metadata"]["video_url"], status
        assert status["result"]["audio_metadata"]["audio_url"], status
        # build_status_view returns the raw result (modelspec is hoisted to the
        # envelope only by the public status view); assert it on the result here.
        assert status["result"]["modelspec"] == "edenn_basic"
        assert status["request"]["music_volume"] == 0.8
        assert status["request"]["source_video_artifact_id"].endswith(
            ":source_video:input"
        )

        source_artifact = next(
            artifact
            for artifact in status["artifacts"]
            if artifact["artifact_type"] == "source_video"
        )
        if input_mode == "video_upload":
            assert source_artifact["metadata"]["source_kind"] == "upload_bytes"
            # Storage is disabled in this harness, so the upload stages locally.
            assert storage.upload_calls == []
        else:
            assert source_artifact["metadata"]["source_kind"] == "remote_url"
            assert status["request"]["video_url"].startswith("http://127.0.0.1:")
            assert "requested_source_video_url" in status["request"]


@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_REAL_PROVIDER_INTEGRATION") != "1"
    or os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1"
    or os.getenv("RUN_ASYNC_V2_STORAGE_INTEGRATION") != "1"
    or not _postgres_env_available()
    or not _storage_env_available(),
    reason=(
        "Set RUN_ASYNC_V2_REAL_PROVIDER_INTEGRATION=1, "
        "RUN_ASYNC_V2_POSTGRES_INTEGRATION=1, and "
        "RUN_ASYNC_V2_STORAGE_INTEGRATION=1 with real provider/storage env."
    ),
)
@pytest.mark.parametrize(
    ("input_mode", "modelspec", "expected_include_vocals"),
    [
        ("video_upload", "edenn_basic", False),
        ("video_url", "edenn_basic", False),
        ("video_upload", "edenn_enhanced", True),
        ("video_upload", "edenn_studio", True),
    ],
    ids=[
        "endpoint-upload-basic",
        "endpoint-url-basic",
        "endpoint-upload-enhanced",
        "endpoint-upload-studio",
    ],
)
def test_async_v2_endpoint_generates_real_music_with_provider(
    input_mode: str,
    modelspec: str,
    expected_include_vocals: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _real_provider_env_available(modelspec):
        pytest.skip(f"Real provider env is not configured for {modelspec}.")

    assert SMOKE_VIDEO.exists()
    suffix = uuid.uuid4().hex
    queue_namespace = f"endpoint-real-{modelspec}-{input_mode}-{suffix}"
    monkeypatch.setenv("ASYNC_V2_QUEUE_NAMESPACE", queue_namespace)
    monkeypatch.setenv("USE_LOCAL_TEMP_DIR", "true")
    monkeypatch.setenv("LOCAL_TEMP_DIR", str(tmp_path / "workflow_temp"))

    settings = DeploymentSettings.from_env()
    storage = create_storage_service(settings)
    repo = AsyncPipelineV2Repository()
    queue = PostgresTaskQueue()
    context = SimpleNamespace(settings=settings, storage=storage)
    app = FastAPI()
    app.include_router(
        create_async_pipeline_v2_router(
            context,  # type: ignore[arg-type]
            repository=repo,
            queue=queue,
        )
    )
    client = TestClient(app)
    orchestrator = VideoGenerationOrchestrator(
        storage=storage,
        llm_image_container=settings.llm_image_container,
        llm_image_sas_ttl_minutes=settings.llm_image_sas_ttl_minutes,
        llm_image_cleanup_delay_seconds=settings.llm_image_cleanup_delay_seconds,
    )
    worker = VideoMusicMonolithWorker(
        repository=repo,
        queue=queue,
        orchestrator=orchestrator,
        settings=settings,
        storage=storage,
        worker_id=f"endpoint-real-worker-{suffix}",
        queue_name=namespaced_queue_name("video-music-pipeline", settings=settings),
        lease_seconds=2400,
    )
    job_id = None
    uploaded: list[tuple[str, str]] = []
    server_context = (
        _serve_directory(SMOKE_VIDEO.parent)
        if input_mode == "video_url"
        else None
    )

    try:
        with (server_context if server_context is not None else nullcontext("")) as base_url:
            # New v2 contract: a freestanding lyrics_prompt keys the vocal
            # path; no verbose flag, no music_style_prompt. Legacy-payload
            # acceptance is pinned separately in test_video_music_prompt_contract.
            prompt_data = (
                {
                    "user_prompt": (
                        "Generate upbeat pop music for this short smoke-test video. "
                        "Keep it concise and synchronized to the visual pacing."
                    ),
                    "lyrics_prompt": (
                        "Write short English female vocal lyrics with a bright hook. "
                        "Use simple original words suitable for a test clip."
                    ),
                }
                if expected_include_vocals
                else {
                    "user_prompt": (
                        "Generate upbeat music for this short smoke-test video. "
                        "Keep it concise and synchronized to the visual pacing."
                    ),
                }
            )
            form_data = {
                "mode": "monolith",
                "modelspec": modelspec,
                "preserve_original_audio": "false",
                "compression_flag": "false",
                "music_volume": "0.75",
                "max_attempts": "2",
                "user_id": "async_v2_endpoint_real_provider_test",
                "session_id": f"endpoint-real-session-{suffix}",
            }
            form_data.update(prompt_data)
            if input_mode == "video_url":
                form_data["video_url"] = f"{base_url}/{SMOKE_VIDEO.name}"
                response = client.post(
                    "/api/v2/jobs/video-music",
                    data=form_data,
                )
            else:
                with SMOKE_VIDEO.open("rb") as handle:
                    response = client.post(
                        "/api/v2/jobs/video-music",
                        files={
                            "video": (
                                f"endpoint-real-{modelspec}-{suffix}.mp4",
                                handle,
                                "video/mp4",
                            )
                        },
                        data=form_data,
                    )

            assert response.status_code == 200, response.text
            accepted = response.json()
            job_id = accepted["job_id"]
            task = queue.get_task(accepted["task_id"])
            assert task is not None
            assert task.queue_name == f"{queue_namespace}:video-music-pipeline"
            assert task.task_type == "video_music_monolith"

            processed = None
            status_view = repo.build_status_view(job_id)
            for _ in range(3):
                processed = asyncio.run(worker.process_one())
                status_view = repo.build_status_view(job_id)
                if processed is None:
                    time.sleep(5)
                    continue
                if processed.status == TaskStatus.COMPLETED:
                    break
                if processed.status == TaskStatus.QUEUED:
                    time.sleep(35)
                    continue
                break

            assert processed is not None
            assert processed.status == TaskStatus.COMPLETED, status_view
            assert status_view["status"] == JobStatus.COMPLETED, status_view
            result = status_view["result"]
            assert result["request_metadata"]["modelspec"] == modelspec
            assert result["video_metadata"]["video_url"], result
            assert result["audio_metadata"]["audio_url"], result
            assert result["video_metadata"]["geometry"]["duration"] > 0
            assert (
                result["request_metadata"]["include_vocals"]
                is expected_include_vocals
            )
            assert result["response_metadata"]["job_received_timestamp"] is not None
            assert result["response_metadata"]["job_finished_timestamp"] is not None

            artifacts = repo.list_artifacts(job_id)
            for artifact in artifacts:
                if artifact.container and artifact.blob_name:
                    uploaded.append((artifact.container, artifact.blob_name))
            artifact_types = {artifact.artifact_type for artifact in artifacts}
            assert {"source_video", "matched_audio", "remixed_video"}.issubset(
                artifact_types
            )
            if modelspec in {"edenn_enhanced", "edenn_studio"}:
                assert result["audio_metadata"]["complete_audio_url"], result
                assert "complete_audio" in artifact_types
    finally:
        if job_id:
            try:
                for artifact in repo.list_artifacts(job_id):
                    if artifact.container and artifact.blob_name:
                        uploaded.append((artifact.container, artifact.blob_name))
            except Exception:
                pass
        seen = set()
        for container, blob_name in uploaded:
            if (container, blob_name) in seen:
                continue
            seen.add((container, blob_name))
            if hasattr(storage, "delete_blob"):
                storage.delete_blob(container=container, blob_name=blob_name)
        if job_id:
            with PostgresClient.from_env() as db:
                db.run_sql("DELETE FROM async_v2_jobs WHERE job_id = %s", params=[job_id])


@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_REAL_PROVIDER_INTEGRATION") != "1"
    or os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1"
    or os.getenv("RUN_ASYNC_V2_STORAGE_INTEGRATION") != "1"
    or not _postgres_env_available()
    or not _storage_env_available()
    or not _real_provider_env_available("edenn_basic"),
    reason=(
        "Set RUN_ASYNC_V2_REAL_PROVIDER_INTEGRATION=1, "
        "RUN_ASYNC_V2_POSTGRES_INTEGRATION=1, and "
        "RUN_ASYNC_V2_STORAGE_INTEGRATION=1 with real basic provider/storage env."
    ),
)
def test_async_v2_endpoint_high_resolution_upload_queue_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert SMOKE_VIDEO.exists()
    high_res_video = tmp_path / "high-resolution-input.mp4"
    _make_high_resolution_video(SMOKE_VIDEO, high_res_video)

    suffix = uuid.uuid4().hex
    queue_namespace = f"endpoint-highres-edenn_basic-{suffix}"
    monkeypatch.setenv("ASYNC_V2_QUEUE_NAMESPACE", queue_namespace)
    monkeypatch.setenv("USE_LOCAL_TEMP_DIR", "true")
    monkeypatch.setenv("LOCAL_TEMP_DIR", str(tmp_path / "workflow_temp"))

    settings = DeploymentSettings.from_env()
    storage = create_storage_service(settings)
    repo = AsyncPipelineV2Repository()
    queue = _MonitoringPostgresTaskQueue(PostgresTaskQueue())
    context = SimpleNamespace(settings=settings, storage=storage)
    app = FastAPI()
    app.include_router(
        create_async_pipeline_v2_router(
            context,  # type: ignore[arg-type]
            repository=repo,
            queue=queue,  # type: ignore[arg-type]
        )
    )
    client = TestClient(app)
    orchestrator = VideoGenerationOrchestrator(
        storage=storage,
        llm_image_container=settings.llm_image_container,
        llm_image_sas_ttl_minutes=settings.llm_image_sas_ttl_minutes,
        llm_image_cleanup_delay_seconds=settings.llm_image_cleanup_delay_seconds,
    )
    worker_id = f"endpoint-highres-worker-{suffix}"
    worker = VideoMusicMonolithWorker(
        repository=repo,
        queue=queue,  # type: ignore[arg-type]
        orchestrator=orchestrator,
        settings=settings,
        storage=storage,
        worker_id=worker_id,
        queue_name=namespaced_queue_name("video-music-pipeline", settings=settings),
        lease_seconds=2400,
    )

    job_id = None
    uploaded: list[tuple[str, str]] = []
    try:
        with high_res_video.open("rb") as handle:
            response = client.post(
                "/api/v2/jobs/video-music",
                files={
                    "video": (
                        f"endpoint-highres-{suffix}.mp4",
                        handle,
                        "video/mp4",
                    )
                },
                data={
                    "mode": "monolith",
                    "modelspec": "edenn_basic",
                    "user_prompt": (
                        "Generate upbeat instrumental music for a high-resolution "
                        "short video queue-system smoke test."
                    ),
                    "preserve_original_audio": "false",
                    "compression_flag": "true",
                    "compression_max_height": "1280",
                    "music_volume": "0.75",
                    "max_attempts": "2",
                    "user_id": "async_v2_endpoint_highres_queue_test",
                    "session_id": f"endpoint-highres-session-{suffix}",
                },
            )

        assert response.status_code == 200, response.text
        accepted = response.json()
        job_id = accepted["job_id"]
        task_id = accepted["task_id"]

        queued_task = queue.get_task(task_id)
        assert queued_task is not None
        assert queued_task.status == TaskStatus.QUEUED
        assert queued_task.queue_name == f"{queue_namespace}:video-music-pipeline"
        assert queued_task.task_type == "video_music_monolith"
        assert queued_task.attempt == 0

        processed = asyncio.run(worker.process_one())
        status_view = repo.build_status_view(job_id)

        assert processed is not None
        assert processed.status == TaskStatus.COMPLETED, status_view
        final_task = queue.get_task(task_id)
        assert final_task is not None
        assert final_task.status == TaskStatus.COMPLETED
        assert final_task.attempt == 1
        assert final_task.lease_owner is None
        assert final_task.finished_at is not None

        transition_pairs = [
            (transition["action"], transition["status"])
            for transition in queue.transitions
        ]
        assert ("enqueue", TaskStatus.QUEUED) in transition_pairs
        assert ("lease", TaskStatus.LEASED) in transition_pairs
        assert ("complete", TaskStatus.COMPLETED) in transition_pairs
        lease_transition = next(
            transition
            for transition in queue.transitions
            if transition["action"] == "lease"
        )
        assert lease_transition["attempt"] == 1
        assert lease_transition["lease_owner"] == worker_id

        assert status_view["status"] == JobStatus.COMPLETED, status_view
        result = status_view["result"]
        assert result["request_metadata"]["modelspec"] == "edenn_basic"
        assert result["video_metadata"]["video_url"], result
        assert result["audio_metadata"]["audio_url"], result
        assert result["video_metadata"]["geometry"]["duration"] > 0
        assert result["video_metadata"]["geometry"]["height"] <= 1280
        assert result["request_metadata"]["include_vocals"] is False

        artifacts = repo.list_artifacts(job_id)
        for artifact in artifacts:
            if artifact.container and artifact.blob_name:
                uploaded.append((artifact.container, artifact.blob_name))
        artifact_types = {artifact.artifact_type for artifact in artifacts}
        assert {"source_video", "matched_audio", "remixed_video"}.issubset(
            artifact_types
        )
        source_roles = {
            artifact.role: artifact
            for artifact in artifacts
            if artifact.artifact_type == "source_video"
        }
        assert source_roles["input"].metadata_json["width"] == 1920
        assert source_roles["input"].metadata_json["height"] == 1920
        compressed = source_roles["compressed_input"]
        assert compressed.metadata_json["compression_applied"] is True
        compression = compressed.metadata_json["compression"]
        assert compression["requested"] is True
        assert compression["applied"] is True
        assert compression["max_height"] == 1280

        event_types = [event.event_type for event in repo.list_events(job_id)]
        assert "job.created" in event_types
        assert "stage.started" in event_types
        assert "job.completed" in event_types
    finally:
        if job_id:
            try:
                for artifact in repo.list_artifacts(job_id):
                    if artifact.container and artifact.blob_name:
                        uploaded.append((artifact.container, artifact.blob_name))
            except Exception:
                pass
        seen = set()
        for container, blob_name in uploaded:
            if (container, blob_name) in seen:
                continue
            seen.add((container, blob_name))
            if hasattr(storage, "delete_blob"):
                storage.delete_blob(container=container, blob_name=blob_name)
        if job_id:
            with PostgresClient.from_env() as db:
                db.run_sql("DELETE FROM async_v2_jobs WHERE job_id = %s", params=[job_id])


# 5.2s — deliberately below the 15s minimum, for guardrail rejection tests.
SMOKE_SHORT_VIDEO = Path(
    "EdennCode/TestSuites/assets/smoke/videos/"
    "sample_clip.mp4"
)


def test_monolith_worker_rejects_too_short_source_with_10007(tmp_path: Path) -> None:
    # The guardrail fires inside the monolith handle path, before compression
    # and the workflow: the job fails permanently with the specific code and
    # the fake orchestrator is never invoked.
    _, repo, queue, storage, settings = _example_client(tmp_path)
    job_id = "job_monolith_guardrail_short"
    source_artifact_id = f"{job_id}:source_video:input"
    source_path = tmp_path / SMOKE_SHORT_VIDEO.name
    source_path.write_bytes(SMOKE_SHORT_VIDEO.read_bytes())
    repo.create_job(
        job_id=job_id,
        job_type="video_music",
        request_json={
            "source_video_artifact_id": source_artifact_id,
            "modelspec": "edenn_basic",
            "mode": "monolith",
            "max_attempts": 2,
        },
        status=JobStatus.QUEUED,
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
            task_id=f"{job_id}:monolith",
            job_id=job_id,
            queue_name="video-music-pipeline",
            task_type="video_music_monolith",
            payload_json={"source_video_artifact_id": source_artifact_id},
            priority=0,
            max_attempts=2,
            idempotency_key=f"{job_id}:monolith:v1",
        )
    )

    processed = _process_monolith_job(
        repo=repo, queue=queue, settings=settings, tmp_path=tmp_path
    )

    assert processed is not None
    assert processed.status == TaskStatus.FAILED
    job = repo.get_job(job_id)
    assert job.status == JobStatus.FAILED
    assert job.error_json["error_code"] == 10007
    assert job.error_json["retryable"] is False
    event_types = [event.event_type for event in repo.events if event.job_id == job_id]
    assert "job.failed" in event_types
