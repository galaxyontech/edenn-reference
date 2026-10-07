"""
Thread-safe in-memory annotation store for local experimentation.

This store is intentionally ephemeral: data is lost on process restart.
It is designed for:
* Development and integration testing without external dependencies.
* Early-stage feature extraction experiments described in the recommendation
  design document Phase 1 (§9 Rollout Plan).

Replace or supplement with a persistent backend (PostgreSQL + pgvector,
or a managed vector store) before any production use.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Dict, List, Optional

from EdennCode.Annotation.core.annotation_event import AnnotationEvent
from EdennCode.Annotation.core.annotation_store import AnnotationStore

logger = logging.getLogger(__name__)


class InMemoryAnnotationStore(AnnotationStore):
    """
    Asyncio-safe in-memory store backed by plain Python lists.

    All mutation is protected by a single :class:`asyncio.Lock` so concurrent
    background writes from the dispatcher do not race.  Read methods acquire the
    same lock, ensuring consistent snapshots.

    The store maintains three parallel indices for O(1) lookup by the most
    common access patterns:

    * ``_all`` — insertion-ordered list of every event (full scan, stats).
    * ``_by_event_type`` — dict mapping ``event_type`` → list of events.
    * ``_by_job_id`` — dict mapping ``job_id`` → list of events.

    Notes
    -----
    * This store does **not** enforce any capacity limit.  For long-running
      experiment sessions, call :meth:`clear` periodically or restart the
      process.
    * The ``asyncio.Lock`` is not re-entrant; do not call public methods from
      within other public methods of this class.

    Examples
    --------
    >>> store = InMemoryAnnotationStore()
    >>> await store.write(MusicGenerationEvent(job_id="run-1", model_spec="edenn_basic"))
    >>> events = await store.query("music_generation", limit=10)
    """

    def __init__(self) -> None:
        self._lock: asyncio.Lock = asyncio.Lock()
        self._all: List[AnnotationEvent] = []
        self._by_event_type: Dict[str, List[AnnotationEvent]] = defaultdict(list)
        self._by_job_id: Dict[str, List[AnnotationEvent]] = defaultdict(list)

    # ------------------------------------------------------------------
    # AnnotationStore interface
    # ------------------------------------------------------------------

    async def write(self, event: AnnotationEvent) -> None:
        """
        Append *event* to the store.

        This method is always called from the dispatcher's background task and
        must not raise.  Errors are caught at the dispatcher level.

        Parameters
        ----------
        event:
            The :class:`~EdennCode.Annotation.core.annotation_event.AnnotationEvent`
            to persist.
        """
        async with self._lock:
            self._all.append(event)
            self._by_event_type[event.event_type].append(event)
            self._by_job_id[event.job_id].append(event)
        logger.debug(
            "InMemoryAnnotationStore: stored %s event_id=%s job_id=%s",
            event.event_type,
            event.event_id,
            event.job_id,
        )

    async def query(
        self,
        event_type: str,
        limit: int = 100,
    ) -> List[AnnotationEvent]:
        """
        Return the most recent *limit* events of *event_type*, oldest-first.

        Parameters
        ----------
        event_type:
            The ``event_type`` string to filter on.
        limit:
            Maximum number of records to return.  Defaults to 100.

        Returns
        -------
        List[AnnotationEvent]
            Matching events in insertion order, capped at *limit*.
        """
        async with self._lock:
            bucket = self._by_event_type.get(event_type, [])
            return list(bucket[-limit:])

    async def get_by_job(self, job_id: str) -> List[AnnotationEvent]:
        """
        Return all events for *job_id* in insertion order.

        Parameters
        ----------
        job_id:
            The pipeline-run identifier.

        Returns
        -------
        List[AnnotationEvent]
            All events for the job, oldest-first.  Empty list if no events are
            found for the given *job_id*.
        """
        async with self._lock:
            return list(self._by_job_id.get(job_id, []))

    # ------------------------------------------------------------------
    # Convenience / diagnostics
    # ------------------------------------------------------------------

    async def all_events(self) -> List[AnnotationEvent]:
        """
        Return every event stored, in insertion order.

        Returns
        -------
        List[AnnotationEvent]
            Full event log.  Returns a copy; mutations do not affect the store.
        """
        async with self._lock:
            return list(self._all)

    async def get_by_job_and_type(
        self,
        job_id: str,
        event_type: str,
    ) -> List[AnnotationEvent]:
        """
        Return events matching both *job_id* and *event_type*.

        Useful for retrieving a specific stage's annotation for a known run
        without scanning unrelated event types.

        Parameters
        ----------
        job_id:
            Pipeline-run identifier.
        event_type:
            The ``event_type`` string to filter on.

        Returns
        -------
        List[AnnotationEvent]
            Matching events in insertion order.
        """
        async with self._lock:
            return [
                e for e in self._by_job_id.get(job_id, [])
                if e.event_type == event_type
            ]

    async def clear(self) -> None:
        """
        Remove all stored events from every index.

        Useful between test cases or experiment runs to avoid state leakage.
        """
        async with self._lock:
            self._all.clear()
            self._by_event_type.clear()
            self._by_job_id.clear()
        logger.debug("InMemoryAnnotationStore: cleared all events.")

    def stats(self) -> Dict[str, object]:
        """
        Return a snapshot of event counts without acquiring the lock.

        This method is intentionally non-async and does not acquire the lock so
        it can be called from synchronous diagnostics code.  The counts may be
        slightly stale if concurrent writes are in-flight.

        Returns
        -------
        dict
            ``{"total_events": int, "by_type": {event_type: count}}``.
        """
        return {
            "total_events": len(self._all),
            "by_type": {k: len(v) for k, v in self._by_event_type.items()},
        }

    def __repr__(self) -> str:
        return (
            f"InMemoryAnnotationStore("
            f"total_events={len(self._all)}, "
            f"event_types={list(self._by_event_type.keys())})"
        )
