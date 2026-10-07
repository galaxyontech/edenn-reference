from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

from EdennCode.Deployment.async_pipeline_v2.models import (
    JobStatus,
    StageStatus,
    TaskEnvelope,
    TaskStatus,
)
from EdennCode.Deployment.async_pipeline_v2.queue.postgres_queue import PostgresTaskQueue
from EdennCode.Deployment.async_pipeline_v2.repositories import (
    MIGRATION_PATH,
    AsyncPipelineV2Repository,
)
from EdennCode.Deployment.postgres_wrapper import PostgresClient
from EdennCode.env import load_env


def _sample_video_music_request(source_video_artifact_id: str) -> dict:
    return {
        "source_video_artifact_id": source_video_artifact_id,
        "modelspec": "edenn_enhanced",
        "user_prompt": "upbeat cinematic travel montage with light vocals",
        "include_vocals": True,
        "preserve_original_audio": False,
        "music_volume": 0.92,
        "mode": "monolith",
    }


def _postgres_env_available() -> bool:
    load_env()
    return bool(
        os.getenv("DATABASE_URL")
        or (os.getenv("PGHOST") and os.getenv("PGDATABASE") and os.getenv("PGUSER"))
    )


def test_migration_has_priority_retry_and_error_columns() -> None:
    migration_sql = MIGRATION_PATH.read_text(encoding="utf-8")

    assert "priority INTEGER NOT NULL DEFAULT 0" in migration_sql
    assert "max_attempts INTEGER NOT NULL DEFAULT 3" in migration_sql
    assert "last_error_json JSONB" in migration_sql
    assert "payload_json JSONB" in migration_sql
    assert "ADD COLUMN IF NOT EXISTS payload_json JSONB" in migration_sql
    assert "FOR UPDATE SKIP LOCKED" not in migration_sql


def test_row_mappers_preserve_actual_video_music_payloads() -> None:
    now = datetime.now(timezone.utc)
    request = _sample_video_music_request("artifact_source_123")

    job = AsyncPipelineV2Repository._job_from_row(
        {
            "job_id": "job_mapper",
            "session_id": "session_mapper",
            "creator_user_id": "creator_mapper",
            "job_type": "video_music",
            "status": JobStatus.PROCESSING,
            "current_stage": "provider-candidate-generation",
            "progress_percent": 52.5,
            "priority": 7,
            "request_json": request,
            "result_json": None,
            "error_json": None,
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
        }
    )

    assert job is not None
    assert job.request_json["modelspec"] == "edenn_enhanced"
    assert job.priority == 7
    assert job.current_stage == "provider-candidate-generation"

    task = PostgresTaskQueue._task_from_row(
        {
            "task_id": "task_mapper",
            "job_id": "job_mapper",
            "queue_name": "provider-candidate-generation",
            "task_type": "generate_music_candidates",
            "status": TaskStatus.QUEUED,
            "payload_json": {
                "analysis_plan_id": "artifact_analysis_123",
                "modelspec": "edenn_enhanced",
                "candidate_count": 2,
            },
            "priority": 11,
            "attempt": 2,
            "max_attempts": 5,
            "lease_owner": None,
            "lease_until": None,
            "not_before": now,
            "idempotency_key": "job_mapper:provider:v1",
            "last_error_json": {"code": "provider_rate_limit"},
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
        }
    )

    assert task is not None
    assert task.payload_json["candidate_count"] == 2
    assert task.priority == 11
    assert task.last_error_json == {"code": "provider_rate_limit"}


