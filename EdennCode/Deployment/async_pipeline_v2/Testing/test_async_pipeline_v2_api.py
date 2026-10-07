from __future__ import annotations

import json
import os
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.async_pipeline_v2.api import create_async_pipeline_v2_router
from EdennCode.Deployment.async_pipeline_v2.artifact_service import ArtifactStagingService
from EdennCode.Deployment.async_pipeline_v2.models import (
    AsyncV2Artifact,
    AsyncV2Job,
    AsyncV2JobEvent,
    JobStatus,
    TaskEnvelope,
    new_id,
)
from EdennCode.Deployment.async_pipeline_v2.queue.postgres_queue import PostgresTaskQueue
from EdennCode.Deployment.async_pipeline_v2.repositories import AsyncPipelineV2Repository
from EdennCode.Deployment.async_pipeline_v2.workers.monolith_worker import (
    VideoMusicMonolithWorker,
)
from EdennCode.Deployment.postgres_wrapper import PostgresClient
from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Deployment.storage import create_storage_service
from EdennCode.Deployment.workflows import VideoGenerationResult
from EdennCode.env import load_env
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata import VideoMetadata


# 20.6s / 320x568 (portrait) / 2.3MB — must satisfy the source-video input guardrails
# (>15s, ≤150s, ≤300MB) because these tests create real video-music jobs.
SMOKE_VIDEO = Path(
    "EdennCode/TestSuites/assets/smoke/videos/Videos2026-04-08_104426_347.mp4"
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


class _DisabledStorage:
    enabled = False


class _MemoryRepository:
    def __init__(self) -> None:
        self.jobs: dict[str, AsyncV2Job] = {}
        self.artifacts: dict[str, AsyncV2Artifact] = {}
        self.events: list[AsyncV2JobEvent] = []

    def create_job(self, **kwargs):
        job = AsyncV2Job(
            job_id=kwargs.get("job_id") or new_id("job"),
            session_id=kwargs.get("session_id"),
            creator_user_id=kwargs.get("creator_user_id"),
            job_type=kwargs["job_type"],
            status=kwargs.get("status") or JobStatus.QUEUED,
            request_json=kwargs["request_json"],
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

    def add_artifact(self, **kwargs):
        artifact = AsyncV2Artifact(
            artifact_id=kwargs.get("artifact_id") or new_id("artifact"),
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

    def list_events(self, job_id: str, *, limit: int | None = None):
        events = [event for event in self.events if event.job_id == job_id]
        return events if limit is None else events[:limit]

    def build_status_view(self, job_id: str):
        job = self.jobs[job_id]
        artifacts = self.list_artifacts(job_id)
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
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "stages": [],
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
                    "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
                }
                for artifact in artifacts
            ],
        }


class _MemoryQueue:
    def __init__(self) -> None:
        self.envelopes: list[TaskEnvelope] = []
        self.canceled_job_ids: list[str] = []

    def enqueue(self, envelope: TaskEnvelope) -> str:
        self.envelopes.append(envelope)
        return envelope.task_id

    def cancel_job_tasks(self, *, job_id: str) -> int:
        self.canceled_job_ids.append(job_id)
        return 1

    def cancel_job_tasks_if_unstarted(self, *, job_id: str) -> tuple[int, bool]:
        # In this fake nothing is ever leased, so cancel is always allowed.
        self.canceled_job_ids.append(job_id)
        return 1, True


def _test_client(tmp_path: Path, *, middleware=None, storage=None):
    repo = _MemoryRepository()
    queue = _MemoryQueue()
    storage = storage if storage is not None else _RecordingStorage()
    context = SimpleNamespace(
        settings=SimpleNamespace(
            workdir=tmp_path,
            upload_container="user-uploads",
            output_container="voicestorage",
            audio_container_name="audio",
        ),
        storage=storage,
    )
    staging = ArtifactStagingService(
        repository=repo,  # type: ignore[arg-type]
        storage=storage,
        upload_container="user-uploads",
    )
    app = FastAPI()
    if middleware is not None:
        app.middleware("http")(middleware)
    app.include_router(
        create_async_pipeline_v2_router(
            context,  # type: ignore[arg-type]
            repository=repo,  # type: ignore[arg-type]
            queue=queue,  # type: ignore[arg-type]
            artifact_staging=staging,
        )
    )
    return TestClient(app), repo, queue, storage


def _assert_public_status_contract(payload: dict) -> None:
    assert "stages" not in payload
    assert "artifacts" not in payload
    # The stored request is not echoed on read — the caller sees only job status,
    # result and events.
    assert "request" not in payload
    # Envelope fields are never duplicated inside the result block.
    result = payload.get("result")
    if isinstance(result, dict):
        for dup in ("job_id", "status", "version"):
            assert dup not in result, f"envelope field '{dup}' duplicated in result"

    def _walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                assert "blob" not in key.lower(), payload
                _walk(child)
        elif isinstance(value, list):
            for item in value:
                _walk(item)

    _walk(payload)


def test_async_v2_api_stages_actual_video_and_enqueues_split_job(tmp_path: Path) -> None:
    assert SMOKE_VIDEO.exists()
    client, repo, queue, storage = _test_client(tmp_path)

    with SMOKE_VIDEO.open("rb") as handle:
        asset_response = client.post(
            "/api/v2/assets/video",
            files={"video": ("smoke video.mp4", handle, "video/mp4")},
            data={"creator_user_id": "creator_api_test", "session_id": "session_api_test"},
        )
    assert asset_response.status_code == 200, asset_response.text
    asset_payload = asset_response.json()
    assert asset_payload["artifact_type"] == "source_video"
    assert "blob_name" not in asset_payload
    source_asset = repo.get_artifact(asset_payload["artifact_id"])
    assert source_asset is not None
    assert source_asset.blob_name.endswith("/source_video.mp4")
    assert asset_payload["metadata"]["duration"] > 0
    assert asset_payload["metadata"]["width"] == 320
    assert asset_payload["metadata"]["height"] == 568
    assert storage.upload_calls[0]["content_type"] == "video/mp4"

    job_response = client.post(
        "/api/v2/jobs/video-music",
        json={
            "source_video_artifact_id": asset_payload["artifact_id"],
            "modelspec": "provider_a",
            "user_prompt": "upbeat short social video music",
            "preserve_original_audio": False,
            "music_volume": 0.8,
            "compression_flag": True,
            "priority": 9,
            "max_attempts": 2,
        },
    )
    assert job_response.status_code == 200, job_response.text
    job_payload = job_response.json()
    assert job_payload["status"] == JobStatus.QUEUED
    # Storage is enabled, so the job routes to the split pipeline (the only
    # supported production path) even though the request named no mode.
    assert queue.envelopes[0].queue_name == "video-preprocess"
    assert queue.envelopes[0].task_type == "video_preprocess"
    assert queue.envelopes[0].payload_json["mode"] == "split"
    assert queue.envelopes[0].priority == 9
    assert queue.envelopes[0].max_attempts == 2

    status_response = client.get(job_payload["status_url"])
    assert status_response.status_code == 200, status_response.text
    status_payload = status_response.json()
    _assert_public_status_contract(status_payload)
    # The request is not echoed on read (see _assert_public_status_contract); the
    # submit-time wiring is asserted on the stored row instead.
    stored_request = repo.jobs[job_payload["job_id"]].request_json
    assert stored_request["modelspec"] == "edenn_basic"
    assert stored_request["mode"] == "split"
    assert stored_request["compression_flag"] is True
    assert stored_request["compression_max_height"] == 1280
    assert stored_request["requested_source_video_artifact_id"] == asset_payload["artifact_id"]
    assert stored_request["source_video_artifact_id"].endswith(":source_video:input")
    source_artifacts = repo.list_artifacts(job_payload["job_id"])
    assert source_artifacts[0].artifact_type == "source_video"
    assert source_artifacts[0].metadata_json["source_artifact_id"] == asset_payload["artifact_id"]

    events_response = client.get(f"/api/v2/jobs/{job_payload['job_id']}/events")
    assert events_response.status_code == 200, events_response.text
    assert [event["event_type"] for event in events_response.json()["events"]] == ["job.created"]

    cancel_response = client.post(f"/api/v2/jobs/{job_payload['job_id']}/cancel")
    assert cancel_response.status_code == 200, cancel_response.text
    assert cancel_response.json()["status"] == JobStatus.CANCELED
    assert queue.canceled_job_ids == [job_payload["job_id"]]
    assert repo.jobs[job_payload["job_id"]].status == JobStatus.CANCELED


