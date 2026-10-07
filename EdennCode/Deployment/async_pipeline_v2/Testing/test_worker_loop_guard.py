"""Resilience tests for the async v2 worker consumer loops.

Historically each worker loop awaited ``worker.process_one()`` with no guard and
the surrounding ``asyncio.gather`` used ``return_exceptions=False``, so a single
exception from ``process_one`` (a transient DB error, a lost-lease ``KeyError``,
a poison task) propagated out and exited the whole worker process — taking every
sibling in-flight task in that replica with it. See
``EdennCode/Deployment/PROD_READINESS_REMEDIATION_PLAN.md`` finding #1.

These tests exercise the *real* loop code paths — the shared
``run_consumer_loop`` and the actual ``run_worker_loop`` functions used in
production by the multi-image / video-monolith and video-split workers — over a
real asyncio event loop. Failures are injected through a small fake worker (the
loop, not the worker, is the unit under test); everything else is real
scheduling, real ``asyncio.gather``, and a real ``asyncio.Event`` shutdown.
Before the fix these tests fail because the injected exception propagates out of
the loop instead of being absorbed.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from EdennCode.Deployment.async_pipeline_v2.workers.loop_guard import run_consumer_loop
from EdennCode.Deployment.async_pipeline_v2.workers import monolith_worker, split_stage_workers

_LOG = logging.getLogger("test.worker_loop_guard")


class _FakeWorker:
    """Fake worker whose ``process_one`` fails the first ``fail_first`` calls.

    After the failing calls it returns ``None`` (idle). Once the total call
    count reaches ``stop_after`` it sets ``shutdown_event`` so a running loop
    exits cleanly. A lock keeps the counter correct under concurrent consumers.
    """

    def __init__(
        self,
        *,
        fail_first: int = 0,
        exc: BaseException | None = None,
        stop_after: int = 3,
        shutdown_event: asyncio.Event | None = None,
        max_concurrency: int = 1,
    ) -> None:
        self.calls = 0
        self._fail_first = fail_first
        self._exc = exc or RuntimeError("injected transient failure")
        self._stop_after = stop_after
        self._shutdown_event = shutdown_event
        self._lock = asyncio.Lock()
        # Attributes the real loop functions read to build their log label.
        self.stage_name = "fake-stage"
        self.queue_name = "fake-queue"
        self.max_concurrency = max_concurrency

    async def process_one(self):
        async with self._lock:
            self.calls += 1
            n = self.calls
        if self._shutdown_event is not None and n >= self._stop_after:
            self._shutdown_event.set()
        if n <= self._fail_first:
            raise self._exc
        return None


def test_loop_absorbs_transient_exception_and_keeps_going() -> None:
    async def _run() -> _FakeWorker:
        ev = asyncio.Event()
        worker = _FakeWorker(fail_first=1, exc=RuntimeError("db blip"), stop_after=3, shutdown_event=ev)
        await run_consumer_loop(
            process_one=worker.process_one,
            poll_interval_seconds=0.01,
            error_backoff_seconds=0.01,
            shutdown_event=ev,
            logger=_LOG,
        )
        return worker

    worker = asyncio.run(asyncio.wait_for(_run(), timeout=5.0))
    # Continued past the first failing iteration rather than dying on it.
    assert worker.calls >= 3


def test_loop_absorbs_lost_lease_keyerror() -> None:
    async def _run() -> _FakeWorker:
        ev = asyncio.Event()
        # KeyError is what queue.complete()/fail() raise when the lease was lost.
        worker = _FakeWorker(fail_first=2, exc=KeyError("lease lost"), stop_after=4, shutdown_event=ev)
        await run_consumer_loop(
            process_one=worker.process_one,
            poll_interval_seconds=0.01,
            error_backoff_seconds=0.01,
            shutdown_event=ev,
            logger=_LOG,
        )
        return worker

    worker = asyncio.run(asyncio.wait_for(_run(), timeout=5.0))
    assert worker.calls >= 4


def test_loop_reraises_cancelled_error() -> None:
    """Graceful shutdown / cooperative stage-abort must still unwind the loop."""

    async def _run() -> None:
        async def _blocking_process_one():
            await asyncio.sleep(3600)

        task = asyncio.create_task(
            run_consumer_loop(
                process_one=_blocking_process_one,
                error_backoff_seconds=0.01,
                logger=_LOG,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(_run(), timeout=5.0))


def test_once_returns_after_a_single_failing_iteration() -> None:
    async def _run() -> _FakeWorker:
        worker = _FakeWorker(fail_first=1, exc=RuntimeError("boom"))
        await run_consumer_loop(
            process_one=worker.process_one,
            once=True,
            error_backoff_seconds=0.01,
            logger=_LOG,
        )
        return worker

    worker = asyncio.run(asyncio.wait_for(_run(), timeout=5.0))
    assert worker.calls == 1  # exactly one attempt, then return (no crash, no loop)


def test_monolith_run_worker_loop_does_not_die_on_exception() -> None:
    """The real loop used by the multi-image and video-monolith workers."""

    async def _run() -> None:
        worker = _FakeWorker(fail_first=1, exc=RuntimeError("db blip"))
        # once=True returns after one (failing) iteration without propagating.
        await monolith_worker.run_worker_loop(worker=worker, once=True)

    # If the exception propagated, asyncio.run would re-raise it here.
    asyncio.run(asyncio.wait_for(_run(), timeout=5.0))


def test_split_run_worker_loop_single_does_not_die_on_exception() -> None:
    async def _run() -> None:
        worker = _FakeWorker(fail_first=1, exc=KeyError("lease lost"))
        await split_stage_workers.run_worker_loop(worker=worker, once=True)

    asyncio.run(asyncio.wait_for(_run(), timeout=5.0))


def test_split_run_worker_loop_concurrent_does_not_die_on_exception() -> None:
    """The gather path (provider workers run several consumers per replica)."""

    async def _run() -> _FakeWorker:
        ev = asyncio.Event()
        worker = _FakeWorker(
            fail_first=2, exc=RuntimeError("poison task"), stop_after=4,
            shutdown_event=ev, max_concurrency=2,
        )
        await split_stage_workers.run_worker_loop(
            worker=worker,
            poll_interval_seconds=0.01,
            concurrency=2,
            shutdown_event=ev,
        )
        return worker

    # The real loop uses a 5s default error backoff; allow headroom.
    worker = asyncio.run(asyncio.wait_for(_run(), timeout=20.0))
    assert worker.calls >= 4
