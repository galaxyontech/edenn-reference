"""Tests for v2 terminal callback delivery (finding #18).

The v2 API accepted callback_url and stored it in the task payload, but no worker
ever delivered it. These tests cover the fix:
- the best-effort delivery primitive (real HTTP POST, correct payload, no-op
  without a URL, swallows receiver errors);
- the real split-stage ``process_one`` terminal transitions — delivery fires on
  a finished success and on a permanent failure, and NOT on a retryable re-queue
  (the critical gate) nor when no callback_url is set.

Everything drives the real code over a real local HTTP receiver.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from EdennCode.Deployment.async_pipeline_v2.callback_delivery import (
    deliver_job_callback,
    deliver_terminal_callback,
)
from EdennCode.Deployment.async_pipeline_v2.models import (
    AsyncV2Job,
    AsyncV2Task,
    JobStatus,
    TaskStatus,
)
from EdennCode.Deployment.async_pipeline_v2.workers import split_stage_workers as ssw
from EdennCode.Deployment.async_pipeline_v2.workers.split_stage_workers import (
    StageTaskResult,
    _SplitStageWorkerBase,
)

_LOG = logging.getLogger("test.callback_delivery")


class _Receiver(BaseHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.server.received.append(json.loads(body))  # type: ignore[attr-defined]
        code = getattr(self.server, "respond_code", 200)  # type: ignore[attr-defined]
        self.send_response(code)
        self.end_headers()


def _start_receiver(respond_code: int = 200):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Receiver)
    server.received = []  # type: ignore[attr-defined]
    server.respond_code = respond_code  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _url(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/cb"


# --- delivery primitive ----------------------------------------------------

def test_deliver_job_callback_posts_terminal_status():
    server = _start_receiver()
    try:
        job = SimpleNamespace(
            job_id="job-1",
            request_json={"callback_url": _url(server)},
            created_at=None,
        )
        asyncio.run(deliver_job_callback(
            job, status="completed", result_json={"ok": True}, error_json=None, logger=_LOG))
        assert len(server.received) == 1
        body = server.received[0]
        assert body["job_id"] == "job-1"
        assert body["status"] == "completed"
        assert body["result"] == {"ok": True}
        assert body["error"] is None
    finally:
        server.shutdown()


def test_callback_strips_internal_cost_breakdown():
    """The webhook payload (a video-music surface) exposes only total_cost +
    creative_duration; the generation/token breakdown stays internal."""
    server = _start_receiver()
    try:
        job = SimpleNamespace(
            job_id="job-cost",
            request_json={"callback_url": _url(server)},
            created_at=None,
        )
        result_json = {
            "job_id": "job-cost",
            "status": "completed",
            "version": "v",
            "cost_metadata": {
                "total_cost": 0.0734,
                "creative_duration": 5.2,
                "model_spec_name": "x",
                "creation_cost": 0.065,
                "creation_times": 1,
                "token_num": 805,
                "token_cost": 0.0084,
            },
            "video_metadata": {"video_url": "https://x/v.mp4"},
        }
        asyncio.run(deliver_job_callback(
            job, status="completed", result_json=result_json, error_json=None, logger=_LOG))
        body = server.received[0]
        # Cost accounting never leaves the service.
        assert "cost_metadata" not in body["result"]
        # envelope duplicates also stripped, as on the status endpoint
        assert "version" not in body["result"]
    finally:
        server.shutdown()


def test_deliver_job_callback_noop_without_url():
    job = SimpleNamespace(job_id="job-2", request_json={}, created_at=None)
    # No URL -> no exception, no POST (nothing to assert beyond "does not raise").
    asyncio.run(deliver_job_callback(
        job, status="completed", result_json=None, error_json=None, logger=_LOG))


def test_delivery_swallows_receiver_error():
    server = _start_receiver(respond_code=500)
    try:
        # A 500 (and any exception) must be swallowed — never raises.
        asyncio.run(deliver_terminal_callback(
            url=_url(server), job_id="job-3", status="failed",
            result_json=None, error_json={"code": 1}, created_at=None, logger=_LOG))
    finally:
        server.shutdown()


def test_delivery_swallows_unreachable_host():
    # Nothing listening on this port -> connection refused -> swallowed.
    asyncio.run(deliver_terminal_callback(
        url="http://127.0.0.1:9/cb", job_id="job-4", status="failed",
        result_json=None, error_json=None, created_at=None, logger=_LOG))


# --- real split-stage process_one terminal transitions ---------------------

class _FakeQueue:
    def __init__(self, task, fail_status):
        self._task = task
        self._fail_status = fail_status
        self._leased = False

    def lease(self, **_):
        if self._leased:
            return None
        self._leased = True
        return self._task

    def complete(self, **_):
        return replace(self._task, status=TaskStatus.COMPLETED)

    def fail(self, **_):
        return replace(self._task, status=self._fail_status)

    def ensure_schema(self):
        pass


class _FakeRepo:
    def __init__(self, job):
        self._job = job

    def get_job(self, _job_id):
        return self._job

    def start_stage_run(self, **_):
        return SimpleNamespace(stage_run_id="sr-1")

    def update_job_status(self, *a, **k):
        pass

    def update_job_status_checked(self, *a, **k):
        return SimpleNamespace(status=k.get("status")), True

    def add_event(self, **_):
        pass

    def update_stage_run(self, *a, **k):
        pass

    def ensure_schema(self):
        pass


class _DeliveryWorker(_SplitStageWorkerBase):
    stage_name = "finalize-test"
    task_type = "finalize-test"
    queue_name = "finalize-test-q"

    def __init__(self, *, repository, queue, finished, raise_exc=None, result_json=None):
        # Bypass the heavy base __init__; set only what process_one touches.
        self.repository = repository
        self.queue = queue
        self.worker_id = "w1"
        self.lease_seconds = 60
        self.retry_backoff_seconds = 1
        self._finished = finished
        self._raise = raise_exc
        self._result_json = result_json

    async def _run_stage(self, *, job, task):
        if self._raise is not None:
            raise self._raise
        return StageTaskResult(
            output_json={}, finished=self._finished, result_json=self._result_json)

    def _commit_stage_success(self, *, task, stage_run_id, result):
        return self.queue.complete(task_id=task.task_id, worker_id=self.worker_id), True

    def _emit_stage_metrics(self, **_):
        pass


def _run_worker(*, callback_url, finished=True, raise_exc=None, fail_status=TaskStatus.FAILED,
                result_json=None, monkeypatch):
    # Neutralize the heartbeat context manager (needs a live queue/DB otherwise).
    class _NoHeartbeat:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(ssw, "StageLeaseHeartbeat", _NoHeartbeat)

    task = AsyncV2Task(
        task_id="t-1", job_id="job-9", queue_name="finalize-test-q",
        task_type="finalize-test", status=TaskStatus.LEASED, payload_json={},
    )
    request = {"callback_url": callback_url} if callback_url else {}
    job = AsyncV2Job(job_id="job-9", job_type="video_music", status=JobStatus.PROCESSING,
                     request_json=request)
    worker = _DeliveryWorker(
        repository=_FakeRepo(job), queue=_FakeQueue(task, fail_status),
        finished=finished, raise_exc=raise_exc, result_json=result_json)
    asyncio.run(worker.process_one())


def test_split_success_delivers_completed(monkeypatch):
    server = _start_receiver()
    try:
        _run_worker(callback_url=_url(server), finished=True,
                    result_json={"video_metadata": {"video_url": "x"}}, monkeypatch=monkeypatch)
        assert len(server.received) == 1
        assert server.received[0]["status"] == JobStatus.COMPLETED
        assert server.received[0]["result"] == {"video_metadata": {"video_url": "x"}}
    finally:
        server.shutdown()


def test_split_non_final_stage_does_not_deliver(monkeypatch):
    server = _start_receiver()
    try:
        # A stage that is not the last one (finished=False) must not fire a callback.
        _run_worker(callback_url=_url(server), finished=False, monkeypatch=monkeypatch)
        assert server.received == []
    finally:
        server.shutdown()


def test_split_permanent_failure_delivers_failed(monkeypatch):
    server = _start_receiver()
    try:
        _run_worker(callback_url=_url(server), raise_exc=RuntimeError("boom"),
                    fail_status=TaskStatus.FAILED, monkeypatch=monkeypatch)
        assert len(server.received) == 1
        assert server.received[0]["status"] == JobStatus.FAILED
        assert server.received[0]["error"] is not None
    finally:
        server.shutdown()


def test_split_retryable_requeue_does_not_deliver(monkeypatch):
    server = _start_receiver()
    try:
        # fail() returns QUEUED (retryable) -> must NOT fire a spurious 'failed' callback.
        _run_worker(callback_url=_url(server), raise_exc=RuntimeError("transient"),
                    fail_status=TaskStatus.QUEUED, monkeypatch=monkeypatch)
        assert server.received == []
    finally:
        server.shutdown()


def test_split_no_callback_url_no_delivery(monkeypatch):
    server = _start_receiver()
    try:
        _run_worker(callback_url=None, finished=True, result_json={"x": 1}, monkeypatch=monkeypatch)
        assert server.received == []
    finally:
        server.shutdown()
