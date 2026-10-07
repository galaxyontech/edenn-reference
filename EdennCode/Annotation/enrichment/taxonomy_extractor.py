"""
TaxonomyExtractor — single-responsibility LLM caller for taxonomy extraction.

Accepts a :class:`~EdennCode.Annotation.enrichment.taxonomy_extraction_input.TaxonomyExtractionInput`,
builds a slot-aware prompt, calls the Azure multimodal client with structured
JSON output enforcement, and returns a typed :class:`~EdennCode.Annotation.enrichment.taxonomy_schema.ExtractedTaxonomy`.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Tuple

from EdennCode.Annotation.enrichment.genre_hierarchy import hierarchy_prompt_block
from EdennCode.Annotation.enrichment.taxonomy_extraction_input import TaxonomyExtractionInput
from EdennCode.Annotation.enrichment.taxonomy_schema import (
    ExtractedTaxonomy,
    TAXONOMY_JSON_SCHEMA,
)
from EdennCode.Annotation.enrichment.text_slot import TextSlot, TextSlotType
from EdennCode.ModelFactory.LanguageModelFactory.azure_based_model_model_gateway import (
    AzureMultimodalClient,
)

logger = logging.getLogger(__name__)

# Slot-type-specific extraction guidance injected into the system prompt.
_SLOT_GUIDANCE: Dict[TextSlotType, str] = {
    TextSlotType.LYRICS: (
        "Lyrics section: derive lyric_themes, lyric_sentiment, mood_tags, and "
        "energy_level from the lyric content.  Treat metaphors literally for theme tagging."
    ),
    TextSlotType.STYLE_PROMPT: (
        "Style/genre prompt: derive genre_level1, genre_level2, genre_tags, "
        "instrument_tags, tempo_class, vocal_style, energy_level, and mood_tags "
        "from the musical style description."
    ),
    TextSlotType.LYRICS_PROMPT: (
        "Lyrics direction prompt: derive theme_tags, lyric_themes, and mood_tags. "
        "This is a creative brief, not the actual lyrics."
    ),
    TextSlotType.GLOBAL_MUSIC_PROMPT: (
        "Global music prompt: this is the highest-signal slot.  Derive genre_level1, "
        "genre_level2, genre_tags, instrument_tags, tempo_class, vocal_style, "
        "energy_level, mood_tags, sentiment, theme_tags, and activity_tags."
    ),
    TextSlotType.SCENE_DESCRIPTION: (
        "Scene descriptions: derive location_types, subject_types, motion_class, "
        "activity_tags, and mood reinforcement from the visual narrative."
    ),
    TextSlotType.VIDEO_DESCRIPTION: (
        "Video description: derive mood_tags, activity_tags, theme_tags, and "
        "sentiment from the overall video narrative."
    ),
}

# Built once at import time; hierarchy_prompt_block() is cheap but pure.
_GENRE_HIERARCHY_BLOCK = hierarchy_prompt_block()

_SYSTEM_PROMPT_HEADER = f"""\
You are a music and video taxonomy expert.  Your task is to extract a structured
semantic taxonomy from the provided text inputs.

Rules:
- Use only information present in the text.  Do not invent tags.
- mood_tags: 2-5 concise lowercase tags (e.g. "melancholic", "energetic", "urban").
- genre_level1 / genre_level2: assign the PRIMARY genre using the hierarchy below.
  Pick exactly one L1 and its best matching L2.  Use "Unknown" only when the genre
  is truly indeterminate.  For cross-genre content (e.g. "lo-fi hip-hop") pick the
  dominant dimension as L1 and leave overflow in genre_tags.
- genre_tags: fine-grained or secondary genre labels not covered by L1/L2
  (e.g. "lo-fi", "chill", "boom-bap").  May be empty.
- instrument_tags: instruments mentioned or strongly implied.
- tempo_class: coarse bucket — "slow", "medium", "fast", or "unknown".
- vocal_style: "melodic", "rhythmic", "spoken", "none" (instrumental), or "unknown".
- energy_level: float 0.0 (very calm) to 1.0 (intense).
- sentiment / lyric_sentiment: "positive", "negative", "neutral", "mixed", or
  "none" (lyric_sentiment only, when no lyrics present).
- theme_tags / lyric_themes / activity_tags: concrete noun phrases, lowercase,
  underscore-separated (e.g. "city_life", "new_beginning").
