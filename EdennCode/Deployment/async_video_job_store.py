from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field as dc_field
from typing import Any, Callable, Optional

from EdennCode.Deployment.api_common import JobStatus
from EdennCode.Deployment.postgres_wrapper import PostgresClient


_ASYNC_VIDEO_JOB_TABLE = "async_video_music_jobs"
_ASYNC_MULTI_IMAGE_JOB_TABLE = "async_multi_image_jobs"


@dataclass
class AsyncVideoJobState:
    status: str
    created_at: int = dc_field(default_factory=lambda: int(time.time()))
    updated_at: int = dc_field(default_factory=lambda: int(time.time()))
    finished_at: Optional[int] = None
    result: Optional[Any] = None
    error: Optional[dict[str, Any]] = None


class InMemoryAsyncVideoJobStore:
    def __init__(self, backing_store: Optional[dict[str, AsyncVideoJobState]] = None) -> None:
        self._jobs = backing_store if backing_store is not None else {}

    @property
    def jobs(self) -> dict[str, AsyncVideoJobState]:
        return self._jobs

    def cleanup_expired(self, *, ttl_seconds: int, now: Optional[int] = None) -> None:
        if ttl_seconds <= 0:
            return
        current_time = now if now is not None else int(time.time())
        terminal_statuses = {JobStatus.COMPLETED, JobStatus.FAILED}
        for job_id, state in list(self._jobs.items()):
            if state.status not in terminal_statuses or state.finished_at is None:
                continue
            if current_time - state.finished_at >= ttl_seconds:
                self._jobs.pop(job_id, None)

    def register(self, job_id: str) -> AsyncVideoJobState:
        state = AsyncVideoJobState(status=JobStatus.PENDING)
        self._jobs[job_id] = state
        return state

    def set_processing(self, job_id: str) -> AsyncVideoJobState:
        state = self._jobs[job_id]
        state.status = JobStatus.PROCESSING
        state.updated_at = int(time.time())
        return state

    def complete(self, job_id: str, result: Any) -> AsyncVideoJobState:
        state = self._jobs[job_id]
        now = int(time.time())
        state.status = JobStatus.COMPLETED
        state.updated_at = now
        state.finished_at = now
        state.result = result
        state.error = None
        return state

    def fail(self, job_id: str, error: dict[str, Any]) -> AsyncVideoJobState:
        state = self._jobs[job_id]
        now = int(time.time())
        state.status = JobStatus.FAILED
        state.updated_at = now
        state.finished_at = now
        state.error = error
        state.result = None
        return state

    def get(self, job_id: str) -> Optional[AsyncVideoJobState]:
        return self._jobs.get(job_id)


