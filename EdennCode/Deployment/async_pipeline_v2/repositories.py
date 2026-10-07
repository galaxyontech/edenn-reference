from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from EdennCode.Deployment.async_pipeline_v2.models import (
    AsyncV2Artifact,
    AsyncV2Job,
    AsyncV2JobEvent,
    AsyncV2StageRun,
    JobStatus,
    StageStatus,
    new_id,
)
from EdennCode.Deployment.postgres_wrapper import PostgresClient


MIGRATION_PATH = Path(__file__).resolve().parent / "migrations" / "001_async_pipeline_v2.sql"


class AsyncPipelineV2Repository:
    def __init__(
        self,
        *,
        client_factory: Callable[[], PostgresClient] = PostgresClient.from_env,
    ) -> None:
        self._client_factory = client_factory
        self._ensure_lock = threading.Lock()
        self._schema_ready = False

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._ensure_lock:
            if self._schema_ready:
                return
            sql_text = MIGRATION_PATH.read_text(encoding="utf-8")
            with self._client_factory() as client:
                client.run_sql(sql_text)
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

    @contextmanager
    def transaction(self) -> Iterator[PostgresClient]:
        """Open a Postgres transaction for multi-table async-v2 writes.

        The repository keeps single-method calls autocommitted for ordinary API
        usage. Split workers use this context for the short final handoff where
        stage completion, job status, events, next-task enqueue, and current
        task completion must succeed or roll back together.
        """
        self.ensure_schema()
        with self._client_factory() as client:
            with client.transaction():
                yield client

    @staticmethod
    def _job_from_row(row: dict[str, Any] | None) -> Optional[AsyncV2Job]:
        if row is None:
            return None
        return AsyncV2Job(
            job_id=str(row["job_id"]),
            session_id=row.get("session_id"),
            creator_user_id=row.get("creator_user_id"),
            actor_user_id=row.get("actor_user_id"),
            job_type=str(row["job_type"]),
            status=str(row["status"]),
            current_stage=row.get("current_stage"),
            progress_percent=float(row.get("progress_percent") or 0),
            priority=int(row.get("priority") or 0),
            request_json=dict(row.get("request_json") or {}),
            result_json=row.get("result_json"),
            error_json=row.get("error_json"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            finished_at=row.get("finished_at"),
        )

    @staticmethod
    def _stage_run_from_row(row: dict[str, Any] | None) -> Optional[AsyncV2StageRun]:
        if row is None:
            return None
        return AsyncV2StageRun(
            stage_run_id=str(row["stage_run_id"]),
            job_id=str(row["job_id"]),
            task_id=row.get("task_id"),
            stage_name=str(row["stage_name"]),
            status=str(row["status"]),
            attempt=int(row.get("attempt") or 1),
            input_json=dict(row.get("input_json") or {}),
            output_json=row.get("output_json"),
            error_json=row.get("error_json"),
            started_at=row.get("started_at"),
            heartbeat_at=row.get("heartbeat_at"),
            finished_at=row.get("finished_at"),
        )

    @staticmethod
    def _artifact_from_row(row: dict[str, Any] | None) -> Optional[AsyncV2Artifact]:
        if row is None:
            return None
        return AsyncV2Artifact(
            artifact_id=str(row["artifact_id"]),
            job_id=str(row["job_id"]),
            artifact_type=str(row["artifact_type"]),
            role=row.get("role"),
            container=row.get("container"),
            blob_name=row.get("blob_name"),
            url=row.get("url"),
            content_type=row.get("content_type"),
            local_path=row.get("local_path"),
            metadata_json=dict(row.get("metadata_json") or {}),
            payload_json=row.get("payload_json"),
            created_at=row.get("created_at"),
        )

    @staticmethod
    def _event_from_row(row: dict[str, Any] | None) -> Optional[AsyncV2JobEvent]:
        if row is None:
            return None
        return AsyncV2JobEvent(
            event_id=str(row["event_id"]),
            job_id=str(row["job_id"]),
            event_type=str(row["event_type"]),
            stage_name=row.get("stage_name"),
            message=row.get("message"),
            payload_json=dict(row.get("payload_json") or {}),
            created_at=row.get("created_at"),
        )

    def create_job(
        self,
        *,
        job_id: Optional[str] = None,
        job_type: str,
        request_json: dict[str, Any],
        session_id: Optional[str] = None,
        creator_user_id: Optional[str] = None,
        priority: int = 0,
        status: str = JobStatus.QUEUED,
    ) -> AsyncV2Job:
        self.ensure_schema()
        actual_job_id = job_id or new_id("job")
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                INSERT INTO async_v2_jobs (
                    job_id, session_id, creator_user_id, job_type, status,
                    priority, request_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                params=[
                    actual_job_id,
                    session_id,
                    creator_user_id,
                    job_type,
                    status,
                    priority,
                    request_json,
                ],
            )
        job = self._job_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if job is None:
            raise KeyError(actual_job_id)
        return job

    def get_job(self, job_id: str) -> Optional[AsyncV2Job]:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                "SELECT * FROM async_v2_jobs WHERE job_id = %s LIMIT 1",
                params=[job_id],
            )
        return self._job_from_row(rows[0] if isinstance(rows, list) and rows else None)

    # A job in one of these states is finished from the client's point of view.
    # Nothing may transition it afterwards: a late worker write must not flip a
    # canceled job to completed, and a reaper must not re-fail a completed job.
    TERMINAL_JOB_STATUSES = (
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.CANCELED,
    )

    def update_job_status(
        self,
        job_id: str,
        *,
        status: str,
        current_stage: Optional[str] = None,
        progress_percent: Optional[float] = None,
        result_json: Optional[dict[str, Any]] = None,
        error_json: Optional[dict[str, Any]] = None,
        finished: bool = False,
        client: Optional[PostgresClient] = None,
    ) -> AsyncV2Job:
        job, _ = self.update_job_status_checked(
            job_id,
            status=status,
            current_stage=current_stage,
            progress_percent=progress_percent,
            result_json=result_json,
            error_json=error_json,
            finished=finished,
            client=client,
        )
        return job

    def update_job_status_checked(
        self,
        job_id: str,
        *,
        status: str,
        current_stage: Optional[str] = None,
        progress_percent: Optional[float] = None,
        result_json: Optional[dict[str, Any]] = None,
        error_json: Optional[dict[str, Any]] = None,
        finished: bool = False,
        client: Optional[PostgresClient] = None,
    ) -> tuple[AsyncV2Job, bool]:
        """Update a job's status unless it is already terminal.

        Returns ``(job, applied)``: the job row after the call, and whether this
        write actually landed. When the job is already terminal the row is
        returned unchanged with ``applied=False`` — callers that emit events or
        deliver callbacks on terminal transitions must skip them in that case.
        """
        self.ensure_schema()
        with self._client_context(client) as active_client:
            rows = active_client.run_sql(
                """
                UPDATE async_v2_jobs
                SET status = %s,
                    current_stage = COALESCE(%s, current_stage),
                    progress_percent = COALESCE(%s, progress_percent),
                    result_json = %s,
                    error_json = %s,
                    updated_at = now(),
                    finished_at = CASE WHEN %s THEN now() ELSE finished_at END
                WHERE job_id = %s
                  AND status NOT IN %s
                RETURNING *
                """,
                params=[
                    status,
                    current_stage,
                    progress_percent,
                    result_json,
                    error_json,
                    finished,
                    job_id,
                    tuple(self.TERMINAL_JOB_STATUSES),
                ],
            )
            job = self._job_from_row(rows[0] if isinstance(rows, list) and rows else None)
            if job is not None:
                return job, True
            # No row updated: either the job is already terminal (guarded no-op)
            # or it does not exist. Distinguish, and hand callers current state.
            rows = active_client.run_sql(
                "SELECT * FROM async_v2_jobs WHERE job_id = %s LIMIT 1",
                params=[job_id],
            )
        current = self._job_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if current is None:
            raise KeyError(job_id)
        return current, False

    def start_stage_run(
        self,
        *,
        job_id: str,
        stage_name: str,
        task_id: Optional[str] = None,
        input_json: Optional[dict[str, Any]] = None,
        attempt: int = 1,
        stage_run_id: Optional[str] = None,
    ) -> AsyncV2StageRun:
        self.ensure_schema()
        actual_stage_run_id = stage_run_id or new_id("stage")
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                INSERT INTO async_v2_stage_runs (
                    stage_run_id, job_id, task_id, stage_name, status, attempt,
                    input_json, heartbeat_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                RETURNING *
                """,
                params=[
                    actual_stage_run_id,
                    job_id,
                    task_id,
                    stage_name,
                    StageStatus.STARTED,
                    attempt,
                    input_json or {},
                ],
            )
        stage_run = self._stage_run_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if stage_run is None:
            raise KeyError(actual_stage_run_id)
        return stage_run

    def update_stage_run(
        self,
        stage_run_id: str,
        *,
        status: str,
        output_json: Optional[dict[str, Any]] = None,
        error_json: Optional[dict[str, Any]] = None,
        heartbeat: bool = False,
        finished: bool = False,
        client: Optional[PostgresClient] = None,
    ) -> AsyncV2StageRun:
        self.ensure_schema()
        with self._client_context(client) as active_client:
            rows = active_client.run_sql(
                """
                UPDATE async_v2_stage_runs
                SET status = %s,
                    output_json = %s,
                    error_json = %s,
                    heartbeat_at = CASE WHEN %s THEN now() ELSE heartbeat_at END,
                    finished_at = CASE WHEN %s THEN now() ELSE finished_at END
                WHERE stage_run_id = %s
                RETURNING *
                """,
                params=[
                    status,
                    output_json,
                    error_json,
                    heartbeat,
                    finished,
                    stage_run_id,
                ],
            )
        stage_run = self._stage_run_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if stage_run is None:
            raise KeyError(stage_run_id)
        return stage_run

    def list_stage_runs(self, job_id: str) -> list[AsyncV2StageRun]:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                SELECT *
                FROM async_v2_stage_runs
                WHERE job_id = %s
                ORDER BY started_at, stage_run_id
                """,
                params=[job_id],
            )
        return [
            stage_run
            for row in (rows if isinstance(rows, list) else [])
            if (stage_run := self._stage_run_from_row(row)) is not None
        ]

    def add_artifact(
        self,
        *,
        job_id: str,
        artifact_type: str,
        artifact_id: Optional[str] = None,
        role: Optional[str] = None,
        container: Optional[str] = None,
        blob_name: Optional[str] = None,
        url: Optional[str] = None,
        content_type: Optional[str] = None,
        local_path: Optional[str] = None,
        metadata_json: Optional[dict[str, Any]] = None,
        payload_json: Optional[dict[str, Any]] = None,
    ) -> AsyncV2Artifact:
        self.ensure_schema()
        actual_artifact_id = artifact_id or new_id("artifact")
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                INSERT INTO async_v2_artifacts (
                    artifact_id, job_id, artifact_type, role, container,
                    blob_name, url, content_type, local_path, metadata_json,
                    payload_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                params=[
                    actual_artifact_id,
                    job_id,
                    artifact_type,
                    role,
                    container,
                    blob_name,
                    url,
                    content_type,
                    local_path,
                    metadata_json or {},
                    payload_json,
                ],
            )
        artifact = self._artifact_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if artifact is None:
            raise KeyError(actual_artifact_id)
        return artifact

    def list_artifacts(self, job_id: str) -> list[AsyncV2Artifact]:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                SELECT *
                FROM async_v2_artifacts
                WHERE job_id = %s
                ORDER BY created_at, artifact_id
                """,
                params=[job_id],
            )
        return [
            artifact
            for row in (rows if isinstance(rows, list) else [])
            if (artifact := self._artifact_from_row(row)) is not None
        ]

    def get_artifact(self, artifact_id: str) -> Optional[AsyncV2Artifact]:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                SELECT *
                FROM async_v2_artifacts
                WHERE artifact_id = %s
                LIMIT 1
                """,
                params=[artifact_id],
            )
        return self._artifact_from_row(rows[0] if isinstance(rows, list) and rows else None)

    def add_event(
        self,
        *,
        job_id: str,
        event_type: str,
        event_id: Optional[str] = None,
        stage_name: Optional[str] = None,
        message: Optional[str] = None,
        payload_json: Optional[dict[str, Any]] = None,
        client: Optional[PostgresClient] = None,
    ) -> AsyncV2JobEvent:
        self.ensure_schema()
        actual_event_id = event_id or new_id("event")
        with self._client_context(client) as active_client:
            rows = active_client.run_sql(
                """
                INSERT INTO async_v2_job_events (
                    event_id, job_id, event_type, stage_name, message, payload_json
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                params=[
                    actual_event_id,
                    job_id,
                    event_type,
                    stage_name,
                    message,
                    payload_json or {},
                ],
            )
        event = self._event_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if event is None:
            raise KeyError(actual_event_id)
        return event

    def list_events(self, job_id: str, *, limit: Optional[int] = None) -> list[AsyncV2JobEvent]:
        self.ensure_schema()
        query = """
            SELECT *
            FROM async_v2_job_events
            WHERE job_id = %s
            ORDER BY created_at, event_id
        """
        params: list[Any] = [job_id]
        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)
        with self._client_factory() as client:
            rows = client.run_sql(query, params=params)
        return [
            event
            for row in (rows if isinstance(rows, list) else [])
            if (event := self._event_from_row(row)) is not None
        ]

    def build_status_view(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        stage_runs = self.list_stage_runs(job_id)
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
            "stages": [
                {
                    "stage_run_id": stage.stage_run_id,
                    "task_id": stage.task_id,
                    "name": stage.stage_name,
                    "status": stage.status,
                    "attempt": stage.attempt,
                    "started_at": stage.started_at.isoformat() if stage.started_at else None,
                    "heartbeat_at": stage.heartbeat_at.isoformat() if stage.heartbeat_at else None,
                    "finished_at": stage.finished_at.isoformat() if stage.finished_at else None,
                    "output": stage.output_json,
                    "error": stage.error_json,
                }
                for stage in stage_runs
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
                    "local_path": artifact.local_path,
                    "metadata": artifact.metadata_json,
                    "payload_available": artifact.payload_json is not None,
                    "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
                }
                for artifact in artifacts
            ],
        }


__all__ = ["AsyncPipelineV2Repository", "MIGRATION_PATH"]