- location_types: physical setting labels (e.g. "urban_exterior", "indoor").
- subject_types: primary subjects (e.g. "person", "cityscape", "product_ui").
- motion_class: "static", "dynamic", "mixed", or "unknown".
- Return empty lists for fields with no applicable text.

{_GENRE_HIERARCHY_BLOCK}

Slot-specific guidance:\
"""


def _build_system_prompt(slots: list[TextSlot]) -> str:
    """Assemble a system prompt that includes guidance for each slot type present."""
    seen_types: set[TextSlotType] = set()
    guidance_lines = []
    for slot in slots:
        if slot.slot_type not in seen_types:
            seen_types.add(slot.slot_type)
            guidance_lines.append(f"\n- {_SLOT_GUIDANCE[slot.slot_type]}")
    return _SYSTEM_PROMPT_HEADER + "".join(guidance_lines)


def _build_user_message(extraction_input: TaxonomyExtractionInput) -> str:
    """Format all slots and context into a single user message string."""
    parts = []
    for slot in extraction_input.slots:
        locale_hint = f" [locale: {slot.locale}]" if slot.locale else ""
        parts.append(
            f"=== {slot.slot_type.value.upper()}{locale_hint} ===\n{slot.text}"
        )

    if extraction_input.context:
        ctx_lines = [f"  {k}: {v}" for k, v in extraction_input.context.items()]
        parts.append("=== CONTEXT ===\n" + "\n".join(ctx_lines))

    return "\n\n".join(parts)


class TaxonomyExtractor:
    """
    Calls the Azure LLM to extract a structured :class:`~EdennCode.Annotation.enrichment.taxonomy_schema.ExtractedTaxonomy`
    from a prepared :class:`~EdennCode.Annotation.enrichment.taxonomy_extraction_input.TaxonomyExtractionInput`.

    Parameters
    ----------
    llm_client:
        Configured :class:`~EdennCode.ModelFactory.LanguageModelFactory.azure_based_model_model_gateway.AzureMultimodalClient`
        instance.  The ``azure_model`` attribute is stored for audit in the
        resulting :class:`~EdennCode.Annotation.enrichment.taxonomy_enrichment_event.TaxonomyEnrichmentEvent`.
    temperature:
        Sampling temperature forwarded to the LLM.  Default ``0.1`` keeps
        taxonomy deterministic and stable across retries.
    """

    def __init__(
        self,
        llm_client: AzureMultimodalClient,
        *,
        temperature: float = 0.1,
    ) -> None:
        self._client = llm_client
        self._temperature = temperature

    @property
    def model_name(self) -> str:
        """Azure deployment name of the underlying LLM."""
        return self._client.azure_model

    async def extract(
        self,
        extraction_input: TaxonomyExtractionInput,
    ) -> Tuple[ExtractedTaxonomy, Dict[str, Any]]:
        """
        Run a single LLM taxonomy extraction call.

        Parameters
        ----------
        extraction_input:
            Prepared input with at least one non-empty slot.

        Returns
        -------
        taxonomy:
            Typed :class:`~EdennCode.Annotation.enrichment.taxonomy_schema.ExtractedTaxonomy` parsed
            from the LLM's structured JSON response.
        token_usage:
            Raw usage dict from the client
            (``{"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}``).

        Raises
        ------
        ValueError
            If *extraction_input* has no usable text slots.
        Exception
            Any exception raised by the underlying LLM client is re-raised
            unchanged so the processor can record it on the enrichment event.
        """
        if not extraction_input.has_usable_text:
            raise ValueError(
                f"TaxonomyExtractor.extract called with no usable slots "
                f"for job_id={extraction_input.job_id!r}"
            )

        system_prompt = _build_system_prompt(extraction_input.slots)
        user_message = _build_user_message(extraction_input)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        logger.info(
            "TaxonomyExtractor: extracting taxonomy for job_id=%s "
            "slots=%s model=%s",
            extraction_input.job_id,
            extraction_input.slot_types,
            self.model_name,
        )
        t0 = time.perf_counter()
        raw_dict, token_usage = await self._client.complete_messages(
            messages,
            json_schema=TAXONOMY_JSON_SCHEMA,
        )
        elapsed = time.perf_counter() - t0
        logger.info(
            "TaxonomyExtractor: completed in %.2fs tokens=%s job_id=%s",
            elapsed,
            token_usage.get("total_tokens", "?"),
            extraction_input.job_id,
        )

        taxonomy = ExtractedTaxonomy.from_llm_dict(raw_dict)
        return taxonomy, token_usage
