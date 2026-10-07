"""Real-cost e2e for the v2 multi-image durable path.

Runs the full flow in-process against the REAL Postgres + REAL blob storage +
REAL basic-tier music provider (edenn_basic): stage images -> create job ->
enqueue multi_image_monolith task -> MultiImageMonolithWorker.process_one() ->
assert the unified MultiImageJobResponse result. Caching is OFF (no plan_cache
passed), matching the eval configuration.

Gated behind the same flags as the video real-provider test so it never runs
(or bills) by accident.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest

from EdennCode.Deployment.async_pipeline_v2.artifact_service import ArtifactStagingService
from EdennCode.Deployment.async_pipeline_v2.models import JobStatus, TaskEnvelope, TaskStatus
from EdennCode.Deployment.async_pipeline_v2.queue.postgres_queue import PostgresTaskQueue
from EdennCode.Deployment.async_pipeline_v2.repositories import AsyncPipelineV2Repository
from EdennCode.Deployment.async_pipeline_v2.workers.multi_image_worker import (
    MultiImageMonolithWorker,
)
from EdennCode.Deployment.multi_image_workflows import MultiImageGenerationOrchestrator
from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Deployment.storage import create_storage_service
from EdennCode.env import load_env
from EdennCode.TestSuites.helpers.paths import MULTI_IMAGE_DESIGN_IMAGES_DIR


def _postgres_env_available() -> bool:
    load_env()
    return bool(
        os.getenv("DATABASE_URL")
        or (os.getenv("PGHOST") and os.getenv("PGDATABASE") and os.getenv("PGUSER"))
    )


def _storage_env_available() -> bool:
    load_env()
    return bool(
        os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        or os.getenv("AZURE_STORAGE_ACCOUNT_URL")
        or (os.getenv("COS_SECRET_ID") and os.getenv("COS_SECRET_KEY"))
    )


def _plan_and_music_env_available() -> bool:
    load_env()
    llm_ready = all(os.getenv(k) for k in ("AZURE_API_KEY", "AZURE_ENDPOINT", "AZURE_MODEL"))
    music_ready = bool(os.getenv("PROVIDER_A_API_KEY"))
    return llm_ready and music_ready


@pytest.mark.remote_integration
@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_REAL_PROVIDER_INTEGRATION") != "1"
    or os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1"
    or os.getenv("RUN_ASYNC_V2_STORAGE_INTEGRATION") != "1"
    or not _postgres_env_available()
    or not _storage_env_available(),
    reason=(
        "Set RUN_ASYNC_V2_REAL_PROVIDER_INTEGRATION=1, "
        "RUN_ASYNC_V2_POSTGRES_INTEGRATION=1, RUN_ASYNC_V2_STORAGE_INTEGRATION=1."
    ),
)
def test_multi_image_v2_real_provider_end_to_end(tmp_path: Path) -> None:
    if not _plan_and_music_env_available():
        pytest.skip("LLM (plan) + basic-tier (music) env not configured.")

    suffix = uuid.uuid4().hex
    job_id = f"test_async_v2_multi_image_{suffix}"
    queue_name = f"test-multi-image-pipeline-{suffix}"

    settings = DeploymentSettings.from_env()
    storage = create_storage_service(settings)
    repo = AsyncPipelineV2Repository()
    queue = PostgresTaskQueue()
    staging = ArtifactStagingService(
        repository=repo, storage=storage, upload_container=settings.upload_container
    )
    worker = MultiImageMonolithWorker(
        repository=repo,
        queue=queue,
        orchestrator=MultiImageGenerationOrchestrator(),
        settings=settings,
        storage=storage,
        worker_id=f"multi_image_worker_{suffix}",
        queue_name=queue_name,
        lease_seconds=1200,
        plan_cache=None,  # caching OFF
    )

    image_files = sorted(p for p in MULTI_IMAGE_DESIGN_IMAGES_DIR.iterdir() if p.is_file())[:2]
    assert len(image_files) >= 2, "need >=2 example images"
    uploaded: list[tuple[str, str]] = []

    try:
        repo.create_job(
            job_id=job_id,
            job_type="multi_image",
            request_json={
                "modelspec": "edenn_basic",
                "user_prompt": "Create an uplifting instrumental track.",
                "align_to_beats": True,
                "per_image_duration": 3.0,
            },
            status=JobStatus.QUEUED,
            priority=5,
        )
        staged_ids: list[str] = []
        for idx, img in enumerate(image_files):
            staged = staging.stage_image_bytes(
                job_id=job_id,
                data=img.read_bytes(),
                filename=img.name,
                destination_dir=tmp_path / "images",
                index=idx,
            )
            staged_ids.append(staged.artifact.artifact_id)
            if staged.artifact.container and staged.artifact.blob_name:
                uploaded.append((staged.artifact.container, staged.artifact.blob_name))

        queue.enqueue(
            TaskEnvelope(
                task_id=f"{job_id}:task",
                job_id=job_id,
                queue_name=queue_name,
                task_type="multi_image_monolith",
                payload_json={"image_artifact_ids": staged_ids, "vocal_sample_artifact_id": None},
                priority=5,
                max_attempts=1,  # no double-bill
                idempotency_key=f"{job_id}:multi_image_monolith:v1",
            )
        )

        processed = asyncio.run(worker.process_one())
        status_view = repo.build_status_view(job_id)

        assert processed is not None, status_view
        assert processed.status == TaskStatus.COMPLETED, status_view
        assert status_view["status"] == JobStatus.COMPLETED, status_view
        result = status_view["result"]
        # Unified response, superset of VideoJobResponse.
        assert result["modelspec"] == "edenn_basic"
        assert result["video_url"], result
        assert result["audio_url"], result
        assert result["complete_audio_url"] == result["audio_url"]
        assert result["complete_audio_duration_s"] and result["complete_audio_duration_s"] > 0
        assert result["complete_audio_size_bytes"] and result["complete_audio_size_bytes"] > 0
        assert result["full_tracks"] and result["full_tracks"][0]["url"]
        assert result["music_title"]
        assert result["video_summary"]["music_title"] == result["music_title"]
        assert result["video_metadata"]["width"] and result["video_metadata"]["height"]
        # N/A video-only concept stays empty.
        assert result["scenes"] == []
        # Planning tokens ARE tracked now (the planning model call always runs).
        assert result["token_usage"] and result["token_usage"] > 0
        assert result["raw_token_usage"]["total_tokens"] > 0
        assert result["token_usage_breakdown"]["image_sequence_planning"]["total_tokens"] > 0
        # Secondary complete-audio slots are null for multi-image (video parity only).
        assert result["secondary_complete_audio_url"] is None
        # A preview thumbnail is generated + uploaded.
        assert result["thumbnail_url"], result

        for artifact in repo.list_artifacts(job_id):
            if artifact.container and artifact.blob_name:
                uploaded.append((artifact.container, artifact.blob_name))
        assert any(a.artifact_type == "output_video" for a in repo.list_artifacts(job_id))
        assert any(a.artifact_type == "complete_audio" for a in repo.list_artifacts(job_id))
        assert any(a.artifact_type == "thumbnail" for a in repo.list_artifacts(job_id))
    finally:
        seen: set[tuple[str, str]] = set()
        for container, blob_name in uploaded:
            if (container, blob_name) in seen:
                continue
            seen.add((container, blob_name))
            if hasattr(storage, "delete_blob"):
                try:
                    storage.delete_blob(container=container, blob_name=blob_name)
                except Exception:
                    pass


def _studio_vocals_env_available() -> bool:
    load_env()
    llm_ready = all(os.getenv(k) for k in ("AZURE_API_KEY", "AZURE_ENDPOINT", "AZURE_MODEL"))
    return llm_ready and bool(os.getenv("PROVIDER_C_API_KEY"))


@pytest.mark.remote_integration
@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_REAL_PROVIDER_INTEGRATION") != "1"
    or os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1"
    or os.getenv("RUN_ASYNC_V2_STORAGE_INTEGRATION") != "1"
    or not _postgres_env_available()
    or not _storage_env_available(),
    reason=(
        "Set RUN_ASYNC_V2_REAL_PROVIDER_INTEGRATION=1, "
        "RUN_ASYNC_V2_POSTGRES_INTEGRATION=1, RUN_ASYNC_V2_STORAGE_INTEGRATION=1."
    ),
)
def test_multi_image_v2_real_provider_vocals_window_end_to_end(tmp_path: Path) -> None:
    """Real vocal run: proves the dead-intro fix + lyric leveling with a real provider.

    Uses the studio tier (vocal-capable) since the enhanced provider has no local
    key. The generated song is far longer than the short slideshow, so the audio
    window selection must land the video on the singing rather than the raw intro.
    """
    if not _studio_vocals_env_available():
        pytest.skip("LLM (plan) + studio-tier (PROVIDER_C) env not configured.")

    suffix = uuid.uuid4().hex
    job_id = f"test_async_v2_multi_image_vocals_{suffix}"
    queue_name = f"test-multi-image-pipeline-{suffix}"

    settings = DeploymentSettings.from_env()
    storage = create_storage_service(settings)
    repo = AsyncPipelineV2Repository()
    queue = PostgresTaskQueue()
    staging = ArtifactStagingService(
        repository=repo, storage=storage, upload_container=settings.upload_container
    )
    worker = MultiImageMonolithWorker(
        repository=repo,
        queue=queue,
        orchestrator=MultiImageGenerationOrchestrator(),
        settings=settings,
        storage=storage,
        worker_id=f"multi_image_worker_{suffix}",
        queue_name=queue_name,
        lease_seconds=1200,
        plan_cache=None,  # caching OFF
    )

    image_files = sorted(p for p in MULTI_IMAGE_DESIGN_IMAGES_DIR.iterdir() if p.is_file())[:2]
    assert len(image_files) >= 2, "need >=2 example images"
    per_image_duration = 3.0
    uploaded: list[tuple[str, str]] = []

    try:
        repo.create_job(
            job_id=job_id,
            job_type="multi_image",
            request_json={
                "modelspec": "edenn_studio",
                "user_prompt": "an upbeat pop song with clear vocals",
                "include_vocals": True,
                "lyrics_language": "english",
                "align_to_beats": True,
                "per_image_duration": per_image_duration,
            },
            status=JobStatus.QUEUED,
            priority=5,
        )
        staged_ids: list[str] = []
        for idx, img in enumerate(image_files):
            staged = staging.stage_image_bytes(
                job_id=job_id,
                data=img.read_bytes(),
                filename=img.name,
                destination_dir=tmp_path / "images",
                index=idx,
            )
            staged_ids.append(staged.artifact.artifact_id)
            if staged.artifact.container and staged.artifact.blob_name:
                uploaded.append((staged.artifact.container, staged.artifact.blob_name))

        queue.enqueue(
            TaskEnvelope(
                task_id=f"{job_id}:task",
                job_id=job_id,
                queue_name=queue_name,
                task_type="multi_image_monolith",
                payload_json={"image_artifact_ids": staged_ids, "vocal_sample_artifact_id": None},
                priority=5,
                max_attempts=1,
                idempotency_key=f"{job_id}:multi_image_monolith:v1",
            )
        )

        processed = asyncio.run(worker.process_one())
        status_view = repo.build_status_view(job_id)
        assert processed is not None, status_view
        assert processed.status == TaskStatus.COMPLETED, status_view
        result = status_view["result"]

        # Real vocals were produced with leveled lyrics.
        assert result["lyrics_timestamps"], result
        assert result["word_level_lyrics_timestamps"], result
        assert result["primary_full_lyrics_timestamps"] == result["lyrics_timestamps"]
        assert result["primary_full_word_level_lyrics_timestamps"] == result["word_level_lyrics_timestamps"]

        # Dead-intro fix: the muxed video window must actually contain singing.
        video_duration = float(result["video_metadata"]["duration"])
        window = float(result["audio_window_start_s"])
        words = result["word_level_lyrics_timestamps"] or result["lyrics_timestamps"]
        first_onset = min(float(w["startS"]) for w in words)
        track_duration = float(result["complete_audio_duration_s"])
        vocals_in_window = any(
            window <= float(w["startS"]) <= window + video_duration for w in words
        )
        # If the track is long enough to move the window and vocals start after the
        # video length, the window must have shifted off t=0 to capture them.
        if track_duration > video_duration + 1.0 and first_onset > video_duration:
            assert window > 0.0, (
                f"window should skip the intro: first vocal at {first_onset}s, "
                f"video {video_duration}s, window {window}s"
            )
        assert vocals_in_window, (
            f"video window [{window}, {window + video_duration}] contains no vocals "
            f"(first onset {first_onset}s)"
        )

        # Preview + accounting fields populated.
        assert result["thumbnail_url"], result
        assert result["token_usage"] and result["token_usage"] > 0
        assert result["secondary_complete_audio_url"] is None

        for artifact in repo.list_artifacts(job_id):
            if artifact.container and artifact.blob_name:
                uploaded.append((artifact.container, artifact.blob_name))
    finally:
        seen: set[tuple[str, str]] = set()
        for container, blob_name in uploaded:
            if (container, blob_name) in seen:
                continue
            seen.add((container, blob_name))
            if hasattr(storage, "delete_blob"):
                try:
                    storage.delete_blob(container=container, blob_name=blob_name)
                except Exception:
                    pass