@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1" or not _postgres_env_available(),
    reason="Set RUN_ASYNC_V2_POSTGRES_INTEGRATION=1 with Postgres env to run.",
)
def test_postgres_repository_and_queue_with_actual_video_music_data() -> None:
    suffix = uuid.uuid4().hex
    job_id = f"test_async_v2_job_{suffix}"
    source_artifact_id = f"test_async_v2_source_{suffix}"
    analysis_artifact_id = f"test_async_v2_analysis_{suffix}"
    task_low = f"test_async_v2_task_low_{suffix}"
    task_high = f"test_async_v2_task_high_{suffix}"
    task_mid = f"test_async_v2_task_mid_{suffix}"
    task_dead = f"test_async_v2_task_dead_{suffix}"
    task_stale = f"test_async_v2_task_stale_{suffix}"
    worker_id = f"worker_test_{suffix}"

    repo = AsyncPipelineV2Repository()
    queue = PostgresTaskQueue()
    repo.ensure_schema()

    try:
        job = repo.create_job(
            job_id=job_id,
            job_type="video_music",
            request_json=_sample_video_music_request(source_artifact_id),
            session_id=f"session_{suffix}",
            creator_user_id="integration-test-user",
            priority=8,
        )
        assert job.status == JobStatus.QUEUED
        assert job.request_json["source_video_artifact_id"] == source_artifact_id

        source = repo.add_artifact(
            artifact_id=source_artifact_id,
            job_id=job_id,
            artifact_type="source_video",
            role="input",
            container="integration-test",
            blob_name=f"jobs/{job_id}/source_video.mp4",
            url="https://example.invalid/jobs/source_video.mp4",
            content_type="video/mp4",
            metadata_json={
                "duration": 15.2,
                "width": 1080,
                "height": 1920,
                "video_hash": f"sha256:{suffix}",
            },
        )
        assert source.metadata_json["duration"] == 15.2

        analysis = repo.add_artifact(
            artifact_id=analysis_artifact_id,
            job_id=job_id,
            artifact_type="analysis_json",
            role="analysis_plan",
            metadata_json={
                "scene_count": 4,
                "overall_mood": "warm, fast-paced, optimistic",
                "effective_modelspec": "edenn_enhanced",
            },
        )
        assert analysis.role == "analysis_plan"

        stage = repo.start_stage_run(
            job_id=job_id,
            stage_name="analysis-and-planning",
            input_json={"source_video_artifact_id": source_artifact_id},
        )
        repo.update_stage_run(
            stage.stage_run_id,
            status=StageStatus.COMPLETED,
            output_json={"analysis_artifact_id": analysis_artifact_id},
            finished=True,
        )
        repo.add_event(
            job_id=job_id,
            event_type="stage.completed",
            stage_name="analysis-and-planning",
            message="Analysis plan created for integration video.",
            payload_json={"analysis_artifact_id": analysis_artifact_id},
        )

        for task_id, priority in [(task_low, 1), (task_high, 10), (task_mid, 5)]:
            queue.enqueue(
                TaskEnvelope(
                    task_id=task_id,
                    job_id=job_id,
                    queue_name="provider-candidate-generation",
                    task_type="generate_music_candidates",
                    payload_json={
                        "analysis_artifact_id": analysis_artifact_id,
                        "modelspec": "edenn_enhanced",
                        "candidate_count": 1,
                    },
                    priority=priority,
                    max_attempts=3,
                    idempotency_key=f"{task_id}:provider:v1",
                )
            )

        leased = queue.lease(
            queue_name="provider-candidate-generation",
            worker_id=worker_id,
            lease_seconds=30,
        )
        assert leased is not None
        assert leased.task_id == task_high
        assert leased.attempt == 1
        assert leased.priority == 10

        heartbeat = queue.heartbeat(
            task_id=leased.task_id,
            worker_id=worker_id,
            lease_seconds=60,
        )
        assert heartbeat.lease_owner == worker_id

        retried = queue.fail(
            task_id=leased.task_id,
            worker_id=worker_id,
            error={"code": "provider_rate_limit", "provider": "provider_b"},
            retry=True,
            backoff_seconds=0,
        )
        assert retried.status == TaskStatus.QUEUED
        assert retried.last_error_json == {
            "code": "provider_rate_limit",
            "provider": "provider_b",
        }

        leased_again = queue.lease(
            queue_name="provider-candidate-generation",
            worker_id=worker_id,
            lease_seconds=30,
        )
        assert leased_again is not None
        assert leased_again.task_id == task_high
        assert leased_again.attempt == 2

        completed = queue.complete(task_id=task_high, worker_id=worker_id)
        assert completed.status == TaskStatus.COMPLETED
        assert completed.finished_at is not None

        next_task = queue.lease(
            queue_name="provider-candidate-generation",
            worker_id=worker_id,
            lease_seconds=30,
        )
        assert next_task is not None
        assert next_task.task_id == task_mid

        queue.enqueue(
            TaskEnvelope(
                task_id=task_dead,
                job_id=job_id,
                queue_name="selection-ranking-remix-finalize",
                task_type="select_rank_and_remix_music",
                payload_json={"candidate_audio_artifact_id": "candidate_1"},
                priority=0,
                max_attempts=1,
            )
        )
        leased_dead = queue.lease(
            queue_name="selection-ranking-remix-finalize",
            worker_id=worker_id,
            lease_seconds=30,
        )
        assert leased_dead is not None
        dead_lettered = queue.fail(
            task_id=leased_dead.task_id,
            worker_id=worker_id,
            error={"code": "ffmpeg_failed"},
            retry=True,
        )
        assert dead_lettered.status == TaskStatus.DEAD_LETTERED

        # Unique queue so the reaper below is scoped to this test's task only and
        # can never requeue another tenant's stale task in a shared database.
        stale_queue = f"analysis-and-planning-stale-{suffix}"
        queue.enqueue(
            TaskEnvelope(
                task_id=task_stale,
                job_id=job_id,
                queue_name=stale_queue,
                task_type="analyze_and_plan_music",
                payload_json={"source_video_artifact_id": source_artifact_id},
            )
        )
        leased_stale = queue.lease(
            queue_name=stale_queue,
            worker_id=worker_id,
            lease_seconds=30,
        )
        assert leased_stale is not None
        with PostgresClient.from_env() as client:
            client.run_sql(
                """
                UPDATE async_v2_tasks
                SET lease_until = now() - interval '1 second'
                WHERE task_id = %s
                """,
                params=[task_stale],
            )
        requeued = queue.requeue_expired_leases(queue_name_prefix=stale_queue)
        assert [task.task_id for task in requeued] == [task_stale]

        repo.update_job_status(
            job_id,
            status=JobStatus.COMPLETED,
            current_stage="selection-ranking-remix-finalize",
            progress_percent=100,
            result_json={
                "video_url": "https://example.invalid/jobs/final.mp4",
                "audio_url": "https://example.invalid/jobs/final.wav",
                "selected_music_id": "music_primary",
            },
            finished=True,
        )
        status_view = repo.build_status_view(job_id)
        assert status_view["status"] == JobStatus.COMPLETED
        assert status_view["result"]["selected_music_id"] == "music_primary"
        assert len(status_view["artifacts"]) == 2
        assert status_view["stages"][0]["name"] == "analysis-and-planning"
    finally:
        with PostgresClient.from_env() as client:
            client.run_sql(
                "DELETE FROM async_v2_jobs WHERE job_id = %s",
                params=[job_id],
            )


