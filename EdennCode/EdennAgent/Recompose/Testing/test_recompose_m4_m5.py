"""M4+M5 tests: planner coherence rules + assembly with slot caching."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from EdennCode.EdennAgent.Recompose.assembly import render_variant
from EdennCode.EdennAgent.Recompose.cutspec import generate_cut_spec
from EdennCode.EdennAgent.Recompose.domain import AssetRecord, Knobs
from EdennCode.EdennAgent.Recompose.musicsheet import build_music_sheet
from EdennCode.EdennAgent.Recompose.planner import plan_recompose
from EdennCode.EdennAgent.Recompose.segmentation import build_segment_tree
from EdennCode.EdennAgent.Recompose.signals import annotate_tree_signals

from .test_recompose_m1_m2 import (
    FAKE_SCENES,
    _render_image,
    _render_two_scene_video,
)


def _render_multi_scene_video(dest: Path, scene_s: float = 4.5) -> Path:
    """Eight visually distinct 4.5s scenes — long enough for the largest
    sparse slot (~4s + headroom) and varied enough that the hard
    anti-repetition rules (no same-scene adjacency, per-scene cap 2) can fill
    a ~20s window at DEFAULT knobs without exhausting."""

    srcs = ["testsrc", "color=c=gray", "testsrc2", "color=c=darkred",
            "smptebars", "color=c=navy", "rgbtestsrc", "color=c=darkgreen"]
    args: list[str] = []
    for s in srcs:
        if s.startswith("color"):
            base = f"{s}:duration={scene_s}:size=320x240:rate=24"
        else:
            base = f"{s}=duration={scene_s}:size=320x240:rate=24"
        args += ["-f", "lavfi", "-i", base]
    filt = "".join(f"[{i}:v]" for i in range(len(srcs))) + f"concat=n={len(srcs)}:v=1[v]"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", *args,
         "-filter_complex", filt, "-map", "[v]",
         "-pix_fmt", "yuv420p", str(dest)],
        check=True, capture_output=True, timeout=120,
    )
    return dest


MULTI_SCENE_TEXTS = [
    ("A colorful animated test pattern with a moving bar.", "energetic pattern"),
    ("A flat gray card with no movement at all.", "calm stillness"),
    ("A second animated pattern with rolling counters.", "busy counters"),
    ("A deep red card, completely static.", "quiet red"),
    ("Broadcast color bars, static reference image.", "neutral bars"),
    ("A navy blue card, completely static.", "calm blue"),
    ("An RGB gradient test chart with color sweeps.", "vivid gradients"),
    ("A dark green card, completely static.", "muted green"),
]


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    root = tmp_path_factory.mktemp("m45")
    # 24s spine, 8 distinct scenes: the ~20s music window must be fillable
    # WITHOUT footage reuse AND without violating scene caps/adjacency at
    # DEFAULT knobs (max_slots_per_scene=2).
    video_path = _render_multi_scene_video(root / "spine.mp4")
    image_path = _render_image(root / "accent.png")
    # 24s pulse track (same recipe as M3, audible throughout, loud middle)
    track = root / "pulse.wav"
    expr = ("(0.55 + 0.45*between(t,8,16))*sin(2*PI*880*t)*lt(mod(t,0.5),0.08)"
            " + 0.02*sin(2*PI*110*t)")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"aevalsrc='{expr}':d=24:s=22050", str(track)],
        check=True, capture_output=True, timeout=60)

    spine = AssetRecord(kind="video", path=str(video_path), duration_s=36.0)
    accent = AssetRecord(kind="image", path=str(image_path),
                         meta={"caption": "still photo of the test pattern"})
    scenes = [
        {"scene_index": i,
         "start_timestamp": i * 4.5, "end_timestamp": (i + 1) * 4.5,
         "visual_summary": summary, "key_actions": actions,
         "mood": "energetic" if i % 2 == 0 else "calm and still"}
        for i, (summary, actions) in enumerate(MULTI_SCENE_TEXTS)
    ]
    trees = {
        spine.asset_id: build_segment_tree(spine, analysis_scenes=scenes),
        accent.asset_id: build_segment_tree(accent),
    }
    accent_leaf = trees[accent.asset_id].root
    accent_leaf.summary = "still photo of the test pattern"
    annotate_tree_signals(trees[spine.asset_id], spine, max_workers=2)
    sheet = build_music_sheet(str(track), window_s=20.0)
    return {"spine": spine, "accent": accent, "trees": trees,
            "sheet": sheet, "workdir": root}


def _plan(media, knobs: Knobs, **kwargs):
    spec = generate_cut_spec(media["sheet"], knobs)
    return plan_recompose(
        assets=[media["spine"], media["accent"]],
        trees=media["trees"],
        sheet=media["sheet"],
        spec=spec,
        spine_asset_id=media["spine"].asset_id,
        **kwargs,
    ), spec


# --------------------------------------------------------------------- planner
def test_single_story_uses_only_the_spine(media) -> None:
    plan, _ = _plan(media, Knobs(coherence_mode="single_story", cut_density="sparse"))
    assert all(s.asset_id == media["spine"].asset_id for s in plan.slots)


def test_spine_and_accents_limits_accent_placement(media) -> None:
    plan, _ = _plan(media, Knobs(coherence_mode="spine_and_accents", cut_density="medium"))
    accents = [s for s in plan.slots if s.asset_id == media["accent"].asset_id]
    # never on structural roles
    assert all(s.spec.role == "body" for s in accents)
    # at most one accent per passage
    per_passage: dict[int, int] = {}
    for s in accents:
        per_passage[s.spec.passage_index] = per_passage.get(s.spec.passage_index, 0) + 1
    assert all(v <= 1 for v in per_passage.values())


def test_every_slot_has_lineage_and_why(media) -> None:
    plan, spec = _plan(media, Knobs(cut_density="medium"))
    assert len(plan.slots) == len(spec.slots)
    for s in plan.slots:
        assert s.node_id and s.asset_id and s.why
    # passages got notes and cover all slots
    covered = sorted(i for p in plan.passages for i in p.slot_indices)
    assert covered == [s.spec.index for s in plan.slots]
    assert all(p.arc_note for p in plan.passages if p.slot_indices)


def test_locks_pin_nodes_across_replans(media) -> None:
    plan_a, _ = _plan(media, Knobs(cut_density="medium"))
    pinned = plan_a.slots[1].node_id
    knobs = Knobs(cut_density="medium", locks={1: pinned})
    plan_b, _ = _plan(media, knobs)
    assert plan_b.slots[1].node_id == pinned and plan_b.slots[1].locked


def test_rejects_more_than_max_source_assets(media) -> None:
    """The owner-approved ceiling is 4 sources (AGENTIC_CREATION.md); a fifth
    is rejected. N-source acceptance up to the cap is covered in
    Testing/test_creation_domain.py."""

    from EdennCode.EdennAgent.Recompose.planner import MAX_SOURCE_ASSETS

    extras = [AssetRecord(kind="image", path=media["accent"].path)
              for _ in range(MAX_SOURCE_ASSETS - 1)]
    with pytest.raises(ValueError, match=f"at most {MAX_SOURCE_ASSETS}"):
        plan_recompose(
            assets=[media["spine"], media["accent"], *extras],
            trees=media["trees"], sheet=media["sheet"],
            spec=generate_cut_spec(media["sheet"], Knobs()),
        )


def _overlapping(plan) -> list[tuple[int, int]]:
    """Pairs of slots whose source footage overlaps (same asset, intersecting
    [seg_in, seg_in+dur] windows) — must ALWAYS be empty (hard no-reuse)."""

    bad = []
    for i, a in enumerate(plan.slots):
        for j, b in enumerate(plan.slots[i + 1:], start=i + 1):
            if a.asset_id != b.asset_id:
                continue
            a0, a1 = a.seg_in_s, a.seg_in_s + a.spec.dur_s
            b0, b1 = b.seg_in_s, b.seg_in_s + b.spec.dur_s
            if min(a1, b1) - max(a0, b0) > 0.08:
                bad.append((a.spec.index, b.spec.index))
    return bad


def test_hard_no_reuse_across_all_densities(media) -> None:
    for density in ("sparse", "medium", "dense"):
        plan, _ = _plan(media, Knobs(cut_density=density, coherence_mode="interleave",
                                     max_slots_per_scene=6))
        assert _overlapping(plan) == [], f"footage reused at {density}"
        stills = [s for s in plan.slots if s.asset_id == media["accent"].asset_id]
        assert len(stills) <= 1, "a still may be used at most once, ever"


def test_no_same_scene_adjacency_and_scene_cap(media) -> None:
    """The repetition bug (2026-07-15): sibling windows of one scene must not
    play back-to-back, and one scene may serve at most max_slots_per_scene."""

    from EdennCode.EdennAgent.Recompose.planner import _scene_ancestor

    plan, _ = _plan(media, Knobs(cut_density="dense", coherence_mode="interleave",
                                 max_slots_per_scene=6))
    scenes = []
    for s in plan.slots:
        tree = media["trees"][s.asset_id]
        node = tree.nodes[s.node_id]
        scenes.append(node.node_id if node.is_still else _scene_ancestor(tree, node))
    for a, b in zip(scenes, scenes[1:]):
        assert a != b, "two consecutive slots came from the same scene"

    plan2, _ = _plan(media, Knobs(cut_density="sparse", coherence_mode="interleave",
                                  max_slots_per_scene=2))
    counts: dict[str, int] = {}
    for s in plan2.slots:
        tree = media["trees"][s.asset_id]
        node = tree.nodes[s.node_id]
        if node.is_still:
            continue
        key = _scene_ancestor(tree, node)
        counts[key] = counts.get(key, 0) + 1
    assert all(v <= 2 for v in counts.values()), counts


def test_offers_are_collected_per_slot(media) -> None:
    from EdennCode.EdennAgent.Recompose.cutspec import generate_cut_spec
    from EdennCode.EdennAgent.Recompose.planner import plan_recompose

    offers = {}
    spec = generate_cut_spec(media["sheet"], Knobs(cut_density="sparse"))
    plan_recompose(
        assets=[media["spine"], media["accent"]], trees=media["trees"],
        sheet=media["sheet"], spec=spec,
        spine_asset_id=media["spine"].asset_id,
        offer_collector=lambda i, cands: offers.__setitem__(i, cands),
    )
    assert set(offers) == {s.index for s in spec.slots}
    assert all(cands for cands in offers.values())


def test_pick_override_honored_and_illegal_repaired(media) -> None:
    plan_a, spec = _plan(media, Knobs(cut_density="sparse"))
    # legal override for slot 1: a different node from slot 1's legal pool
    offers = {}
    from EdennCode.EdennAgent.Recompose.planner import plan_recompose
    plan_recompose(
        assets=[media["spine"], media["accent"]], trees=media["trees"],
        sheet=media["sheet"], spec=spec, spine_asset_id=media["spine"].asset_id,
        offer_collector=lambda i, c: offers.__setitem__(i, c),
    )
    alt = next((n.node_id for n in offers[1] if n.node_id != plan_a.slots[1].node_id), None)
    overrides = {1: alt} if alt else {}
    overrides[0] = "seg_does_not_exist"  # illegal -> auto-repair, never crash
    plan_b, _ = _plan(media, Knobs(cut_density="sparse"), pick_overrides=overrides)
    if alt:
        assert plan_b.slots[1].node_id == alt and "[llm]" in plan_b.slots[1].why
    assert plan_b.slots[0].node_id, "illegal override must repair to a legal pick"
    assert _overlapping(plan_b) == []


def test_exclude_intervals_keeps_shorts_distinct(media) -> None:
    """Long->short: a second short cut from the same source must not reuse
    the first short's footage (exclude_intervals joins the hard rule)."""

    from EdennCode.EdennAgent.Recompose.musicsheet import truncate_sheet

    knobs = Knobs(cut_density="sparse", coherence_mode="single_story",
                  max_slots_per_scene=3)
    sheet = truncate_sheet(media["sheet"], 10.0)  # two 10s shorts < 36s source
    spec = generate_cut_spec(sheet, knobs)
    plan_a = plan_recompose(
        assets=[media["spine"], media["accent"]], trees=media["trees"],
        sheet=sheet, spec=spec, spine_asset_id=media["spine"].asset_id,
    )
    used = {}
    for s in plan_a.slots:
        used.setdefault(s.asset_id, []).append((s.seg_in_s, s.seg_in_s + s.spec.dur_s))
    plan_b = plan_recompose(
        assets=[media["spine"], media["accent"]], trees=media["trees"],
        sheet=sheet, spec=spec, spine_asset_id=media["spine"].asset_id,
        exclude_intervals=used,
    )
    combined = plan_a.model_copy(deep=True)
    combined.slots = plan_a.slots + plan_b.slots
    assert _overlapping(combined) == [], "short B reused short A's footage"


