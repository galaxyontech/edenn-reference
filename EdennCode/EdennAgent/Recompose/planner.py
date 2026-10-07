"""The planner: CutSpec slots -> RecomposePlan with structural coherence.

Coherence is enforced by construction, not scoring hope (DESIGN.md §1):

- passages align to the MusicSheet's phrases;
- ``coherence_mode`` decides each passage's candidate pool —
  ``single_story``: spine asset only, everywhere;
  ``spine_and_accents``: spine everywhere, the accent asset allowed ONLY on
  the first slot of a passage (accents land where the music breathes) and
  never in opening/hero/closing;
  ``interleave``: every asset everywhere (the W0 behavior, kept for A/B);
- a continuity term prefers entity overlap with the previous slot;
- ``knobs.locks`` pin slots to nodes across re-plans.

Selection is a deterministic scorer with an injectable one-turn LLM
refinement hook (``llm_pick_fn``) — the deterministic path is the fallback
and the test target; the LLM pass arrives with the agentic wiring (M6).
"""

from __future__ import annotations

from typing import Callable, Optional

from .domain import (
    AssetRecord,
    CutSpec,
    Knobs,
    MusicSheet,
    Passage,
    PlannedSlot,
    RecomposePlan,
    SegmentNode,
    SegmentTree,
)
from .segmentation import ensure_leaves_for
from .understanding import arousal_score

# score weights (W0-derived, continuity added)
W_FIT = 0.35
W_MOTION = 0.25
W_CONTINUITY = 0.20
W_NOVELTY = 0.10
SYNTHETIC_PENALTY = 0.08
DARK_HOT_PENALTY = 0.25
ROLE_BONUS = 0.25

# Offers stream: (slot_index, legal top-k candidates) — consumed by the LLM
# planning pass (llm_planner.py) to build its one-shot prompt.
OfferCollector = Callable[[int, list[SegmentNode]], None]
TOP_K_OFFERS = 5


class MaterialExhaustedError(RuntimeError):
    """Not enough unused footage for the requested density/window.

    Footage reuse is HARD-banned (owner directive 2026-07-15): once a cut is
    used it may never appear again. The agentic layer surfaces this as
    "reduce cut density or shorten the window"."""


def _entity_overlap(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb)


def _scene_ancestor(tree: SegmentTree, node: SegmentNode) -> str:
    """The node's visual family for anti-repetition: the SEMANTIC scene when
    the analysis attached one (fragments of one observation scene are the
    same family), else the level-1 fragment it descends from."""

    if node.scene_key:
        return node.scene_key
    current = node
    while current.level > 1 and current.parent_id:
        current = tree.nodes[current.parent_id]
    return current.scene_key or current.node_id


_REUSE_EPS_S = 0.05

# Owner-approved source ceiling (AGENTIC_CREATION.md, 2026-07-25): plans may
# pool material from up to this many assets. The machinery is N-source (slots
# carry per-asset provenance; exclusion/scene keys are per-asset) — the cap is
# a product decision, raised from the original two-asset MVP.
MAX_SOURCE_ASSETS = 4


def _free_window(
    node: SegmentNode,
    dur_s: float,
    used: dict[str, list[tuple[float, float]]],
) -> Optional[float]:
    """Earliest in-point inside ``node`` whose [in, in+dur] overlaps NO used
    footage of the asset. None when the node has no free window that long."""

    spans = sorted(used.get(node.asset_id) or [])
    cursor = node.start_s
    for a, b in spans:
        if a - cursor >= dur_s - _REUSE_EPS_S and cursor + dur_s <= node.end_s + _REUSE_EPS_S:
            return cursor
        cursor = max(cursor, b)
        if cursor >= node.end_s:
            return None
    if node.end_s - cursor >= dur_s - _REUSE_EPS_S:
        return cursor
    return None


def _node_arousal(node: SegmentNode) -> float:
    return arousal_score(node.text())


