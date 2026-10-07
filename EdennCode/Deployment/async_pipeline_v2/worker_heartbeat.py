from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from EdennCode.Deployment.async_pipeline_v2.models import StageStatus


logger = logging.getLogger(__name__)


class StageLeaseHeartbeat:
    """Keep a leased async v2 task alive while a long stage runs.

    Provider polling, video compression, and rendering can exceed the queue
    lease if the worker does not renew it. This async context manager renews
    the task lease and updates the stage-run heartbeat at a fixed interval
    without changing stage business logic. Workers should enter the context only
    after a task has been leased and its stage_run row has been created.
    """

    def __init__(
        self,
        *,
        queue: Any,
        repository: Any,
        task_id: str,
        worker_id: str,
        lease_seconds: int,
        stage_run_id: Optional[str],
        interval_seconds: Optional[float] = None,
        stage_status: str = StageStatus.PROCESSING,
        diagnostic_logger: Optional[logging.Logger] = None,
    ) -> None:
        self.queue = queue
        self.repository = repository
        self.task_id = task_id
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.stage_run_id = stage_run_id
        self.interval_seconds = interval_seconds or self._default_interval(lease_seconds)
        self.stage_status = stage_status
        self.logger = diagnostic_logger or logger
        self._task: asyncio.Task[None] | None = None

    @staticmethod
    def _default_interval(lease_seconds: int) -> float:
        lease = max(1, int(lease_seconds or 1))
        return max(5.0, min(60.0, lease / 3.0))

    async def __aenter__(self) -> "StageLeaseHeartbeat":
        await self.beat_once()
        self._task = asyncio.create_task(self._run(), name=f"heartbeat:{self.task_id}")
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            return

    async def beat_once(self) -> None:
        """Renew the task lease and mark the current stage_run as alive."""

        self.queue.heartbeat(
            task_id=self.task_id,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if self.stage_run_id:
            self.repository.update_stage_run(
                self.stage_run_id,
                status=self.stage_status,
                heartbeat=True,
            )

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            try:
                await self.beat_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning(
                    "Async v2 heartbeat failed for task %s: %s",
                    self.task_id,
                    exc,
                    exc_info=True,
                )


__all__ = ["StageLeaseHeartbeat"]
