"""
Genre hierarchy — two-level controlled vocabulary for fast retrieval.

L1 is the coarse filter applied first (B-tree equality scan).
L2 is the mid-tier sub-genre applied second (B-tree equality scan).
genre_tags remains a free-text overflow for fine-grained or cross-genre signals.

Retrieval strategy:
    1. Hard filter on (genre_level1, genre_level2) — index seek.
    2. If bucket too small, widen to genre_level1 siblings.
    3. Rank within bucket by embedding similarity.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, List

# ---------------------------------------------------------------------------
# The hierarchy
# ---------------------------------------------------------------------------

GENRE_HIERARCHY: Dict[str, List[str]] = {
    "Electronic": [
        "Ambient Electronic",
        "House",
        "Techno",
        "Trap / Bass",
        "Synthwave",
        "EDM",
        "Drum and Bass",
    ],
    "Rock": [
        "Indie Rock",
        "Alternative Rock",
        "Metal",
        "Classic Rock",
        "Punk",
        "Post-Rock",
    ],
    "Pop": [
        "Mainstream Pop",
        "Indie Pop",
        "K-Pop",
        "Dance Pop",
        "Bedroom Pop",
    ],
    "Hip-Hop / R&B": [
        "Trap",
        "Lo-fi Hip-Hop",
        "R&B",
        "Conscious Hip-Hop",
        "Drill",
    ],
    "Folk / Country": [
        "Acoustic Folk",
        "Singer-Songwriter",
        "Bluegrass",
        "Country Pop",
        "Americana",
    ],
    "Jazz / Blues": [
        "Jazz",
        "Blues",
        "Soul",
        "Neo-Soul",
        "Bossa Nova",
    ],
    "Classical / Orchestral": [
        "Cinematic",
        "Minimalist",
        "Baroque",
        "Contemporary Classical",
    ],
    "World": [
        "Latin",
        "Afrobeats",
        "Reggae",
        "Celtic",
        "Arabic",
        "Indian Classical",
    ],
    "Ambient / Experimental": [
        "Dark Ambient",
        "Drone",
        "Noise",
        "Glitch",
        "Shoegaze",
    ],
    "Other": [
        "Children",
        "Novelty",
    ],
}

# Flat lists for JSON schema enum constraints
GENRE_L1_VALUES: List[str] = sorted(GENRE_HIERARCHY.keys()) + ["Unknown"]
GENRE_L2_VALUES: List[str] = sorted(
    {l2 for children in GENRE_HIERARCHY.values() for l2 in children}
) + ["Unknown"]

# Fast lookup: L2 value → its canonical L1 parent
L2_TO_L1: Dict[str, str] = {
    l2: l1
    for l1, children in GENRE_HIERARCHY.items()
    for l2 in children
}

_VALID_L1: FrozenSet[str] = frozenset(GENRE_L1_VALUES)
_VALID_L2: FrozenSet[str] = frozenset(GENRE_L2_VALUES)


def validate_genre_level1(value: str) -> str:
    """Return *value* if it is a valid L1 label, otherwise ``"Unknown"``."""
    return value if value in _VALID_L1 else "Unknown"


def validate_genre_level2(value: str) -> str:
    """Return *value* if it is a valid L2 label, otherwise ``"Unknown"``."""
    return value if value in _VALID_L2 else "Unknown"


def l1_for_l2(genre_level2: str) -> str:
    """Return the canonical L1 parent of *genre_level2*, or ``"Unknown"``."""
    return L2_TO_L1.get(genre_level2, "Unknown")


def l2_children(genre_level1: str) -> List[str]:
    """Return all valid L2 values for *genre_level1*, or empty list."""
    return list(GENRE_HIERARCHY.get(genre_level1, []))


# ---------------------------------------------------------------------------
# Human-readable hierarchy string — injected into LLM system prompt
# ---------------------------------------------------------------------------

def hierarchy_prompt_block() -> str:
    """
    Return a compact multi-line string listing the full L1 → L2 mapping,
    suitable for embedding directly in a system prompt.
    """
    lines = ["Genre hierarchy (pick one L1 and its best matching L2):"]
    for l1, children in GENRE_HIERARCHY.items():
        lines.append(f"  {l1}: {', '.join(children)}")
    lines.append("  Unknown: (use when no genre is determinable)")
    return "\n".join(lines)