def test_probe_max_window_finds_the_feasible_length(media, tmp_path) -> None:
    from EdennCode.EdennAgent.Recompose.planner import probe_max_window

    # Rich fixture: the full 20s window is feasible at defaults.
    w, n = probe_max_window(assets=[media["spine"], media["accent"]],
                            trees=media["trees"], sheet=media["sheet"],
                            knobs=Knobs(), spine_asset_id=media["spine"].asset_id)
    assert w == media["sheet"].window_s and n > 0

    # Scarce material: probe returns a SHORTER feasible window, not a crash.
    short_path = _render_two_scene_video(tmp_path / "short.mp4", half_s=3.0)
    short = AssetRecord(kind="video", path=str(short_path), duration_s=6.0)
    trees = {short.asset_id: build_segment_tree(short)}
    w2, n2 = probe_max_window(assets=[short], trees=trees, sheet=media["sheet"],
                              knobs=Knobs(coherence_mode="single_story"))
    assert w2 is None or w2 < media["sheet"].window_s


def test_material_exhaustion_raises_clearly(media, tmp_path) -> None:
    from EdennCode.EdennAgent.Recompose.planner import MaterialExhaustedError

    # A 6s spine cannot fill the ~19s window without reuse -> clear error.
    short_path = _render_two_scene_video(tmp_path / "short.mp4", half_s=3.0)
    short = AssetRecord(kind="video", path=str(short_path), duration_s=6.0)
    trees = {short.asset_id: build_segment_tree(short)}
    spec = generate_cut_spec(media["sheet"], Knobs(coherence_mode="single_story"))
    with pytest.raises(MaterialExhaustedError, match="reduce cut_density"):
        plan_recompose(assets=[short], trees=trees, sheet=media["sheet"], spec=spec)


