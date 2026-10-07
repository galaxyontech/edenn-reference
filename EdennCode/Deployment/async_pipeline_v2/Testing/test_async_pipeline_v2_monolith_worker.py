from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from EdennCode.Deployment.auth.usage_recorder import (
    UsageRecorder,
    set_usage_recorder_override,
)
from EdennCode.Deployment.async_pipeline_v2.artifact_service import (
    ArtifactStagingService,
    source_video_blob_name,
)
from EdennCode.Deployment.async_pipeline_v2.models import (
    AsyncV2Artifact,
    JobStatus,
    TaskEnvelope,
    TaskStatus,
)
from EdennCode.Deployment.async_pipeline_v2.queue.postgres_queue import PostgresTaskQueue
from EdennCode.Deployment.async_pipeline_v2.repositories import AsyncPipelineV2Repository
from EdennCode.Deployment.async_pipeline_v2.video_source_preparation import (
    VideoSourcePreparationService,
)
from EdennCode.Deployment.async_pipeline_v2.workers.monolith_worker import VideoMusicMonolithWorker
from EdennCode.Deployment.postgres_wrapper import PostgresClient
from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Deployment.storage import create_storage_service
from EdennCode.Deployment.workflows import VideoGenerationOrchestrator, VideoGenerationResult
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata import VideoMetadata
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.word_ts import WordTS
from EdennCode.env import load_env


# 20.6s / 320x568 (portrait) / 2.3MB — must satisfy the source-video input
# guardrails (>15s, <=150s, <=300MB) because these tests exercise guarded paths.
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


def _video_music_request(source_video_artifact_id: str) -> dict:
    return {
        "source_video_artifact_id": source_video_artifact_id,
        "modelspec": "edenn_basic",
        "user_prompt": "make this short clip feel upbeat and cinematic",
        "include_vocals": False,
        "vocal_gender": "female",
        "preserve_original_audio": False,
        "music_volume": 0.85,
        "mode": "monolith",
    }


def _real_provider_request(source_video_artifact_id: str, *, modelspec: str) -> dict:
    return {
        "source_video_artifact_id": source_video_artifact_id,
        "modelspec": modelspec,
        "user_prompt": (
            "Create a concise upbeat female vocal pop hook that matches the pacing "
            "of this short social video."
        ),
        "include_vocals": True,
        "vocal_gender": "female",
        "preserve_original_audio": False,
        "music_volume": 0.75,
        "mode": "monolith",
    }


def _save_real_provider_payload(modelspec: str, payload: dict) -> None:
    output_dir = (os.getenv("SAVE_ASYNC_V2_REAL_PROVIDER_PAYLOADS_DIR") or "").strip()
    if not output_dir:
        return
    target_dir = Path(output_dir).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"async_v2_monolith_real_provider_{modelspec}_{int(time.time())}.json"
    target_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


@dataclass
class _FakeSettings:
    upload_container: str = "user-uploads"
    output_container: str = "generated-media"
    audio_container_name: str = "generated-audio"
    workdir: Path = Path("/tmp/async-v2-worker-test")


class _RecordingStorage:
    enabled = True

    def __init__(self) -> None:
        self.upload_calls: list[dict] = []

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
        return f"https://storage.test/{container}/{blob_name}?sig=fake"


class _MemoryRepository:
    def __init__(self) -> None:
        self.artifacts: dict[str, AsyncV2Artifact] = {}
        self.events: list[dict] = []

    def get_artifact(self, artifact_id: str):
        return self.artifacts.get(artifact_id)

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
        )
        self.artifacts[artifact.artifact_id] = artifact
        return artifact

    def add_event(self, **kwargs):
        self.events.append(kwargs)
        return kwargs


