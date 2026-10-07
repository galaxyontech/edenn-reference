"""
VisualTaxonomyExtractor — deterministic + LLM visual feature extraction.

Two-phase extraction:

1. **Deterministic** — computed from existing annotation events with no LLM call:
   pacing, scene density, platform hint, aspect ratio class, resolution class.

2. **Semantic LLM** — called when scene or video description events are present.
   Extracts setting, lighting, color mood, content type, subject focus,
   motion intensity, visual style, camera motion, dominant colors, and visual tags.

The two results are merged into a single :class:`~EdennCode.Annotation.enrichment.visual_taxonomy_schema.ExtractedVisualTaxonomy`.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from EdennCode.Annotation.enrichment.visual_taxonomy_schema import (
    ExtractedVisualTaxonomy,
    VISUAL_TAXONOMY_JSON_SCHEMA,
)
from EdennCode.ModelFactory.LanguageModelFactory.azure_based_model_model_gateway import (
    AzureMultimodalClient,
)

logger = logging.getLogger(__name__)

# Pacing thresholds (average scene duration in seconds)
_PACING_SLOW_THRESHOLD = 6.0
_PACING_MEDIUM_THRESHOLD = 3.0

# Aspect ratio boundaries for platform detection
_PORTRAIT_MAX_RATIO = 0.7    # 9:16 ≈ 0.5625
_LANDSCAPE_MIN_RATIO = 1.4   # 16:9 ≈ 1.778

_SYSTEM_PROMPT = """\
You are a visual media analyst specialising in video content taxonomy for music recommendation.

Your task: analyse the provided video scene descriptions and extract a structured semantic taxonomy.

