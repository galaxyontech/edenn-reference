from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from EdennCode.Deployment.async_pipeline_v2.artifact_service import (
    ArtifactStagingService,
    guess_video_content_type,
    sha256_file,
    source_video_blob_name,
)
from EdennCode.Deployment.async_pipeline_v2.models import AsyncV2Artifact, JobStatus
from EdennCode.Deployment.async_pipeline_v2.repositories import AsyncPipelineV2Repository
from EdennCode.Deployment.postgres_wrapper import PostgresClient
from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Deployment.storage import create_storage_service
from EdennCode.env import load_env


SMOKE_VIDEO = Path(
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


def _actual_video_music_request(source_video_artifact_id: str | None = None) -> dict:
    payload = {
        "modelspec": "edenn_enhanced",
        "user_prompt": "sync an energetic pop track to this short social video",
        "include_vocals": True,
        "vocal_gender": "female",
        "preserve_original_audio": False,
        "music_volume": 0.9,
        "mode": "monolith",
    }
    if source_video_artifact_id:
        payload["source_video_artifact_id"] = source_video_artifact_id
    return payload


class _RecordingStorage:
    enabled = True

    def __init__(self) -> None:
        self.upload_calls: list[dict] = []
        self.sas_calls: list[dict] = []

    def upload_path(self, *, container: str, path: Path, blob_name: str | None = None, content_type: str | None = None):
        self.upload_calls.append(
            {
                "container": container,
                "path": path,
                "blob_name": blob_name,
                "content_type": content_type,
            }
        )
        return blob_name or path.name

    def generate_sas_url(self, *, container: str, blob_name: str, ttl_minutes: int | None = None, require_signed: bool = False):
        self.sas_calls.append(
            {
                "container": container,
                "blob_name": blob_name,
                "ttl_minutes": ttl_minutes,
                "require_signed": require_signed,
            }
        )
        return f"https://storage.test/{container}/{blob_name}?sig=fake"


class _RecordingRepository:
    def __init__(self) -> None:
        self.artifacts: list[AsyncV2Artifact] = []
        self.events: list[dict] = []

    def add_artifact(self, **kwargs):
        artifact = AsyncV2Artifact(
            artifact_id=kwargs.get("artifact_id") or "artifact_recorded",
            job_id=kwargs["job_id"],
            artifact_type=kwargs["artifact_type"],
            role=kwargs.get("role"),
            container=kwargs.get("container"),
            blob_name=kwargs.get("blob_name"),
            url=kwargs.get("url"),
            content_type=kwargs.get("content_type"),
            local_path=kwargs.get("local_path"),
            metadata_json=kwargs.get("metadata_json") or {},
            created_at=datetime.now(timezone.utc),
        )
        self.artifacts.append(artifact)
        return artifact

    def add_event(self, **kwargs):
        self.events.append(kwargs)
        return kwargs


def test_stage_video_path_uploads_actual_smoke_video_and_records_metadata() -> None:
    assert SMOKE_VIDEO.exists()
    repo = _RecordingRepository()
    storage = _RecordingStorage()
    service = ArtifactStagingService(
        repository=repo,  # type: ignore[arg-type]
        storage=storage,
        upload_container="user-uploads",
    )

    staged = service.stage_video_path(
        job_id="job_stage_video",
        source_path=SMOKE_VIDEO,
        artifact_id="artifact_source_video",
        request_metadata=_actual_video_music_request(),
    )

    expected_blob = source_video_blob_name(
        job_id="job_stage_video",
        source_path=SMOKE_VIDEO,
    )
    assert staged.artifact.artifact_id == "artifact_source_video"
    assert staged.artifact.blob_name == expected_blob
    assert staged.artifact.container == "user-uploads"
    assert staged.upload_url == f"https://storage.test/user-uploads/{expected_blob}?sig=fake"
    assert staged.content_type == "video/mp4"
    assert staged.video_hash == sha256_file(SMOKE_VIDEO)
    assert staged.video_metadata["duration"] > 0
    assert staged.video_metadata["width"] == 360
    assert staged.video_metadata["height"] == 360

    assert storage.upload_calls == [
        {
            "container": "user-uploads",
            "path": SMOKE_VIDEO.resolve(),
            "blob_name": expected_blob,
            "content_type": "video/mp4",
        }
    ]
    assert repo.artifacts[0].metadata_json["sha256"] == staged.video_hash
    assert repo.artifacts[0].metadata_json["request"]["modelspec"] == "edenn_enhanced"
    assert repo.artifacts[0].metadata_json["video_metadata"]["path"] == SMOKE_VIDEO.name
    assert repo.events[0]["event_type"] == "asset.staged"
    assert repo.events[0]["payload_json"]["artifact_id"] == "artifact_source_video"


def test_stage_video_bytes_uses_sanitized_filename_and_same_metadata_path(tmp_path: Path) -> None:
    repo = _RecordingRepository()
    storage = _RecordingStorage()
    service = ArtifactStagingService(
        repository=repo,  # type: ignore[arg-type]
        storage=storage,
        upload_container="user-uploads",
    )

    staged = service.stage_video_bytes(
        job_id="job_upload_bytes",
        data=SMOKE_VIDEO.read_bytes(),
        filename="../unsafe source video.mp4",
        destination_dir=tmp_path,
        request_metadata=_actual_video_music_request(),
    )

    assert staged.local_path.parent == tmp_path.resolve()
    assert staged.local_path.name == "unsafe source video.mp4"
    assert staged.video_metadata["duration"] > 0
    assert staged.video_hash == sha256_file(SMOKE_VIDEO)


def test_guess_video_content_type_prefers_upload_content_type() -> None:
    assert guess_video_content_type(Path("clip.mov")) == "video/quicktime"
    assert guess_video_content_type(
        Path("clip.mp4"),
        upload_content_type="video/custom",
    ) == "video/custom"


@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1" or not _postgres_env_available(),
    reason="Set RUN_ASYNC_V2_POSTGRES_INTEGRATION=1 with Postgres env to run.",
)
def test_postgres_artifact_staging_with_actual_smoke_video() -> None:
    suffix = uuid.uuid4().hex
    job_id = f"test_async_v2_stage_asset_{suffix}"
    artifact_id = f"test_async_v2_source_asset_{suffix}"
    repo = AsyncPipelineV2Repository()
    storage = _RecordingStorage()
    service = ArtifactStagingService(
        repository=repo,
        storage=storage,
        upload_container="user-uploads",
    )

    try:
        repo.create_job(
            job_id=job_id,
            job_type="video_music",
            request_json=_actual_video_music_request(artifact_id),
            status=JobStatus.QUEUED,
            priority=4,
        )
        staged = service.stage_video_path(
            job_id=job_id,
            source_path=SMOKE_VIDEO,
            artifact_id=artifact_id,
            request_metadata=_actual_video_music_request(artifact_id),
        )

        persisted = repo.get_artifact(artifact_id)
        assert persisted is not None
        assert persisted.metadata_json["sha256"] == staged.video_hash
        assert persisted.metadata_json["duration"] > 0
        assert persisted.blob_name == source_video_blob_name(
            job_id=job_id,
            source_path=SMOKE_VIDEO,
        )

        events = repo.list_events(job_id)
        assert [event.event_type for event in events] == ["asset.staged"]

        status_view = repo.build_status_view(job_id)
        assert status_view["request"]["source_video_artifact_id"] == artifact_id
        assert len(status_view["artifacts"]) == 1
        assert status_view["artifacts"][0]["artifact_type"] == "source_video"
        assert status_view["artifacts"][0]["metadata"]["video_metadata"]["path"] == SMOKE_VIDEO.name
    finally:
        with PostgresClient.from_env() as client:
            client.run_sql(
                "DELETE FROM async_v2_jobs WHERE job_id = %s",
                params=[job_id],
            )


