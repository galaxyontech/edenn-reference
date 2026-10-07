from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

from EdennCode.Deployment.async_pipeline_v2.models import (
    AsyncV2Task,
    JobStatus,
    TaskEnvelope,
    TaskStatus,
)
from EdennCode.Deployment.async_pipeline_v2.repositories import AsyncPipelineV2Repository
from EdennCode.Deployment.postgres_wrapper import PostgresClient


# Client-safe, provider-neutral error the reaper records when it dead-letters a
# task whose worker died and whose attempts are exhausted.
_REAPER_DEAD_LETTER_ERROR = {
    "message": (
        "The job was stopped because its worker stopped responding and the maximum "
        "number of attempts was reached."
    ),
    "type": "LeaseExpiredMaxAttempts",
    "retryable": False,
}


class PostgresTaskQueue:
    def __init__(
        self,
        *,
        client_factory: Callable[[], PostgresClient] = PostgresClient.from_env,
        ensure_schema: bool = True,
    ) -> None:
        self._client_factory = client_factory
        self._ensure_schema_enabled = ensure_schema
        self._ensure_lock = threading.Lock()
        self._schema_ready = False

    def ensure_schema(self) -> None:
        if not self._ensure_schema_enabled or self._schema_ready:
            return
        with self._ensure_lock:
            if self._schema_ready:
                return
            AsyncPipelineV2Repository(client_factory=self._client_factory).ensure_schema()
            self._schema_ready = True

    @contextmanager
    def _client_context(
        self,
        client: Optional[PostgresClient] = None,
    ) -> Iterator[PostgresClient]:
        if client is not None:
            yield client
            return
        with self._client_factory() as created:
            yield created

    @staticmethod
    def _task_from_row(row: dict[str, Any] | None) -> Optional[AsyncV2Task]:
        if row is None:
            return None
        return AsyncV2Task(
            task_id=str(row["task_id"]),
            job_id=str(row["job_id"]),
            queue_name=str(row["queue_name"]),
            task_type=str(row["task_type"]),
            status=str(row["status"]),
            payload_json=dict(row.get("payload_json") or {}),
            priority=int(row.get("priority") or 0),
            attempt=int(row.get("attempt") or 0),
            max_attempts=int(row.get("max_attempts") or 3),
            lease_owner=row.get("lease_owner"),
            lease_until=row.get("lease_until"),
            not_before=row.get("not_before"),
            idempotency_key=row.get("idempotency_key"),
            last_error_json=row.get("last_error_json"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            finished_at=row.get("finished_at"),
        )

    def enqueue(
        self,
        envelope: TaskEnvelope,
        *,
        client: Optional[PostgresClient] = None,
    ) -> str:
        self.ensure_schema()
        with self._client_context(client) as active_client:
            rows = active_client.run_sql(
                """
                INSERT INTO async_v2_tasks (
                    task_id, job_id, queue_name, task_type, status,
                    payload_json, priority, max_attempts, not_before,
                    idempotency_key
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()), %s)
                ON CONFLICT (task_id) DO UPDATE SET
                    payload_json = EXCLUDED.payload_json,
                    priority = EXCLUDED.priority,
                    max_attempts = EXCLUDED.max_attempts,
                    not_before = EXCLUDED.not_before,
                    idempotency_key = EXCLUDED.idempotency_key,
                    updated_at = now()
                RETURNING task_id
                """,
                params=[
                    envelope.task_id,
                    envelope.job_id,
                    envelope.queue_name,
                    envelope.task_type,
                    TaskStatus.QUEUED,
                    envelope.payload_json,
                    envelope.priority,
                    envelope.max_attempts,
                    envelope.not_before,
                    envelope.idempotency_key,
                ],
            )
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("Failed to enqueue task.")
        return str(rows[0]["task_id"])

    def get_task(self, task_id: str) -> Optional[AsyncV2Task]:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                "SELECT * FROM async_v2_tasks WHERE task_id = %s LIMIT 1",
                params=[task_id],
            )
        return self._task_from_row(rows[0] if isinstance(rows, list) and rows else None)

    def lease(
        self,
        *,
        queue_name: str,
        worker_id: str,
        lease_seconds: int,
    ) -> AsyncV2Task | None:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                WITH candidate AS (
                    SELECT task_id
                    FROM async_v2_tasks
                    WHERE queue_name = %s
                      AND status = %s
                      AND not_before <= now()
                    ORDER BY priority DESC, created_at, task_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE async_v2_tasks task
                SET status = %s,
                    lease_owner = %s,
                    lease_until = now() + (%s || ' seconds')::interval,
                    attempt = attempt + 1,
                    updated_at = now(),
                    last_error_json = NULL
                FROM candidate
                WHERE task.task_id = candidate.task_id
                RETURNING task.*
                """,
                params=[
                    queue_name,
                    TaskStatus.QUEUED,
                    TaskStatus.LEASED,
                    worker_id,
                    lease_seconds,
                ],
            )
        return self._task_from_row(rows[0] if isinstance(rows, list) and rows else None)

    def heartbeat(self, *, task_id: str, worker_id: str, lease_seconds: int) -> AsyncV2Task:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                UPDATE async_v2_tasks
                SET lease_until = now() + (%s || ' seconds')::interval,
                    updated_at = now()
                WHERE task_id = %s
                  AND status = %s
                  AND lease_owner = %s
                RETURNING *
                """,
                params=[lease_seconds, task_id, TaskStatus.LEASED, worker_id],
            )
        task = self._task_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if task is None:
            raise KeyError(task_id)
        return task

    def complete(
        self,
        *,
        task_id: str,
        worker_id: str,
        client: Optional[PostgresClient] = None,
    ) -> AsyncV2Task:
        self.ensure_schema()
        with self._client_context(client) as active_client:
            rows = active_client.run_sql(
                """
                UPDATE async_v2_tasks
                SET status = %s,
                    lease_owner = NULL,
                    lease_until = NULL,
                    updated_at = now(),
                    finished_at = now()
                WHERE task_id = %s
                  AND status = %s
                  AND lease_owner = %s
                RETURNING *
                """,
                params=[TaskStatus.COMPLETED, task_id, TaskStatus.LEASED, worker_id],
            )
        task = self._task_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if task is None:
            raise KeyError(task_id)
        return task

    def fail(
        self,
        *,
        task_id: str,
        worker_id: str,
        error: dict[str, Any],
        retry: bool,
        backoff_seconds: int = 0,
        client: Optional[PostgresClient] = None,
    ) -> AsyncV2Task:
        self.ensure_schema()
        with self._client_context(client) as active_client:
            rows = active_client.run_sql(
                """
                UPDATE async_v2_tasks
                SET status = CASE
                        WHEN %s AND attempt < max_attempts THEN %s
                        WHEN %s THEN %s
                        ELSE %s
                    END,
                    lease_owner = NULL,
                    lease_until = NULL,
                    not_before = CASE
                        WHEN %s AND attempt < max_attempts
                        THEN now() + (%s || ' seconds')::interval
                        ELSE not_before
                    END,
                    last_error_json = %s,
                    updated_at = now(),
                    finished_at = CASE
                        WHEN %s AND attempt < max_attempts THEN NULL
                        ELSE now()
                    END
                WHERE task_id = %s
                  AND status = %s
                  AND lease_owner = %s
                RETURNING *
                """,
                params=[
                    retry,
                    TaskStatus.QUEUED,
                    retry,
                    TaskStatus.DEAD_LETTERED,
                    TaskStatus.FAILED,
                    retry,
                    backoff_seconds,
                    error,
                    retry,
                    task_id,
                    TaskStatus.LEASED,
                    worker_id,
                ],
            )
        task = self._task_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if task is None:
            raise KeyError(task_id)
        return task

    def requeue_expired_leases(
        self,
        *,
        limit: int = 100,
        queue_name_prefix: Optional[str] = None,
    ) -> list[AsyncV2Task]:
        """Recover tasks whose lease expired because their owning worker died.

        Each stale leased task is transitioned based on its owning job's status and
        its attempt count, all read under one ``FOR UPDATE SKIP LOCKED`` lock:

        - Owning job already terminal (completed/failed/canceled): close the orphan
          task as COMPLETED and leave the job row UNTOUCHED. A worker that finished a
          job (writing ``result_json``) but crashed before completing its task must
          never have its billed result clobbered by the reaper.
        - Job not terminal and ``attempt < max_attempts``: requeue (QUEUED) for a
          live worker to retake — the existing self-heal.
        - Job not terminal and ``attempt >= max_attempts``: dead-letter the task and
          mark the owning job FAILED via a dedicated, guarded write that never
          references ``result_json`` — this bounds the poison-task loop (the task no
          longer re-leases forever) without stranding the job in ``processing``.

        When ``queue_name_prefix`` is provided, only queues under that namespace
        prefix are reaped, so a namespaced worker never touches another namespace's
        tasks in a shared database.
        """
        self.ensure_schema()
        prefix_clause = "AND t.queue_name LIKE %s" if queue_name_prefix else ""
        terminal = [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED]
        params: list[Any] = []
        params += terminal          # is_requeue: job.status NOT IN (...)
        params += terminal          # job_terminal: job.status IN (...)
        params.append(TaskStatus.LEASED)   # WHERE t.status = %s
        if queue_name_prefix:
            params.append(f"{queue_name_prefix}%")
        params.append(limit)               # LIMIT %s
        params += [                         # status CASE
            TaskStatus.COMPLETED,           #   job terminal -> close orphan
            TaskStatus.QUEUED,              #   requeue
            TaskStatus.DEAD_LETTERED,       #   attempts exhausted
        ]
        with self._client_factory() as client:
            rows = client.run_sql(
                f"""
                WITH stale AS (
                    SELECT t.task_id,
                           (j.status NOT IN (%s, %s, %s)
                            AND t.attempt < t.max_attempts) AS is_requeue,
                           (j.status IN (%s, %s, %s)) AS job_terminal
                    FROM async_v2_tasks t
                    JOIN async_v2_jobs j ON j.job_id = t.job_id
                    WHERE t.status = %s
                      AND t.lease_until IS NOT NULL
                      AND t.lease_until < now()
                      {prefix_clause}
                    ORDER BY t.lease_until, t.task_id
                    LIMIT %s
                    FOR UPDATE OF t SKIP LOCKED
                )
                UPDATE async_v2_tasks task
                SET status = CASE
                        WHEN stale.job_terminal THEN %s
                        WHEN stale.is_requeue THEN %s
                        ELSE %s
                    END,
                    lease_owner = NULL,
                    lease_until = NULL,
                    not_before = CASE WHEN stale.is_requeue THEN now() ELSE not_before END,
                    finished_at = CASE WHEN stale.is_requeue THEN NULL ELSE now() END,
                    updated_at = now()
                FROM stale
                WHERE task.task_id = stale.task_id
                RETURNING task.*
                """,
                params=params,
            )
            affected = [
                task
                for row in (rows if isinstance(rows, list) else [])
                if (task := self._task_from_row(row)) is not None
            ]
            dead_job_ids = [
                task.job_id for task in affected
                if task.status == TaskStatus.DEAD_LETTERED
            ]
            if dead_job_ids:
                # Dedicated write: mark ONLY genuinely-dead jobs FAILED. It never
                # references result_json (so it cannot NULL a billed result) and is
                # guarded against terminal jobs (so a job that completed in the race
                # window between the CTE read and here is left untouched).
                placeholders = ", ".join(["%s"] * len(dead_job_ids))
                client.run_sql(
                    f"""
                    UPDATE async_v2_jobs
                    SET status = %s,
                        error_json = %s,
                        updated_at = now(),
                        finished_at = now()
                    WHERE job_id IN ({placeholders})
                      AND status NOT IN (%s, %s, %s)
                    """,
                    params=[
                        JobStatus.FAILED,
                        _REAPER_DEAD_LETTER_ERROR,
                        *dead_job_ids,
                        JobStatus.COMPLETED,
                        JobStatus.FAILED,
                        JobStatus.CANCELED,
                    ],
                )
        return affected

    def cancel_job_tasks(self, *, job_id: str) -> int:
        self.ensure_schema()
        with self._client_factory() as client:
            result = client.run_sql(
                """
                UPDATE async_v2_tasks
                SET status = %s,
                    lease_owner = NULL,
                    lease_until = NULL,
                    updated_at = now(),
                    finished_at = now()
                WHERE job_id = %s
                  AND status IN (%s, %s)
                """,
                params=[TaskStatus.CANCELED, job_id, TaskStatus.QUEUED, TaskStatus.LEASED],
            )
        return int(result) if isinstance(result, int) else 0

    def cancel_job_tasks_if_unstarted(self, *, job_id: str) -> tuple[int, bool]:
        """Cancel a job's tasks only when none has ever started.

        Business rule: generation is billed the moment work starts, so a job is
        cancelable only while every one of its tasks is still queued (never
        leased). Returns ``(canceled_count, allowed)``; ``allowed=False`` means
        at least one task is or was in flight and the job must run to
        completion. Any task that slips into a lease between the check and the
        update keeps running — the terminal-state guard on the job row then
        makes its late write a no-op, so state stays consistent.
        """
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                SELECT count(*) AS started
                FROM async_v2_tasks
                WHERE job_id = %s
                  AND status NOT IN (%s, %s)
                """,
                params=[job_id, TaskStatus.QUEUED, TaskStatus.CANCELED],
            )
            started = 0
            if isinstance(rows, list) and rows:
                first = rows[0]
                started = int(first.get("started", 0) if isinstance(first, dict) else first[0])
            if started:
                return 0, False
            result = client.run_sql(
                """
                UPDATE async_v2_tasks
                SET status = %s,
                    lease_owner = NULL,
                    lease_until = NULL,
                    updated_at = now(),
                    finished_at = now()
                WHERE job_id = %s
                  AND status = %s
                """,
                params=[TaskStatus.CANCELED, job_id, TaskStatus.QUEUED],
            )
        return (int(result) if isinstance(result, int) else 0), True


__all__ = ["PostgresTaskQueue"]
