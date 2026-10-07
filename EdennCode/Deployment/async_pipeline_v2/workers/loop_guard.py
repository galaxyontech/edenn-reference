"""Crash-safe consumer loop for async pipeline v2 workers.

Every worker replica runs one or more consumer loops that repeatedly lease and
process a task. Historically each loop awaited ``worker.process_one()`` with no
guard, and the surrounding ``asyncio.gather(...)`` used the default
``return_exceptions=False``. So a single exception from ``process_one`` — a
transient Postgres error while leasing or writing bookkeeping, a lost-lease
``KeyError`` from ``complete()``/``fail()`` after the reaper requeued a task, or
a poison task — propagated out of the loop and exited the whole process,
killing every sibling in-flight task in that replica.

``run_consumer_loop`` is the shared, crash-safe loop body. It catches any
``Exception`` from one iteration, logs it, backs off, and continues, so no
single task can take the replica down. ``asyncio.CancelledError`` is
deliberately NOT caught (it is a ``BaseException``, and we re-raise it
explicitly for clarity): graceful shutdown and cooperative stage cancellation
must still unwind the loop.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

# Cap the escalating error backoff so a persistently failing dependency (e.g. a
# DB outage) does not stretch the retry interval without bound.
_MAX_ERROR_BACKOFF_SECONDS = 60.0


async def run_consumer_loop(
    *,
    process_one: Callable[[], Awaitable[Optional[object]]],
    once: bool = False,
    poll_interval_seconds: float = 2.0,
    error_backoff_seconds: float = 5.0,
    shutdown_event: Optional[asyncio.Event] = None,
    logger: logging.Logger,
    label: str = "worker",
) -> None:
    """Drain a queue by calling ``process_one`` until told to stop.

    Behavior on the happy path is identical to a bare
    ``while True: task = await process_one(); ...`` loop: it returns after one
    iteration when ``once`` is set, sleeps ``poll_interval_seconds`` when the
    queue is idle (``process_one`` returned ``None``), and returns promptly when
    ``shutdown_event`` is set. The only added behavior is that an exception from
    an iteration is logged and skipped (with an escalating, capped backoff)
    instead of crashing the process.
    """
    consecutive_failures = 0
    while True:
        if shutdown_event is not None and shutdown_event.is_set():
            return
        try:
            task = await process_one()
        except asyncio.CancelledError:
            # Shutdown / cooperative cancellation — let it unwind the loop.
            raise
        except Exception:
            consecutive_failures += 1
            logger.exception(
                "%s: worker iteration failed (%d consecutive); continuing",
                label,
                consecutive_failures,
            )
            if once:
                return
            backoff = min(
                error_backoff_seconds * consecutive_failures,
                _MAX_ERROR_BACKOFF_SECONDS,
            )
            await asyncio.sleep(backoff)
            continue

        consecutive_failures = 0
        if once:
            return
        if task is None:
            await asyncio.sleep(poll_interval_seconds)


__all__ = ["run_consumer_loop"]
