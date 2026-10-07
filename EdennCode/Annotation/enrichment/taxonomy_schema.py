"""
Taxonomy output schema — the structured result of one LLM enrichment call.

``ExtractedTaxonomy`` is the typed Python representation.
``TAXONOMY_JSON_SCHEMA`` is the strict JSON schema passed to the LLM via
``complete_messages(..., json_schema=TAXONOMY_JSON_SCHEMA)`` to enforce
deterministic structured output.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from EdennCode.Annotation.enrichment.genre_hierarchy import (
    GENRE_L1_VALUES,
    GENRE_L2_VALUES,
    validate_genre_level1,
    validate_genre_level2,
)


@dataclass
class ExtractedTaxonomy:
    """
    Structured semantic taxonomy extracted from one pipeline job's text inputs.

    All list fields default to empty; fields with no applicable text input
    (e.g. ``lyric_themes`` when the job is instrumental) will be empty lists
    or the sentinel string ``"none"``.

    Mood and affect
    ---------------
    mood_tags:
        Atmosphere/emotion tags derived from any text slot
        (e.g. ``["reflective", "hopeful", "urban"]``).
    sentiment:
        Overall affective tone: ``"positive"``, ``"negative"``,
        ``"neutral"``, or ``"mixed"``.
    energy_level:
        Continuous proxy for musical energy on ``[0.0, 1.0]``.
        Derived from style/prompt language (e.g. "driving", "intense" → high).

    Music character — hierarchical genre
    -------------------------------------
    genre_level1:
        Coarse genre bucket from the controlled L1 vocabulary
        (e.g. ``"Electronic"``, ``"Hip-Hop / R&B"``).
        Indexed as a plain TEXT column — primary hard filter at search time.
    genre_level2:
        Mid-tier sub-genre from the L2 vocabulary for *genre_level1*
        (e.g. ``"Synthwave"``, ``"Trap"``).
        Indexed as a plain TEXT column — secondary hard filter at search time.
    genre_tags:
        Free-text fine-grained or cross-genre overflow labels
        (e.g. ``["lo-fi", "chill", "boom-bap"]``).
        GIN-indexed JSONB — used as a soft boost after L1/L2 hard filter.
    instrument_tags:
        Instruments mentioned or implied (e.g. ``["piano", "synth", "female vocal"]``).
    tempo_class:
        Coarse tempo bucket: ``"slow"``, ``"medium"``, ``"fast"``, or
        ``"unknown"``.
    vocal_style:
        Vocal delivery style: ``"melodic"``, ``"rhythmic"``, ``"spoken"``,
        ``"none"`` (instrumental), or ``"unknown"``.

    Content themes
    --------------
    theme_tags:
        Narrative/content themes (e.g. ``["journey", "city_life", "connection"]``).
    activity_tags:
        Real-world activity contexts (e.g. ``["commute", "daily_life"]``).

    Visual context (from scene descriptions)
    -----------------------------------------
    location_types:
        Physical setting labels (e.g. ``["urban_exterior", "transit", "indoor"]``).
    subject_types:
        Primary subjects depicted (e.g. ``["person", "cityscape", "product_ui"]``).
    motion_class:
        Overall motion quality: ``"static"``, ``"dynamic"``, ``"mixed"``,
        or ``"unknown"``.

    Lyric-specific (populated only when a LYRICS slot is present)
    --------------------------------------------------------------
    lyric_themes:
        Concrete topics appearing in the lyrics
        (e.g. ``["sunrise", "rain", "new_beginning"]``).
    lyric_sentiment:
        Lyric-specific sentiment: ``"positive"``, ``"negative"``,
        ``"neutral"``, ``"mixed"``, or ``"none"`` when no lyrics present.
    """

    # Mood and affect
    mood_tags: List[str] = field(default_factory=list)
    sentiment: str = "neutral"
    energy_level: float = 0.5

    # Music character — hierarchical genre (primary retrieval dimensions)
    genre_level1: str = "Unknown"
    genre_level2: str = "Unknown"
    genre_tags: List[str] = field(default_factory=list)
    instrument_tags: List[str] = field(default_factory=list)
    tempo_class: str = "unknown"
    vocal_style: str = "unknown"

    # Content themes
    theme_tags: List[str] = field(default_factory=list)
    activity_tags: List[str] = field(default_factory=list)

    # Visual context
    location_types: List[str] = field(default_factory=list)
    subject_types: List[str] = field(default_factory=list)
    motion_class: str = "unknown"

    # Lyric-specific
    lyric_themes: List[str] = field(default_factory=list)
    lyric_sentiment: str = "none"

    @classmethod
    def from_llm_dict(cls, data: Dict[str, Any]) -> "ExtractedTaxonomy":
        """
        Construct from a raw LLM response dict, applying safe defaults for
        any missing or wrongly-typed fields.

        ``genre_level1`` and ``genre_level2`` are validated against the
        controlled vocabulary; invalid values fall back to ``"Unknown"``.

        Parameters
        ----------
        data:
            Parsed JSON dict returned by the LLM.

        Returns
        -------
        ExtractedTaxonomy
            A fully populated instance; never raises on missing keys.
        """
        def _str_list(key: str) -> List[str]:
            val = data.get(key, [])
            return [str(v) for v in val] if isinstance(val, list) else []

        def _str(key: str, default: str) -> str:
            val = data.get(key, default)
            return str(val) if val is not None else default

        def _float(key: str, default: float) -> float:
            try:
                return float(data.get(key, default))
            except (TypeError, ValueError):
                return default

        return cls(
            mood_tags=_str_list("mood_tags"),
            sentiment=_str("sentiment", "neutral"),
            energy_level=_float("energy_level", 0.5),
            genre_level1=validate_genre_level1(_str("genre_level1", "Unknown")),
            genre_level2=validate_genre_level2(_str("genre_level2", "Unknown")),
            genre_tags=_str_list("genre_tags"),
            instrument_tags=_str_list("instrument_tags"),
            tempo_class=_str("tempo_class", "unknown"),
            vocal_style=_str("vocal_style", "unknown"),
            theme_tags=_str_list("theme_tags"),
            activity_tags=_str_list("activity_tags"),
            location_types=_str_list("location_types"),
            subject_types=_str_list("subject_types"),
            motion_class=_str("motion_class", "unknown"),
            lyric_themes=_str_list("lyric_themes"),
            lyric_sentiment=_str("lyric_sentiment", "none"),
        )


# ---------------------------------------------------------------------------
# JSON schema enforced on LLM output via structured outputs API
# ---------------------------------------------------------------------------

TAXONOMY_JSON_SCHEMA: Dict[str, Any] = {
    "name": "extracted_taxonomy",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "mood_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "2-5 mood/atmosphere tags derived from any input.",
            },
            "sentiment": {
                "type": "string",
                "enum": ["positive", "negative", "neutral", "mixed"],
                "description": "Overall affective tone.",
            },
            "energy_level": {
                "type": "number",
                "description": "Musical energy 0.0 (very calm) to 1.0 (intense).",
            },
            "genre_level1": {
                "type": "string",
                "enum": GENRE_L1_VALUES,
                "description": (
                    "Primary genre bucket — pick the single best L1 label from the "
                    "controlled vocabulary.  Use 'Unknown' only when no genre is determinable."
                ),
            },
            "genre_level2": {
                "type": "string",
                "enum": GENRE_L2_VALUES,
                "description": (
                    "Sub-genre within genre_level1 — pick the single best L2 label "
                    "that is a valid child of the chosen genre_level1.  "
                    "Use 'Unknown' only when no sub-genre is determinable."
                ),
            },
            "genre_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Additional fine-grained or cross-genre labels not captured by "
                    "genre_level1/genre_level2 (e.g. 'lo-fi', 'chill', 'boom-bap').  "
                    "May be empty."
                ),
            },
            "instrument_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Instruments mentioned or implied by style text.",
            },
            "tempo_class": {
                "type": "string",
                "enum": ["slow", "medium", "fast", "unknown"],
                "description": "Coarse tempo bucket.",
            },
            "vocal_style": {
                "type": "string",
                "enum": ["melodic", "rhythmic", "spoken", "none", "unknown"],
                "description": "Vocal delivery style; use 'none' for instrumentals.",
            },
            "theme_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Narrative or content themes.",
            },
            "activity_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Real-world activity contexts this content suits.",
            },
            "location_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Physical settings from scene descriptions.",
            },
            "subject_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Primary subjects depicted in scenes.",
            },
            "motion_class": {
                "type": "string",
                "enum": ["static", "dynamic", "mixed", "unknown"],
                "description": "Overall motion quality of scenes.",
            },
            "lyric_themes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Concrete topics in lyrics. Empty list if no lyrics.",
            },
            "lyric_sentiment": {
                "type": "string",
                "enum": ["positive", "negative", "neutral", "mixed", "none"],
                "description": "Lyric-specific sentiment. Use 'none' if no lyrics.",
            },
        },
        "required": [
            "mood_tags", "sentiment", "energy_level",
            "genre_level1", "genre_level2",
            "genre_tags", "instrument_tags", "tempo_class", "vocal_style",
            "theme_tags", "activity_tags",
            "location_types", "subject_types", "motion_class",
            "lyric_themes", "lyric_sentiment",
        ],
        "additionalProperties": False,
    },
}
