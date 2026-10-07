"""One turn at a time per session, across every replica.

A turn reads the session, reasons, calls tools, and writes the session back many
times over. Two turns on one session interleaving is not a race to be tidied up
afterwards — it is two agents editing the same document with different ideas of
what is in it, and the loser's work disappears.

The existing lock is an ``asyncio.Lock`` in a dictionary, which serialises turns
inside ONE process. That is why the deployment is pinned to a single replica: a
second replica has its own dictionary and knows nothing about the first one's
turns. This lock lives in the database instead, so every replica contends on the
same thing.

Two properties matter and both come from Postgres rather than from us:

* A **session-scoped advisory lock** is released when the connection closes, so
  a replica that is killed mid-turn does not leave the session locked forever —
  which is the failure mode that makes people afraid of database locks.
* ``pg_try_advisory_lock`` does not wait. A turn that cannot get the lock is
  told the session is busy, rather than piling up behind one that may be running
  a two-minute render.

It degrades deliberately: with no database (the standalone runs in memory), it
falls back to the in-process lock, which is exactly correct for one replica.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Optional

logger = logging.getLogger(__name__)


class SessionBusy(RuntimeError):
    """Another turn is already running for this session.

    Not an error in the session — a statement about timing. The caller should
    tell the user their previous message is still being worked on, not retry
    silently and definitely not start a second turn.
    """


def lock_key(session_id: str) -> int:
    """A stable 64-bit key for this session.

    Derived from the id rather than from a counter, so every replica computes
    the same number without coordinating, and namespaced so it cannot collide
    with another feature's advisory lock in the same database.
    """

    digest = hashlib.sha256(f"edenn.agentic_audio.turn:{session_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class TurnLock:
    """Serialises turns per session, in the database when there is one."""

    def __init__(
        self,
        *,
        client_factory: Optional[Callable[[], Any]] = None,
        on_enter: Optional[Callable[[], None]] = None,
        on_exit: Optional[Callable[[], None]] = None,
    ) -> None:
        self._client_factory = client_factory
        self._local: dict[str, asyncio.Lock] = {}
        # Every turn passes through here, which makes it the one honest place to
        # count what is in flight — a shutdown that does not know cannot wait.
        self._on_enter = on_enter
        self._on_exit = on_exit

    @property
    def distributed(self) -> bool:
        return self._client_factory is not None

    @asynccontextmanager
    async def hold(self, session_id: str) -> AsyncIterator[None]:
        """Hold the turn lock, or raise :class:`SessionBusy` immediately."""

        if self._on_enter is not None:
            self._on_enter()
        try:
            async with self._hold_inner(session_id):
                yield
        finally:
            if self._on_exit is not None:
                self._on_exit()

    @asynccontextmanager
    async def _hold_inner(self, session_id: str) -> AsyncIterator[None]:
        if self._client_factory is None:
            # One replica: an in-process lock is not a compromise, it is the
            # right tool. Still non-blocking, so the caller's experience is the
            # same in both modes.
            lock = self._local.setdefault(session_id, asyncio.Lock())
            if lock.locked():
                raise SessionBusy(session_id)
            async with lock:
                yield
            return

        key = lock_key(session_id)
        client = None
        acquired = False
        try:
            client = await asyncio.to_thread(self._client_factory)
            acquired = await asyncio.to_thread(self._try_lock, client, key)
            if not acquired:
                raise SessionBusy(session_id)
            yield
        finally:
            if client is not None:
                if acquired:
                    try:
                        await asyncio.to_thread(self._unlock, client, key)
                    except Exception:  # noqa: BLE001 - closing releases it anyway
                        logger.debug("advisory unlock failed for %s", session_id,
                                     exc_info=True)
                try:
                    await asyncio.to_thread(client.close)
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def _try_lock(client: Any, key: int) -> bool:
        rows = client.run_sql("SELECT pg_try_advisory_lock(%s) AS got", params=[key])
        return bool(rows and rows[0].get("got"))

    @staticmethod
    def _unlock(client: Any, key: int) -> None:
        client.run_sql("SELECT pg_advisory_unlock(%s)", params=[key])


__all__ = ["SessionBusy", "TurnLock", "lock_key"]