def test_llm_planner_end_to_end_with_scripted_client(media) -> None:
    import asyncio
    from EdennCode.EdennAgent.Recompose.cutspec import generate_cut_spec
    from EdennCode.EdennAgent.Recompose.llm_planner import plan_recompose_llm
    from EdennCode.EdennAgent.Recompose.planner import plan_recompose

    spec = generate_cut_spec(media["sheet"], Knobs(cut_density="sparse"))
    offers = {}
    plan_recompose(
        assets=[media["spine"], media["accent"]], trees=media["trees"],
        sheet=media["sheet"], spec=spec, spine_asset_id=media["spine"].asset_id,
        offer_collector=lambda i, c: offers.__setitem__(i, c),
    )
    # Script: pick the LAST offer for slot 0 (legal), an invalid id for slot 1
    # (repair path), deterministic for the rest; write passage notes.
    scripted = {
        "slots": [
            {"index": 0, "node_id": offers[0][-1].node_id, "why": "stronger establishing look"},
            {"index": 1, "node_id": "seg_bogus", "why": "n/a"},
        ],
        "passages": [
            {"index": p, "semantic_focus": "test pattern story", "arc_note": "hold the pattern"}
            for p in sorted({s.passage_index for s in spec.slots})
        ],
    }

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def complete_messages(self, messages, *, json_schema, max_tokens=4000):
            self.calls.append((messages, json_schema))
            return scripted, {"total_tokens": 42}

    client = FakeClient()
    plan = asyncio.run(plan_recompose_llm(
        llm_client=client, assets=[media["spine"], media["accent"]],
        trees=media["trees"], sheet=media["sheet"], spec=spec,
        spine_asset_id=media["spine"].asset_id, hypothesis="test",
    ))
    assert len(client.calls) == 1, "exactly one model call per plan"
    assert plan.slots[0].node_id == offers[0][-1].node_id
    assert plan.slots[0].why == "[llm] stronger establishing look"
    assert plan.slots[1].node_id != "seg_bogus", "bogus pick must be repaired"
    assert _overlapping(plan) == []
    assert all(p.arc_note == "hold the pattern" for p in plan.passages if p.slot_indices)


