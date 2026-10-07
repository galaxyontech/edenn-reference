"""
Abstract store interface for annotation events.

All concrete stores — in-memory, PostgreSQL, cloud — must implement
:class:`AnnotationStore`.  The interface is intentionally minimal so that
the contract can be satisfied by simple test doubles as well as production backends.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from EdennCode.Annotation.core.annotation_event import AnnotationEvent


class AnnotationStore(ABC):
    """
    Abstract base class for annotation event persistence.

    Stores are always written to asynchronously via the dispatcher; all methods
    are therefore coroutines.  Implementations MUST be safe to call concurrently —
    use an asyncio lock or a thread-safe data structure as appropriate.

    Methods
    -------
    write(event):
        Persist a single event.  Must not raise; log and swallow on failure so that
        a broken store never disrupts the generation pipeline.
    query(event_type, limit):
        Return the most recent *limit* events of the given type, newest-last.
    get_by_job(job_id):
        Return all events associated with a pipeline run, in insertion order.
    """

    @abstractmethod
    async def write(self, event: AnnotationEvent) -> None:
        """Persist *event* to the backing store."""
        ...

    @abstractmethod
    async def query(self, event_type: str, limit: int = 100) -> List[AnnotationEvent]:
        """
        Return the most recent *limit* events of *event_type*, in insertion order.

        Parameters
        ----------
        event_type:
            The ``event_type`` field value to filter on (e.g. ``"music_generation"``).
        limit:
            Maximum number of records to return.  Defaults to 100.

        Returns
        -------
        List[AnnotationEvent]
            Matching events, oldest-first, capped at *limit*.
        """
        ...

    @abstractmethod
    async def get_by_job(self, job_id: str) -> List[AnnotationEvent]:
        """
        Return all events for *job_id* in insertion order.

        Parameters
        ----------
        job_id:
            The pipeline-run identifier set at the start of each
            :class:`~EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.VideoMusicWorkflowE2E`
            invocation.

        Returns
        -------
        List[AnnotationEvent]
            All events for the given job, oldest-first.
        """
        ...

    @abstractmethod
    async def get_by_job_and_type(
        self,
        job_id: str,
        event_type: str,
    ) -> List[AnnotationEvent]:
        """
        Return events matching both *job_id* and *event_type*.

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
        ...

    @abstractmethod
    async def all_events(self) -> List[AnnotationEvent]:
        """
        Return every stored event in insertion order.

        Used by :class:`~EdennCode.Annotation.enrichment.enrichment_processor.EnrichmentProcessor`
        to scan for unenriched jobs.

        Returns
        -------
        List[AnnotationEvent]
            Full event log.
        """
        ...
