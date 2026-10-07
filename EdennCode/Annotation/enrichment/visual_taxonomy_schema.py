"""
Visual taxonomy output schema — structured result of one visual enrichment call.

``ExtractedVisualTaxonomy`` bundles both deterministically computed features
(pacing, platform hint, resolution) and LLM-extracted semantic features
(setting, lighting, content type, visual style, etc.) into a single typed object.

``VISUAL_TAXONOMY_JSON_SCHEMA`` covers only the semantic fields that require LLM
inference — computed fields are filled programmatically before the LLM is called.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class ExtractedVisualTaxonomy:
    """
    Full visual taxonomy for one pipeline job, combining deterministic and
    LLM-extracted features.

    Deterministic (computed from event data — no LLM)
    -------------------------------------------------
    pacing_class:
        Edit rhythm bucket: ``"slow"`` (avg scene ≥ 6 s), ``"medium"`` (3–6 s),
        ``"fast"`` (< 3 s), or ``"unknown"``.
    avg_scene_duration_s:
        Mean scene length in seconds (``video_duration / scene_count``).
    scene_density_per_min:
        Number of scene cuts per minute.
    platform_hint:
        Inferred distribution surface from aspect ratio:
        ``"tiktok"`` (portrait < 0.7), ``"youtube"`` (landscape > 1.4),
        ``"instagram"`` (square-ish), or ``"generic"``.
    aspect_ratio_class:
        Coarse aspect ratio bucket: ``"portrait"``, ``"landscape"``, ``"square"``,
        or ``"unknown"``.
    resolution_class:
        Vertical resolution bucket: ``"sd"`` (< 720 p), ``"hd"`` (720 p),
        ``"fhd"`` (1080 p), ``"4k"`` (2160 p+), or ``"unknown"``.

    Semantic (LLM-extracted from scene descriptions)
    -------------------------------------------------
    setting_type:
        Physical environment: ``"indoor"``, ``"outdoor"``, ``"mixed"``, or
        ``"unknown"``.
    time_of_day:
        Inferred time of day: ``"morning"``, ``"day"``, ``"evening"``,
        ``"night"``, or ``"unknown"``.
    lighting_mood:
        Dominant lighting quality: ``"bright"``, ``"dark"``, ``"dramatic"``,
        ``"natural"``, ``"neon"``, or ``"unknown"``.
    color_mood:
        Overall color palette feel: ``"warm"``, ``"cool"``, ``"neutral"``,
        ``"vibrant"``, ``"muted"``, or ``"unknown"``.
    content_type:
        Video content category: ``"lifestyle"``, ``"narrative"``, ``"product"``,
        ``"performance"``, ``"nature"``, ``"travel"``, ``"food"``, or ``"unknown"``.
    subject_focus:
        Dominant visual subject: ``"person"``, ``"object"``, ``"landscape"``,
        ``"abstract"``, ``"mixed"``, or ``"unknown"``.
    motion_intensity:
        How kinetic the visuals feel: ``"static"``, ``"gentle"``,
        ``"moderate"``, ``"intense"``, or ``"unknown"``.
    visual_style:
        Cinematographic/production style: ``"cinematic"``, ``"documentary"``,
        ``"vlog"``, ``"animation"``, ``"aesthetic"``, or ``"unknown"``.
    camera_motion:
        Dominant camera movement: ``"static"``, ``"handheld"``, ``"panning"``,
        ``"tracking"``, ``"mixed"``, or ``"unknown"``.
    dominant_colors:
        2–5 descriptive color labels inferred from scene descriptions
        (e.g. ``["warm_orange", "deep_blue", "golden"]``).
    visual_tags:
        2–8 freeform semantic tags capturing standout visual elements
        (e.g. ``["golden_hour", "urban_exterior", "crowd", "transit"]``).
    """

    # Deterministic (default = unknown / zero until computed)
    pacing_class: str = "unknown"
    avg_scene_duration_s: float = 0.0
    scene_density_per_min: float = 0.0
    platform_hint: str = "generic"
    aspect_ratio_class: str = "unknown"
    resolution_class: str = "unknown"

    # LLM-semantic
    setting_type: str = "unknown"
    time_of_day: str = "unknown"
    lighting_mood: str = "unknown"
    color_mood: str = "unknown"
    content_type: str = "unknown"
    subject_focus: str = "unknown"
    motion_intensity: str = "unknown"
    visual_style: str = "unknown"
    camera_motion: str = "unknown"
    dominant_colors: List[str] = field(default_factory=list)
    visual_tags: List[str] = field(default_factory=list)

    @classmethod
    def from_llm_dict(
        cls,
        data: Dict[str, Any],
        *,
        computed: Dict[str, Any] | None = None,
    ) -> "ExtractedVisualTaxonomy":
        """
        Construct from a raw LLM response dict, merging in pre-computed
        deterministic fields.

        Parameters
        ----------
        data:
            Parsed JSON dict returned by the LLM (semantic fields only).
        computed:
            Dict of deterministically computed fields to overlay onto the
            instance after LLM field population.

        Returns
        -------
        ExtractedVisualTaxonomy
            Fully populated instance; never raises on missing keys.
        """
        def _str(key: str, default: str) -> str:
            val = data.get(key, default)
            return str(val) if val is not None else default

        def _str_list(key: str) -> List[str]:
            val = data.get(key, [])
            return [str(v) for v in val] if isinstance(val, list) else []

        instance = cls(
            setting_type=_str("setting_type", "unknown"),
            time_of_day=_str("time_of_day", "unknown"),
            lighting_mood=_str("lighting_mood", "unknown"),
            color_mood=_str("color_mood", "unknown"),
            content_type=_str("content_type", "unknown"),
            subject_focus=_str("subject_focus", "unknown"),
            motion_intensity=_str("motion_intensity", "unknown"),
            visual_style=_str("visual_style", "unknown"),
            camera_motion=_str("camera_motion", "unknown"),
            dominant_colors=_str_list("dominant_colors"),
            visual_tags=_str_list("visual_tags"),
        )
        if computed:
            for key, value in computed.items():
                setattr(instance, key, value)
        return instance


# ---------------------------------------------------------------------------
# JSON schema — covers only the LLM-inferred semantic fields
# ---------------------------------------------------------------------------

VISUAL_TAXONOMY_JSON_SCHEMA: Dict[str, Any] = {
    "name": "extracted_visual_taxonomy",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "setting_type": {
                "type": "string",
                "enum": ["indoor", "outdoor", "mixed", "unknown"],
                "description": "Physical environment of the dominant scenes.",
            },
            "time_of_day": {
                "type": "string",
                "enum": ["morning", "day", "evening", "night", "unknown"],
                "description": "Inferred time of day from lighting and scene cues.",
            },
            "lighting_mood": {
                "type": "string",
                "enum": ["bright", "dark", "dramatic", "natural", "neon", "unknown"],
                "description": "Dominant lighting quality across all scenes.",
            },
            "color_mood": {
                "type": "string",
                "enum": ["warm", "cool", "neutral", "vibrant", "muted", "unknown"],
                "description": "Overall color palette feel of the video.",
            },
            "content_type": {
                "type": "string",
                "enum": ["lifestyle", "narrative", "product", "performance", "nature", "travel", "food", "unknown"],
                "description": "High-level content category.",
            },
            "subject_focus": {
                "type": "string",
                "enum": ["person", "object", "landscape", "abstract", "mixed", "unknown"],
                "description": "Dominant visual subject across scenes.",
            },
            "motion_intensity": {
                "type": "string",
                "enum": ["static", "gentle", "moderate", "intense", "unknown"],
                "description": "How kinetic and energetic the visuals feel.",
            },
            "visual_style": {
                "type": "string",
                "enum": ["cinematic", "documentary", "vlog", "animation", "aesthetic", "unknown"],
                "description": "Cinematographic or production style.",
            },
            "camera_motion": {
                "type": "string",
                "enum": ["static", "handheld", "panning", "tracking", "mixed", "unknown"],
                "description": "Dominant camera movement style.",
            },
            "dominant_colors": {
                "type": "array",
                "items": {"type": "string"},
                "description": "2-5 descriptive color labels (e.g. 'warm_orange', 'deep_blue').",
            },
            "visual_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "2-8 lowercase semantic tags for standout visual elements.",
            },
        },
        "required": [
            "setting_type", "time_of_day", "lighting_mood", "color_mood",
            "content_type", "subject_focus", "motion_intensity",
            "visual_style", "camera_motion",
            "dominant_colors", "visual_tags",
        ],
        "additionalProperties": False,
    },
}
