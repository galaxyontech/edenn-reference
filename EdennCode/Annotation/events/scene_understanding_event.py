"""
Annotation event emitted after Stage 2 (SceneSegmentation).

Captures the full set of LLM-generated scene descriptions for a pipeline run.
This is the richest semantic signal available before music generation and maps
directly to the per-track ``mood_tags`` and ``activity_tags`` in the
recommendation track schema.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from EdennCode.Annotation.core.annotation_event import AnnotationEvent


@dataclass
class SceneAnnotation:
    """
    Structured representation of a single scene from the segmentation stage.

    Attributes
    ----------
    scene_index:
        Zero-based index of the scene within the video.
    start_s:
        Scene start timestamp in seconds.
    end_s:
        Scene end timestamp in seconds.
    visual_summary:
        LLM-generated description of the dominant visual content.
    key_actions:
        LLM-generated description of motion or actions in the scene.
    mood:
        LLM-inferred atmospheric mood (e.g. ``"tense"``, ``"joyful"``).
    """

    scene_index: int
    start_s: float
    end_s: float
    visual_summary: str
    key_actions: str
    mood: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scene_index": self.scene_index,
            "start_s": self.start_s,
            "end_s": self.end_s,
            "visual_summary": self.visual_summary,
            "key_actions": self.key_actions,
            "mood": self.mood,
        }


@dataclass(kw_only=True)
class SceneUnderstandingEvent(AnnotationEvent):
    """
    Aggregated scene understanding output for a complete pipeline run.

    Emitted once per pipeline run after all scenes have been segmented and
    analysed by the multimodal LLM.  The scenes list is stored in a single
    event (rather than one event per scene) to keep the store query pattern
    simple: one ``get_by_job`` call returns the full scene context.

    Attributes
    ----------
    event_type:
        Always ``"scene_understanding"``.  Do not change.
    scene_count:
        Total number of scenes detected.  Redundant with ``len(scenes)`` but
        useful as a quick filter without deserialising the scene list.
    scenes:
        Ordered list of :class:`SceneAnnotation` objects, one per detected scene.
    dominant_moods:
        De-duplicated list of mood strings across all scenes, derived at event
        construction time.  Provides a cheap per-run mood fingerprint without
        re-scanning the scene list.
    llm_prompt_version:
        Identifier for the enrichment prompt version used to generate the scene
        descriptions.  Bump when the scene-analysis prompt changes so affected
        records can be re-enriched.
    stage_latency_s:
        Wall-clock seconds the scene segmentation stage took.
    token_usage:
        LLM token counts for this stage, keyed by
        ``"prompt_tokens"``, ``"completion_tokens"``, ``"total_tokens"``.
    """

    event_type: str = "scene_understanding"
    scene_count: int = 0
    scenes: List[SceneAnnotation] = field(default_factory=list)
    dominant_moods: List[str] = field(default_factory=list)
    llm_prompt_version: str = "v1"
    stage_latency_s: float = 0.0
    token_usage: Dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_scene_understandings(
        cls,
        *,
        job_id: str,
        scene_understandings: List[Any],
        stage_latency_s: float = 0.0,
        token_usage: Dict[str, int] | None = None,
        llm_prompt_version: str = "v1",
        metadata: Dict[str, Any] | None = None,
    ) -> "SceneUnderstandingEvent":
        """
        Construct a :class:`SceneUnderstandingEvent` from pipeline
        ``SceneUnderstanding`` dataclass instances.

        Parameters
        ----------
        job_id:
            Pipeline-run identifier for cross-event correlation.
        scene_understandings:
            List of ``SceneUnderstanding`` dataclass instances as produced by
            the ``SceneSegmentationStage``.
        stage_latency_s:
            Wall-clock duration of the segmentation stage in seconds.
        token_usage:
            LLM token counters for the stage.  ``None`` defaults to all-zero.
        llm_prompt_version:
            Version string for the scene-analysis enrichment prompt.
        metadata:
            Optional extra context forwarded to the base event ``metadata`` field.
        """
        scenes = [
            SceneAnnotation(
                scene_index=s.scene_index,
                start_s=s.start_timestamp,
                end_s=s.end_timestamp,
                visual_summary=s.visual_summary,
                key_actions=s.key_actions,
                mood=s.mood,
            )
            for s in scene_understandings
        ]
        dominant_moods = list(dict.fromkeys(
            s.mood for s in scenes if s.mood
        ))
        return cls(
            job_id=job_id,
            scene_count=len(scenes),
            scenes=scenes,
            dominant_moods=dominant_moods,
            llm_prompt_version=llm_prompt_version,
            stage_latency_s=stage_latency_s,
            token_usage=token_usage or {},
            metadata=metadata or {},
        )
