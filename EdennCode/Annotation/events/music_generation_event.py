"""
Annotation event emitted after Stage 4 (MusicGeneration).

This is the highest-value annotation event in the pipeline.  It consolidates the
complete generation record — provider identity, prompts, lyrics, timing metadata,
and video context — into a single event that maps directly onto the track schema
defined in the recommendation design document (§4.1 Canonical Track Schema).

It is the primary record used for:
* Offline LLM enrichment (generating ``llm_tags`` and ``llm_description``).
* Embedding computation for ANN retrieval.
* Provider cost and latency tracking.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from EdennCode.Annotation.core.annotation_event import AnnotationEvent


@dataclass(kw_only=True)
class MusicGenerationEvent(AnnotationEvent):
    """
    Complete record of a single music generation result.

    This event is emitted once per pipeline run immediately after the
    ``MusicGenerationStage`` returns.  It carries sufficient context to
    reconstruct the canonical track record required by the recommendation system
    without re-running any upstream stage.

    Provider fields
    ---------------
    model_spec:
        Normalised model specifier: ``"edenn_basic"``, ``"edenn_enhanced"``,
        or ``"edenn_studio"``.
    provider_name:
        Human-readable provider label: ``"provider_a"``, ``"provider_b"``, or
        ``"provider_c"``.
    task_id:
        Provider-side task or job identifier.  ``None`` for ProviderA which
        returns a track directly.
    audio_id:
        Provider-side audio asset identifier.  ``None`` if not returned.

    Generation config fields
    ------------------------
    include_vocals:
        Whether the generation targeted a vocal output.
    vocal_gender:
        Gender hint passed to the provider (``"male"``, ``"female"``, or ``""``).
    vocal_id_used:
        The voice clone ID actually used.  ``None`` for non-clone paths.
    lyrics_language:
        The language code used for lyric generation (e.g. ``"EN"``,
        ``"CHINESE_MAINLAND"``).

    Prompt fields (carried forward from Stage 3.1)
    -----------------------------------------------
    style_prompt:
        Style/vibe description forwarded to the provider.  ``None`` for
        EDENN_BASIC.
    lyrics_prompt:
        Lyric content guidance.  ``None`` for EDENN_BASIC or instrumental.
    combined_prompt:
        Single ProviderA-format prompt for EDENN_BASIC.  ``None`` for others.

    Output fields
    -------------
    music_filename:
        Base filename of the trimmed/matched music file written to the temp
        folder.  Stored relative (no absolute path) for portability.
    complete_music_filename:
        Base filename of the full-length generated audio (before trimming).
    has_lyrics:
        ``True`` when at least one lyrics timestamp was returned.
    full_lyrics_text:
        Raw lyrics text of the primary track.  ``None`` for instrumental.
    line_timestamp_count:
        Number of line-level lyric timestamp entries.
    word_timestamp_count:
        Number of word-level lyric timestamp entries.
    matching_used_track:
        Label of the track selected by the music matching stage
        (``"primary"`` or ``"secondary"``).

    Video context fields
    --------------------
    video_duration_s:
        Duration of the input video in seconds.  Music generation targets this
        length; deviations indicate extension rounds.
    video_category:
        Category inferred by Stage 0 (``"ADVERTISEMENT"``, ``"VLOG"``, etc.).
    scene_count:
        Number of scenes detected in the input video.
    overall_mood:
        Video-level mood string from Stage 3, carried here for convenience.

    Performance fields
    ------------------
    generation_latency_s:
        Wall-clock seconds the music generation stage took.  ``None`` if not
        instrumented.
    extension_rounds:
        Number of track-extension rounds performed before the music met the
        required duration.  0 for first-try success.
    """

    event_type: str = "music_generation"

    # Provider
    model_spec: str = ""
    provider_name: str = ""
    task_id: Optional[str] = None
    audio_id: Optional[str] = None

    # Generation config
    include_vocals: bool = False
    vocal_gender: str = ""
    vocal_id_used: Optional[str] = None
    lyrics_language: Optional[str] = None

    # Prompts (carried from Stage 3.1)
    style_prompt: Optional[str] = None
    lyrics_prompt: Optional[str] = None
    combined_prompt: Optional[str] = None

    # Output paths (relative filenames only — no absolute paths)
    music_filename: Optional[str] = None
    complete_music_filename: Optional[str] = None

    # Lyrics
    has_lyrics: bool = False
    full_lyrics_text: Optional[str] = None
    line_timestamp_count: int = 0
    word_timestamp_count: int = 0

    # Matching
    matching_used_track: Optional[str] = None

    # Video context
    video_duration_s: float = 0.0
    video_category: str = ""
    scene_count: int = 0
    overall_mood: str = ""

    # Performance
    generation_latency_s: Optional[float] = None
    extension_rounds: int = 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @classmethod
    def provider_name_for_spec(cls, model_spec: str) -> str:
        """
        Return the human-readable provider name for a normalised model spec string.

        Parameters
        ----------
        model_spec:
            One of ``"edenn_basic"``, ``"edenn_enhanced"``, ``"edenn_studio"``.

        Returns
        -------
        str
            ``"provider_a"``, ``"provider_b"``, ``"provider_c"``, or ``"unknown"`` if the
            spec is not recognised.
        """
        mapping = {
            "edenn_basic": "provider_a",
            "edenn_enhanced": "provider_b",
            "edenn_studio": "provider_c",
        }
        return mapping.get(model_spec.lower().strip(), "unknown")
