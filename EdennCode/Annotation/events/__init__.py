"""Typed annotation event dataclasses for each pipeline stage."""
from EdennCode.Annotation.events.request_context_event import RequestContextEvent
from EdennCode.Annotation.events.video_feature_event import VideoFeatureEvent
from EdennCode.Annotation.events.scene_understanding_event import (
    SceneAnnotation,
    SceneUnderstandingEvent,
)
from EdennCode.Annotation.events.video_understanding_event import VideoUnderstandingEvent
from EdennCode.Annotation.events.music_prompt_event import MusicPromptEvent
from EdennCode.Annotation.events.music_generation_event import MusicGenerationEvent
from EdennCode.Annotation.events.remix_completion_event import RemixCompletionEvent

__all__ = [
    "RequestContextEvent",
    "VideoFeatureEvent",
    "SceneAnnotation",
    "SceneUnderstandingEvent",
    "VideoUnderstandingEvent",
    "MusicPromptEvent",
    "MusicGenerationEvent",
    "RemixCompletionEvent",
]