def test_async_v2_json_transport_invalid_field_is_422_not_500(tmp_path: Path) -> None:
    """A malformed JSON field is the client's mistake: sanitized 422, never an
    opaque 500, and the pydantic error blob (input echoes/ctx) stays server-side."""
    client, repo, queue, storage = _test_client(tmp_path)

    response = client.post(
        "/api/v2/jobs/video-music",
        json={
            "video_url": "https://cdn.example.com/videos/a.mp4",
            "mode": "bogus-mode",
            "music_volume": "not-a-number",
        },
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "mode" in detail and "music_volume" in detail
    assert "bogus-mode" not in detail  # no input echo
    assert not repo.jobs  # nothing persisted for an invalid request


def test_async_v2_json_transport_unparseable_body_is_400(tmp_path: Path) -> None:
    client, repo, queue, storage = _test_client(tmp_path)
    response = client.post(
        "/api/v2/jobs/video-music",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400, response.text
    assert "JSON" in response.json()["detail"]


def test_async_v2_cancel_returns_409_once_job_started(tmp_path: Path) -> None:
    """Once any task has started, work is billed and runs to completion: cancel
    is rejected with 409 and the job status is left untouched."""
    client, repo, queue, storage = _test_client(tmp_path)

    job_response = client.post(
        "/api/v2/jobs/video-music",
        data={
            "video_url": "https://cdn.example.com/videos/started.mp4?sig=test",
            "modelspec": "edenn_basic",
            "mode": "monolith",
        },
    )
    assert job_response.status_code == 200, job_response.text
    job_id = job_response.json()["job_id"]

    # Simulate a worker having leased the task already.
    queue.cancel_job_tasks_if_unstarted = lambda *, job_id: (0, False)  # type: ignore[assignment]
    repo.update_job_status(job_id, status=JobStatus.PROCESSING)

    cancel_response = client.post(f"/api/v2/jobs/{job_id}/cancel")
    assert cancel_response.status_code == 409, cancel_response.text
    assert "billed" in cancel_response.json()["detail"]
    assert repo.jobs[job_id].status == JobStatus.PROCESSING


def test_async_v2_api_accepts_video_url_without_frontend_artifact_id(tmp_path: Path) -> None:
    client, repo, queue, storage = _test_client(tmp_path)

    job_response = client.post(
        "/api/v2/jobs/video-music",
        data={
            "video_url": "https://cdn.example.com/videos/direct-source.mp4?sig=test",
            "modelspec": "edenn_basic",
            "user_prompt": "upbeat direct url music",
            "mode": "monolith",
            "priority": "4",
            "max_attempts": "1",
            "user_id": "creator_direct_url",
            "session_id": "session_direct_url",
        },
    )

    assert job_response.status_code == 200, job_response.text
    job_payload = job_response.json()
    assert job_payload["status"] == JobStatus.QUEUED
    # An explicit "monolith" request does not steer routing: with storage
    # enabled the job still runs the split pipeline, and the requested mode is
    # only recorded on the job.created event.
    assert queue.envelopes[0].queue_name == "video-preprocess"
    assert queue.envelopes[0].task_type == "video_preprocess"
    assert queue.envelopes[0].payload_json["mode"] == "split"
    assert queue.envelopes[0].payload_json["source_video_artifact_id"].endswith(
        ":source_video:input"
    )
    assert queue.envelopes[0].priority == 4
    assert queue.envelopes[0].max_attempts == 1
    assert storage.upload_calls == []
    created_events = [e for e in repo.events if e.event_type == "job.created"]
    assert created_events[-1].payload_json["mode"] == "split"
    assert created_events[-1].payload_json["requested_mode"] == "monolith"

    status_response = client.get(job_payload["status_url"])
    assert status_response.status_code == 200, status_response.text
    status_payload = status_response.json()
    _assert_public_status_contract(status_payload)
    stored_request = repo.jobs[job_payload["job_id"]].request_json
    assert stored_request["video_url"] == (
        "https://cdn.example.com/videos/direct-source.mp4?sig=test"
    )
    assert stored_request["requested_source_video_url"] == (
        "https://cdn.example.com/videos/direct-source.mp4?sig=test"
    )
    assert stored_request["source_video_artifact_id"].endswith(":source_video:input")
    assert "requested_source_video_artifact_id" not in stored_request
    source_artifact = repo.list_artifacts(job_payload["job_id"])[0]
    assert source_artifact.artifact_type == "source_video"
    assert source_artifact.url == (
        "https://cdn.example.com/videos/direct-source.mp4?sig=test"
    )
    assert source_artifact.content_type == "video/mp4"
    assert source_artifact.metadata_json["source_url"] == (
        "https://cdn.example.com/videos/direct-source.mp4?sig=test"
    )
    assert source_artifact.metadata_json["source_filename"] == "direct-source.mp4"
    assert repo.jobs[job_payload["job_id"]].creator_user_id == "creator_direct_url"
    assert repo.jobs[job_payload["job_id"]].session_id == "session_direct_url"


def test_async_v2_api_falls_back_to_monolith_only_without_storage(tmp_path: Path) -> None:
    """A storage-less environment (local single-process runs) is the one place
    the monolith path is still used: split physically cannot move artifacts
    between workers there, so the default routes monolith instead of failing."""
    client, repo, queue, storage = _test_client(tmp_path, storage=_DisabledStorage())

    job_response = client.post(
        "/api/v2/jobs/video-music",
        data={
            "video_url": "https://cdn.example.com/videos/local-run.mp4?sig=test",
            "modelspec": "edenn_basic",
        },
    )
    assert job_response.status_code == 200, job_response.text
    assert queue.envelopes[0].queue_name == "video-music-pipeline"
    assert queue.envelopes[0].task_type == "video_music_monolith"
    assert queue.envelopes[0].payload_json["mode"] == "monolith"


def test_async_v2_api_explicit_split_without_storage_is_400(tmp_path: Path) -> None:
    client, repo, queue, storage = _test_client(tmp_path, storage=_DisabledStorage())

    job_response = client.post(
        "/api/v2/jobs/video-music",
        data={
            "video_url": "https://cdn.example.com/videos/local-run.mp4?sig=test",
            "modelspec": "edenn_basic",
            "mode": "split",
        },
    )
    assert job_response.status_code == 400, job_response.text
    assert "storage" in job_response.json()["detail"]
    assert queue.envelopes == []


def test_async_v2_api_accepts_video_upload_like_v1(tmp_path: Path) -> None:
    assert SMOKE_VIDEO.exists()
    client, repo, queue, storage = _test_client(tmp_path)

    with SMOKE_VIDEO.open("rb") as handle:
        job_response = client.post(
            "/api/v2/jobs/video-music",
            files={"video": ("direct-upload.mp4", handle, "video/mp4")},
            data={
                "modelspec": "edenn_basic",
                "user_prompt": "upbeat direct upload music",
                "compression_flag": "true",
                "music_volume": "0.75",
                "user_id": "creator_upload",
            },
        )

    assert job_response.status_code == 200, job_response.text
    job_payload = job_response.json()
    # Storage is enabled, so the upload-sourced job routes split like any other.
    assert queue.envelopes[0].queue_name == "video-preprocess"
    assert queue.envelopes[0].task_type == "video_preprocess"
    assert queue.envelopes[0].payload_json["source_video_artifact_id"].endswith(
        ":source_video:input"
    )
    assert storage.upload_calls[0]["content_type"] == "video/mp4"

    status_payload = client.get(job_payload["status_url"]).json()
    _assert_public_status_contract(status_payload)
    stored_request = repo.jobs[job_payload["job_id"]].request_json
    assert stored_request["source_video_artifact_id"].endswith(":source_video:input")
    assert stored_request["compression_flag"] is True
    assert stored_request["music_volume"] == 0.75
    source_artifact = repo.list_artifacts(job_payload["job_id"])[0]
    assert source_artifact.artifact_type == "source_video"
    assert source_artifact.metadata_json["source_kind"] == "upload_bytes"
    assert repo.jobs[job_payload["job_id"]].creator_user_id == "creator_upload"


def test_async_v2_api_requires_exactly_one_video_source(tmp_path: Path) -> None:
    client, repo, _, _ = _test_client(tmp_path)
    asset_job = repo.create_job(
        job_id="asset_job_existing_source",
        job_type="asset_staging",
        request_json={"source": "test"},
        status=JobStatus.COMPLETED,
    )
    source = repo.add_artifact(
        artifact_id="artifact_existing_source",
        job_id=asset_job.job_id,
        artifact_type="source_video",
        role="input",
        url="https://cdn.example.com/existing.mp4",
        content_type="video/mp4",
        metadata_json={"source_url": "https://cdn.example.com/existing.mp4"},
    )

    missing_response = client.post(
        "/api/v2/jobs/video-music",
        json={"modelspec": "edenn_basic"},
    )
    assert missing_response.status_code == 400
    assert "video upload or video_url" in missing_response.json()["detail"]

    both_response = client.post(
        "/api/v2/jobs/video-music",
        json={
            "source_video_artifact_id": source.artifact_id,
            "video_url": "https://cdn.example.com/direct.mp4",
            "modelspec": "edenn_basic",
        },
    )
    assert both_response.status_code == 400
    assert "not both" in both_response.json()["detail"]


def test_async_v2_job_status_refreshes_signed_result_urls(tmp_path: Path) -> None:
    client, repo, _, storage = _test_client(tmp_path)
    job_id = "job_completed_signed_url_refresh"
    expired_result = {
        "job_id": job_id,
        "status": JobStatus.COMPLETED,
        "upload_url": "https://storage.test/old/upload.mp4?sig=expired",
        "audio_url": "https://storage.test/old/matched.mp3?sig=expired",
        "complete_audio_url": "https://storage.test/old/complete.mp3?sig=expired",
        "secondary_complete_audio_url": "https://storage.test/old/secondary.mp3?sig=expired",
        "video_url": "https://storage.test/old/remixed.mp4?sig=expired",
        "thumbnail_blob": f"jobs/{job_id}/thumbnail/thumbnail.webp",
        "thumbnail_url": None,
        "video_metadata": {"duration": 5.2, "width": 360, "height": 360},
        "scenes": [],
        "video_summary": {},
        "music_description": {},
        "modelspec": "edenn_basic",
    }
    repo.create_job(
        job_id=job_id,
        job_type="video_music",
        request_json={"modelspec": "edenn_basic"},
        status=JobStatus.COMPLETED,
    )
    repo.update_job_status(
        job_id,
        status=JobStatus.COMPLETED,
        current_stage="selection_ranking_remix_finalize",
        progress_percent=100,
        result_json=expired_result,
        finished=True,
    )
    artifact_specs = [
        ("source_video", "input", "user-uploads", f"jobs/{job_id}/input/source.mp4"),
        ("matched_audio", "primary", "audio", f"jobs/{job_id}/audio/matched.mp3"),
        ("complete_audio", "primary", "audio", f"jobs/{job_id}/audio/complete.mp3"),
        (
            "secondary_complete_audio",
            "secondary",
            "audio",
            f"jobs/{job_id}/audio/secondary.mp3",
        ),
        ("remixed_video", "final", "voicestorage", f"jobs/{job_id}/video/remixed.mp4"),
        (
            "thumbnail",
            "thumbnail",
            "voicestorage",
            f"jobs/{job_id}/thumbnail/thumbnail.webp",
        ),
    ]
    for artifact_type, role, container, blob_name in artifact_specs:
        repo.add_artifact(
            artifact_id=f"{job_id}:{artifact_type}:{role}",
            job_id=job_id,
            artifact_type=artifact_type,
            role=role,
            container=container,
            blob_name=blob_name,
            url=f"https://storage.test/old/{artifact_type}?sig=expired",
            content_type="application/octet-stream",
        )

    sas_calls = []

    def fresh_url(
        *,
        container: str,
        blob_name: str,
        ttl_minutes: int | None = None,
        require_signed: bool = False,
    ) -> str:
        sas_calls.append((container, blob_name))
        return (
            f"https://storage.test/fresh/{container}/{blob_name}"
            f"?sig=fresh-{len(sas_calls)}"
        )

    storage.generate_sas_url = fresh_url  # type: ignore[method-assign]

    status_response = client.get(f"/api/v2/jobs/{job_id}")
    assert status_response.status_code == 200, status_response.text
    status_payload = status_response.json()
    _assert_public_status_contract(status_payload)
    result = status_payload["result"]

    assert result["upload_url"] == (
        f"https://storage.test/fresh/user-uploads/jobs/{job_id}/input/source.mp4?sig=fresh-1"
    )
    assert result["audio_url"] == (
        f"https://storage.test/fresh/audio/jobs/{job_id}/audio/matched.mp3?sig=fresh-2"
    )
    assert result["complete_audio_url"] == (
        f"https://storage.test/fresh/audio/jobs/{job_id}/audio/complete.mp3?sig=fresh-3"
    )
    assert result["secondary_complete_audio_url"] == (
        f"https://storage.test/fresh/audio/jobs/{job_id}/audio/secondary.mp3?sig=fresh-4"
    )
    assert result["video_url"] == (
        f"https://storage.test/fresh/voicestorage/jobs/{job_id}/video/remixed.mp4?sig=fresh-5"
    )
    assert result["thumbnail_url"] == (
        f"https://storage.test/fresh/voicestorage/jobs/{job_id}/thumbnail/thumbnail.webp?sig=fresh-6"
    )
    assert len(sas_calls) == len(artifact_specs)
    assert repo.jobs[job_id].result_json == expired_result


def test_async_v2_job_status_refreshes_full_tracks_even_without_blob(tmp_path: Path) -> None:
    # Multi-image regression: nested full_tracks[].url must be re-signed against the
    # current account on GET. A track carrying only a stale URL on a foreign account
    # (no persisted blob) is refreshed by deriving the blob from that URL.
    client, repo, _, storage = _test_client(tmp_path)
    job_id = "job_multi_image_full_tracks_refresh"
    stale_result = {
        "job_id": job_id,
        "status": JobStatus.COMPLETED,
        "modelspec": "edenn_enhanced",
        "video_metadata": {"duration": 15.0, "width": 720, "height": 1024},
        "scenes": [],
        "video_summary": {},
        "music_description": "p",
        "full_tracks": [
            {  # blob persisted -> re-signed from blob
                "blob": f"jobs/{job_id}/audio/complete/primary_audio.mp3",
                "url": "https://primary.storage.example.invalid/audio/old/primary.mp3?sig=expired",
                "filename": "primary_audio.mp3", "duration_s": 203.0, "size_bytes": 10,
            },
            {  # NO blob, stale URL on a DIFFERENT account -> blob derived from url
                "url": f"https://voice.storage.example.invalid/audio/jobs/{job_id}/audio/complete/tracks/track_1.mp3?sig=expired",
                "filename": "track_1.mp3", "duration_s": 177.0, "size_bytes": 9,
            },
        ],
    }
    repo.create_job(job_id=job_id, job_type="multi_image",
                    request_json={"modelspec": "edenn_enhanced"}, status=JobStatus.COMPLETED)
    repo.update_job_status(job_id, status=JobStatus.COMPLETED, current_stage="multi_image",
                           progress_percent=100, result_json=stale_result, finished=True)

    def fresh_url(*, container: str, blob_name: str, ttl_minutes=None, require_signed=False) -> str:
        return f"https://primary.storage.example.invalid/{container}/{blob_name}?sig=fresh"

    storage.generate_sas_url = fresh_url  # type: ignore[method-assign]

    result = client.get(f"/api/v2/jobs/{job_id}").json()["result"]
    tracks = result["full_tracks"]
    # both tracks now point at the current account, freshly signed
    assert tracks[0]["url"] == (
        f"https://primary.storage.example.invalid/audio/jobs/{job_id}/audio/complete/primary_audio.mp3?sig=fresh"
    )
    assert tracks[1]["url"] == (
        f"https://primary.storage.example.invalid/audio/jobs/{job_id}/audio/complete/tracks/track_1.mp3?sig=fresh"
    )
    assert "voicestorage" not in tracks[1]["url"]


def test_async_v2_job_status_remints_flat_urls_from_stale_foreign_account(tmp_path: Path) -> None:
    # Legacy job written by an older image: flat *_url fields carry a baked SAS on a
    # decommissioned/renamed account (blob-not-found) and NO *_blob field. GET must
    # still re-sign them against the current account by deriving the blob from the URL.
    client, repo, _, storage = _test_client(tmp_path)
    job_id = "job_legacy_foreign_account_flat_urls"
    stale = {
        "job_id": job_id,
        "status": JobStatus.COMPLETED,
        "modelspec": "edenn_enhanced",
        "video_metadata": {"duration": 10.0, "width": 720, "height": 1024},
        "scenes": [], "video_summary": {}, "music_description": "p",
        # baked foreign-account URLs, no blob fields at all
        "audio_url": f"https://primary.storage.example.invalid/audio/jobs/{job_id}/audio/complete/primary_audio.mp3?sig=expired",
        "video_url": f"https://primary.storage.example.invalid/generated-media/jobs/{job_id}/video/slideshow.mp4?sig=expired",
        "thumbnail_url": f"https://primary.storage.example.invalid/generated-media/jobs/{job_id}/thumbnail/thumb.webp?sig=expired",
    }
    repo.create_job(job_id=job_id, job_type="multi_image",
                    request_json={"modelspec": "edenn_enhanced"}, status=JobStatus.COMPLETED)
    repo.update_job_status(job_id, status=JobStatus.COMPLETED, current_stage="multi_image",
                           progress_percent=100, result_json=stale, finished=True)

    def fresh_url(*, container: str, blob_name: str, ttl_minutes=None, require_signed=False) -> str:
        return f"https://storage.test/fresh/{container}/{blob_name}?sig=fresh"

    storage.generate_sas_url = fresh_url  # type: ignore[method-assign]

    result = client.get(f"/api/v2/jobs/{job_id}").json()["result"]
    # audio -> audio container; video/thumbnail -> output container; all on the current account
    assert result["audio_url"] == f"https://storage.test/fresh/audio/jobs/{job_id}/audio/complete/primary_audio.mp3?sig=fresh"
    assert result["video_url"] == f"https://storage.test/fresh/voicestorage/jobs/{job_id}/video/slideshow.mp4?sig=fresh"
    assert result["thumbnail_url"] == f"https://storage.test/fresh/voicestorage/jobs/{job_id}/thumbnail/thumb.webp?sig=fresh"
    for k in ("audio_url", "video_url", "thumbnail_url"):
        assert "primarystorage" not in result[k]


def test_async_v2_job_status_builds_missing_thumbnail_url_from_blob(tmp_path: Path) -> None:
    client, repo, _, storage = _test_client(tmp_path)
    job_id = "job_completed_thumbnail_blob_only"
    thumbnail_blob = f"jobs/{job_id}/thumbnail/thumbnail.webp"
    result_json = {
        "job_id": job_id,
        "status": JobStatus.COMPLETED,
        "video_url": "https://storage.test/final-video.mp4?sig=still-present",
        "thumbnail_blob": thumbnail_blob,
        "thumbnail_url": None,
        "video_metadata": {"duration": 5.2, "width": 360, "height": 360},
        "scenes": [],
        "video_summary": {},
        "music_description": {},
        "modelspec": "edenn_basic",
    }
    repo.create_job(
        job_id=job_id,
        job_type="video_music",
        request_json={"modelspec": "edenn_basic"},
        status=JobStatus.COMPLETED,
    )
    repo.update_job_status(
        job_id,
        status=JobStatus.COMPLETED,
        current_stage="selection_ranking_remix_finalize",
        progress_percent=100,
        result_json=result_json,
        finished=True,
    )

    def fresh_url(
        *,
        container: str,
        blob_name: str,
        ttl_minutes: int | None = None,
        require_signed: bool = False,
    ) -> str:
        return f"https://storage.test/fresh/{container}/{blob_name}?sig=fresh"

    storage.generate_sas_url = fresh_url  # type: ignore[method-assign]

    status_response = client.get(f"/api/v2/jobs/{job_id}")
    assert status_response.status_code == 200, status_response.text
    status_payload = status_response.json()
    _assert_public_status_contract(status_payload)
    result = status_payload["result"]

    assert result["thumbnail_url"] == (
        f"https://storage.test/fresh/voicestorage/{thumbnail_blob}?sig=fresh"
    )
    assert "thumbnail_blob" not in result
    assert repo.jobs[job_id].result_json == result_json


def _nested_result(job_id: str, *, music_prompt: Any) -> dict[str, Any]:
    """A result in the current block shape, with expired media URLs."""
    return {
        "job_id": job_id,
        "status": JobStatus.COMPLETED,
        "version": "test",
        "modelspec": "edenn_basic",
        "response_metadata": {},
        "cost_metadata": {},
        "video_metadata": {
            "video_url": "https://storage.test/old/video.mp4?sig=expired",
            "thumbnail_url": "https://storage.test/old/thumb.webp?sig=expired",
            "geometry": {"width": 360, "height": 360, "duration": 5.2},
            "video_summary": {},
        },
        "audio_metadata": {
            "audio_url": "https://storage.test/old/matched.mp3?sig=expired",
            "complete_audio_url": "https://storage.test/old/complete.mp3?sig=expired",
            "music_title": "Neon Drift",
            "music_description": music_prompt,
        },
    }


def test_async_v2_job_status_refreshes_nested_result_urls(tmp_path: Path) -> None:
    # The refresh tables key off field PATHS. If they were left pointing at the old
    # flat names, this GET would still return 200 — carrying the expired SAS baked
    # into the stored row. Assert on the `fresh` marker, not merely on presence.
    client, repo, _, storage = _test_client(tmp_path)
    job_id = "job_nested_video_music_refresh"
    stored = _nested_result(job_id, music_prompt={})
    repo.create_job(
        job_id=job_id,
        job_type="video_music",
        request_json={"modelspec": "edenn_basic"},
        status=JobStatus.COMPLETED,
    )
    repo.update_job_status(
        job_id,
        status=JobStatus.COMPLETED,
        current_stage="selection_ranking_remix_finalize",
        progress_percent=100,
        result_json=stored,
        finished=True,
    )
    artifact_specs = [
        ("matched_audio", "primary", "audio", f"jobs/{job_id}/audio/matched.mp3"),
        ("complete_audio", "primary", "audio", f"jobs/{job_id}/audio/complete.mp3"),
        ("remixed_video", "final", "voicestorage", f"jobs/{job_id}/video/remixed.mp4"),
        ("thumbnail", "thumbnail", "voicestorage", f"jobs/{job_id}/thumbnail/t.webp"),
    ]
    for artifact_type, role, container, blob_name in artifact_specs:
        repo.add_artifact(
            artifact_id=f"{job_id}:{artifact_type}:{role}",
            job_id=job_id,
            artifact_type=artifact_type,
            role=role,
            container=container,
            blob_name=blob_name,
            url=f"https://storage.test/old/{artifact_type}?sig=expired",
            content_type="application/octet-stream",
        )

    sas_calls: list[tuple[str, str]] = []

    def fresh_url(
        *,
        container: str,
        blob_name: str,
        ttl_minutes: int | None = None,
        require_signed: bool = False,
    ) -> str:
        sas_calls.append((container, blob_name))
        return f"https://storage.test/fresh/{container}/{blob_name}?sig=fresh"

    storage.generate_sas_url = fresh_url  # type: ignore[method-assign]

    status_payload = client.get(f"/api/v2/jobs/{job_id}").json()
    _assert_public_status_contract(status_payload)
    result = status_payload["result"]

    assert result["audio_metadata"]["audio_url"] == (
        f"https://storage.test/fresh/audio/jobs/{job_id}/audio/matched.mp3?sig=fresh"
    )
    assert result["audio_metadata"]["complete_audio_url"] == (
        f"https://storage.test/fresh/audio/jobs/{job_id}/audio/complete.mp3?sig=fresh"
    )
    assert result["video_metadata"]["video_url"] == (
        f"https://storage.test/fresh/voicestorage/jobs/{job_id}/video/remixed.mp4?sig=fresh"
    )
    assert result["video_metadata"]["thumbnail_url"] == (
        f"https://storage.test/fresh/voicestorage/jobs/{job_id}/thumbnail/t.webp?sig=fresh"
    )
    # Every artifact re-signed exactly once: no stale URL slipped through, and the
    # legacy flat tier did not fire a second time on the same row.
    assert len(sas_calls) == len(artifact_specs)
    assert repo.jobs[job_id].result_json == stored


def test_async_v2_job_status_refreshes_nested_multi_image_urls_without_artifacts(
    tmp_path: Path,
) -> None:
    # Multi-image renders its slideshow as `output_video`, and the current response
    # carries no blob names at all — so with no artifact rows to key on, every URL
    # has to be re-signed by recovering the blob path from the stale URL itself.
    client, repo, _, storage = _test_client(tmp_path)
    job_id = "job_nested_multi_image_refresh"
    stored = _nested_result(job_id, music_prompt="a warm lo-fi loop")
    stored["video_metadata"]["video_url"] = (
        f"https://old.blob.core.windows.net/voicestorage/jobs/{job_id}/video/slideshow.mp4?sig=expired"
    )
    stored["audio_metadata"]["complete_audio_url"] = (
        f"https://old.blob.core.windows.net/audio/jobs/{job_id}/audio/complete_audio.mp3?sig=expired"
    )
    repo.create_job(
        job_id=job_id,
        job_type="multi_image",
        request_json={"modelspec": "edenn_basic"},
        status=JobStatus.COMPLETED,
    )
    repo.update_job_status(
        job_id,
        status=JobStatus.COMPLETED,
        current_stage="multi_image",
        progress_percent=100,
        result_json=stored,
        finished=True,
    )

    def fresh_url(
        *,
        container: str,
        blob_name: str,
        ttl_minutes: int | None = None,
        require_signed: bool = False,
    ) -> str:
        return f"https://current.blob.core.windows.net/{container}/{blob_name}?sig=fresh"

    storage.generate_sas_url = fresh_url  # type: ignore[method-assign]

    result = client.get(f"/api/v2/jobs/{job_id}").json()["result"]

    assert result["video_metadata"]["video_url"] == (
        f"https://current.blob.core.windows.net/voicestorage/jobs/{job_id}/video/slideshow.mp4?sig=fresh"
    )
    assert result["audio_metadata"]["complete_audio_url"] == (
        f"https://current.blob.core.windows.net/audio/jobs/{job_id}/audio/complete_audio.mp3?sig=fresh"
    )
    # Re-signed against the CURRENT account, not the decommissioned one.
    assert "old.blob.core.windows.net" not in json.dumps(result)
    assert "sig=expired" not in json.dumps(result)


def test_async_v2_result_persistence_generates_thumbnail_when_missing(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / SMOKE_VIDEO.name
    source_path.write_bytes(SMOKE_VIDEO.read_bytes())
    matched_audio = tmp_path / "matched_audio.wav"
    remixed_video = tmp_path / "remixed_video.mp4"
    matched_audio.write_bytes(b"RIFF....WAVEfmt matched")
    remixed_video.write_bytes(source_path.read_bytes())
    job_id = "job_generate_missing_thumbnail"
    repo = _MemoryRepository()
    storage = _RecordingStorage()
    settings = SimpleNamespace(
        workdir=tmp_path,
        upload_container="user-uploads",
        output_container="voicestorage",
        audio_container_name="audio",
    )
    source_artifact = repo.add_artifact(
        artifact_id=f"{job_id}:source_video:input",
        job_id=job_id,
        artifact_type="source_video",
        role="input",
        container="voicestorage",
        blob_name=f"jobs/{job_id}/input/source_video.mp4",
        url="https://storage.test/source_video.mp4?sig=fake",
        content_type="video/mp4",
        local_path=str(source_path),
    )
    result = VideoGenerationResult(
        video_metadata=VideoMetadata.from_file(source_path),
        scenes=[],
        video_summary={},
        video_title="Thumbnail regression",
        video_description="",
        music_prompt={},
        music_prompt_in_chinese={},
        generated_music_path=matched_audio,
        complete_generated_music_path=None,
        secondary_complete_generated_music_path=None,
        remixed_video_path=remixed_video,
        include_vocals=False,
        vocal_gender="unknown",
        lyrics_timestamps=[],
        word_level_lyrics_timestamps=[],
        thumbnail_path=tmp_path / "missing_thumbnail.webp",
        used_music_model_spec="edenn_basic",
    )

    response = VideoMusicMonolithWorker(
        repository=repo,  # type: ignore[arg-type]
        queue=_MemoryQueue(),  # type: ignore[arg-type]
        orchestrator=None,
        settings=settings,
        storage=storage,
        worker_id="thumbnail-regression-worker",
    )._persist_result_assets_and_build_response(
        job_id=job_id,
        request={"modelspec": "edenn_basic"},
        source_artifact=source_artifact,
        source_video_path=source_path,
        result=result,
    )

    assert response.video_metadata.thumbnail_url == (
        f"https://storage.test/voicestorage/jobs/{job_id}/thumbnail/thumbnail.webp?sig=fake"
    )
    # The blob name is a storage internal now — assert it on the upload, not the response.
    thumbnail_upload = next(
        call for call in storage.upload_calls if call["content_type"] == "image/webp"
    )
    assert thumbnail_upload["blob_name"] == f"jobs/{job_id}/thumbnail/thumbnail.webp"
    assert thumbnail_upload["path"].exists()


def test_async_v2_api_split_mode_starts_with_video_preprocess(tmp_path: Path) -> None:
    client, _, queue, _ = _test_client(tmp_path)

    with SMOKE_VIDEO.open("rb") as handle:
        asset_response = client.post(
            "/api/v2/assets/video",
            files={"video": ("split-source.mp4", handle, "video/mp4")},
            data={"creator_user_id": "creator_split_test", "session_id": "session_split_test"},
        )
    assert asset_response.status_code == 200, asset_response.text
    asset_payload = asset_response.json()

    job_response = client.post(
        "/api/v2/jobs/video-music",
        json={
            "source_video_artifact_id": asset_payload["artifact_id"],
            "modelspec": "edenn_basic",
            "user_prompt": "upbeat split mode test",
            "mode": "split",
            "compression_flag": True,
            "compression_max_height": 128,
            "priority": 11,
            "max_attempts": 2,
        },
    )
    assert job_response.status_code == 200, job_response.text

    assert queue.envelopes[0].queue_name == "video-preprocess"
    assert queue.envelopes[0].task_type == "video_preprocess"
    assert queue.envelopes[0].task_id.endswith(":video-preprocess")
    assert queue.envelopes[0].payload_json["mode"] == "split"
    assert queue.envelopes[0].priority == 11
    assert queue.envelopes[0].max_attempts == 2


def test_async_v2_api_split_mode_can_namespace_queue_for_isolated_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASYNC_V2_QUEUE_NAMESPACE", "pytest-api")
    client, _, queue, _ = _test_client(tmp_path)

    with SMOKE_VIDEO.open("rb") as handle:
        asset_response = client.post(
            "/api/v2/assets/video",
            files={"video": ("split-source.mp4", handle, "video/mp4")},
            data={"creator_user_id": "creator_split_test", "session_id": "session_split_test"},
        )
    assert asset_response.status_code == 200, asset_response.text
    asset_payload = asset_response.json()

    job_response = client.post(
        "/api/v2/jobs/video-music",
        json={
            "source_video_artifact_id": asset_payload["artifact_id"],
            "modelspec": "edenn_basic",
            "user_prompt": "upbeat split mode test",
            "mode": "split",
        },
    )
    assert job_response.status_code == 200, job_response.text

    assert queue.envelopes[0].queue_name == "pytest-api:video-preprocess"
    assert queue.envelopes[0].task_type == "video_preprocess"


def test_async_v2_api_split_mode_rejects_local_only_source_artifact(tmp_path: Path) -> None:
    client, repo, queue, _ = _test_client(tmp_path)
    local_source = tmp_path / "local-only.mp4"
    local_source.write_bytes(SMOKE_VIDEO.read_bytes())
    asset_job = repo.create_job(
        job_id="asset_job_local_only",
        job_type="asset_staging",
        request_json={"source": "local"},
        status=JobStatus.COMPLETED,
    )
    source = repo.add_artifact(
        artifact_id="artifact_local_only_source",
        job_id=asset_job.job_id,
        artifact_type="source_video",
        role="input",
        content_type="video/mp4",
        local_path=str(local_source),
        metadata_json={"source_kind": "local_path", "source_filename": local_source.name},
    )

    job_response = client.post(
        "/api/v2/jobs/video-music",
        json={
            "source_video_artifact_id": source.artifact_id,
            "modelspec": "edenn_basic",
            "user_prompt": "split mode should reject local-only source",
            "mode": "split",
        },
    )

    assert job_response.status_code == 400
    assert "durable URL" in job_response.json()["detail"]
    assert queue.envelopes == []


def test_async_v2_api_split_mode_requires_storage(tmp_path: Path) -> None:
    repo = _MemoryRepository()
    queue = _MemoryQueue()
    context = SimpleNamespace(
        settings=SimpleNamespace(workdir=tmp_path, upload_container="user-uploads"),
        storage=_DisabledStorage(),
    )
    app = FastAPI()
    app.include_router(
        create_async_pipeline_v2_router(
            context,  # type: ignore[arg-type]
            repository=repo,  # type: ignore[arg-type]
            queue=queue,  # type: ignore[arg-type]
        )
    )
    client = TestClient(app)
    asset_job = repo.create_job(
        job_id="asset_job_remote_no_storage",
        job_type="asset_staging",
        request_json={"source": "url"},
        status=JobStatus.COMPLETED,
    )
    source = repo.add_artifact(
        artifact_id="artifact_remote_no_storage",
        job_id=asset_job.job_id,
        artifact_type="source_video",
        role="input",
        content_type="video/mp4",
        metadata_json={
            "source_kind": "remote_url",
            "source_url": "https://cdn.example.com/source.mp4",
            "source_filename": "source.mp4",
        },
    )

    job_response = client.post(
        "/api/v2/jobs/video-music",
        json={
            "source_video_artifact_id": source.artifact_id,
            "modelspec": "edenn_basic",
            "user_prompt": "split mode should require storage",
            "mode": "split",
            "compression_flag": False,
        },
    )

    assert job_response.status_code == 400
    assert "requires storage" in job_response.json()["detail"]
    assert queue.envelopes == []


@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1"
    or os.getenv("RUN_ASYNC_V2_STORAGE_INTEGRATION") != "1"
    or not _postgres_env_available()
    or not _storage_env_available(),
    reason="Set RUN_ASYNC_V2_POSTGRES_INTEGRATION=1 and RUN_ASYNC_V2_STORAGE_INTEGRATION=1.",
)
def test_async_v2_api_real_postgres_storage_upload_enqueue_cancel(tmp_path: Path) -> None:
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
    uploaded: list[tuple[str, str]] = []
    job_ids: list[str] = []

    try:
        with SMOKE_VIDEO.open("rb") as handle:
            asset_response = client.post(
                "/api/v2/assets/video",
                files={"video": (f"api-real-{uuid.uuid4().hex}.mp4", handle, "video/mp4")},
                data={
                    "creator_user_id": "async_v2_api_real_test",
                    "session_id": "async_v2_api_real_session",
                },
            )
        assert asset_response.status_code == 200, asset_response.text
        asset_payload = asset_response.json()
        job_ids.append(asset_payload["job_id"])
        source_asset = repo.get_artifact(asset_payload["artifact_id"])
        assert source_asset is not None
        uploaded.append((source_asset.container, source_asset.blob_name))
        assert asset_payload["metadata"]["duration"] > 0
        assert asset_payload["url"]

        job_response = client.post(
            "/api/v2/jobs/video-music",
            json={
                "source_video_artifact_id": asset_payload["artifact_id"],
                "modelspec": "edenn_enhanced",
                "user_prompt": "sync energetic pop music to the short clip",
                "preserve_original_audio": False,
                "music_volume": 0.75,
                "priority": 6,
                "max_attempts": 2,
            },
        )
        assert job_response.status_code == 200, job_response.text
        job_payload = job_response.json()
        job_ids.append(job_payload["job_id"])

        status_response = client.get(job_payload["status_url"])
        assert status_response.status_code == 200, status_response.text
        status_payload = status_response.json()
        _assert_public_status_contract(status_payload)
        assert status_payload["status"] in {JobStatus.QUEUED, JobStatus.PROCESSING}
        assert status_payload["priority"] == 6
        stored_request = repo.jobs[job_payload["job_id"]].request_json
        assert stored_request["modelspec"] == "edenn_enhanced"
        assert "include_vocals" not in stored_request
        assert "vocal_gender" not in stored_request
        source_artifact = repo.list_artifacts(job_payload["job_id"])[0]
        assert source_artifact.artifact_type == "source_video"
        assert source_artifact.metadata_json["source_artifact_id"] == asset_payload["artifact_id"]

        events_response = client.get(job_payload["events_url"])
        assert events_response.status_code == 200, events_response.text
        event_types = [event["event_type"] for event in events_response.json()["events"]]
        assert "job.created" in event_types

        cancel_response = client.post(f"/api/v2/jobs/{job_payload['job_id']}/cancel")
        assert cancel_response.status_code == 200, cancel_response.text
        assert cancel_response.json()["status"] == JobStatus.CANCELED

        task = queue.get_task(job_payload["task_id"])
        assert task is not None
        assert task.status == "canceled"
    finally:
        seen = set()
        for container, blob_name in uploaded:
            if container and blob_name and (container, blob_name) not in seen:
                seen.add((container, blob_name))
                if hasattr(storage, "delete_blob"):
                    storage.delete_blob(container=container, blob_name=blob_name)
        if job_ids:
            with PostgresClient.from_env() as db:
                for job_id in job_ids:
                    db.run_sql("DELETE FROM async_v2_jobs WHERE job_id = %s", params=[job_id])


def test_async_v2_job_status_restores_declared_key_order(tmp_path: Path) -> None:
    # The result lives in a JSONB column, which reorders object keys by (length,
    # name). Without a fix, the same logical result serializes one way from v1 and
    # another from v2. Assert v2 hands back the declared order.
    client, repo, _, _ = _test_client(tmp_path)
    job_id = "job_key_order"
    stored = _nested_result(job_id, music_prompt={})
    # Simulate what JSONB does to the row on the way in.
    jsonb_like = {k: stored[k] for k in sorted(stored, key=lambda k: (len(k), k))}
    repo.create_job(
        job_id=job_id,
        job_type="video_music",
        request_json={"modelspec": "edenn_basic"},
        status=JobStatus.COMPLETED,
    )
    repo.update_job_status(
        job_id,
        status=JobStatus.COMPLETED,
        current_stage="selection_ranking_remix_finalize",
        progress_percent=100,
        result_json=jsonb_like,
        finished=True,
    )

    resp = client.get(f"/api/v2/jobs/{job_id}").json()
    # modelspec is upleveled to the envelope, not nested in the result; the
    # envelope duplicates (job_id/status/version) are stripped from the block.
    assert resp["modelspec"] == "edenn_basic"
    result = resp["result"]
    # cost_metadata is stripped at egress — no cost accounting leaves the service.
    assert list(result) == [
        "response_metadata",
        "video_metadata",
        "audio_metadata",
    ]
    # Blocks are ordered too. Only the keys the row actually carries are emitted, so
    # assert the order is the declared order restricted to those — not a fixed list.
    from EdennCode.Deployment.api_video_generation import AudioMetadataBlock

    declared = [
        key
        for key in AudioMetadataBlock.model_fields
        if key in result["audio_metadata"]
    ]
    assert list(result["audio_metadata"]) == declared
    # Nothing dropped on the way through.
    assert set(result["audio_metadata"]) == set(stored["audio_metadata"])


def test_async_v2_job_status_leaves_a_legacy_flat_result_intact(tmp_path: Path) -> None:
    # A job completed before the response was restructured is replayed verbatim.
    # Reordering must not prune the keys it doesn't recognise.
    client, repo, _, _ = _test_client(tmp_path)
    job_id = "job_legacy_key_order"
    legacy = {
        "job_id": job_id,
        "status": JobStatus.COMPLETED,
        "audio_url": "https://storage.test/a.mp3?sig=x",
        "video_url": "https://storage.test/v.mp4?sig=x",
        "full_tracks": [],
        "planning_metadata": {},
        "modelspec": "edenn_basic",
        # Legacy spelling of the cost block — must be stripped at egress like
        # the current cost_metadata block.
        "cost": {"creation_cost": 0.065, "creation_times": 1},
    }
    repo.create_job(
        job_id=job_id,
        job_type="multi_image",
        request_json={"modelspec": "edenn_basic"},
        status=JobStatus.COMPLETED,
    )
    repo.update_job_status(
        job_id,
        status=JobStatus.COMPLETED,
        current_stage="multi_image",
        progress_percent=100,
        result_json=legacy,
        finished=True,
    )

    resp = client.get(f"/api/v2/jobs/{job_id}").json()
    # modelspec is upleveled to the envelope even from a legacy flat result and
    # the envelope duplicates + cost accounting are stripped; the rest is
    # replayed verbatim.
    assert resp["modelspec"] == "edenn_basic"
    result = resp["result"]
    assert set(result) == set(legacy) - {"modelspec", "job_id", "status", "cost"}
    assert result["full_tracks"] == []
    assert result["planning_metadata"] == {}


def test_async_v2_job_status_does_not_echo_the_request(tmp_path: Path) -> None:
    # The stored request holds what the caller sent plus server-injected internals
    # (recommendation asset ids, orchestration knobs, a worker-local vocal path).
    # None of it is returned on read — the status response carries no request block
    # at all. The worker reads request_json from the database, not this view.
    client, repo, _, _ = _test_client(tmp_path)
    job_id = "job_request_echo"
    request_json = {
        "user_prompt": "Create a steady instrumental electronic track.",
        "modelspec": "edenn_basic",
        "music_volume": 0.8,
        "include_vocals": False,
        "video_id": "v1",
        "creative_id": "c1",
        "primary_music_id": "m1",
        "selected_music_id": "m1",
        "alignment_id": "a1",
        "mode": "monolith",
        "source_video_artifact_id": f"{job_id}:source_video:input",
        "vocal_sample_path": "/app/api_workdir/tmp/vocal.m4a",
    }
    repo.create_job(
        job_id=job_id,
        job_type="video_music",
        request_json=request_json,
        status=JobStatus.QUEUED,
    )

    payload = client.get(f"/api/v2/jobs/{job_id}").json()

    assert "request" not in payload
    # The row itself is untouched — the worker still needs every one of them.
    assert repo.jobs[job_id].request_json == request_json


def _valid_image_bytes(width: int = 768, height: int = 768) -> bytes:
    """A real, decodable PNG at/above the 720px minimum edge for staging validation."""
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (width, height), (120, 60, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_v2_multi_image_rejects_undersized_image(tmp_path: Path) -> None:
    """An image below the 720px minimum edge is a clean 400 at submit."""
    client, repo, queue, storage = _test_client(tmp_path)
    resp = client.post(
        "/api/v2/jobs/multi-image-music",
        files=[
            ("images", ("ok1.png", _valid_image_bytes(), "image/png")),
            ("images", ("small.png", _valid_image_bytes(480, 900), "image/png")),
            ("images", ("ok2.png", _valid_image_bytes(), "image/png")),
        ],
        data={"user_prompt": "x"},
    )
    assert resp.status_code == 400, resp.text
    assert "720px" in resp.json()["detail"]


def test_v2_multi_image_priority_capped_at_three(tmp_path: Path) -> None:
    """Priority is four levels 0-3; 4+ is rejected by request validation (422)."""
    client, repo, queue, storage = _test_client(tmp_path)
    resp = client.post(
        "/api/v2/jobs/multi-image-music",
        files=[
            ("images", (f"i{n}.png", _valid_image_bytes(), "image/png")) for n in range(3)
        ],
        data={"user_prompt": "x", "priority": "4"},
    )
    assert resp.status_code == 422, resp.text


def test_v2_submit_stamps_principal_into_request_json(tmp_path: Path) -> None:
    """An authenticated submit persists auth_user_id/auth_key_prefix in request_json."""
    import logging

    from EdennCode.Deployment.auth.key_store import Principal
    from EdennCode.Deployment.auth.middleware import create_auth_middleware
    from EdennCode.Deployment.Testing.test_auth_middleware import StubKeyStore

    principal = Principal(user_id="user-3", key_prefix="sk-abc123def")
    middleware = create_auth_middleware(
        mode="enforce",
        key_store=StubKeyStore(principal),
        logger=logging.getLogger("test-v2-submit-auth"),
    )
    client, repo, queue, storage = _test_client(tmp_path, middleware=middleware)

    response = client.post(
        "/api/v2/jobs/multi-image-music",
        files=[
            ("images", ("image1.png", _valid_image_bytes(), "image/png")),
            ("images", ("image2.png", _valid_image_bytes(), "image/png")),
            ("images", ("image3.png", _valid_image_bytes(), "image/png")),
        ],
        data={"user_prompt": "test prompt"},
        headers={"Authorization": "Bearer sk-anything"},
    )

    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    job = repo.jobs[job_id]
    assert job.request_json["auth_user_id"] == "user-3"
    assert job.request_json["auth_key_prefix"] == "sk-abc123def"
    assert job.creator_user_id == "user-3"  # enforce mode override
    # request_json (the payload) must match the DB column under enforce, too.
    assert job.request_json["creator_user_id"] == "user-3"


# ── Source-video input guardrails at the API boundary ─────────────────────────

# 5.2s — deliberately below the 15s minimum, for guardrail rejection tests.
SMOKE_SHORT_VIDEO = Path(
    "EdennCode/TestSuites/assets/smoke/videos/"
    "sample_clip.mp4"
)


def _staged_source_artifact(repo, *, artifact_id: str, metadata: dict) -> None:
    repo.add_artifact(
        artifact_id=artifact_id,
        job_id="job_prestaged",
        artifact_type="source_video",
        role="input",
        container="user-uploads",
        blob_name="jobs/job_prestaged/input/source_video.mp4",
        url="https://storage.test/user-uploads/jobs/job_prestaged/input/source_video.mp4?sig=fake",
        content_type="video/mp4",
        local_path=None,
        metadata_json=metadata,
    )


def test_create_job_rejects_too_long_staged_artifact_with_10006(tmp_path: Path) -> None:
    client, repo, queue, _ = _test_client(tmp_path)
    _staged_source_artifact(
        repo,
        artifact_id="staged_too_long",
        metadata={"size_bytes": 1024, "duration": 200.0},
    )
    response = client.post(
        "/api/v2/jobs/video-music",
        json={"source_video_artifact_id": "staged_too_long", "user_prompt": "x"},
    )
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error_code"] == 10006
    assert detail["retryable"] is False
    # Rejected before any job row or task exists.
    assert repo.jobs == {}
    assert queue.envelopes == []


def test_create_job_rejects_oversize_staged_artifact_with_10005(tmp_path: Path) -> None:
    client, repo, queue, _ = _test_client(tmp_path)
    _staged_source_artifact(
        repo,
        artifact_id="staged_too_large",
        metadata={"size_bytes": 300 * 1024 * 1024 + 1, "duration": 60.0},
    )
    response = client.post(
        "/api/v2/jobs/video-music",
        json={"source_video_artifact_id": "staged_too_large", "user_prompt": "x"},
    )
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error_code"] == 10005
    assert detail["retryable"] is False
    assert repo.jobs == {}
    assert queue.envelopes == []


def test_create_job_rejects_too_short_upload_with_failed_job_row(tmp_path: Path) -> None:
    client, repo, queue, _ = _test_client(tmp_path)
    with SMOKE_SHORT_VIDEO.open("rb") as handle:
        response = client.post(
            "/api/v2/jobs/video-music",
            data={"user_prompt": "short clip", "mode": "monolith"},
            files={"video": ("short.mp4", handle, "video/mp4")},
        )
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error_code"] == 10007
    assert detail["retryable"] is False
    # The job row exists, is failed with the same code, and nothing was enqueued.
    assert len(repo.jobs) == 1
    job = next(iter(repo.jobs.values()))
    assert job.status == JobStatus.FAILED
    assert job.error_json["error_code"] == 10007
    assert queue.envelopes == []
    event_types = [event.event_type for event in repo.events if event.job_id == job.job_id]
    assert "job.failed" in event_types


def test_stage_video_asset_rejects_too_short_upload_with_10007(tmp_path: Path) -> None:
    client, repo, _, _ = _test_client(tmp_path)
    with SMOKE_SHORT_VIDEO.open("rb") as handle:
        response = client.post(
            "/api/v2/assets/video",
            files={"video": ("short.mp4", handle, "video/mp4")},
        )
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error_code"] == 10007
    assert detail["retryable"] is False
    asset_job = next(iter(repo.jobs.values()))
    assert asset_job.status == JobStatus.FAILED
    assert asset_job.error_json["error_code"] == 10007


def test_create_job_defaults_compression_flag_on_for_json_and_form(tmp_path: Path) -> None:
    # The transport-layer defaults are what actually reach workers: a JSON or
    # form submit that omits compression_flag must persist it as true.
    client, repo, queue, _ = _test_client(tmp_path)
    _staged_source_artifact(
        repo,
        artifact_id="staged_ok",
        metadata={"size_bytes": 1024, "duration": 30.0},
    )
    json_response = client.post(
        "/api/v2/jobs/video-music",
        json={"source_video_artifact_id": "staged_ok", "user_prompt": "x"},
    )
    assert json_response.status_code == 200, json_response.text
    json_job = repo.jobs[json_response.json()["job_id"]]
    assert json_job.request_json["compression_flag"] is True
    assert json_job.request_json["compression_max_height"] == 1280

    with SMOKE_VIDEO.open("rb") as handle:
        form_response = client.post(
            "/api/v2/jobs/video-music",
            data={"user_prompt": "x", "mode": "monolith"},
            files={"video": ("ok.mp4", handle, "video/mp4")},
        )
    assert form_response.status_code == 200, form_response.text
    form_job = repo.jobs[form_response.json()["job_id"]]
    assert form_job.request_json["compression_flag"] is True
