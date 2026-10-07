from __future__ import annotations

from typing import Any, Protocol

from EdennCode.Deployment.async_pipeline_v2.models import AsyncV2Task, TaskEnvelope


class TaskQueue(Protocol):
    def enqueue(self, envelope: TaskEnvelope, *, client: Any = None) -> str:
        ...

    def lease(
        self,
        *,
        queue_name: str,
        worker_id: str,
        lease_seconds: int,
    ) -> AsyncV2Task | None:
        ...

    def heartbeat(self, *, task_id: str, worker_id: str, lease_seconds: int) -> AsyncV2Task:
        ...

    def complete(
        self,
        *,
        task_id: str,
        worker_id: str,
        client: Any = None,
    ) -> AsyncV2Task:
        ...

    def fail(
        self,
        *,
        task_id: str,
        worker_id: str,
        error: dict,
        retry: bool,
        backoff_seconds: int = 0,
    ) -> AsyncV2Task:
        ...
