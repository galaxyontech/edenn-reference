"""Core annotation primitives: base event, store interface, and dispatcher."""
from EdennCode.Annotation.core.annotation_event import AnnotationEvent
from EdennCode.Annotation.core.annotation_store import AnnotationStore
from EdennCode.Annotation.core.annotation_dispatcher import AnnotationDispatcher

__all__ = [
    "AnnotationEvent",
    "AnnotationStore",
    "AnnotationDispatcher",
]