def plan_recompose(
    *,
    assets: list[AssetRecord],
    trees: dict[str, SegmentTree],
    sheet: MusicSheet,
    spec: CutSpec,
    hypothesis: str = "",
    spine_asset_id: Optional[str] = None,
    parent_plan_id: Optional[str] = None,
    offer_collector: Optional[OfferCollector] = None,
    pick_overrides: Optional[dict[int, str]] = None,
    exclude_intervals: Optional[dict[str, list[tuple[float, float]]]] = None,
) -> RecomposePlan:
    if not assets:
        raise ValueError("plan_recompose needs at least one asset")
    if len(assets) > MAX_SOURCE_ASSETS:
        raise ValueError(
            f"at most {MAX_SOURCE_ASSETS} source assets per plan "
            f"(owner-approved ceiling, AGENTIC_CREATION.md); got {len(assets)}")
    by_id = {a.asset_id: a for a in assets}
    spine = spine_asset_id or _pick_spine(assets, trees)
    if spine not in by_id:
        raise KeyError(f"spine asset not in assets: {spine}")
    accents = [a.asset_id for a in assets if a.asset_id != spine]

    knobs = spec.knobs
    bite_reservations: dict[int, tuple[str, float]] = {}
    if knobs.sound_bites > 0:
        pre_used = {aid: sorted(spans) for aid, spans in (exclude_intervals or {}).items()}
        bites = _select_sound_bites(spec, trees, spine, pre_used, knobs.sound_bites)
        bite_reservations = apply_sound_bites(spec, bites)
    passages = _build_passages(spec, sheet)
    plan = RecomposePlan(
        parent_plan_id=parent_plan_id,
        hypothesis=hypothesis,
        knobs=knobs,
        passages=passages,
        source_asset_ids=[a.asset_id for a in assets],
        spine_asset_id=spine,
    )

    used_nodes: set[str] = set()
    # Pre-consumed footage (e.g. slices already used by OTHER shorts cut from
    # the same long source) participates in the same hard no-reuse rule.
    used_intervals: dict[str, list[tuple[float, float]]] = {
        aid: sorted(spans) for aid, spans in (exclude_intervals or {}).items()
    }
    scene_serve_count: dict[str, int] = {}
    prev_scene: Optional[str] = None
    prev_node: Optional[SegmentNode] = None
    accent_used_in_passage: set[int] = set()

    for slot_spec in spec.slots:
        if slot_spec.index in bite_reservations:
            # Sound bite: the reserved speech span plays intact — no scoring,
            # no free-window recentering, source audio at full in assembly.
            node_id, seg_in = bite_reservations[slot_spec.index]
            node = _find_node(trees, node_id)
            planned = PlannedSlot(
                spec=slot_spec, node_id=node.node_id, asset_id=node.asset_id,
                seg_in_s=round(seg_in, 4), locked=True,
                why=f"[bite] source voice plays intact ({slot_spec.dur_s:.1f}s)",
            )
            plan.slots.append(planned)
            used_nodes.add(node.node_id)
            used_intervals.setdefault(node.asset_id, []).append(
                (planned.seg_in_s, planned.seg_in_s + slot_spec.dur_s))
            scene = _scene_ancestor(trees[node.asset_id], node)
            scene_serve_count[scene] = scene_serve_count.get(scene, 0) + 1
            prev_scene, prev_node = scene, node
            for p in passages:
                if p.index == slot_spec.passage_index:
                    p.slot_indices.append(slot_spec.index)
            continue

        allowed = _allowed_assets(
            slot_spec, knobs.coherence_mode, spine, accents,
            passages, accent_used_in_passage,
        )
        candidates: list[SegmentNode] = []
        for aid in allowed:
            tree = trees[aid]
            candidates.extend(
                ensure_leaves_for(tree, by_id[aid], slot_spec.dur_s)
            )
        # HARD no-reuse: a candidate is legal only with a free window (stills:
        # a single use, ever — a repeated frozen frame reads as repetition).
        # HARD anti-repetition: never the same level-1 scene as the previous
        # slot (adjacent sibling windows read as a stutter cut), and one scene
        # may serve at most knobs.max_slots_per_scene slots total.
        def _legal(n: SegmentNode) -> bool:
            if n.is_still:
                return n.node_id not in used_nodes
            if _free_window(n, slot_spec.dur_s, used_intervals) is None:
                return False
            scene = _scene_ancestor(trees[n.asset_id], n)
            if scene == prev_scene:
                return False
            return scene_serve_count.get(scene, 0) < knobs.max_slots_per_scene

        legal = [n for n in candidates if _legal(n)]
        if not legal:
            raise MaterialExhaustedError(
                f"slot {slot_spec.index} ({slot_spec.dur_s:.2f}s, role={slot_spec.role}): "
                f"no unused, non-repetitive footage left — reduce cut_density, shorten "
                f"the window, raise max_slots_per_scene, or add material"
            )

        scored = sorted(
            legal,
            key=lambda n: _score(n, slot_spec, prev_node, used_nodes, knobs),
            reverse=True,
        )
        if offer_collector is not None:
            offer_collector(slot_spec.index, scored[:TOP_K_OFFERS])

        locked_node_id = knobs.locks.get(slot_spec.index)
        override_id = (pick_overrides or {}).get(slot_spec.index)
        if locked_node_id:
            node = _find_node(trees, locked_node_id)
        elif override_id and any(n.node_id == override_id for n in legal):
            node = next(n for n in legal if n.node_id == override_id)
        else:
            node = scored[0]  # override missing/illegal -> auto-repair to best legal

        seg_in = node.start_s
        if not node.is_still:
            free = _free_window(node, slot_spec.dur_s, used_intervals)
            seg_in = free if free is not None else node.start_s
        pick_src = "det"
        if locked_node_id:
            pick_src = "locked"
        elif override_id:
            pick_src = "llm" if node.node_id == override_id else "llm-repaired"
        planned = PlannedSlot(
            spec=slot_spec,
            node_id=node.node_id,
            asset_id=node.asset_id,
            seg_in_s=round(seg_in, 4),
            locked=bool(locked_node_id),
            why=(f"[{pick_src}] role={slot_spec.role} energy={slot_spec.energy:.2f} "
                 f"arousal={_node_arousal(node):.2f} "
                 f"{'accent' if node.asset_id != spine else 'spine'} "
                 f"mood='{node.mood[:40]}'"),
        )
        plan.slots.append(planned)
        used_nodes.add(node.node_id)
        if node.is_still:
            prev_scene = node.node_id
        else:
            used_intervals.setdefault(node.asset_id, []).append(
                (planned.seg_in_s, planned.seg_in_s + slot_spec.dur_s)
            )
            scene = _scene_ancestor(trees[node.asset_id], node)
            scene_serve_count[scene] = scene_serve_count.get(scene, 0) + 1
            prev_scene = scene
        prev_node = node
        if node.asset_id != spine:
            accent_used_in_passage.add(slot_spec.passage_index)
        for p in passages:
            if p.index == slot_spec.passage_index:
                p.slot_indices.append(slot_spec.index)

    _write_passage_notes(plan, trees)
    return plan


