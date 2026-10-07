"""Understanding in textual space (DESIGN.md §1, M2).

The planner only ever reads text + numbers. This module turns assets into
that text: video observations (the agentic-audio analysis shape) attach to
tree nodes in segmentation.attach_analysis_scenes; images get a caption via
an injectable captioner; entities are extracted deterministically (no new
models) for the coherence machinery.

Analysis itself is injectable — callers pass stored observations (as the W0
spike did) or an ``analyze_fn`` wired to ``preview_pre_generation`` at the
platform layer (M6). This module never imports the orchestrator.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Awaitable, Callable, Optional

from .domain import AssetRecord, SegmentTree
from .segmentation import attach_analysis_scenes, build_segment_tree

# Arousal keywords (proven in the W0 spike): how "hot" a segment reads.
_HIGH_AROUSAL = re.compile(
    r"explos|chaos|chaotic|panic|run|running|urgent|fast|intense|crowd|fire|"
    r"smoke|action|energetic|jump|dance|impact|rubble|collaps|debris|flee",
    re.I,
)
_LOW_AROUSAL = re.compile(
    r"calm|quiet|still|somber|slow|gentle|serene|sits|sitting|close-up|"
    r"portrait|stares|looking|peaceful|soft|empty|aftermath|mourn",
    re.I,
)


def arousal_score(text: str) -> float:
    """0 (calm) .. 1 (hot); 0.5 when the text says nothing either way."""

    hi = len(_HIGH_AROUSAL.findall(text))
    lo = len(_LOW_AROUSAL.findall(text))
    if hi == lo == 0:
        return 0.5
    return round(hi / (hi + lo), 3)


# Deterministic keyword extraction: lowercase tokens minus stopwords/verbs of
# speech, keep informative unigrams + adjacent bigrams by frequency.
_TOKEN = re.compile(r"[a-z][a-z\-']+")
_STOP = frozenset(
    """a an and are as at be but by for from has have in into is it its of on or
    that the their there this to was were with while over under near around
    appears appear seems seem shows show showing shown scene camera video view
    visible feel feels feeling mood looks look looking left right foreground
    background frame shot cut close up wide""".split()
)


def extract_entities(text: str, top_n: int = 8) -> list[str]:
    tokens = [t for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 2]
    if not tokens:
        return []
    unigrams = Counter(tokens)
    bigrams = Counter(
        f"{a} {b}" for a, b in zip(tokens, tokens[1:])
        if unigrams[a] > 1 and unigrams[b] > 1
    )
    ranked = [w for w, _ in (bigrams + unigrams).most_common(top_n * 2)]
    out: list[str] = []
    for w in ranked:  # drop unigrams already covered by a kept bigram
        if any(w != k and w in k for k in out):
            continue
        out.append(w)
        if len(out) >= top_n:
            break
    return out


CaptionFn = Callable[[AssetRecord], Awaitable[str]]


async def understand_asset(
    asset: AssetRecord,
    *,
    observation: Optional[dict[str, Any]] = None,
    caption_fn: Optional[CaptionFn] = None,
) -> SegmentTree:
    """AssetRecord -> SegmentTree with textual understanding attached.

    Video: level-1 tree from visual cuts ∪ ``observation`` scenes (the
    agentic-audio bootstrap shape), text attached by overlap. Image: single
    still leaf captioned via ``caption_fn`` (or ``meta['caption']``).
    """

    if asset.kind == "image":
        tree = build_segment_tree(asset)
        leaf = tree.root
        caption = str(asset.meta.get("caption") or "")
        if caption_fn is not None and not caption:
            caption = await caption_fn(asset)
        leaf.summary = caption
        leaf.mood = str(asset.meta.get("mood") or "")
        leaf.entities = extract_entities(leaf.text())
        return tree

    scenes = (observation or {}).get("scenes") or []
    tree = build_segment_tree(asset, analysis_scenes=scenes)
    # build_segment_tree attaches text when scenes exist; make sure the root
    # carries the global summary for inheritance into synthetic splits.
    root = tree.root
    if observation and not root.summary:
        root.summary = str(observation.get("video_summary", {}).get("overall_mood")
                           if isinstance(observation.get("video_summary"), dict)
                           else observation.get("video_description") or "")
        root.mood = str((observation.get("music_prompt") or {}).get("global_mood") or "")
        root.entities = extract_entities(root.text())
    return tree


def refresh_entities(tree: SegmentTree) -> None:
    """Recompute entities on nodes whose text changed (e.g. after a refine pass)."""

    for node in tree.nodes.values():
        if node.text():
            node.entities = extract_entities(node.text())