class _FakeOrchestrator:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.calls: list[dict] = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        video_path = Path(kwargs["video_path"])
        self.output_dir.mkdir(parents=True, exist_ok=True)
        matched_audio = self.output_dir / "matched_audio.wav"
        complete_audio = self.output_dir / "complete_audio.wav"
        remixed_video = self.output_dir / "remixed_video.mp4"
        thumbnail = self.output_dir / "thumbnail.webp"
        matched_audio.write_bytes(b"RIFF....WAVEfmt ")
        complete_audio.write_bytes(b"RIFF....WAVEfmt complete")
        shutil.copyfile(video_path, remixed_video)
        thumbnail.write_bytes(b"RIFFWEBP")

        return VideoGenerationResult(
            video_metadata=VideoMetadata.from_file(video_path),
            scenes=[
                SimpleNamespace(
                    scene_index=0,
                    start_timestamp=0.0,
                    end_timestamp=2.6,
                    visual_summary="A quick opening shot from a square social video.",
                    key_actions="camera movement",
                    mood="energetic",
                ),
                SimpleNamespace(
                    scene_index=1,
                    start_timestamp=2.6,
                    end_timestamp=5.2,
                    visual_summary="The clip ends with a fast visual beat.",
                    key_actions="cut and motion",
                    mood="upbeat",
                ),
            ],
            video_summary={
                "overall_mood": "upbeat",
                "pacing": "fast",
                "content_type": "social_video",
            },
            video_title="Smoke Video Test",
            video_description="A short smoke-test video for async pipeline v2.",
            music_prompt={
                "style_prompt": "upbeat cinematic pop",
                "lyrics_prompt": "",
                "global_mood": "optimistic",
                "instruments": ["drums", "synth"],
            },
            music_prompt_in_chinese=None,
            generated_music_path=matched_audio,
            complete_generated_music_path=complete_audio,
            secondary_complete_generated_music_path=None,
            remixed_video_path=remixed_video,
            include_vocals=False,
            vocal_gender="female",
            lyrics_timestamps=[WordTS(text="beat", startS=0.0, endS=1.0, i=0)],
            word_level_lyrics_timestamps=[WordTS(text="beat", startS=0.0, endS=1.0, i=0)],
            matching_used_track="primary",
            thumbnail_path=thumbnail,
            token_usage={"prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17},
            token_usage_breakdown={
                "user_prompt_preprocessor": {
                    "prompt_tokens": 1,
                    "completion_tokens": 2,
                    "total_tokens": 3,
                }
            },
            used_music_model_spec=kwargs.get("modelspec", "edenn_basic"),
            user_requested_language="en",
            job_id=kwargs["job_id"],
            video_id=kwargs["video_id"],
            creative_id=kwargs["creative_id"],
            primary_music_id=kwargs["primary_music_id"],
            selected_music_id=kwargs["selected_music_id"],
            alignment_id=kwargs["alignment_id"],
            music_start_s=0.0,
            alignment_score=0.91,
            alignment_details={"policy": "fake-orchestrator"},
            generation_api_call_count=2,
        )


def test_monolith_result_persistence_uses_actual_smoke_video_metadata(tmp_path: Path) -> None:
    repo = _MemoryRepository()
    storage = _RecordingStorage()
    orchestrator = _FakeOrchestrator(tmp_path / "outputs")
    worker = VideoMusicMonolithWorker(
        repository=repo,  # type: ignore[arg-type]
        queue=None,  # type: ignore[arg-type]
        orchestrator=orchestrator,
        settings=_FakeSettings(workdir=tmp_path),
        storage=storage,
        worker_id="worker-fast",
    )
    source_artifact = AsyncV2Artifact(
        artifact_id="source_artifact_fast",
        job_id="job_fast",
        artifact_type="source_video",
        role="input",
        container="user-uploads",
        blob_name=source_video_blob_name(job_id="job_fast", source_path=SMOKE_VIDEO),
        url="https://storage.test/user-uploads/jobs/job_fast/input/source_video.mp4?sig=fake",
        content_type="video/mp4",
        local_path=str(SMOKE_VIDEO.resolve()),
        metadata_json={"video_metadata": {"path": SMOKE_VIDEO.name}},
    )
    workflow_result = asyncio.run(
        orchestrator.run(
            video_path=SMOKE_VIDEO,
            job_id="job_fast",
            video_id="video_fast",
            creative_id="creative_fast",
            primary_music_id="music_fast",
            selected_music_id="music_fast",
            alignment_id="alignment_fast",
            modelspec="edenn_basic",
        )
    )
    response = worker._persist_result_assets_and_build_response(
        job_id="job_fast",
        request=_video_music_request("source_artifact_fast"),
        source_artifact=source_artifact,
        source_video_path=SMOKE_VIDEO,
        result=workflow_result,
    )

    payload = response.model_dump(mode="json")
    assert payload["job_id"] == "job_fast"
    # Geometry is trimmed to client-facing dimensions (no path / probe extras).
    assert set(payload["video_metadata"]["geometry"]) <= {"fps", "width", "height", "duration"}
    assert "path" not in payload["video_metadata"]["geometry"]
    assert payload["video_metadata"]["geometry"]["duration"] > 0
    # The built result_json (stored row) keeps the full billing breakdown and now
    # also carries the client-facing total_cost + creative_duration first. Only the
    # client GET read path strips this down to {total_cost, creative_duration}; the
    # stored payload asserted here still sees every breakdown field.
    assert payload["cost_metadata"] == {
        "total_cost": 0.130312,
        "creative_duration": payload["video_metadata"]["geometry"]["duration"],
        "model_spec_name": "edenn-perceptron-1.1",
        "creation_cost": 0.13,
        "creation_times": 2,
        "token_num": 17,
        "token_cost": 0.000312,
    }
    assert set(repo.artifacts) == {
        "job_fast:matched_audio:primary",
        "job_fast:complete_audio:primary",
        "job_fast:remixed_video:final",
        "job_fast:thumbnail:thumbnail",
    }


def test_monolith_worker_compresses_source_before_workflow_when_requested(tmp_path: Path) -> None:
    source_path = tmp_path / SMOKE_VIDEO.name
    shutil.copyfile(SMOKE_VIDEO, source_path)
    repo = _MemoryRepository()
    storage = _RecordingStorage()
    worker = VideoMusicMonolithWorker(
        repository=repo,  # type: ignore[arg-type]
        queue=None,  # type: ignore[arg-type]
        orchestrator=_FakeOrchestrator(tmp_path / "outputs"),
        settings=_FakeSettings(workdir=tmp_path),
        storage=storage,
        worker_id="worker-compress",
    )
    source_artifact = AsyncV2Artifact(
        artifact_id="source_artifact_compress",
        job_id="job_compress",
        artifact_type="source_video",
        role="input",
        container="user-uploads",
        blob_name=source_video_blob_name(job_id="job_compress", source_path=source_path),
        url="https://storage.test/user-uploads/jobs/job_compress/input/source_video.mp4?sig=fake",
        content_type="video/mp4",
        local_path=str(source_path.resolve()),
        metadata_json={"video_metadata": VideoMetadata.from_file(source_path).to_dict()},
    )
    request = {
        **_video_music_request("source_artifact_compress"),
        "compression_flag": True,
        "compression_max_height": 128,
    }

    compressed_path, compressed_artifact, compression_info = asyncio.run(
        worker._prepare_source_video_for_workflow(
            job_id="job_compress",
            request=request,
            source_artifact=source_artifact,
            source_video_path=source_path.resolve(),
        )
    )

    assert compression_info["requested"] is True
    assert compression_info["applied"] is True
    assert compression_info["max_height"] == 128
    assert compressed_path != source_path.resolve()
    assert compressed_path.exists()
    assert compressed_artifact.artifact_id == "job_compress:source_video:compressed_input"
    assert compressed_artifact.role == "compressed_input"
    assert compressed_artifact.blob_name == "jobs/job_compress/input/compressed/source_video.mp4"
    assert repo.artifacts[compressed_artifact.artifact_id].metadata_json["output_video_metadata"]["height"] == 128
    assert storage.upload_calls[-1]["container"] == "user-uploads"
    assert storage.upload_calls[-1]["content_type"] == "video/mp4"
    assert repo.events[-1]["event_type"] == "artifact.created"
    assert repo.events[-1]["payload_json"]["compression"]["applied"] is True


def test_source_preparation_reuses_compressed_artifact_on_retry(tmp_path: Path) -> None:
    source_path = tmp_path / SMOKE_VIDEO.name
    shutil.copyfile(SMOKE_VIDEO, source_path)
    repo = _MemoryRepository()
    storage = _RecordingStorage()
    service = VideoSourcePreparationService(
        repository=repo,  # type: ignore[arg-type]
        settings=_FakeSettings(workdir=tmp_path),
        storage=storage,
        stage_name="video_preprocess",
    )
    source_artifact = AsyncV2Artifact(
        artifact_id="source_artifact_retry",
        job_id="job_retry",
        artifact_type="source_video",
        role="input",
        container="user-uploads",
        blob_name=source_video_blob_name(job_id="job_retry", source_path=source_path),
        url="https://storage.test/user-uploads/jobs/job_retry/input/source_video.mp4?sig=fake",
        content_type="video/mp4",
        local_path=str(source_path.resolve()),
        metadata_json={"video_metadata": VideoMetadata.from_file(source_path).to_dict()},
    )
    request = {
        **_video_music_request("source_artifact_retry"),
        "compression_flag": True,
        "compression_max_height": 128,
    }

    first = asyncio.run(
        service.prepare_for_workflow(
            job_id="job_retry",
            request=request,
            source_artifact=source_artifact,
            source_video_path=source_path.resolve(),
        )
    )
    second = asyncio.run(
        service.prepare_for_workflow(
            job_id="job_retry",
            request=request,
            source_artifact=source_artifact,
            source_video_path=source_path.resolve(),
        )
    )

    assert first.artifact.artifact_id == "job_retry:source_video:compressed_input"
    assert second.artifact.artifact_id == first.artifact.artifact_id
    assert second.path == first.path
    assert second.compression_info["reused_existing_artifact"] is True
    assert second.compression_info["compressed_artifact_id"] == first.artifact.artifact_id
    assert len(storage.upload_calls) == 1
    assert len(repo.events) == 1
    assert repo.events[0]["stage_name"] == "video_preprocess"


def test_source_preparation_keeps_original_artifact_when_compression_opted_out(tmp_path: Path) -> None:
    # Compression is ON by default now, so "not requested" means an explicit
    # compression_flag=false opt-out in the request. The opt-out only skips
    # downscaling: an already-H.264 source (like this fixture) passes through
    # untouched with no artifact recorded.
    source_path = tmp_path / SMOKE_VIDEO.name
    shutil.copyfile(SMOKE_VIDEO, source_path)
    repo = _MemoryRepository()
    storage = _RecordingStorage()
    service = VideoSourcePreparationService(
        repository=repo,  # type: ignore[arg-type]
        settings=_FakeSettings(workdir=tmp_path),
        storage=storage,
        stage_name="video_preprocess",
    )
    source_artifact = AsyncV2Artifact(
        artifact_id="source_artifact_no_compress",
        job_id="job_no_compress",
        artifact_type="source_video",
        role="input",
        container="user-uploads",
        blob_name=source_video_blob_name(job_id="job_no_compress", source_path=source_path),
        url="https://storage.test/user-uploads/jobs/job_no_compress/input/source_video.mp4?sig=fake",
        content_type="video/mp4",
        local_path=str(source_path.resolve()),
        metadata_json={"video_metadata": VideoMetadata.from_file(source_path).to_dict()},
    )

    prepared = asyncio.run(
        service.prepare_for_workflow(
            job_id="job_no_compress",
            request={
                **_video_music_request("source_artifact_no_compress"),
                "compression_flag": False,
            },
            source_artifact=source_artifact,
            source_video_path=source_path.resolve(),
        )
    )

    assert prepared.path == source_path.resolve()
    assert prepared.artifact is source_artifact
    assert prepared.compression_info["requested"] is False
    assert prepared.compression_info["applied"] is False
    assert prepared.compression_info["max_height"] == 1280
    assert prepared.compression_info["source_artifact_id"] == source_artifact.artifact_id
    assert not repo.artifacts
    assert not storage.upload_calls
    assert not repo.events


def test_source_preparation_opted_out_still_normalizes_non_h264_to_h264(tmp_path: Path) -> None:
    # compression_flag=false skips downscaling but NOT the H.264 delivery
    # guarantee: a non-H.264 source must be re-encoded at original resolution
    # and recorded as the usual compressed_input artifact for retry reuse.
    from EdennCode.Util.MediaUtils import get_video_codec, get_video_dimensions, resolve_ffmpeg_binary
    import subprocess

    source_path = tmp_path / "hevc_like_upload.mp4"
    subprocess.run(
        [
            resolve_ffmpeg_binary(), "-y",
            "-f", "lavfi", "-i", "color=c=blue:size=96x320:rate=1:duration=16",
            "-an", "-c:v", "mpeg4", "-pix_fmt", "yuv420p", str(source_path),
        ],
        check=True, capture_output=True,
    )
    assert get_video_codec(source_path) != "h264"

    repo = _MemoryRepository()
    storage = _RecordingStorage()
    service = VideoSourcePreparationService(
        repository=repo,  # type: ignore[arg-type]
        settings=_FakeSettings(workdir=tmp_path),
        storage=storage,
        stage_name="video_preprocess",
    )
    source_artifact = AsyncV2Artifact(
        artifact_id="source_artifact_hevc_like",
        job_id="job_hevc_like",
        artifact_type="source_video",
        role="input",
        container="user-uploads",
        blob_name=source_video_blob_name(job_id="job_hevc_like", source_path=source_path),
        url="https://storage.test/user-uploads/jobs/job_hevc_like/input/source_video.mp4?sig=fake",
        content_type="video/mp4",
        local_path=str(source_path.resolve()),
        metadata_json={"video_metadata": VideoMetadata.from_file(source_path).to_dict()},
    )

    prepared = asyncio.run(
        service.prepare_for_workflow(
            job_id="job_hevc_like",
            request={
                **_video_music_request("source_artifact_hevc_like"),
                "compression_flag": False,
            },
            source_artifact=source_artifact,
            source_video_path=source_path.resolve(),
        )
    )

    assert prepared.path != source_path.resolve()
    assert prepared.path.name == "hevc_like_upload_h264.mp4"
    assert get_video_codec(prepared.path) == "h264"
    # Opt-out must not downscale: resolution is preserved.
    assert get_video_dimensions(prepared.path) == get_video_dimensions(source_path)
    assert prepared.compression_info["requested"] is False
    assert prepared.compression_info["applied"] is True
    assert prepared.artifact.artifact_id == "job_hevc_like:source_video:compressed_input"
    assert repo.events[-1]["event_type"] == "artifact.created"


def test_source_preparation_defaults_to_compression_and_noops_below_max_height(tmp_path: Path) -> None:
    # No compression_flag in the request: the default is ON, but a source whose
    # height is within compression_max_height passes through untouched (the
    # current standard only downscales sources taller than 1280).
    source_path = tmp_path / SMOKE_VIDEO.name
    shutil.copyfile(SMOKE_VIDEO, source_path)
    repo = _MemoryRepository()
    storage = _RecordingStorage()
    service = VideoSourcePreparationService(
        repository=repo,  # type: ignore[arg-type]
        settings=_FakeSettings(workdir=tmp_path),
        storage=storage,
        stage_name="video_preprocess",
    )
    source_artifact = AsyncV2Artifact(
        artifact_id="source_artifact_default_compress",
        job_id="job_default_compress",
        artifact_type="source_video",
        role="input",
        container="user-uploads",
        blob_name=source_video_blob_name(job_id="job_default_compress", source_path=source_path),
        url="https://storage.test/user-uploads/jobs/job_default_compress/input/source_video.mp4?sig=fake",
        content_type="video/mp4",
        local_path=str(source_path.resolve()),
        metadata_json={"video_metadata": VideoMetadata.from_file(source_path).to_dict()},
    )

    request = _video_music_request("source_artifact_default_compress")
    assert "compression_flag" not in request
    prepared = asyncio.run(
        service.prepare_for_workflow(
            job_id="job_default_compress",
            request=request,
            source_artifact=source_artifact,
            source_video_path=source_path.resolve(),
        )
    )

    assert prepared.path == source_path.resolve()
    assert prepared.artifact is source_artifact
    assert prepared.compression_info["requested"] is True
    assert prepared.compression_info["applied"] is False
    # No compressed artifact is recorded when the no-upscale rule skips ffmpeg.
    assert not repo.artifacts
    assert not storage.upload_calls


@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1" or not _postgres_env_available(),
    reason="Set RUN_ASYNC_V2_POSTGRES_INTEGRATION=1 with Postgres env to run.",
)
def test_postgres_monolith_worker_completes_actual_staged_video_job(tmp_path: Path) -> None:
    suffix = uuid.uuid4().hex
    job_id = f"test_async_v2_monolith_{suffix}"
    source_artifact_id = f"test_async_v2_monolith_source_{suffix}"
    task_id = f"test_async_v2_monolith_task_{suffix}"
    queue_name = f"test-video-music-pipeline-{suffix}"
    repo = AsyncPipelineV2Repository()
    queue = PostgresTaskQueue()
    storage = _RecordingStorage()
    settings = _FakeSettings(workdir=tmp_path)
    staging = ArtifactStagingService(
        repository=repo,
        storage=storage,
        upload_container=settings.upload_container,
    )
    worker = VideoMusicMonolithWorker(
        repository=repo,
        queue=queue,
        orchestrator=_FakeOrchestrator(tmp_path / "outputs"),
        settings=settings,
        storage=storage,
        worker_id=f"worker_{suffix}",
        queue_name=queue_name,
        lease_seconds=30,
    )

    try:
        repo.create_job(
            job_id=job_id,
            job_type="video_music",
            request_json=_video_music_request(source_artifact_id),
            status=JobStatus.QUEUED,
            priority=3,
        )
        staging.stage_video_path(
            job_id=job_id,
            source_path=SMOKE_VIDEO,
            artifact_id=source_artifact_id,
            request_metadata=_video_music_request(source_artifact_id),
        )
        queue.enqueue(
            TaskEnvelope(
                task_id=task_id,
                job_id=job_id,
                queue_name=queue_name,
                task_type="video_music_monolith",
                payload_json={"source_video_artifact_id": source_artifact_id},
                priority=3,
                max_attempts=2,
                idempotency_key=f"{job_id}:monolith:v1",
            )
        )

        processed = asyncio.run(worker.process_one())
        assert processed is not None
        assert processed.status == TaskStatus.COMPLETED

        status_view = repo.build_status_view(job_id)
        assert status_view["status"] == JobStatus.COMPLETED
        assert status_view["result"]["video_metadata"]["video_url"]
        assert status_view["result"]["audio_metadata"]["audio_url"]
        assert status_view["result"]["video_metadata"]["geometry"]["duration"] > 0
        artifact_types = {artifact["artifact_type"] for artifact in status_view["artifacts"]}
        assert {
            "source_video",
            "matched_audio",
            "complete_audio",
            "remixed_video",
            "thumbnail",
        }.issubset(artifact_types)
        event_types = [event.event_type for event in repo.list_events(job_id)]
        assert "stage.started" in event_types
        assert "job.completed" in event_types
    finally:
        with PostgresClient.from_env() as client:
            client.run_sql("DELETE FROM async_v2_jobs WHERE job_id = %s", params=[job_id])


