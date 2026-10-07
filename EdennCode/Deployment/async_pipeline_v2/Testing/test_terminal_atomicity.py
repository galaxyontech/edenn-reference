"""Real-Postgres atomicity tests for terminal bookkeeping (finding #36).

The monolith / multi-image workers commit their terminal state (task complete or
fail + the job's terminal/requeued status + result_json) inside one
``repository.transaction()``. This closes the window where a crash between
``queue.complete()``/``fail()`` and ``update_job_status`` strands the job
non-terminal (or loses the result). These tests prove the composition the workers
use — ``queue.complete()``/``fail()`` with ``client=`` joined to the repository
transaction — rolls back together on a mid-commit crash and commits together on
success.

Opt-in and isolated (RUN_ASYNC_V2_POSTGRES_INTEGRATION, unique queue namespace,
cascade teardown). This is not reaper-sensitive, so it is not race-prone on a
shared DB.
"""
from __future__ import annotations

import os
import uuid

import pytest

from EdennCode.Deployment.async_pipeline_v2.models import (
    JobStatus,
    TaskEnvelope,
    TaskStatus,
)
from EdennCode.Deployment.async_pipeline_v2.queue.postgres_queue import PostgresTaskQueue
from EdennCode.Deployment.async_pipeline_v2.repositories import AsyncPipelineV2Repository
from EdennCode.Deployment.postgres_wrapper import PostgresClient
from EdennCode.env import load_env


def _postgres_env_available() -> bool:
    load_env()
    return bool(
        os.getenv("DATABASE_URL")
        or (os.getenv("PGHOST") and os.getenv("PGDATABASE") and os.getenv("PGUSER"))
    )


_SKIP = pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1" or not _postgres_env_available(),
    reason="Set RUN_ASYNC_V2_POSTGRES_INTEGRATION=1 with Postgres env to run.",
)


class _Boom(Exception):
    pass


def _delete_job(job_id: str) -> None:
    with PostgresClient.from_env() as client:
        client.run_sql("DELETE FROM async_v2_jobs WHERE job_id = %s", params=[job_id])


def _setup(max_attempts: int):
    suffix = uuid.uuid4().hex
    ns = f"atomtest_{suffix}"
    job_id = f"atomjob_{suffix}"
    task_id = f"atomtask_{suffix}"
    worker_id = f"atomworker_{suffix}"
    repo = AsyncPipelineV2Repository()
    queue = PostgresTaskQueue()
    repo.ensure_schema()
    repo.create_job(job_id=job_id, job_type="atom_test", request_json={})
    queue.enqueue(TaskEnvelope(
        task_id=task_id, job_id=job_id, queue_name=ns, task_type="atom_test",
        payload_json={}, priority=0, max_attempts=max_attempts,
        idempotency_key=f"{task_id}:atom:v1"))
    leased = queue.lease(queue_name=ns, worker_id=worker_id, lease_seconds=60)
    assert leased is not None
    return repo, queue, job_id, task_id, worker_id


@_SKIP
def test_failure_bookkeeping_is_atomic() -> None:
    repo, queue, job_id, task_id, worker_id = _setup(max_attempts=1)
    err = {"message": "boom", "type": "X", "retryable": False}
    try:
        # Rollback: crash after fail() + job-update, before commit.
        with repo.transaction() as client:
            queue.fail(task_id=task_id, worker_id=worker_id, error=err,
                       retry=True, client=client)
            repo.update_job_status(job_id, status=JobStatus.FAILED,
                                   error_json=err, finished=True, client=client)
            raise _Boom()
    except _Boom:
        pass
    try:
        assert queue.get_task(task_id).status == TaskStatus.LEASED  # fail() rolled back
        assert repo.get_job(job_id).status != JobStatus.FAILED       # job-update rolled back

        # Commit: both together.
        with repo.transaction() as client:
            queue.fail(task_id=task_id, worker_id=worker_id, error=err,
                       retry=True, client=client)
            repo.update_job_status(job_id, status=JobStatus.FAILED,
                                   error_json=err, finished=True, client=client)
        assert queue.get_task(task_id).status == TaskStatus.DEAD_LETTERED
        assert repo.get_job(job_id).status == JobStatus.FAILED
    finally:
        _delete_job(job_id)


@_SKIP
def test_success_bookkeeping_is_atomic() -> None:
    repo, queue, job_id, task_id, worker_id = _setup(max_attempts=3)
    billed = {"video_metadata": {"video_url": "blob://x"}}
    try:
        # Rollback: crash after complete() + job-update(result), before commit.
        with repo.transaction() as client:
            queue.complete(task_id=task_id, worker_id=worker_id, client=client)
            repo.update_job_status(job_id, status=JobStatus.COMPLETED,
                                   result_json=billed, finished=True, client=client)
            raise _Boom()
    except _Boom:
        pass
    try:
        # Neither committed: task still leased (reaper can re-run), no result written.
        assert queue.get_task(task_id).status == TaskStatus.LEASED
        job = repo.get_job(job_id)
        assert job.status != JobStatus.COMPLETED
        assert job.result_json is None

        # Commit: result and task completion together.
        with repo.transaction() as client:
            queue.complete(task_id=task_id, worker_id=worker_id, client=client)
            repo.update_job_status(job_id, status=JobStatus.COMPLETED,
                                   result_json=billed, finished=True, client=client)
        assert queue.get_task(task_id).status == TaskStatus.COMPLETED
        job = repo.get_job(job_id)
        assert job.status == JobStatus.COMPLETED
        assert job.result_json == billed
    finally:
        _delete_job(job_id)
