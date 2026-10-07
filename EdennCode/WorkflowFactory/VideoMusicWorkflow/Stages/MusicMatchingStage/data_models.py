from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class VideoEvent:
    """Salient timepoints in VIDEO timeline."""
    t: float
    weight: float
    kind: str  # "motion" | "fallback" | "cut" etc.


@dataclass(frozen=True)
class MusicEvent:
    """Salient timepoints in MUSIC timeline."""
    t: float
    weight: float
    kind: str  # "onset" | "line_start" | "section_start"


@dataclass(frozen=True)
class WindowScore:
    """One candidate window in the music that overlays the full video length."""
    music_start_s: float
    music_end_s: float
    score: float
    details: Dict[str, Any]