class _FakeUsageTable:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def upsert_entity(self, row: dict) -> None:
        self.rows.append(row)


@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1" or not _postgres_env_available(),
    reason="Set RUN_ASYNC_V2_POSTGRES_INTEGRATION=1 with Postgres env to run.",
)
def test_monolith_success_records_usage(tmp_path: Path) -> None:
    suffix = uuid.uuid4().hex
    job_id = f"test_async_v2_monolith_usage_{suffix}"
    source_artifact_id = f"test_async_v2_monolith_usage_source_{suffix}"
    task_id = f"test_async_v2_monolith_usage_task_{suffix}"
    queue_name = f"test-video-music-pipeline-usage-{suffix}"
    repo = AsyncPipelineV2Repository()
    queue = PostgresTaskQueue()
    storage = _RecordingStorage()
    settings = _FakeSettings(workdir=tmp_path)
    staging = ArtifactStagingService(
        repository=repo,
        storage=storage,
        upload_container=settings.upload_container,
    )
    worker = VideoMusicMonolithWorker(
        repository=repo,
        queue=queue,
        orchestrator=_FakeOrchestrator(tmp_path / "outputs"),
        settings=settings,
        storage=storage,
        worker_id=f"worker_{suffix}",
        queue_name=queue_name,
        # Generous margin: this test round-trips many sequential statements
        # against a remote Postgres instance, and a tight lease can expire
        # before the terminal commit lands under real network latency.
        lease_seconds=300,
    )

    table = _FakeUsageTable()
    set_usage_recorder_override(
        UsageRecorder(table, auth_mode="log", logger=logging.getLogger("test-usage-recorder"))
    )
    try:
        request_json = dict(_video_music_request(source_artifact_id))
        request_json["auth_user_id"] = "user-5"
        request_json["auth_key_prefix"] = "sk-abc123def"
        repo.create_job(
            job_id=job_id,
            job_type="video_music",
            request_json=request_json,
            status=JobStatus.QUEUED,
            priority=3,
        )
        staging.stage_video_path(
            job_id=job_id,
            source_path=SMOKE_VIDEO,
            artifact_id=source_artifact_id,
            request_metadata=request_json,
        )
        queue.enqueue(
            TaskEnvelope(
                task_id=task_id,
                job_id=job_id,
                queue_name=queue_name,
                task_type="video_music_monolith",
                payload_json={"source_video_artifact_id": source_artifact_id},
                priority=3,
                max_attempts=2,
                idempotency_key=f"{job_id}:monolith:v1",
            )
        )

        async def _run_and_settle():
            outcome = await worker.process_one()
            # record_v2_job_usage schedules the table write as a fire-and-forget
            # background task; give the loop a chance to run it to completion
            # before asyncio.run() tears the loop down.
            await asyncio.sleep(0.05)
            return outcome

        processed = asyncio.run(_run_and_settle())
        assert processed is not None
        assert processed.status == TaskStatus.COMPLETED

        assert len(table.rows) == 1
        row = table.rows[0]
        assert row["PartitionKey"] == "user-5"
        assert row["endpoint"] == "/api/v2/jobs/video-music"
        assert row["status"] == "completed"
        assert row["generation_call_count"] == 2
    finally:
        set_usage_recorder_override(None)
        with PostgresClient.from_env() as client:
            client.run_sql("DELETE FROM async_v2_jobs WHERE job_id = %s", params=[job_id])


