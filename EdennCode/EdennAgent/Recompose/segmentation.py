"""SegmentTree construction and lazy, demand-driven deepening.

Level 1 comes from visual scene detection (``detect_scene_cuts``) reconciled
with the analysis pipeline's semantic scenes. Deeper levels exist only where
the CutSpec demands shots shorter than the leaves that exist — segmentation
depth follows music x taste, never a constant (DESIGN.md §1).

Detection runs on the FULL file once per sensitivity level and is cached on
the tree (``cut_cache``), so lazy splits never re-run detection.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from EdennCode.Util.MediaUtils.ffmpeg_utils import detect_scene_cuts

from .domain import AssetRecord, SegmentNode, SegmentTree

logger = logging.getLogger(__name__)

MIN_LEAF_S = 0.5
BOUNDARY_MERGE_TOL_S = 0.25

# Sensitivity ladder for lazy deepening: level-1 uses the library defaults;
# deeper levels lower the content threshold so more cuts surface.
SENSITIVITY_LADDER: list[tuple[str, dict[str, Any]]] = [
    ("default", {}),
    ("fine", {"pyscene_method": "content", "pyscene_content_threshold": 12.0,
              "min_scene_len_s": 0.4}),
    ("hairline", {"pyscene_method": "content", "pyscene_content_threshold": 6.0,
                  "min_scene_len_s": 0.3}),
]


def _merged_boundaries(a: list[float], b: list[float], tol: float = BOUNDARY_MERGE_TOL_S) -> list[float]:
    """Union of two boundary lists, deduped within ``tol`` (earlier list wins)."""

    out: list[float] = []
    for t in sorted([*a, *b]):
        if not out or t - out[-1] > tol:
            out.append(t)
    return out


def _cached_cuts(tree: SegmentTree, asset: AssetRecord, level_name: str) -> list[float]:
    if level_name in tree.cut_cache:
        return tree.cut_cache[level_name]
    params = dict(SENSITIVITY_LADDER)[level_name]
    try:
        cuts = detect_scene_cuts(Path(asset.path), **params)
    except Exception as exc:  # noqa: BLE001 - detection is best-effort; splits fall back
        logger.warning("scene detection (%s) failed for %s: %s", level_name, asset.path, exc)
        cuts = []
    tree.cut_cache[level_name] = [round(float(c), 3) for c in cuts]
    return tree.cut_cache[level_name]


def build_segment_tree(
    asset: AssetRecord,
    analysis_scenes: Optional[list[dict[str, Any]]] = None,
) -> SegmentTree:
    """Root + level-1 scenes. Images become a single still leaf.

    ``analysis_scenes`` is the agentic-audio observation shape
    (start_timestamp/end_timestamp/visual_summary/key_actions/mood); its
    boundaries are merged with visual cuts and its text attached by overlap.
    """

    root = SegmentNode(
        asset_id=asset.asset_id,
        start_s=0.0,
        end_s=float(asset.duration_s or 0.0),
        level=0,
        is_still=asset.kind == "image",
    )
    tree = SegmentTree(asset_id=asset.asset_id, root_id=root.node_id, nodes={root.node_id: root})
    if asset.kind == "image":
        return tree

    visual = _cached_cuts(tree, asset, "default")
    semantic = []
    for sc in analysis_scenes or []:
        t = float(sc.get("start_timestamp") or 0.0)
        if t > 0:
            semantic.append(t)
    # Merge tolerance >= MIN_LEAF_S so adjacent visual+semantic boundaries can
    # never produce a leaf shorter than the minimum.
    boundaries = _merged_boundaries(visual, semantic, tol=max(BOUNDARY_MERGE_TOL_S, MIN_LEAF_S))
    boundaries = [b for b in boundaries if MIN_LEAF_S <= b <= root.end_s - MIN_LEAF_S]
    tree.add_children(root.node_id, boundaries)
    if analysis_scenes:
        attach_analysis_scenes(tree, analysis_scenes)
    return tree


def attach_analysis_scenes(tree: SegmentTree, scenes: list[dict[str, Any]]) -> None:
    """Attach semantic text to nodes by MAXIMUM temporal overlap."""

    from .understanding import extract_entities  # local import: no cycle at module load

    windows = []
    for sc in scenes:
        start = float(sc.get("start_timestamp") or 0.0)
        end = float(sc.get("end_timestamp") or 0.0)
        if end > start:
            windows.append((start, end, sc))
    for node in tree.nodes.values():
        best, best_overlap = None, 0.0
        for start, end, sc in windows:
            overlap = min(node.end_s, end) - max(node.start_s, start)
            if overlap > best_overlap:
                best, best_overlap = sc, overlap
        if best is None:
            continue
        node.summary = str(best.get("visual_summary") or "")
        node.key_actions = str(best.get("key_actions") or "")
        node.mood = str(best.get("mood") or "")
        node.entities = extract_entities(node.text())
        node.text_inherited = False
        # Semantic-scene identity: fragments of one observation scene are the
        # SAME visual family for anti-repetition purposes.
        node.scene_key = f"obs_scene_{best.get('scene_index', id(best))}"


def split_node(
    tree: SegmentTree,
    asset: AssetRecord,
    node_id: str,
    target_dur_s: float,
) -> list[str]:
    """Lazily deepen ``node`` until its pieces approach ``target_dur_s``.

    Ladder: cached finer scene detection within the node window; if detection
    yields nothing, uniform bisection to the target (flagged in
    quality_flags so the planner can deprioritize synthetic boundaries).
    Never creates leaves shorter than MIN_LEAF_S.
    """

    node = tree.node(node_id)
    if node.child_ids:
        return node.child_ids
    if node.is_still or node.dur_s <= max(target_dur_s, MIN_LEAF_S) * 1.5:
        return []

    for level_name, _ in SENSITIVITY_LADDER[1:]:
        cuts = [
            c for c in _cached_cuts(tree, asset, level_name)
            if node.start_s + MIN_LEAF_S <= c <= node.end_s - MIN_LEAF_S
        ]
        if cuts:
            return tree.add_children(node_id, cuts)

    # Synthetic fallback: uniform split toward the target duration.
    pieces = max(2, round(node.dur_s / max(target_dur_s, MIN_LEAF_S)))
    step = node.dur_s / pieces
    if step < MIN_LEAF_S:
        pieces = int(node.dur_s // MIN_LEAF_S)
        if pieces < 2:
            return []
        step = node.dur_s / pieces
    boundaries = [node.start_s + step * i for i in range(1, pieces)]
    child_ids = tree.add_children(node_id, boundaries)
    for cid in child_ids:
        tree.node(cid).quality_flags.append("synthetic_boundary")
    return child_ids


def ensure_leaves_for(
    tree: SegmentTree,
    asset: AssetRecord,
    target_dur_s: float,
    headroom_s: float = 0.2,
) -> list[SegmentNode]:
    """The demand loop: deepen for VARIETY, then return every fitting node.

    Candidates are any node — leaf or internal — long enough for the target
    (still leaves always qualify): an internal node is contiguous footage and
    can serve a slot even after its children exist. Deepening happens only
    when leaves are oversized relative to the target, so short slots get
    distinct sub-moments instead of the same long scene recentered. The
    planner is responsible for avoiding temporal overlap between picks.
    """

    need = target_dur_s + headroom_s
    for _ in range(len(SENSITIVITY_LADDER) + 1):  # bounded: ladder + synthetic
        oversized = [n for n in tree.leaves() if not n.is_still and n.dur_s > need * 3]
        if not oversized:
            break
        if not any(
            split_node(tree, asset, n.node_id, max(need, n.dur_s / 3))
            for n in oversized
        ):
            break
    return [n for n in tree.nodes.values() if n.is_still or n.dur_s >= need]
