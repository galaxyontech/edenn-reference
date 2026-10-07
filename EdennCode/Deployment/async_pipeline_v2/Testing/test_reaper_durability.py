"""Real-Postgres crash-recovery tests for the lease reaper (finding #0).

The reaper (``requeue_expired_leases``) recovers tasks orphaned when a worker
dies (lease expires without ``fail()`` being called). Before the fix it requeued
every stale leased task with no attempt ceiling and never read the owning job's
status, so a poison task re-leased forever and a crash after a job was already
billed could be re-run (or its result NULLed). These tests prove:

1. a repeatedly-crashing task dead-letters after ``max_attempts`` instead of
   re-leasing forever, and its job is marked FAILED;
2. a task whose job already COMPLETED (billed) is NOT re-run and its
   ``result_json`` is never clobbered — the money-path invariant;
3. a ``max_attempts=1`` task dead-letters on the first reap and fails its job.

Opt-in and isolated: gated behind RUN_ASYNC_V2_POSTGRES_INTEGRATION, each test
uses a unique queue namespace as BOTH the task's ``queue_name`` and the reaper's
``queue_name_prefix`` so it only ever touches its own rows, forces lease expiry
with the DB clock, and deletes its job (ON DELETE CASCADE) in teardown.
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


def _force_lease_expired(task_id: str) -> None:
    with PostgresClient.from_env() as client:
        client.run_sql(
            "UPDATE async_v2_tasks SET lease_until = now() - interval '5 seconds' "
            "WHERE task_id = %s",
            params=[task_id],
        )


def _delete_job(job_id: str) -> None:
    with PostgresClient.from_env() as client:
        client.run_sql("DELETE FROM async_v2_jobs WHERE job_id = %s", params=[job_id])


def _enqueue(queue, *, job_id, task_id, queue_name, max_attempts):
    queue.enqueue(
        TaskEnvelope(
            task_id=task_id, job_id=job_id, queue_name=queue_name,
            task_type="reaper_durability_test", payload_json={},
            priority=0, max_attempts=max_attempts,
            idempotency_key=f"{task_id}:reaper:v1",
        )
    )


@_SKIP
def test_reaper_dead_letters_when_attempts_exhausted() -> None:
    # NOTE: run against a DEDICATED test database. On a shared DB another (unfixed)
    # reaper with no prefix can race this test's task. A single reap after setting
    # attempt==max_attempts (rather than N lease cycles) minimizes that window.
    suffix = uuid.uuid4().hex
    ns = f"reapertest_{suffix}"
    job_id = f"reaperjob_{suffix}"
    task_id = f"reapertask_{suffix}"
    worker_id = f"reaperworker_{suffix}"
    repo = AsyncPipelineV2Repository()
    queue = PostgresTaskQueue()
    repo.ensure_schema()
    try:
        repo.create_job(job_id=job_id, job_type="reaper_test", request_json={})
        _enqueue(queue, job_id=job_id, task_id=task_id, queue_name=ns, max_attempts=3)
        leased = queue.lease(queue_name=ns, worker_id=worker_id, lease_seconds=60)
        assert leased is not None and leased.task_id == task_id
        # Simulate the last allowed attempt crashing (worker died, no fail()):
        # attempt == max_attempts, lease expired, job still non-terminal.
        with PostgresClient.from_env() as client:
            client.run_sql(
                "UPDATE async_v2_tasks SET attempt = max_attempts, "
                "lease_until = now() - interval '5 seconds' WHERE task_id = %s",
                params=[task_id],
            )

        affected = queue.requeue_expired_leases(queue_name_prefix=ns)

        # Assert on what THIS reap did (its return value), which distinguishes the
        # reaper-under-test's transition from a concurrent reaper on a shared DB:
        # if another reaper handled the task first, ours never sees it (skip); if
        # ours acts, the returned status is exactly its decision (catches the bug —
        # the old reaper returns QUEUED here).
        mine = [t for t in affected if t.task_id == task_id]
        if not mine:
            pytest.skip("a concurrent reaper handled the task first (shared DB); "
                        "run against a dedicated database")
        assert mine[0].status == TaskStatus.DEAD_LETTERED
        # Owning job is marked FAILED (not stranded in processing).
        assert repo.get_job(job_id).status == JobStatus.FAILED
    finally:
        _delete_job(job_id)


@_SKIP
def test_reaper_leaves_completed_billed_job_untouched() -> None:
    suffix = uuid.uuid4().hex
    ns = f"reapertest_{suffix}"
    job_id = f"reaperjob_{suffix}"
    task_id = f"reapertask_{suffix}"
    worker_id = f"reaperworker_{suffix}"
    billed = {"video_metadata": {"video_url": "blob://final.mp4"}, "cost": {"charged": True}}
    repo = AsyncPipelineV2Repository()
    queue = PostgresTaskQueue()
    repo.ensure_schema()
    try:
        repo.create_job(job_id=job_id, job_type="reaper_test", request_json={})
        _enqueue(queue, job_id=job_id, task_id=task_id, queue_name=ns, max_attempts=1)
        # Worker leased, finished the job (wrote result_json), then crashed before
        # completing its task: job COMPLETED but task still LEASED.
        queue.lease(queue_name=ns, worker_id=worker_id, lease_seconds=60)
        repo.update_job_status(job_id, status=JobStatus.COMPLETED, result_json=billed, finished=True)
        _force_lease_expired(task_id)

        affected = queue.requeue_expired_leases(queue_name_prefix=ns)

        # The money invariant — robust even if a concurrent reaper also acts,
        # because no reaper (old or new) ever writes this already-COMPLETED job row.
        job = repo.get_job(job_id)
        assert job.status == JobStatus.COMPLETED, job.status
        assert job.result_json == billed, "reaper must never clobber a billed result"
        # When THIS reap handled the orphan, it closed it (not requeued for re-run).
        mine = [t for t in affected if t.task_id == task_id]
        if mine:
            assert mine[0].status == TaskStatus.COMPLETED
    finally:
        _delete_job(job_id)


@_SKIP
def test_reaper_dead_letters_max_attempts_one_and_fails_job() -> None:
    suffix = uuid.uuid4().hex
    ns = f"reapertest_{suffix}"
    job_id = f"reaperjob_{suffix}"
    task_id = f"reapertask_{suffix}"
    worker_id = f"reaperworker_{suffix}"
    repo = AsyncPipelineV2Repository()
    queue = PostgresTaskQueue()
    repo.ensure_schema()
    try:
        repo.create_job(job_id=job_id, job_type="reaper_test", request_json={})
        _enqueue(queue, job_id=job_id, task_id=task_id, queue_name=ns, max_attempts=1)
        queue.lease(queue_name=ns, worker_id=worker_id, lease_seconds=60)  # attempt -> 1
        _force_lease_expired(task_id)

        affected = queue.requeue_expired_leases(queue_name_prefix=ns)

        mine = [t for t in affected if t.task_id == task_id]
        if not mine:
            pytest.skip("a concurrent reaper handled the task first (shared DB); "
                        "run against a dedicated database")
        assert mine[0].status == TaskStatus.DEAD_LETTERED
        job = repo.get_job(job_id)
        assert job.status == JobStatus.FAILED
        assert job.result_json is None
    finally:
        _delete_job(job_id)