Rules:
- Base your answers entirely on the scene and video descriptions provided.  Do not invent details.
- setting_type: where the action takes place — indoor/outdoor/mixed/unknown.
- time_of_day: infer from lighting cues (golden hour → morning or evening, harsh sun → day, etc.).
- lighting_mood: the emotional quality of the lighting — bright/dark/dramatic/natural/neon/unknown.
- color_mood: the dominant color palette feel — warm/cool/neutral/vibrant/muted/unknown.
- content_type: what kind of video this is — lifestyle/narrative/product/performance/nature/travel/food/unknown.
- subject_focus: what is primarily on screen — person/object/landscape/abstract/mixed/unknown.
- motion_intensity: how kinetic the visuals feel — static/gentle/moderate/intense/unknown.
- visual_style: the production aesthetic — cinematic/documentary/vlog/animation/aesthetic/unknown.
- camera_motion: dominant camera movement — static/handheld/panning/tracking/mixed/unknown.
- dominant_colors: 2-5 lowercase color descriptors (e.g. "warm_orange", "deep_blue", "golden").
- visual_tags: 2-8 lowercase semantic tags for standout visual elements \
(e.g. "golden_hour", "urban_exterior", "crowd_scene", "transit", "time_lapse").
"""


def _compute_deterministic(events: List[Any]) -> Dict[str, Any]:
    """
    Compute pacing, platform hint, and resolution from existing event data.
    No network call.  Returns a dict of field_name → value.
    """
    result: Dict[str, Any] = {}

    scene_ev = next((e for e in events if getattr(e, "event_type", "") == "scene_understanding"), None)
    feature_ev = next((e for e in events if getattr(e, "event_type", "") == "video_feature"), None)

    duration_s: float = getattr(feature_ev, "duration_s", 0.0) or 0.0

    # Pacing and scene density
    scene_count = 0
    if scene_ev is not None:
        scenes = getattr(scene_ev, "scenes", None) or []
        scene_count = len(scenes) if isinstance(scenes, list) else (getattr(scene_ev, "scene_count", 0) or 0)

    if scene_count > 0 and duration_s > 0:
        avg_dur = duration_s / scene_count
        density = scene_count / (duration_s / 60.0)
        result["avg_scene_duration_s"] = round(avg_dur, 2)
        result["scene_density_per_min"] = round(density, 2)
        if avg_dur >= _PACING_SLOW_THRESHOLD:
            result["pacing_class"] = "slow"
        elif avg_dur >= _PACING_MEDIUM_THRESHOLD:
            result["pacing_class"] = "medium"
        else:
            result["pacing_class"] = "fast"

    # Platform hint and resolution from video dimensions
    if feature_ev is not None:
        width: Optional[int] = getattr(feature_ev, "width", None)
        height: Optional[int] = getattr(feature_ev, "height", None)
        if width and height:
            ratio = width / height
            if ratio < _PORTRAIT_MAX_RATIO:
                result["aspect_ratio_class"] = "portrait"
                result["platform_hint"] = "tiktok"
            elif ratio > _LANDSCAPE_MIN_RATIO:
                result["aspect_ratio_class"] = "landscape"
                result["platform_hint"] = "youtube"
            else:
                result["aspect_ratio_class"] = "square"
                result["platform_hint"] = "instagram"

            if height < 720:
                result["resolution_class"] = "sd"
            elif height < 1080:
                result["resolution_class"] = "hd"
            elif height < 2160:
                result["resolution_class"] = "fhd"
            else:
                result["resolution_class"] = "4k"

    return result


def _build_user_message(events: List[Any]) -> str:
    """Format scene and video description events into an LLM user message."""
    parts: List[str] = []

    scene_ev = next((e for e in events if getattr(e, "event_type", "") == "scene_understanding"), None)
    video_ev = next((e for e in events if getattr(e, "event_type", "") == "video_understanding"), None)
    feature_ev = next((e for e in events if getattr(e, "event_type", "") == "video_feature"), None)

    if feature_ev is not None:
        w = getattr(feature_ev, "width", None)
        h = getattr(feature_ev, "height", None)
        dur = getattr(feature_ev, "duration_s", None)
        meta_parts = []
        if dur:
            meta_parts.append(f"duration={dur:.1f}s")
        if w and h:
            meta_parts.append(f"resolution={w}x{h}")
        if meta_parts:
            parts.append("=== VIDEO METADATA ===\n" + "  ".join(meta_parts))

    if video_ev is not None:
        lines = []
        if getattr(video_ev, "video_description", None):
            lines.append(f"Description: {video_ev.video_description}")
        if getattr(video_ev, "overall_mood", None):
            lines.append(f"Overall mood: {video_ev.overall_mood}")
        if getattr(video_ev, "core_message", None):
            lines.append(f"Core message: {video_ev.core_message}")
        if lines:
            parts.append("=== VIDEO UNDERSTANDING ===\n" + "\n".join(lines))

    if scene_ev is not None:
        scenes = getattr(scene_ev, "scenes", []) or []
        dominant = getattr(scene_ev, "dominant_moods", []) or []
        scene_lines = []
        for sc in scenes:
            idx = getattr(sc, "scene_index", "?")
            start = getattr(sc, "start_s", 0)
            end = getattr(sc, "end_s", 0)
            summary = getattr(sc, "visual_summary", "")
            actions = getattr(sc, "key_actions", "")
            mood = getattr(sc, "mood", "")
            scene_lines.append(
                f"[Scene {idx} | {start:.1f}s–{end:.1f}s] {summary} "
                f"| Actions: {actions} | Mood: {mood}"
            )
        header = f"=== SCENES ({len(scenes)} total, dominant moods: {', '.join(dominant)}) ==="
        parts.append(header + "\n" + "\n".join(scene_lines))

    return "\n\n".join(parts) if parts else ""


def _has_visual_text(events: List[Any]) -> bool:
    for ev in events:
        etype = getattr(ev, "event_type", "")
        if etype in ("scene_understanding", "video_understanding"):
            return True
    return False


class VisualTaxonomyExtractor:
    """
    Extracts a rich :class:`~EdennCode.Annotation.enrichment.visual_taxonomy_schema.ExtractedVisualTaxonomy`
    from annotation events via a two-phase process:

    1. Deterministic computation from ``VideoFeatureEvent`` and
       ``SceneUnderstandingEvent`` — always runs, never touches the network.
    2. LLM semantic extraction from ``SceneUnderstandingEvent`` scene descriptions
       and ``VideoUnderstandingEvent`` — only when visual text is available.

    Parameters
    ----------
    llm_client:
        Configured :class:`~EdennCode.ModelFactory.LanguageModelFactory.azure_based_model_model_gateway.AzureMultimodalClient`.
    temperature:
        Sampling temperature.  Default ``0.1`` for consistent taxonomy output.
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
        return self._client.azure_model

    async def extract(
        self,
        events: List[Any],
    ) -> Tuple[ExtractedVisualTaxonomy, Dict[str, Any], bool]:
        """
        Run visual taxonomy extraction for one job's events.

        Parameters
        ----------
        events:
            All annotation events for the job (any order, any types).

        Returns
        -------
        taxonomy:
            Populated :class:`~EdennCode.Annotation.enrichment.visual_taxonomy_schema.ExtractedVisualTaxonomy`.
        token_usage:
            LLM token usage dict, or empty dict if LLM was not called.
        deterministic_only:
            ``True`` when no visual text was found and only computed features
            were populated.
        """
        computed = _compute_deterministic(events)

        if not _has_visual_text(events):
            logger.info(
                "VisualTaxonomyExtractor: no visual text events — "
                "returning deterministic features only"
            )
            taxonomy = ExtractedVisualTaxonomy.from_llm_dict({}, computed=computed)
            return taxonomy, {}, True

        user_message = _build_user_message(events)
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

        logger.info(
            "VisualTaxonomyExtractor: calling LLM for job with %d events model=%s",
            len(events),
            self.model_name,
        )
        t0 = time.perf_counter()
        raw_dict, token_usage = await self._client.complete_messages(
            messages,
            json_schema=VISUAL_TAXONOMY_JSON_SCHEMA,
        )
        elapsed = time.perf_counter() - t0
        logger.info(
            "VisualTaxonomyExtractor: completed in %.2fs tokens=%s",
            elapsed,
            token_usage.get("total_tokens", "?"),
        )

        taxonomy = ExtractedVisualTaxonomy.from_llm_dict(raw_dict, computed=computed)
        return taxonomy, token_usage, False