@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1"
    or os.getenv("RUN_ASYNC_V2_STORAGE_INTEGRATION") != "1"
    or not _postgres_env_available()
    or not _storage_env_available(),
    reason="Set RUN_ASYNC_V2_POSTGRES_INTEGRATION=1 and RUN_ASYNC_V2_STORAGE_INTEGRATION=1.",
)
def test_monolith_worker_uploads_outputs_to_configured_storage(tmp_path: Path) -> None:
    suffix = uuid.uuid4().hex
    job_id = f"test_async_v2_monolith_storage_{suffix}"
    source_artifact_id = f"test_async_v2_monolith_storage_source_{suffix}"
    task_id = f"test_async_v2_monolith_storage_task_{suffix}"
    queue_name = f"test-video-music-pipeline-{suffix}"
    settings = DeploymentSettings.from_env()
    storage = create_storage_service(settings)
    repo = AsyncPipelineV2Repository()
    queue = PostgresTaskQueue()
    staging = ArtifactStagingService(
        repository=repo,
        storage=storage,
        upload_container=settings.upload_container,
    )
    worker = VideoMusicMonolithWorker(
        repository=repo,
        queue=queue,
        orchestrator=_FakeOrchestrator(tmp_path / "outputs"),
        settings=settings,
        storage=storage,
        worker_id=f"worker_{suffix}",
        queue_name=queue_name,
        lease_seconds=30,
    )
    uploaded: list[tuple[str, str]] = []

    try:
        uploaded.append(
            (
                settings.upload_container,
                source_video_blob_name(job_id=job_id, source_path=SMOKE_VIDEO),
            )
        )
        repo.create_job(
            job_id=job_id,
            job_type="video_music",
            request_json=_video_music_request(source_artifact_id),
            status=JobStatus.QUEUED,
            priority=3,
        )
        staged = staging.stage_video_path(
            job_id=job_id,
            source_path=SMOKE_VIDEO,
            artifact_id=source_artifact_id,
            request_metadata=_video_music_request(source_artifact_id),
        )
        if staged.artifact.container and staged.artifact.blob_name:
            uploaded.append((staged.artifact.container, staged.artifact.blob_name))
        queue.enqueue(
            TaskEnvelope(
                task_id=task_id,
                job_id=job_id,
                queue_name=queue_name,
                task_type="video_music_monolith",
                payload_json={"source_video_artifact_id": source_artifact_id},
                priority=3,
                max_attempts=1,
            )
        )

        processed = asyncio.run(worker.process_one())
        assert processed is not None
        assert processed.status == TaskStatus.COMPLETED

        artifacts = repo.list_artifacts(job_id)
        for artifact in artifacts:
            if artifact.container and artifact.blob_name:
                uploaded.append((artifact.container, artifact.blob_name))
        status_view = repo.build_status_view(job_id)
        assert status_view["result"]["video_metadata"]["video_url"]
        assert status_view["result"]["audio_metadata"]["audio_url"]
        assert any(artifact.artifact_type == "remixed_video" for artifact in artifacts)
    finally:
        seen = set()
        for container, blob_name in uploaded:
            if (container, blob_name) in seen:
                continue
            seen.add((container, blob_name))
            if hasattr(storage, "delete_blob"):
                storage.delete_blob(container=container, blob_name=blob_name)
        with PostgresClient.from_env() as client:
            client.run_sql("DELETE FROM async_v2_jobs WHERE job_id = %s", params=[job_id])


