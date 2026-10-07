"""
Base annotation event model.

Every concrete annotation event in this system inherits from :class:`AnnotationEvent`.
All events carry a shared set of fields that allow cross-event correlation and
schema migration without breaking downstream consumers.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass(kw_only=True)
class AnnotationEvent:
    """
    Base class for all pipeline annotation events.

    An annotation event is an immutable snapshot of structured data captured at a
    specific point in the generation pipeline.  It is designed to be emitted
    fire-and-forget via :class:`~EdennCode.Annotation.core.annotation_dispatcher.AnnotationDispatcher`
    and written to one or more :class:`~EdennCode.Annotation.core.annotation_store.AnnotationStore`
    implementations without blocking the main execution loop.

    Attributes
    ----------
    event_type:
        Stable string identifier for the event category (e.g. ``"music_generation"``).
        Consumers query the store by this value; never change it after the first
        production write.
    job_id:
        Opaque identifier shared by all events belonging to the same pipeline run.
        Must be set by the orchestrator before emitting any events for the run.
    schema_version:
        Monotonically increasing string (``"v1"``, ``"v2"``, …) that lets consumers
        detect enrichment-prompt migrations or field additions without reading diffs.
        Bump when any field's semantics change; do **not** bump for purely additive
        optional fields unless they change how existing fields are interpreted.
    timestamp_utc:
        Unix epoch (float, seconds) captured at event construction time on the host
        machine.  Suitable for relative latency analysis; treat as wall-clock, not
        as a monotonic counter.
    event_id:
        Globally unique identifier for this specific event instance.  Used for
        idempotency in store writes.
    metadata:
        Arbitrary key-value bag for debugging context that does not warrant a typed
        field.  Keep sparse — prefer typed subclass fields for anything queried.
    """

    event_type: str
    job_id: str
    schema_version: str = "v1"
    timestamp_utc: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """
        Return a plain-dict representation of the event suitable for serialisation.

        Subclasses should override this if they contain non-primitive fields
        (e.g. ``Path`` objects) that require custom serialisation.
        """
        import dataclasses
        return dataclasses.asdict(self)
