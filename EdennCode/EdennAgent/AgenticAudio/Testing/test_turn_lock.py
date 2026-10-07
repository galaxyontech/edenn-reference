"""One turn at a time per session — including across replicas.

The lock was an ``asyncio.Lock`` in a dictionary, which serialises turns inside
ONE process. That is why the deployment is pinned to a single replica: a second
replica has its own dictionary and knows nothing about the first one's turns.

The distributed half needs a real Postgres, because advisory locks and their
release-on-disconnect behaviour are the whole subject. The in-process half needs
nothing and runs everywhere.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any
from urllib.parse import urlparse

import pytest

from EdennCode.EdennAgent.AgenticAudio.agent.turn_lock import (
    SessionBusy,
    TurnLock,
    lock_key,
)

DSN = os.getenv("EDENN_TEST_PG_DSN", "").strip()


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
# the key                                                                     #
# ---------------------------------------------------------------------------#


def test_the_same_session_always_maps_to_the_same_key() -> None:
    """Every replica has to compute the same number without coordinating."""
    assert lock_key("sess_a") == lock_key("sess_a")


def test_different_sessions_do_not_share_a_key() -> None:
    assert lock_key("sess_a") != lock_key("sess_b")


def test_the_key_fits_in_a_postgres_bigint() -> None:
    key = lock_key("sess_" + "x" * 200)
    assert -(2 ** 63) <= key < 2 ** 63


# ---------------------------------------------------------------------------#
# in-process (one replica)                                                    #
# ---------------------------------------------------------------------------#


def test_a_second_turn_on_the_same_session_is_refused() -> None:
    async def scenario() -> None:
        lock = TurnLock()
        async with lock.hold("sess_1"):
            with pytest.raises(SessionBusy):
                async with lock.hold("sess_1"):
                    pass

    asyncio.run(scenario())


def test_a_turn_on_another_session_is_unaffected() -> None:
    async def scenario() -> None:
        lock = TurnLock()
        async with lock.hold("sess_1"):
            async with lock.hold("sess_2"):
                pass

    asyncio.run(scenario())


def test_the_lock_is_released_when_the_turn_ends() -> None:
    async def scenario() -> None:
        lock = TurnLock()
        async with lock.hold("sess_1"):
            pass
        async with lock.hold("sess_1"):
            pass

    asyncio.run(scenario())


def test_the_lock_is_released_when_a_turn_raises() -> None:
    """A turn that fails must not lock the session out of every future turn."""

    async def scenario() -> None:
        lock = TurnLock()
        with pytest.raises(RuntimeError):
            async with lock.hold("sess_1"):
                raise RuntimeError("the tool blew up")
        async with lock.hold("sess_1"):
            pass

    asyncio.run(scenario())


def test_refusing_is_immediate_rather_than_queueing() -> None:
    """A turn that cannot get the lock is told the session is busy, rather than
    piling up behind one that may be running a two-minute render."""

    async def scenario() -> None:
        lock = TurnLock()
        async with lock.hold("sess_1"):
            await asyncio.wait_for(_expect_busy(lock, "sess_1"), timeout=1.0)

    async def _expect_busy(lock: TurnLock, sid: str) -> None:
        with pytest.raises(SessionBusy):
            async with lock.hold(sid):
                pass

    asyncio.run(scenario())


# ---------------------------------------------------------------------------#
# across replicas                                                             #
# ---------------------------------------------------------------------------#

pg = pytest.mark.skipif(
    not DSN, reason="set EDENN_TEST_PG_DSN to a throwaway Postgres to run these"
)


@pg
def test_two_replicas_cannot_hold_the_same_session() -> None:
    """The whole point. Two TurnLock instances are two processes as far as the
    database is concerned — each opens its own connection."""

    async def scenario() -> None:
        sid = f"sess_{uuid.uuid4().hex[:10]}"
        replica_a = TurnLock(client_factory=_factory())
        replica_b = TurnLock(client_factory=_factory())
        async with replica_a.hold(sid):
            with pytest.raises(SessionBusy):
                async with replica_b.hold(sid):
                    pass

    asyncio.run(scenario())


@pg
def test_two_replicas_can_hold_different_sessions() -> None:
    async def scenario() -> None:
        a, b = TurnLock(client_factory=_factory()), TurnLock(client_factory=_factory())
        async with a.hold(f"sess_{uuid.uuid4().hex[:8]}"):
            async with b.hold(f"sess_{uuid.uuid4().hex[:8]}"):
                pass

    asyncio.run(scenario())


@pg
def test_the_lock_is_handed_back_between_replicas() -> None:
    async def scenario() -> None:
        sid = f"sess_{uuid.uuid4().hex[:10]}"
        a, b = TurnLock(client_factory=_factory()), TurnLock(client_factory=_factory())
        async with a.hold(sid):
            pass
        async with b.hold(sid):  # must not raise
            pass

    asyncio.run(scenario())


@pg
def test_a_replica_that_dies_mid_turn_does_not_lock_the_session_forever() -> None:
    """The failure that makes people afraid of database locks.

    A session-scoped advisory lock is released when the connection closes, so
    losing a replica returns the session rather than stranding it.
    """
    from EdennCode.Deployment.postgres_wrapper import PostgresClient

    sid = f"sess_{uuid.uuid4().hex[:10]}"
    key = lock_key(sid)

    # A "replica" takes the lock and then vanishes without unlocking.
    doomed: PostgresClient = _factory()()
    got = doomed.run_sql("SELECT pg_try_advisory_lock(%s) AS got", params=[key])
    assert got[0]["got"] is True
    doomed.close()  # the process died; only the connection went away

    async def scenario() -> None:
        survivor = TurnLock(client_factory=_factory())
        async with survivor.hold(sid):  # must not raise
            pass

    asyncio.run(scenario())


@pg
def test_the_lock_is_released_across_replicas_when_a_turn_raises() -> None:
    async def scenario() -> None:
        sid = f"sess_{uuid.uuid4().hex[:10]}"
        a, b = TurnLock(client_factory=_factory()), TurnLock(client_factory=_factory())
        with pytest.raises(RuntimeError):
            async with a.hold(sid):
                raise RuntimeError("boom")
        async with b.hold(sid):
            pass

    asyncio.run(scenario())


@pg
def test_it_reports_which_mode_it_is_in() -> None:
    """Whether turns are serialised across replicas is not something to guess at
    from behaviour in an incident."""
    assert TurnLock(client_factory=_factory()).distributed is True
    assert TurnLock().distributed is False


# ---------------------------------------------------------------------------#
# what the caller is told                                                     #
# ---------------------------------------------------------------------------#


def test_a_busy_session_is_a_409_not_a_500(tmp_path, monkeypatch) -> None:
    """The lock changed from waiting to refusing, which is right for a turn that
    may be a two-minute render — but only if the caller can tell "try again in a
    moment" from "this broke". A 500 says the wrong thing, and invites a retry
    that reads as a new instruction.

    Driven by making the turn raise, rather than by racing a real one: the race
    is what the lock is FOR, and a test that has to win it to pass would be the
    flakiest thing in the suite.
    """
    from EdennCode.EdennAgent.AgenticAudio.agent.agent import AgenticAudioAgent
    from EdennCode.EdennAgent.AgenticAudio.Testing.test_agentic_audio_api import (
        _AUDIO,
        _bootstrap_decisions,
        _client_with_decisions,
        _create_session,
    )

    client, _, _, _, source = _client_with_decisions(tmp_path, _bootstrap_decisions() * 2)
    sid = _create_session(client, source)["session_id"]

    async def busy(*_args, **_kwargs):
        raise SessionBusy(sid)

    monkeypatch.setattr(AgenticAudioAgent, "handle_user_message", busy)
    response = client.post(f"{_AUDIO}/sessions/{sid}/messages", json={"content": "hi"})

    assert response.status_code == 409, response.text
    assert "moment" in response.json()["detail"]


def test_a_busy_session_does_not_read_as_a_missing_one(tmp_path, monkeypatch) -> None:
    """404 would tell a client the session is gone and stop it retrying at all."""
    from EdennCode.EdennAgent.AgenticAudio.agent.agent import AgenticAudioAgent
    from EdennCode.EdennAgent.AgenticAudio.Testing.test_agentic_audio_api import (
        _AUDIO,
        _bootstrap_decisions,
        _client_with_decisions,
        _create_session,
    )

    client, _, _, _, source = _client_with_decisions(tmp_path, _bootstrap_decisions() * 2)
    sid = _create_session(client, source)["session_id"]

    async def busy(*_args, **_kwargs):
        raise SessionBusy(sid)

    monkeypatch.setattr(AgenticAudioAgent, "handle_choice", busy)
    response = client.post(
        f"{_AUDIO}/sessions/{sid}/choices",
        json={"choice_type": "clarification", "target_id": "music_only"},
    )
    assert response.status_code == 409, response.text
