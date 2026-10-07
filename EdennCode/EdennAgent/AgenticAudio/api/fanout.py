"""Getting an event to every viewer, including the ones on another replica.

A turn's events are delivered to the sockets held by THIS process. With one
replica that is everyone; with two it is roughly half, and the other half sit
watching a session that appears frozen until their next poll. It is the last
thing pinning the deployment to a single replica.

Postgres `LISTEN`/`NOTIFY` carries the cross-replica half. The studio already
requires Postgres for everything else that matters, so this adds no new
infrastructure — no broker to run, no second thing to be down at 3am. What it
buys is exactly what is missing: a replica publishes what it just sent locally,
and every other replica delivers it to its own sockets.

The tradeoffs are real and worth stating rather than discovering:

* **NOTIFY has an 8000-byte payload ceiling.** A session snapshot is far bigger
  than that. So the notification carries the session id and the event TYPE, not
  the event: a peer replica is told "something happened here" and reads the
  session itself. That is slightly more work per event and immune to the size
  limit, which a payload-carrying design is not.
* **It is fire-and-forget.** A replica that is not listening at the moment of
  the NOTIFY never learns. That is acceptable because it is not the durability
  story — the snapshot in the database is — and a client that misses a live
  event still reconciles on its next poll or reconnect.
* **It degrades to exactly today's behaviour.** With no listener configured,
  fan-out is local-only, which is correct for one replica.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# One channel for the whole studio. Postgres channel names are identifiers, so
# this cannot be per-session without running into the identifier length limit
# and a LISTEN per session; the session id rides in the payload instead.
CHANNEL = "edenn_agentic_audio_events"

# Well under the 8000-byte NOTIFY ceiling. If a payload ever approaches it, the
# design is wrong rather than the limit being tight.
MAX_PAYLOAD = 4000


class CrossReplicaFanout:
    """Publishes local events to other replicas and applies theirs.

    ``deliver`` is called with a session id when another replica reports
    activity; the router uses it to push the current snapshot to its own
    sockets.
    """

    def __init__(
        self,
        *,
        client_factory: Optional[Callable[[], Any]] = None,
        deliver: Optional[Callable[[str, str], Awaitable[None]]] = None,
    ) -> None:
        self._client_factory = client_factory
        self._deliver = deliver
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self._warned = False
        self.published = 0
        self.received = 0

    @property
    def enabled(self) -> bool:
        return self._client_factory is not None

    # ---- publishing ------------------------------------------------------

    async def publish(self, session_id: str, event_type: str) -> None:
        """Tell other replicas that this session moved. Never raises.

        Fan-out is a live convenience layered on top of a durable snapshot; a
        failure here must not fail the turn that produced the event.
        """

        if not self.enabled:
            return
        payload = json.dumps({"s": session_id, "t": event_type})
        if len(payload) > MAX_PAYLOAD:
            logger.warning("fanout: payload too large for %s, dropping", session_id)
            return
        try:
            await asyncio.to_thread(self._notify, payload)
            self.published += 1
        except Exception:  # noqa: BLE001 - never break a turn over fan-out
            logger.debug("fanout: publish failed", exc_info=True)

    def _notify(self, payload: str) -> None:
        client = self._client_factory()
        try:
            # The channel is a module constant; the PAYLOAD carries a
            # caller-influenced session id and is parameterised, because
            # concatenating it into the statement would be an injection point.
            client.run_sql("SELECT pg_notify(%s, %s)", params=[CHANNEL, payload])
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    # ---- listening -------------------------------------------------------

    async def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._listen_forever())

    async def stop(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _listen_forever(self) -> None:
        """Reconnecting listener. A dropped connection is normal, not an error."""

        while not self._stopping:
            try:
                await self._listen_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - reconnect rather than give up
                # The FIRST failure is loud. A listener that can never connect
                # retries silently forever otherwise, and the symptom is only
                # that collaborators on other replicas stop seeing turns — which
                # nobody traces back to a debug line.
                if not self._warned:
                    self._warned = True
                    logger.warning(
                        "fanout: listener could not start; cross-replica events "
                        "are not being delivered", exc_info=True,
                    )
                else:
                    logger.debug("fanout: listener dropped, retrying", exc_info=True)
            if not self._stopping:
                await asyncio.sleep(2.0)

    async def _listen_once(self) -> None:
        import select

        client = await asyncio.to_thread(self._client_factory)
        try:
            connection = await asyncio.to_thread(client.connect)
            # connect() leaves a transaction open (the pgvector registration
            # runs a query), and psycopg2 refuses to change the session mode
            # inside one. End it first, or LISTEN never even gets set up.
            await asyncio.to_thread(connection.rollback)
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(f"LISTEN {CHANNEL}")
            while not self._stopping:
                ready = await asyncio.to_thread(select.select, [connection], [], [], 1.0)
                if not ready[0]:
                    continue
                await asyncio.to_thread(connection.poll)
                while connection.notifies:
                    note = connection.notifies.pop(0)
                    await self._apply(note.payload)
        finally:
            try:
                await asyncio.to_thread(client.close)
            except Exception:  # noqa: BLE001
                pass

    async def _apply(self, payload: str) -> None:
        try:
            body = json.loads(payload)
            session_id = str(body.get("s") or "")
            event_type = str(body.get("t") or "")
        except Exception:  # noqa: BLE001 - a malformed notification is not fatal
            return
        if not session_id or self._deliver is None:
            return
        self.received += 1
        try:
            await self._deliver(session_id, event_type)
        except Exception:  # noqa: BLE001 - one bad delivery must not stop the listener
            logger.debug("fanout: delivery failed for %s", session_id, exc_info=True)


__all__ = ["CHANNEL", "MAX_PAYLOAD", "CrossReplicaFanout"]
