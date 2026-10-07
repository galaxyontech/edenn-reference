"""
Non-blocking annotation event dispatcher.

The dispatcher is the single entry-point for emitting annotation events from
pipeline stages.  It intentionally decouples the main execution loop from the
annotation layer: callers fire-and-forget with :meth:`AnnotationDispatcher.emit`,
which schedules background tasks without blocking or raising.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, List, Optional, Sequence

from EdennCode.Annotation.core.annotation_event import AnnotationEvent
from EdennCode.Annotation.core.annotation_store import AnnotationStore

logger = logging.getLogger(__name__)


class AnnotationDispatcher:
    """
    Routes annotation events to one or more :class:`~EdennCode.Annotation.core.annotation_store.AnnotationStore`
    implementations without blocking the calling coroutine.

    Design constraints
    ------------------
    * :meth:`emit` must never be awaited in pipeline code.  It is synchronous and
      schedules a background ``asyncio`` task.
    * If no event loop is running (e.g. inside a synchronous unit test), the emit
      call is silently dropped with a debug-level log.  Pipeline behaviour is
      identical with or without a dispatcher attached.
    * Store write failures are caught and logged at WARNING level; they never
      propagate to the caller.

    Parameters
    ----------
    stores:
        One or more :class:`AnnotationStore` implementations to write to.
        Events are fanned-out to all stores in order; one failing store does not
        prevent the others from receiving the event.

    Examples
    --------
    >>> dispatcher = AnnotationDispatcher(stores=[InMemoryAnnotationStore()])
    >>> dispatcher.emit(MusicGenerationEvent(job_id="run-1", ...))
    """

    def __init__(self, stores: Sequence[AnnotationStore]) -> None:
        self._stores: List[AnnotationStore] = list(stores)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def emit(self, event: AnnotationEvent) -> None:
        """
        Schedule *event* to be written to all configured stores.

        This method is **synchronous** and returns immediately.  Writes are
        performed as ``asyncio`` background tasks on the currently running event
        loop.  If no event loop is running the event is silently discarded.

        Parameters
        ----------
        event:
            The fully constructed :class:`AnnotationEvent` to emit.  The caller
            retains ownership; the dispatcher does not mutate the event.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "AnnotationDispatcher.emit called with no running event loop; "
                "event %s (%s) discarded.",
                event.event_id,
                event.event_type,
            )
            return

        loop.create_task(
            self._fan_out(event),
            name=f"annotation-{event.event_type}-{event.event_id}",
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fan_out(self, event: AnnotationEvent) -> None:
        """Write *event* to every configured store, catching per-store errors."""
        for store in self._stores:
            try:
                await store.write(event)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "AnnotationStore %s failed to write event %s (%s): %s",
                    type(store).__name__,
                    event.event_id,
                    event.event_type,
                    exc,
                )


def safe_emit_annotation(
    dispatcher: Optional[AnnotationDispatcher],
    event_factory: Callable[[], AnnotationEvent],
) -> None:
    """
    Best-effort wrapper for pipeline annotation emission.

    ``AnnotationDispatcher.emit`` already isolates async store-write failures,
    but callers still construct event objects before calling it.  This helper
    wraps both event construction and synchronous dispatch so annotation bugs
    never break the main generation path.
    """
    if dispatcher is None:
        return

    try:
        dispatcher.emit(event_factory())
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Annotation emission failed before background dispatch; generation will continue: %s",
            exc,
            exc_info=True,
        )