@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_STORAGE_INTEGRATION") != "1" or not _storage_env_available(),
    reason="Set RUN_ASYNC_V2_STORAGE_INTEGRATION=1 with storage env to run.",
)
def test_configured_storage_uploads_actual_smoke_video_and_cleans_up() -> None:
    suffix = uuid.uuid4().hex
    job_id = f"test_async_v2_storage_{suffix}"
    settings = DeploymentSettings.from_env()
    storage = create_storage_service(settings)
    if not getattr(storage, "enabled", False):
        pytest.skip("Configured storage service is disabled.")

    repo = _RecordingRepository()
    service = ArtifactStagingService(
        repository=repo,  # type: ignore[arg-type]
        storage=storage,
        upload_container=settings.upload_container,
    )
    staged = None
    try:
        staged = service.stage_video_path(
            job_id=job_id,
            source_path=SMOKE_VIDEO,
            artifact_id=f"test_async_v2_storage_asset_{suffix}",
            request_metadata=_actual_video_music_request(),
        )
        assert staged.upload_blob == source_video_blob_name(
            job_id=job_id,
            source_path=SMOKE_VIDEO,
        )
        assert staged.upload_url
        assert repo.artifacts[0].metadata_json["sha256"] == sha256_file(SMOKE_VIDEO)
    finally:
        if staged and staged.upload_blob and hasattr(storage, "delete_blob"):
            storage.delete_blob(
                container=settings.upload_container,
                blob_name=staged.upload_blob,
            )
