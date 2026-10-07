"""Completed-job guard tests for the monolith / multi-image workers (finding #2).

If a worker leases a task whose job already COMPLETED (e.g. a requeued orphan
whose original worker recovered and finished the job), it must NOT re-run the
paid generation — it just closes the task. These tests drive the real
``process_one`` and prove that, for an already-completed job, no paid work runs
(a tripwire raises if it does), the task is completed, and a ``task.skipped``
event is recorded.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from EdennCode.Deployment.async_pipeline_v2.models import (
    AsyncV2Job,
    AsyncV2Task,
    JobStatus,
    TaskStatus,
)
from EdennCode.Deployment.async_pipeline_v2.workers.monolith_worker import (
    VideoMusicMonolithWorker,
)
from EdennCode.Deployment.async_pipeline_v2.workers.multi_image_worker import (
    MultiImageMonolithWorker,
)


class _FakeQueue:
    def __init__(self, task):
        self._task = task
        self._leased = False
        self.completed: list[str] = []

    def lease(self, **_):
        if self._leased:
            return None
        self._leased = True
        return self._task

    def complete(self, *, task_id, worker_id, client=None):
        self.completed.append(task_id)
        return replace(self._task, status=TaskStatus.COMPLETED)


class _TripwireRepo:
    """get_job returns a COMPLETED job; any further data access is a test failure
    (it would mean the completed-job guard did not short-circuit)."""

    def __init__(self, job):
        self._job = job
        self.events: list[dict] = []

    def get_job(self, _job_id):
        return self._job

    def add_event(self, **kw):
        self.events.append(kw)

    def get_artifact(self, *_a, **_k):  # reached only if the guard failed to fire
        raise AssertionError("guard did not fire: worker started real work")

    def start_stage_run(self, *_a, **_k):
        raise AssertionError("guard did not fire: worker started a stage run")


def _completed_job(job_id: str) -> AsyncV2Job:
    return AsyncV2Job(
        job_id=job_id, job_type="test", status=JobStatus.COMPLETED,
        request_json={}, result_json={"video_metadata": {"video_url": "blob://x"}})


def _task(job_id: str, task_type: str) -> AsyncV2Task:
    return AsyncV2Task(
        task_id=f"{job_id}:task", job_id=job_id, queue_name="q",
        task_type=task_type, status=TaskStatus.LEASED, payload_json={})


def test_monolith_guard_skips_completed_job():
    job = _completed_job("mono1")
    queue = _FakeQueue(_task("mono1", "video_music_monolith"))
    repo = _TripwireRepo(job)
    worker = VideoMusicMonolithWorker.__new__(VideoMusicMonolithWorker)
    worker.repository = repo
    worker.queue = queue
    worker.worker_id = "w1"
    worker.queue_name = "q"
    worker.lease_seconds = 60

    result = asyncio.run(worker.process_one())

    assert result is not None and result.status == TaskStatus.COMPLETED
    assert queue.completed == ["mono1:task"]  # task closed, not re-run
    assert any(e["event_type"] == "task.skipped" for e in repo.events)


def test_multi_image_guard_skips_completed_job(tmp_path: Path):
    job = _completed_job("mi1")
    queue = _FakeQueue(_task("mi1", "multi_image_monolith"))
    repo = _TripwireRepo(job)
    worker = MultiImageMonolithWorker.__new__(MultiImageMonolithWorker)
    worker.repository = repo
    worker.queue = queue
    worker.worker_id = "w1"
    worker.queue_name = "q"
    worker.lease_seconds = 60
    worker.workdir = tmp_path

    result = asyncio.run(worker.process_one())

    assert result is not None and result.status == TaskStatus.COMPLETED
    assert queue.completed == ["mi1:task"]
    assert any(e["event_type"] == "task.skipped" for e in repo.events)
