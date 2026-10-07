"""
TextSlot — typed container for a single piece of text to be enriched.

Each slot carries its semantic type so the extractor prompt can apply
different extraction rules per type without inspecting the text content.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class TextSlotType(str, Enum):
    """
    Semantic category of the text content held in a :class:`TextSlot`.

    The taxonomy extractor applies different extraction rules per type:

    * ``LYRICS`` — generated lyric text; yields ``lyric_themes``,
      ``lyric_sentiment``, mood reinforcement.
    * ``STYLE_PROMPT`` — musical style/genre/vibe description; yields
      ``genre_tags``, ``instrument_tags``, ``energy_level``, ``tempo_class``.
    * ``LYRICS_PROMPT`` — lyric content *direction* (not the lyrics themselves);
      yields ``theme_tags``, ``lyric_themes``.
    * ``GLOBAL_MUSIC_PROMPT`` — full enriched music prompt including tempo,
      instruments, and emotional arc; highest-signal slot for audio features.
    * ``SCENE_DESCRIPTION`` — concatenated per-scene LLM summaries; yields
      ``location_types``, ``subject_types``, ``motion_class``.
    * ``VIDEO_DESCRIPTION`` — overall video narrative; yields ``mood_tags``,
      ``activity_tags``, ``theme_tags``.
    """

    LYRICS = "lyrics"
    STYLE_PROMPT = "style_prompt"
    LYRICS_PROMPT = "lyrics_prompt"
    GLOBAL_MUSIC_PROMPT = "global_music_prompt"
    SCENE_DESCRIPTION = "scene_description"
    VIDEO_DESCRIPTION = "video_description"


@dataclass
class TextSlot:
    """
    A single named piece of text ready for LLM taxonomy extraction.

    Attributes
    ----------
    slot_type:
        Semantic category determining which extraction rules apply.
    text:
        The actual text content.  Must be non-empty; callers should filter
        before constructing.
    source_field:
        Dotted path to the originating field, e.g.
        ``"MusicGenerationEvent.full_lyrics_text"``.  Used for debugging
        and audit only.
    locale:
        BCP-47 locale hint (``"en"``, ``"zh-CN"``).  ``None`` means unknown.
        Passed to the extractor so it can adjust sentiment/theme analysis for
        non-English text.
    """

    slot_type: TextSlotType
    text: str
    source_field: str
    locale: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.text or not self.text.strip():
            raise ValueError(
                f"TextSlot.text must be non-empty (source_field={self.source_field!r})"
            )