MIN_BITE_S = 2.5
MAX_BITE_S = 9.0
BITE_LEAD_IN_S = 0.15


def _select_sound_bites(
    spec: CutSpec,
    trees: dict[str, SegmentTree],
    spine: str,
    used_intervals: dict[str, list[tuple[float, float]]],
    n_bites: int,
) -> list[tuple[int, int, str, float, float]]:
    """Choose speech spans to preserve intact and the slot-runs they replace.

    Returns [(first_slot_idx, last_slot_idx, node_id, seg_in, bite_dur)].
    Anchors: opening third, the hero slot, closing third — the moments where
    the source's own voice earns its place. Runs of consecutive beat-aligned
    slots are merged to cover the bite, so the grid survives around it.
    """

    tree = trees[spine]
    if not tree.audio_activity:
        return []

    def taken(a: float, b: float) -> bool:
        for u0, u1 in used_intervals.get(spine, []):
            if min(b, u1) - max(a, u0) > 0.05:
                return True
        return False

    # Per-NODE carving: intersect each speech-flagged segment with the
    # activity spans and carve a bite window inside it. Continuous broadcast
    # commentary runs far longer than a bite, so the window starts at the
    # intersection onset; the tail is softened by the duck padding. Finding
    # interior sentence ends needs ASR — a known next step.
    TARGET_CARVE_S = 6.0
    candidates: list[tuple[float, float, str]] = []
    # ALL nodes, coarse first: lazy deepening may have split leaves below
    # sentence length, but an internal node is contiguous footage — and audio
    # continuity across an interior picture cut is fine (a J-cut).
    bite_nodes = sorted(
        (n for n in tree.nodes.values()
         if not n.is_still and n.level >= 1 and n.dur_s >= MIN_BITE_S
         and (n.speech or 0) >= 0.5),
        key=lambda n: (n.level, n.start_s),
    )
    for node in bite_nodes:
        for a, b in tree.audio_activity:
            ov_a, ov_b = max(a, node.start_s), min(b, node.end_s)
            if ov_b - ov_a < MIN_BITE_S or taken(ov_a, ov_b):
                continue
            end = ov_b if ov_b - ov_a <= MAX_BITE_S else ov_a + TARGET_CARVE_S
            candidates.append((ov_a, end, node.node_id))
            break  # one bite window per node is plenty
    if not candidates:
        return []

    hero_idx = next((s.index for s in spec.slots if s.role == "hero"),
                    len(spec.slots) // 2)
    total = len(spec.slots)
    anchors = [1, hero_idx, max(total - 3, 2)][:max(n_bites, 0)]

    bites: list[tuple[int, int, str, float, float]] = []
    used_slots: set[int] = set()
    used_bite_spans: list[tuple[float, float]] = []
    for anchor in anchors:
        if not candidates:
            break
        # pick the longest remaining span not colliding with chosen bites
        pool = [c for c in candidates
                if not any(min(c[1], b1) - max(c[0], b0) > 0.05
                           for b0, b1 in used_bite_spans)]
        if not pool:
            break
        a, b, node_id = max(pool, key=lambda c: c[1] - c[0])
        bite_dur = (b - a) + BITE_LEAD_IN_S
        # merge consecutive slots starting at the anchor until they cover it
        first = min(anchor, total - 1)
        while first in used_slots and first < total - 1:
            first += 1
        last, covered = first, 0.0
        while covered < bite_dur and last < total:
            if last in used_slots:
                break
            covered += spec.slots[last].dur_s
            last += 1
        last -= 1
        if covered < bite_dur - 0.05 or last < first:
            continue
        bites.append((first, last, node_id, max(a - BITE_LEAD_IN_S, 0.0), covered))
        used_slots.update(range(first, last + 1))
        used_bite_spans.append((a, b))
        candidates = [c for c in candidates if c[2] != node_id]
    return bites


def apply_sound_bites(spec: CutSpec, bites: list[tuple[int, int, str, float, float]]) -> dict[int, tuple[str, float]]:
    """Merge each bite's slot-run into ONE bite slot in the spec (in place).

    Returns {new_slot_index: (node_id, seg_in)} reservations for the planner.
    """

    if not bites:
        return {}
    replaced: dict[int, tuple[int, str, float, float]] = {
        first: (last, node_id, seg_in, dur) for first, last, node_id, seg_in, dur in bites
    }
    skip: set[int] = set()
    for first, last, *_ in bites:
        skip.update(range(first + 1, last + 1))

    new_slots: list = []
    reservations: dict[int, tuple[str, float]] = {}
    for slot in spec.slots:
        if slot.index in skip:
            continue
        if slot.index in replaced:
            last, node_id, seg_in, dur = replaced[slot.index]
            slot = slot.model_copy(update={
                "dur_s": round(dur, 4), "is_bite": True,
                "role": slot.role if slot.role != "body" else "body",
            })
            reservations[len(new_slots)] = (node_id, seg_in)
        slot = slot.model_copy(update={"index": len(new_slots)})
        new_slots.append(slot)
    spec.slots = new_slots
    return reservations


def probe_max_window(
    *,
    assets: list[AssetRecord],
    trees: dict[str, SegmentTree],
    sheet: MusicSheet,
    knobs: Knobs,
    spine_asset_id: Optional[str] = None,
    min_window_s: float = 8.0,
    grid_s: float = 2.0,
) -> tuple[Optional[float], int]:
    """Longest music window this material can fill under the hard rules.

    Dry deterministic plans on truncated sheets (no model calls, no renders)
    — the agent's negotiation data: "this footage supports an Xs cut at this
    density; want that, or sparser cuts / more material?" Returns
    (window_s or None, n_slots).
    """

    from .cutspec import generate_cut_spec
    from .musicsheet import truncate_sheet

    def feasible(window_s: float) -> Optional[int]:
        view = truncate_sheet(sheet, window_s)
        if len(view.beats) < 4:
            return None
        try:
            spec = generate_cut_spec(view, knobs)
            plan = plan_recompose(
                assets=assets, trees=trees, sheet=view, spec=spec,
                spine_asset_id=spine_asset_id,
            )
            return len(plan.slots)
        except (MaterialExhaustedError, ValueError):
            return None

    lo, hi = min_window_s, sheet.window_s
    if feasible(hi) is not None:
        return hi, feasible(hi) or 0
    best: Optional[float] = None
    best_slots = 0
    while hi - lo > grid_s:
        mid = (lo + hi) / 2
        n = feasible(mid)
        if n is not None:
            best, best_slots = mid, n
            lo = mid
        else:
            hi = mid
    if best is None:
        n = feasible(min_window_s)
        if n is not None:
            return min_window_s, n
        return None, 0
    return round(best, 1), best_slots


def _pick_spine(assets: list[AssetRecord], trees: dict[str, SegmentTree]) -> str:
    """Default spine = the asset with the most narrative material (video first,
    most level-1 leaves)."""

    def key(a: AssetRecord) -> tuple[int, int]:
        return (1 if a.kind == "video" else 0, len(trees[a.asset_id].leaves()))

    return max(assets, key=key).asset_id


def _build_passages(spec: CutSpec, sheet: MusicSheet) -> list[Passage]:
    indices = sorted({s.passage_index for s in spec.slots})
    out = []
    for idx in indices:
        ph = next((p for p in sheet.phrases if p.index == idx), None)
        out.append(Passage(
            index=idx,
            start_s=ph.start_s if ph else 0.0,
            end_s=ph.end_s if ph else 0.0,
            semantic_focus="",
        ))
    return out


def _allowed_assets(
    slot,
    mode: str,
    spine: str,
    accents: list[str],
    passages: list[Passage],
    accent_used_in_passage: set[int],
) -> list[str]:
    if mode == "single_story" or not accents:
        return [spine]
    if mode == "interleave":
        return [spine, *accents]
    # spine_and_accents: accent allowed only on the FIRST slot of a passage
    # that has not used its accent yet, and never on structural roles.
    passage = next((p for p in passages if p.index == slot.passage_index), None)
    is_passage_first = passage is not None and not passage.slot_indices
    if (
        is_passage_first
        and slot.role == "body"
        and slot.passage_index not in accent_used_in_passage
    ):
        return [spine, *accents]
    return [spine]


def _score(
    node: SegmentNode,
    slot,
    prev_node: Optional[SegmentNode],
    used_nodes: set[str],
    knobs: Optional[Knobs] = None,
) -> float:
    arousal = _node_arousal(node)
    fit = 1.0 - abs(arousal - slot.energy)
    motion = node.motion if node.motion is not None else 0.0
    # Luma-diff under-reads motion in dark footage (diffs scale with luma):
    # compensate against a mid-gray reference so underexposed action still
    # reads as action (found on a dark film transfer, 2026-07-15).
    if node.brightness is not None and 0 < node.brightness < 60.0:
        motion *= min(60.0 / max(node.brightness, 12.0), 3.0)
    motion_n = min(motion / 30.0, 1.0)  # W0 luma-diff scale: ~30 is very hot
    motion_term = motion_n if slot.energy >= 0.5 else 1.0 - motion_n
    # Continuity must be THEMATIC, not same-scene: sibling windows of one
    # scene share near-identical entities, so an unrestricted bonus rewarded
    # stutter cuts (repetition bug, 2026-07-15). Same-scene adjacency is
    # hard-banned in legality; the bonus only rewards cross-scene cohesion,
    # capped so identical-text siblings elsewhere don't dominate.
    continuity = 0.0
    if prev_node is not None and not prev_node.text_inherited:
        continuity = min(_entity_overlap(node.entities, prev_node.entities), 0.6)
    novelty = 0.0 if node.node_id in used_nodes else 1.0

    s = (W_FIT * fit + W_MOTION * motion_term
         + W_CONTINUITY * continuity + W_NOVELTY * novelty)
    if "synthetic_boundary" in node.quality_flags:
        s -= SYNTHETIC_PENALTY
    if "dark" in node.quality_flags and slot.energy >= 0.5:
        s -= DARK_HOT_PENALTY
    if slot.role == "opening":
        s += ROLE_BONUS * (1.0 - abs(arousal - 0.5))
    elif slot.role == "hero":
        s += ROLE_BONUS * motion_n
    elif slot.role == "closing":
        s += ROLE_BONUS * (1.0 - arousal)
    if node.is_still and slot.role != "body":
        s -= ROLE_BONUS  # stills are accents, not structural beats
    # Source-speech awareness (only matters when the original bed survives):
    # narration is audible where the music is quiet, and chopping a sentence
    # into a sub-1.2s slot is audible, period.
    if knobs is not None and knobs.source_audio == "duck" and node.speech:
        s += 0.15 * node.speech * (1.0 - slot.energy)
        if slot.dur_s < 1.2:
            s -= 0.20 * node.speech
    return s


def _find_node(trees: dict[str, SegmentTree], node_id: str) -> SegmentNode:
    for tree in trees.values():
        if node_id in tree.nodes:
            return tree.nodes[node_id]
    raise KeyError(f"node not found in any tree: {node_id}")


def _write_passage_notes(plan: RecomposePlan, trees: dict[str, SegmentTree]) -> None:
    """Deterministic focus/arc notes from what was actually chosen."""

    for passage in plan.passages:
        chosen = [s for s in plan.slots if s.spec.passage_index == passage.index]
        if not chosen:
            continue
        ents: list[str] = []
        for s in chosen:
            ents.extend(_find_node(trees, s.node_id).entities[:3])
        seen: list[str] = []
        for e in ents:
            if e not in seen:
                seen.append(e)
        passage.semantic_focus = ", ".join(seen[:4])
        n_accent = sum(1 for s in chosen if s.asset_id != plan.spine_asset_id)
        passage.arc_note = (
            f"{len(chosen)} shots on {passage.semantic_focus or 'the spine'}"
            + (f", {n_accent} accent" if n_accent else "")
        )