@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1" or not _postgres_env_available(),
    reason="Set RUN_ASYNC_V2_POSTGRES_INTEGRATION=1 with Postgres env to run.",
)
def test_postgres_repository_transaction_rolls_back_multi_table_status_and_events() -> None:
    suffix = uuid.uuid4().hex
    job_id = f"test_async_v2_tx_job_{suffix}"
    source_artifact_id = f"test_async_v2_tx_source_{suffix}"
    event_id = f"test_async_v2_tx_event_{suffix}"
    repo = AsyncPipelineV2Repository()
    repo.ensure_schema()

    try:
        repo.create_job(
            job_id=job_id,
            job_type="video_music",
            request_json=_sample_video_music_request(source_artifact_id),
            priority=3,
        )

        with pytest.raises(RuntimeError, match="rollback requested"):
            with repo.transaction() as client:
                repo.update_job_status(
                    job_id,
                    status=JobStatus.PROCESSING,
                    current_stage="transaction_test",
                    progress_percent=33,
                    client=client,
                )
                repo.add_event(
                    job_id=job_id,
                    event_id=event_id,
                    event_type="transaction.test",
                    stage_name="transaction_test",
                    message="This event must roll back.",
                    payload_json={"source_video_artifact_id": source_artifact_id},
                    client=client,
                )
                raise RuntimeError("rollback requested")

        job = repo.get_job(job_id)
        assert job is not None
        assert job.status == JobStatus.QUEUED
        assert job.current_stage is None
        assert job.progress_percent == 0
        assert all(event.event_id != event_id for event in repo.list_events(job_id))
    finally:
        with PostgresClient.from_env() as client:
            client.run_sql("DELETE FROM async_v2_jobs WHERE job_id = %s", params=[job_id])


