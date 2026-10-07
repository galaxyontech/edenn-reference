"""Events reach viewers on other replicas.

A turn's events go to the sockets held by THIS process. With one replica that is
everyone; with two it is roughly half, and the other half watch a session that
appears frozen until their next poll. It is the last thing pinning the
deployment to a single replica.

Whether a message actually crosses between two processes is not something a fake
can tell you, so the cross-replica tests use a real Postgres and two independent
fan-out objects — which is what two replicas are, from the database's side.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any
from urllib.parse import urlparse

import pytest

from EdennCode.EdennAgent.AgenticAudio.api.fanout import (
    CHANNEL,
    MAX_PAYLOAD,
    CrossReplicaFanout,
)

DSN = os.getenv("EDENN_TEST_PG_DSN", "").strip()
pg = pytest.mark.skipif(
    not DSN, reason="set EDENN_TEST_PG_DSN to a throwaway Postgres to run these"
)


def _factory():
    from EdennCode.Deployment.postgres_wrapper import (
        PostgresClient,
        PostgresConnectionConfig,
    )

    parsed = urlparse(DSN)

    def make() -> Any:
        return PostgresClient(
            PostgresConnectionConfig(
                host=parsed.hostname or "127.0.0.1",
                port=parsed.port or 5432,
                database=(parsed.path or "/postgres").lstrip("/"),
                user=parsed.username or "postgres",
                password=parsed.password,
                sslmode="disable",
                connect_timeout=10,
            )
        )

    return make


# ---------------------------------------------------------------------------#
# degrading to one replica                                                    #
# ---------------------------------------------------------------------------#


def test_with_no_database_it_is_simply_off() -> None:
    """One replica needs no cross-replica anything, and must not pay for it."""
    fan = CrossReplicaFanout()
    assert fan.enabled is False
    asyncio.run(fan.publish("sess_1", "candidate.cards"))
    assert fan.published == 0


def test_publishing_never_raises_even_when_the_database_is_gone() -> None:
    """Fan-out is a live convenience on top of a durable snapshot. A failure
    here must not fail the turn that produced the event."""

    def broken():
        raise RuntimeError("no database today")

    fan = CrossReplicaFanout(client_factory=broken)
    asyncio.run(fan.publish("sess_1", "x"))  # must not raise
    assert fan.published == 0


def test_an_oversized_payload_is_dropped_rather_than_attempted() -> None:
    """NOTIFY has an 8000-byte ceiling; exceeding it is an error at the database
    rather than a truncation, so it is caught here."""
    fan = CrossReplicaFanout(client_factory=lambda: None)
    asyncio.run(fan.publish("s" * (MAX_PAYLOAD + 100), "x"))
    assert fan.published == 0


def test_a_malformed_notification_does_not_stop_the_listener() -> None:
    seen: list[str] = []

    async def deliver(session_id: str, event_type: str) -> None:
        seen.append(session_id)

    fan = CrossReplicaFanout(client_factory=lambda: None, deliver=deliver)
    asyncio.run(fan._apply("this is not json"))
    asyncio.run(fan._apply('{"s": "sess_1", "t": "x"}'))
    assert seen == ["sess_1"]


def test_a_failing_delivery_does_not_stop_the_listener() -> None:
    """One socket that will not accept a message must not silence the replica."""

    async def deliver(session_id: str, event_type: str) -> None:
        raise RuntimeError("socket is gone")

    fan = CrossReplicaFanout(client_factory=lambda: None, deliver=deliver)
    asyncio.run(fan._apply('{"s": "sess_1", "t": "x"}'))  # must not raise


# ---------------------------------------------------------------------------#
# across replicas                                                             #
# ---------------------------------------------------------------------------#


@pg
def test_an_event_published_on_one_replica_reaches_another() -> None:
    """The whole point. Two fan-out objects with their own connections are two
    replicas as far as the database is concerned."""
    session_id = f"sess_{uuid.uuid4().hex[:10]}"
    got: list[tuple[str, str]] = []
    arrived = asyncio.Event()

    async def deliver(sid: str, event_type: str) -> None:
        if sid == session_id:
            got.append((sid, event_type))
            arrived.set()

    async def scenario() -> None:
        listener = CrossReplicaFanout(client_factory=_factory(), deliver=deliver)
        publisher = CrossReplicaFanout(client_factory=_factory())
        await listener.start()
        try:
            await asyncio.sleep(1.5)  # let LISTEN settle
            await publisher.publish(session_id, "candidate.cards")
            await asyncio.wait_for(arrived.wait(), timeout=15)
        finally:
            await listener.stop()

    asyncio.run(scenario())
    assert got == [(session_id, "candidate.cards")]


@pg
def test_the_publisher_reports_what_it_sent() -> None:
    async def scenario() -> int:
        fan = CrossReplicaFanout(client_factory=_factory())
        await fan.publish(f"sess_{uuid.uuid4().hex[:8]}", "x")
        return fan.published

    assert asyncio.run(scenario()) == 1


@pg
def test_a_listener_survives_being_stopped_and_started() -> None:
    """A deploy restarts replicas constantly; a listener that cannot be cycled
    is one that leaks a connection every time."""

    async def scenario() -> None:
        fan = CrossReplicaFanout(client_factory=_factory(), deliver=None)
        await fan.start()
        await asyncio.sleep(0.5)
        await fan.stop()
        await fan.start()
        await asyncio.sleep(0.5)
        await fan.stop()

    asyncio.run(scenario())


@pg
def test_the_channel_name_is_namespaced() -> None:
    """The database is shared with other services; a generic channel name would
    make their notifications ours."""
    assert "edenn" in CHANNEL and "agentic_audio" in CHANNEL


def test_a_listener_that_can_never_connect_says_so_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bug this file caught: the listener failed on every attempt and the
    retry loop logged it at DEBUG, so it retried silently forever. The only
    symptom was collaborators on other replicas quietly not seeing turns, which
    nobody traces back to a debug line.
    """
    import logging

    def broken():
        raise RuntimeError("no database")

    async def scenario() -> None:
        fan = CrossReplicaFanout(client_factory=broken, deliver=None)
        await fan.start()
        await asyncio.sleep(0.3)
        await fan.stop()

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())

    warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "cross-replica" in r.getMessage()
    ]
    assert len(warnings) == 1, f"expected exactly one loud warning, got {len(warnings)}"
