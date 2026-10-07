"""The loop-stall watchdog turns a silent event-loop freeze into a CRITICAL log
plus a full stack dump — the diagnostic for the 2026-07-16 split-analysis hangs
where a blocked synchronous call starved every consumer and lease heartbeat."""
import asyncio
import logging
import time

from EdennCode.Deployment.async_pipeline_v2.worker_main import _start_loop_stall_watchdog


def test_watchdog_fires_when_loop_is_blocked(caplog):
    async def scenario():
        _start_loop_stall_watchdog(asyncio.get_running_loop(), stall_seconds=0.3)
        # Simulate the failure mode: a synchronous call blocking the loop long
        # enough that the watchdog's ping goes unanswered.
        time.sleep(1.0)
        await asyncio.sleep(0.05)

    with caplog.at_level(logging.CRITICAL):
        asyncio.run(scenario())

    assert any("unresponsive" in record.message for record in caplog.records)


def test_watchdog_stays_quiet_on_responsive_loop(caplog):
    async def scenario():
        _start_loop_stall_watchdog(asyncio.get_running_loop(), stall_seconds=0.5)
        await asyncio.sleep(0.2)  # loop keeps servicing callbacks

    with caplog.at_level(logging.CRITICAL):
        asyncio.run(scenario())

    assert not any("unresponsive" in record.message for record in caplog.records)
