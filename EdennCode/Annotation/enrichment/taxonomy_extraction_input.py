"""
TaxonomyExtractionInput — normalised container of text slots for one pipeline job.

The ``from_job_events`` class method is the canonical way to construct this
from a list of raw annotation events.  It encodes the mapping from event type
→ text slot type so the extractor and processor remain event-schema-agnostic.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from EdennCode.Annotation.core.annotation_event import AnnotationEvent
from EdennCode.Annotation.enrichment.text_slot import TextSlot, TextSlotType

logger = logging.getLogger(__name__)

# Maximum characters per SCENE_DESCRIPTION slot to keep prompt size bounded.
_MAX_SCENE_DESCRIPTION_CHARS = 2_000
# Maximum number of scenes to include in the description slot.
_MAX_SCENE_COUNT = 10


@dataclass
class TaxonomyExtractionInput:
    """
    Normalised set of text inputs for a single LLM taxonomy extraction call.

    A ``TaxonomyExtractionInput`` bundles all usable text from the annotation
    events of one pipeline run into an ordered list of :class:`~EdennCode.Annotation.enrichment.text_slot.TextSlot`
    objects plus a lightweight ``context`` dict for non-textual signals that
    help the LLM calibrate its response (e.g. ``overall_mood``,
    ``video_category``, ``video_duration_s``).

    Attributes
    ----------
    job_id:
        Pipeline-run identifier.  All annotation events contributing to this
        input share this ``job_id``.
    source_event_ids:
        Ordered list of ``event_id`` values from the events that contributed
        text slots.  Used by the processor for audit and deduplication.
    slots:
        Ordered, deduplicated list of :class:`TextSlot` instances.  Each slot
        carries its semantic type so the extractor prompt adapts per type.
        Empty if no usable text was found — the processor will skip the job.
    context:
        Non-textual context signals forwarded to the extractor prompt as
        auxiliary metadata.  Example keys: ``overall_mood``,
        ``video_category``, ``video_duration_s``, ``include_vocals``,
        ``tempo_bpm``, ``instruments``.
    extraction_prompt_version:
        Version of the extraction prompt that should be used to enrich this
        input.  Persisted on the resulting :class:`~EdennCode.Annotation.enrichment.taxonomy_enrichment_event.TaxonomyEnrichmentEvent`
        so the processor can detect stale enrichments when the prompt is
        updated.
    """

    job_id: str
    source_event_ids: List[str] = field(default_factory=list)
    slots: List[TextSlot] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)
    extraction_prompt_version: str = "v1"

    @property
    def has_usable_text(self) -> bool:
        """``True`` when at least one non-empty text slot is present."""
        return len(self.slots) > 0

    @property
    def slot_types(self) -> List[str]:
        """Ordered list of slot type names present in this input."""
        return [s.slot_type.value for s in self.slots]

    # ------------------------------------------------------------------
    # Builder
    # ------------------------------------------------------------------

    @classmethod
    def from_job_events(
        cls,
        events: List[AnnotationEvent],
        *,
        extraction_prompt_version: str = "v1",
    ) -> "TaxonomyExtractionInput":
        """
        Build a :class:`TaxonomyExtractionInput` from all annotation events
        belonging to a single pipeline run.

        This method encodes the canonical mapping from event type → slot type:

        * ``MusicGenerationEvent`` → ``LYRICS`` (from ``full_lyrics_text``),
          ``STYLE_PROMPT`` (from ``style_prompt`` or ``combined_prompt``),
          ``LYRICS_PROMPT`` (from ``lyrics_prompt``).
        * ``MusicPromptEvent`` → ``STYLE_PROMPT``, ``LYRICS_PROMPT``,
          ``GLOBAL_MUSIC_PROMPT`` (from ``prompt_dict["global_music_prompt"]``).
          Also populates ``context["tempo_bpm"]`` and
          ``context["instruments"]`` from ``prompt_dict``.
        * ``SceneUnderstandingEvent`` → ``SCENE_DESCRIPTION`` (up to
          ``_MAX_SCENE_COUNT`` scenes, truncated to ``_MAX_SCENE_DESCRIPTION_CHARS``).
        * ``VideoUnderstandingEvent`` → ``VIDEO_DESCRIPTION``, and populates
          ``context["overall_mood"]``, ``context["core_message"]``.
        * ``RequestContextEvent`` → populates ``context["video_category"]``,
          ``context["include_vocals"]``, ``context["vocal_gender"]``.
        * ``VideoFeatureEvent`` → populates ``context["video_duration_s"]``.

        Unknown event types are silently skipped.  Empty / ``None`` text
        fields are silently skipped.  Duplicate text content (same string from
        two different events) is deduplicated — only the first occurrence is
        kept.

        Parameters
        ----------
        events:
            All annotation events for a single ``job_id``, in any order.
        extraction_prompt_version:
            Prompt version string forwarded to the input for staleness
            detection.

        Returns
        -------
        TaxonomyExtractionInput
            A fully constructed input.  ``has_usable_text`` will be ``False``
            if no usable text was found (e.g. a pure instrumental job with
            no prompt information).
        """
        if not events:
            job_id = ""
        else:
            job_id = events[0].job_id

        slots: List[TextSlot] = []
        source_event_ids: List[str] = []
        context: Dict[str, Any] = {}
        seen_texts: set[str] = set()

        def _add_slot(
            slot_type: TextSlotType,
            text: Optional[str],
            source_field: str,
            event_id: str,
            locale: Optional[str] = None,
        ) -> None:
            if not text or not text.strip():
                return
            normalised = text.strip()
            if normalised in seen_texts:
                return
            seen_texts.add(normalised)
            slots.append(TextSlot(
                slot_type=slot_type,
                text=normalised,
                source_field=source_field,
                locale=locale,
            ))
            if event_id not in source_event_ids:
                source_event_ids.append(event_id)

        for event in events:
            etype = event.event_type

            if etype == "music_generation":
                locale = getattr(event, "lyrics_language", None)
                _add_slot(TextSlotType.LYRICS,
                          getattr(event, "full_lyrics_text", None),
                          "MusicGenerationEvent.full_lyrics_text",
                          event.event_id, locale=locale)
                _add_slot(TextSlotType.STYLE_PROMPT,
                          getattr(event, "style_prompt", None),
                          "MusicGenerationEvent.style_prompt",
                          event.event_id)
                _add_slot(TextSlotType.STYLE_PROMPT,
                          getattr(event, "combined_prompt", None),
                          "MusicGenerationEvent.combined_prompt",
                          event.event_id)
                _add_slot(TextSlotType.LYRICS_PROMPT,
                          getattr(event, "lyrics_prompt", None),
                          "MusicGenerationEvent.lyrics_prompt",
                          event.event_id)
                # Carry overall mood into context
                if getattr(event, "overall_mood", None):
                    context.setdefault("overall_mood", event.overall_mood)
                if getattr(event, "video_category", None):
                    context.setdefault("video_category", event.video_category)
                if getattr(event, "video_duration_s", None):
                    context.setdefault("video_duration_s", event.video_duration_s)
                if getattr(event, "include_vocals", None) is not None:
                    context.setdefault("include_vocals", event.include_vocals)
                # Track identity
                if getattr(event, "provider_name", None):
                    context.setdefault("provider_name", event.provider_name)
                if getattr(event, "model_spec", None):
                    context.setdefault("model_spec", event.model_spec)
                if getattr(event, "music_filename", None):
                    context.setdefault("music_filename", event.music_filename)
                if getattr(event, "complete_music_filename", None):
                    context.setdefault("complete_music_filename", event.complete_music_filename)

            elif etype == "music_prompt":
                _add_slot(TextSlotType.STYLE_PROMPT,
                          getattr(event, "style_prompt", None),
                          "MusicPromptEvent.style_prompt",
                          event.event_id)
                _add_slot(TextSlotType.STYLE_PROMPT,
                          getattr(event, "combined_prompt", None),
                          "MusicPromptEvent.combined_prompt",
                          event.event_id)
                _add_slot(TextSlotType.LYRICS_PROMPT,
                          getattr(event, "lyrics_prompt", None),
                          "MusicPromptEvent.lyrics_prompt",
                          event.event_id)
                prompt_dict: Dict[str, Any] = getattr(event, "prompt_dict", {}) or {}
                _add_slot(TextSlotType.GLOBAL_MUSIC_PROMPT,
                          prompt_dict.get("global_music_prompt"),
                          "MusicPromptEvent.prompt_dict.global_music_prompt",
                          event.event_id)
                if prompt_dict.get("tempo_bpm"):
                    context.setdefault("tempo_bpm", prompt_dict["tempo_bpm"])
                if prompt_dict.get("instruments"):
                    context.setdefault("instruments", prompt_dict["instruments"])
                if getattr(event, "generation_language", None):
                    context.setdefault("generation_language", event.generation_language)

            elif etype == "scene_understanding":
                scenes = getattr(event, "scenes", []) or []
                if scenes:
                    capped = scenes[:_MAX_SCENE_COUNT]
                    parts = []
                    for sc in capped:
                        parts.append(
                            f"[Scene {sc.scene_index}] "
                            f"{sc.visual_summary} "
                            f"Actions: {sc.key_actions} "
                            f"Mood: {sc.mood}"
                        )
                    combined = "\n".join(parts)[:_MAX_SCENE_DESCRIPTION_CHARS]
                    _add_slot(TextSlotType.SCENE_DESCRIPTION,
                              combined,
                              "SceneUnderstandingEvent.scenes",
                              event.event_id)

            elif etype == "video_understanding":
                _add_slot(TextSlotType.VIDEO_DESCRIPTION,
                          getattr(event, "video_description", None),
                          "VideoUnderstandingEvent.video_description",
                          event.event_id)
                if getattr(event, "overall_mood", None):
                    context.setdefault("overall_mood", event.overall_mood)
                if getattr(event, "core_message", None):
                    context.setdefault("core_message", event.core_message)

            elif etype == "request_context":
                if getattr(event, "detected_category", None):
                    context.setdefault("video_category", event.detected_category)
                if getattr(event, "include_vocals", None) is not None:
                    context.setdefault("include_vocals", event.include_vocals)
                if getattr(event, "vocal_gender", None):
                    context.setdefault("vocal_gender", event.vocal_gender)

            elif etype == "video_feature":
                if getattr(event, "duration_s", None):
                    context.setdefault("video_duration_s", event.duration_s)

        return cls(
            job_id=job_id,
            source_event_ids=source_event_ids,
            slots=slots,
            context=context,
            extraction_prompt_version=extraction_prompt_version,
        )
