"""Tests for the orphaned-lease reaper and graceful shutdown draining.

These are DB-free: they exercise the worker-loop shutdown behavior and the reaper
sidecar's plumbing with fakes. The SQL-level namespace scoping of
``requeue_expired_leases`` is covered by the DB-backed queue suite.
"""
from __future__ import annotations

import asyncio
import unittest

from EdennCode.Deployment.async_pipeline_v2.worker_main import _run_lease_reaper
from EdennCode.Deployment.async_pipeline_v2.workers.monolith_worker import (
    run_worker_loop as run_monolith_loop,
)
from EdennCode.Deployment.async_pipeline_v2.workers.split_stage_workers import (
    run_worker_loop as run_split_loop,
)


class _FakeWorker:
    """Minimal worker stand-in: counts process_one calls, optionally stops the loop."""

    max_concurrency = 1

    def __init__(self, *, stop_event: asyncio.Event | None = None, stop_after: int = 0):
        self.calls = 0
        self._stop_event = stop_event
        self._stop_after = stop_after

    async def process_one(self):
        self.calls += 1
        if self._stop_event is not None and self.calls >= self._stop_after:
            self._stop_event.set()
        return None


class _FakeQueue:
    def __init__(self):
        self.calls = []

    def requeue_expired_leases(self, *, limit: int = 100, queue_name_prefix=None):
        self.calls.append({"limit": limit, "queue_name_prefix": queue_name_prefix})
        return []


class ShutdownDrainTest(unittest.IsolatedAsyncioTestCase):
    async def test_split_loop_returns_immediately_when_already_shutting_down(self):
        ev = asyncio.Event()
        ev.set()
        worker = _FakeWorker()
        await asyncio.wait_for(
            run_split_loop(worker=worker, poll_interval_seconds=0.01,
                           concurrency=1, shutdown_event=ev),
            timeout=2,
        )
        self.assertEqual(worker.calls, 0, "must not lease new work once shutting down")

    async def test_split_loop_stops_after_shutdown_signal(self):
        ev = asyncio.Event()
        worker = _FakeWorker(stop_event=ev, stop_after=1)
        await asyncio.wait_for(
            run_split_loop(worker=worker, poll_interval_seconds=0.01,
                           concurrency=1, shutdown_event=ev),
            timeout=2,
        )
        # one poll happened, then the shutdown check ended the loop
        self.assertEqual(worker.calls, 1)

    async def test_monolith_loop_stops_after_shutdown_signal(self):
        ev = asyncio.Event()
        worker = _FakeWorker(stop_event=ev, stop_after=1)
        await asyncio.wait_for(
            run_monolith_loop(worker=worker, poll_interval_seconds=0.01,
                              shutdown_event=ev),
            timeout=2,
        )
        self.assertEqual(worker.calls, 1)


class ReaperTest(unittest.IsolatedAsyncioTestCase):
    async def test_reaper_scopes_to_namespace_prefix_and_exits_on_shutdown(self):
        queue = _FakeQueue()
        ev = asyncio.Event()
        task = asyncio.create_task(_run_lease_reaper(
            queue=queue, shutdown_event=ev,
            queue_name_prefix="japan-prod:", interval_seconds=0.01,
        ))
        await asyncio.sleep(0.05)
        ev.set()
        await asyncio.wait_for(task, timeout=2)
        self.assertTrue(queue.calls, "reaper should have run at least one pass")
        self.assertEqual(queue.calls[0]["queue_name_prefix"], "japan-prod:")

    async def test_reaper_survives_a_failing_pass(self):
        class _BoomQueue(_FakeQueue):
            def requeue_expired_leases(self, *, limit=100, queue_name_prefix=None):
                self.calls.append(True)
                if len(self.calls) == 1:
                    raise RuntimeError("transient DB blip")
                return []

        queue = _BoomQueue()
        ev = asyncio.Event()
        task = asyncio.create_task(_run_lease_reaper(
            queue=queue, shutdown_event=ev,
            queue_name_prefix=None, interval_seconds=0.01,
        ))
        await asyncio.sleep(0.08)
        ev.set()
        await asyncio.wait_for(task, timeout=2)
        # first pass raised, loop kept going and ran again
        self.assertGreaterEqual(len(queue.calls), 2)


if __name__ == "__main__":
    unittest.main()
