"""
Annotation event emitted after Stage 3 (VideoUnderstanding).

Captures the holistic video-level semantic summary produced by the LLM.
The ``overall_mood`` and ``core_message`` fields feed directly into the
music recommendation rubric's *Intent Match* and *Contextual Fit* dimensions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from EdennCode.Annotation.core.annotation_event import AnnotationEvent


@dataclass(kw_only=True)
class VideoUnderstandingEvent(AnnotationEvent):
    """
    Video-level semantic summary produced by the VideoUnderstandingStage.

    This event consolidates the LLM's high-level interpretation of the entire
    video into a single record.  It is emitted once per pipeline run and
    provides the clearest signal for downstream recommendation features such as
    mood-based retrieval and genre tagging.

    Attributes
    ----------
    event_type:
        Always ``"video_understanding"``.  Do not change.
    video_title:
        Auto-generated title for the video produced by the LLM.
    video_description:
        One-to-two sentence description of the video's content and purpose.
    overall_mood:
        Single-phrase mood label for the whole video (e.g. ``"uplifting"``,
        ``"melancholic"``).  Maps to ``mood_tags`` in the recommendation schema.
    core_message:
        The primary narrative or emotional intent the video conveys.  Useful as
        a textual feature for embedding-based retrieval.
    has_explicit_call_to_action:
        Whether the LLM detected a direct call-to-action (common in
        advertisement-category videos).
    stage_latency_s:
        Wall-clock seconds the video understanding stage took.
    token_usage:
        LLM token counts for this stage, keyed by
        ``"prompt_tokens"``, ``"completion_tokens"``, ``"total_tokens"``.
    raw_descriptions:
        Full JSON-serialisable dict returned by the LLM for this stage.  Stored
        for audit purposes; consumers should prefer the typed fields above.
    """

    event_type: str = "video_understanding"
    video_title: str = ""
    video_description: str = ""
    overall_mood: str = ""
    core_message: str = ""
    has_explicit_call_to_action: bool = False
    stage_latency_s: float = 0.0
    token_usage: Dict[str, int] = field(default_factory=dict)
    raw_descriptions: Optional[Dict[str, Any]] = None