class PostgresAsyncVideoJobStore:
    def __init__(
        self,
        *,
        client_factory: Callable[[], PostgresClient] = PostgresClient.from_env,
        table_name: str = _ASYNC_VIDEO_JOB_TABLE,
    ) -> None:
        self._client_factory = client_factory
        self._table = table_name
        self._ensure_lock = threading.Lock()
        self._schema_ready = False

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._ensure_lock:
            if self._schema_ready:
                return
            with self._client_factory() as client:
                client.run_sql(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self._table} (
                        job_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        result_json JSONB,
                        error_json JSONB,
                        created_at BIGINT NOT NULL,
                        updated_at BIGINT NOT NULL,
                        finished_at BIGINT
                    )
                    """
                )
                client.run_sql(
                    f"""
                    CREATE INDEX IF NOT EXISTS {self._table}_status_finished_idx
                    ON {self._table} (status, finished_at)
                    """
                )
            self._schema_ready = True

    @staticmethod
    def _state_from_row(row: dict[str, Any] | None) -> Optional[AsyncVideoJobState]:
        if row is None:
            return None
        return AsyncVideoJobState(
            status=str(row["status"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            finished_at=(
                int(row["finished_at"]) if row.get("finished_at") is not None else None
            ),
            result=row.get("result_json"),
            error=row.get("error_json"),
        )

    def cleanup_expired(self, *, ttl_seconds: int, now: Optional[int] = None) -> None:
        if ttl_seconds <= 0:
            return
        self._ensure_schema()
        cutoff = (now if now is not None else int(time.time())) - ttl_seconds
        with self._client_factory() as client:
            client.run_sql(
                f"""
                DELETE FROM {self._table}
                WHERE status IN (%s, %s)
                  AND finished_at IS NOT NULL
                  AND finished_at <= %s
                """,
                params=[JobStatus.COMPLETED, JobStatus.FAILED, cutoff],
            )

    def register(self, job_id: str) -> AsyncVideoJobState:
        self._ensure_schema()
        now = int(time.time())
        with self._client_factory() as client:
            rows = client.run_sql(
                f"""
                INSERT INTO {self._table} (
                    job_id, status, result_json, error_json,
                    created_at, updated_at, finished_at
                )
                VALUES (%s, %s, NULL, NULL, %s, %s, NULL)
                ON CONFLICT (job_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    result_json = NULL,
                    error_json = NULL,
                    updated_at = EXCLUDED.updated_at,
                    finished_at = NULL
                RETURNING *
                """,
                params=[job_id, JobStatus.PENDING, now, now],
            )
        state = self._state_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if state is None:
            raise KeyError(job_id)
        return state

    def set_processing(self, job_id: str) -> AsyncVideoJobState:
        self._ensure_schema()
        now = int(time.time())
        with self._client_factory() as client:
            rows = client.run_sql(
                f"""
                UPDATE {self._table}
                SET status = %s, updated_at = %s
                WHERE job_id = %s
                RETURNING *
                """,
                params=[JobStatus.PROCESSING, now, job_id],
            )
        state = self._state_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if state is None:
            raise KeyError(job_id)
        return state

    def complete(self, job_id: str, result: Any) -> AsyncVideoJobState:
        self._ensure_schema()
        now = int(time.time())
        with self._client_factory() as client:
            rows = client.run_sql(
                f"""
                UPDATE {self._table}
                SET status = %s,
                    result_json = %s,
                    error_json = NULL,
                    updated_at = %s,
                    finished_at = %s
                WHERE job_id = %s
                RETURNING *
                """,
                params=[JobStatus.COMPLETED, result, now, now, job_id],
            )
        state = self._state_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if state is None:
            raise KeyError(job_id)
        return state

    def fail(self, job_id: str, error: dict[str, Any]) -> AsyncVideoJobState:
        self._ensure_schema()
        now = int(time.time())
        with self._client_factory() as client:
            rows = client.run_sql(
                f"""
                UPDATE {self._table}
                SET status = %s,
                    result_json = NULL,
                    error_json = %s,
                    updated_at = %s,
                    finished_at = %s
                WHERE job_id = %s
                RETURNING *
                """,
                params=[JobStatus.FAILED, error, now, now, job_id],
            )
        state = self._state_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if state is None:
            raise KeyError(job_id)
        return state

    def get(self, job_id: str) -> Optional[AsyncVideoJobState]:
        self._ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                f"""
                SELECT *
                FROM {self._table}
                WHERE job_id = %s
                LIMIT 1
                """,
                params=[job_id],
            )
        return self._state_from_row(rows[0] if isinstance(rows, list) and rows else None)


def build_async_job_store_from_env(
    *,
    mode_env_var: str,
    table_name: str,
    memory_store: Optional[InMemoryAsyncVideoJobStore] = None,
    client_factory: Callable[[], PostgresClient] = PostgresClient.from_env,
) -> InMemoryAsyncVideoJobStore | PostgresAsyncVideoJobStore:
    """Build an async job store from environment configuration.

    ``mode_env_var`` selects ``auto`` (default), ``postgres`` or ``memory``.
    In ``auto`` mode a Postgres store is used when Postgres connection env vars
    are present, otherwise the in-memory store is used. ``table_name`` scopes the
    Postgres-backed store so different job families do not share a table.
    ``client_factory`` lets the caller pass a pooled connection factory so the
    client-facing API reuses connections instead of reconnecting per call.
    """

    raw_mode = (os.getenv(mode_env_var) or "auto").strip().lower()
    has_postgres_env = bool(
        os.getenv("DATABASE_URL")
        or (os.getenv("PGHOST") and os.getenv("PGDATABASE") and os.getenv("PGUSER"))
    )
    mode = "postgres" if raw_mode == "auto" and has_postgres_env else raw_mode
    if mode == "postgres":
        return PostgresAsyncVideoJobStore(table_name=table_name, client_factory=client_factory)
    if mode in {"auto", "memory", "inmemory", "in-memory"}:
        return memory_store or InMemoryAsyncVideoJobStore()
    raise ValueError(
        f"{mode_env_var} must be 'auto', 'postgres', or 'memory'."
    )


def build_async_video_job_store_from_env(
    *,
    memory_store: Optional[InMemoryAsyncVideoJobStore] = None,
) -> InMemoryAsyncVideoJobStore | PostgresAsyncVideoJobStore:
    return build_async_job_store_from_env(
        mode_env_var="ASYNC_VIDEO_MUSIC_JOB_STORE",
        table_name=_ASYNC_VIDEO_JOB_TABLE,
        memory_store=memory_store,
    )


def build_async_multi_image_job_store_from_env(
    *,
    memory_store: Optional[InMemoryAsyncVideoJobStore] = None,
    client_factory: Callable[[], PostgresClient] = PostgresClient.from_env,
) -> InMemoryAsyncVideoJobStore | PostgresAsyncVideoJobStore:
    return build_async_job_store_from_env(
        mode_env_var="ASYNC_MULTI_IMAGE_JOB_STORE",
        table_name=_ASYNC_MULTI_IMAGE_JOB_TABLE,
        memory_store=memory_store,
        client_factory=client_factory,
    )


__all__ = [
    "AsyncVideoJobState",
    "InMemoryAsyncVideoJobStore",
    "PostgresAsyncVideoJobStore",
    "build_async_job_store_from_env",
    "build_async_video_job_store_from_env",
    "build_async_multi_image_job_store_from_env",
]
