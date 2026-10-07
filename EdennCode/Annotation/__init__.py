"""
EdennCode Annotation System
===========================

Non-blocking annotation layer for the VideoMusicWorkflow pipeline.  Captures
structured events at each stage for feature extraction and downstream LLM-based
music recommendation.

Quick start
-----------
>>> from EdennCode.Annotation import AnnotationDispatcher, InMemoryAnnotationStore
>>> store = InMemoryAnnotationStore()
>>> dispatcher = AnnotationDispatcher(stores=[store])
# Pass dispatcher to VideoMusicWorkflowE2EInput; the pipeline does the rest.
"""
from EdennCode.Annotation.core.annotation_event import AnnotationEvent
from EdennCode.Annotation.core.annotation_store import AnnotationStore
from EdennCode.Annotation.core.annotation_dispatcher import AnnotationDispatcher
from EdennCode.Annotation.store.in_memory_store import InMemoryAnnotationStore

__all__ = [
    "AnnotationEvent",
    "AnnotationStore",
    "AnnotationDispatcher",
    "InMemoryAnnotationStore",
]