# -------------------------------------------------------------------- assembly
def test_render_variant_end_to_end_with_cache(media) -> None:
    plan, _ = _plan(media, Knobs(cut_density="sparse"))
    variant = render_variant(plan, media["sheet"], [media["spine"], media["accent"]],
                             media["trees"], media["workdir"], label="t1")
    out = Path(variant.path)
    assert out.exists() and out.stat().st_size > 10_000
    rep = variant.cut_report
    assert rep is not None
    assert abs(rep.duration_s - sum(s.spec.dur_s for s in plan.slots)) < 0.5
    assert rep.cut_to_beat_ms_median is not None and rep.cut_to_beat_ms_median <= 40.0
    assert rep.cut_to_beat_ms_p95 <= 80.0

    # cache: re-render reuses every slot file (mtimes unchanged)
    mtimes = {p: Path(p).stat().st_mtime_ns for p in variant.slot_renders.values()}
    variant2 = render_variant(plan, media["sheet"], [media["spine"], media["accent"]],
                              media["trees"], media["workdir"], label="t2")
    for p in variant2.slot_renders.values():
        assert Path(p).stat().st_mtime_ns == mtimes.get(p, Path(p).stat().st_mtime_ns)
    assert set(variant2.slot_renders.values()) == set(variant.slot_renders.values())


def test_still_accent_renders_ken_burns(media) -> None:
    knobs = Knobs(coherence_mode="interleave", cut_density="sparse")
    plan, _ = _plan(media, knobs)
    if all(s.asset_id != media["accent"].asset_id for s in plan.slots):
        pytest.skip("selector never chose the still in this configuration")
    variant = render_variant(plan, media["sheet"], [media["spine"], media["accent"]],
                             media["trees"], media["workdir"], label="kb")
    assert Path(variant.path).exists()