@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1" or not _postgres_env_available(),
    reason="Set RUN_ASYNC_V2_POSTGRES_INTEGRATION=1 with Postgres env to run.",
)
def test_postgres_queue_can_enqueue_and_complete_inside_repository_transaction() -> None:
    suffix = uuid.uuid4().hex
    job_id = f"test_async_v2_queue_tx_job_{suffix}"
    source_artifact_id = f"test_async_v2_queue_tx_source_{suffix}"
    current_task_id = f"test_async_v2_queue_tx_current_{suffix}"
    next_task_id = f"test_async_v2_queue_tx_next_{suffix}"
    worker_id = f"worker_queue_tx_{suffix}"
    repo = AsyncPipelineV2Repository()
    queue = PostgresTaskQueue()
    repo.ensure_schema()

    try:
        repo.create_job(
            job_id=job_id,
            job_type="video_music",
            request_json=_sample_video_music_request(source_artifact_id),
            priority=4,
        )
        queue.enqueue(
            TaskEnvelope(
                task_id=current_task_id,
                job_id=job_id,
                queue_name="transaction-current",
                task_type="transaction_current",
                payload_json={},
                max_attempts=2,
            )
        )
        leased = queue.lease(
            queue_name="transaction-current",
            worker_id=worker_id,
            lease_seconds=60,
        )
        assert leased is not None
        assert leased.task_id == current_task_id

        with repo.transaction() as client:
            queue.enqueue(
                TaskEnvelope(
                    task_id=next_task_id,
                    job_id=job_id,
                    queue_name="transaction-next",
                    task_type="transaction_next",
                    payload_json={"from_task_id": current_task_id},
                    max_attempts=2,
                ),
                client=client,
            )
            repo.add_event(
                job_id=job_id,
                event_type="transaction.handoff",
                stage_name="transaction_current",
                payload_json={"next_task_id": next_task_id},
                client=client,
            )
            queue.complete(task_id=current_task_id, worker_id=worker_id, client=client)

        current_task = queue.get_task(current_task_id)
        next_task = queue.get_task(next_task_id)
        assert current_task is not None
        assert current_task.status == TaskStatus.COMPLETED
        assert next_task is not None
        assert next_task.status == TaskStatus.QUEUED
        assert [
            event.payload_json["next_task_id"]
            for event in repo.list_events(job_id)
            if event.event_type == "transaction.handoff"
        ] == [next_task_id]
    finally:
        with PostgresClient.from_env() as client:
            client.run_sql("DELETE FROM async_v2_jobs WHERE job_id = %s", params=[job_id])


@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1" or not _postgres_env_available(),
    reason="Set RUN_ASYNC_V2_POSTGRES_INTEGRATION=1 with Postgres env to run.",
)
def test_postgres_queue_transaction_rolls_back_enqueue_and_complete_together() -> None:
    suffix = uuid.uuid4().hex
    job_id = f"test_async_v2_queue_tx_rollback_job_{suffix}"
    source_artifact_id = f"test_async_v2_queue_tx_rollback_source_{suffix}"
    current_task_id = f"test_async_v2_queue_tx_rollback_current_{suffix}"
    next_task_id = f"test_async_v2_queue_tx_rollback_next_{suffix}"
    worker_id = f"worker_queue_tx_rollback_{suffix}"
    repo = AsyncPipelineV2Repository()
    queue = PostgresTaskQueue()
    repo.ensure_schema()

    try:
        repo.create_job(
            job_id=job_id,
            job_type="video_music",
            request_json=_sample_video_music_request(source_artifact_id),
            priority=4,
        )
        queue.enqueue(
            TaskEnvelope(
                task_id=current_task_id,
                job_id=job_id,
                queue_name="transaction-rollback-current",
                task_type="transaction_current",
                payload_json={},
                max_attempts=2,
            )
        )
        leased = queue.lease(
            queue_name="transaction-rollback-current",
            worker_id=worker_id,
            lease_seconds=60,
        )
        assert leased is not None

        with pytest.raises(RuntimeError, match="forced rollback"):
            with repo.transaction() as client:
                queue.enqueue(
                    TaskEnvelope(
                        task_id=next_task_id,
                        job_id=job_id,
                        queue_name="transaction-rollback-next",
                        task_type="transaction_next",
                        payload_json={"from_task_id": current_task_id},
                        max_attempts=2,
                    ),
                    client=client,
                )
                queue.complete(
                    task_id=current_task_id,
                    worker_id=worker_id,
                    client=client,
                )
                raise RuntimeError("forced rollback")

        current_task = queue.get_task(current_task_id)
        assert current_task is not None
        assert current_task.status == TaskStatus.LEASED
        assert current_task.lease_owner == worker_id
        assert queue.get_task(next_task_id) is None
    finally:
        with PostgresClient.from_env() as client:
            client.run_sql("DELETE FROM async_v2_jobs WHERE job_id = %s", params=[job_id])