@pytest.mark.parametrize(
    "modelspec",
    ["edenn_basic", "edenn_enhanced", "edenn_studio"],
    ids=["real-provider_a-basic", "real-provider_b-enhanced", "real-provider_c-studio"],
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
def test_real_provider_monolith_worker_for_modelspec(
    modelspec: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _real_provider_env_available(modelspec):
        pytest.skip(f"Real provider env is not configured for {modelspec}.")

    suffix = uuid.uuid4().hex
    job_id = f"test_async_v2_real_{modelspec}_{suffix}"
    source_artifact_id = f"test_async_v2_real_source_{modelspec}_{suffix}"
    task_id = f"test_async_v2_real_task_{modelspec}_{suffix}"
    local_temp_dir = tmp_path / "workflow_temp"
    monkeypatch.setenv("USE_LOCAL_TEMP_DIR", "true")
    monkeypatch.setenv("LOCAL_TEMP_DIR", str(local_temp_dir))

    settings = DeploymentSettings.from_env()
    storage = create_storage_service(settings)
    repo = AsyncPipelineV2Repository()
    queue = PostgresTaskQueue()
    request = _real_provider_request(source_artifact_id, modelspec=modelspec)
    staging = ArtifactStagingService(
        repository=repo,
        storage=storage,
        upload_container=settings.upload_container,
    )
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
        worker_id=f"real_provider_worker_{suffix}",
        lease_seconds=2400,
    )
    uploaded: list[tuple[str, str]] = []

    try:
        repo.create_job(
            job_id=job_id,
            job_type="video_music",
            request_json=request,
            status=JobStatus.QUEUED,
            priority=5,
        )
        staged = staging.stage_video_path(
            job_id=job_id,
            source_path=SMOKE_VIDEO,
            artifact_id=source_artifact_id,
            request_metadata=request,
        )
        if staged.artifact.container and staged.artifact.blob_name:
            uploaded.append((staged.artifact.container, staged.artifact.blob_name))
        queue.enqueue(
            TaskEnvelope(
                task_id=task_id,
                job_id=job_id,
                queue_name="video-music-pipeline",
                task_type="video_music_monolith",
                payload_json={"source_video_artifact_id": source_artifact_id},
                priority=5,
                max_attempts=2,
                idempotency_key=f"{job_id}:real-provider-monolith:v1",
            )
        )

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
        _save_real_provider_payload(modelspec, status_view)

        assert processed is not None
        assert processed.status == TaskStatus.COMPLETED, status_view
        assert status_view["status"] == JobStatus.COMPLETED, status_view
        result = status_view["result"]
        request_metadata = result["request_metadata"]
        response_metadata = result["response_metadata"]
        assert request_metadata["modelspec"] == modelspec
        assert result["video_metadata"]["video_url"], result
        assert result["audio_metadata"]["audio_url"], result
        assert result["video_metadata"]["geometry"]["duration"] > 0
        assert request_metadata["include_vocals"] is True
        assert response_metadata["job_received_timestamp"] is not None
        assert response_metadata["job_finished_timestamp"] is not None
        assert (
            response_metadata["job_finished_timestamp"]
            >= response_metadata["job_received_timestamp"]
        )

        artifacts = repo.list_artifacts(job_id)
        for artifact in artifacts:
            if artifact.container and artifact.blob_name:
                uploaded.append((artifact.container, artifact.blob_name))
        artifact_types = {artifact.artifact_type for artifact in artifacts}
        assert {"source_video", "matched_audio", "remixed_video"}.issubset(artifact_types)
        if modelspec in {"edenn_enhanced", "edenn_studio"}:
            assert "complete_audio" in artifact_types
            assert result["audio_metadata"]["complete_audio_url"], result
    finally:
        seen = set()
        for container, blob_name in uploaded:
            if (container, blob_name) in seen:
                continue
            seen.add((container, blob_name))
            if hasattr(storage, "delete_blob"):
                storage.delete_blob(container=container, blob_name=blob_name)
        with PostgresClient.from_env() as client:
            client.run_sql("DELETE FROM async_v2_jobs WHERE job_id = %s", params=[job_id])
