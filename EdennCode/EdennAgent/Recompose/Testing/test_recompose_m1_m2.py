"""M1+M2 tests: domain objects, SegmentTree build/lazy-split, signals,
textual understanding. Media fixtures are tiny real videos rendered with
ffmpeg (two visually distinct halves so scene detection has a boundary)."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from EdennCode.EdennAgent.Recompose.domain import (
    AssetRecord,
    Knobs,
    MusicSheet,
    RecomposePlan,
    SegmentTree,
)
from EdennCode.EdennAgent.Recompose.segmentation import (
    MIN_LEAF_S,
    build_segment_tree,
    ensure_leaves_for,
    split_node,
)
from EdennCode.EdennAgent.Recompose.signals import annotate_tree_signals
from EdennCode.EdennAgent.Recompose.understanding import (
    extract_entities,
    understand_asset,
)


# ------------------------------------------------------------------ fixtures
def _render_two_scene_video(dest: Path, half_s: float = 3.0) -> Path:
    """testsrc (moving) then a flat gray card (static) — one hard cut."""

    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc=duration={half_s}:size=320x240:rate=24",
         "-f", "lavfi", "-i", f"color=c=gray:duration={half_s}:size=320x240:rate=24",
         "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]",
         "-pix_fmt", "yuv420p", str(dest)],
        check=True, capture_output=True, timeout=120,
    )
    return dest


def _render_image(dest: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=duration=0.05:size=320x240:rate=24",
         "-frames:v", "1", str(dest)],
        check=True, capture_output=True, timeout=60,
    )
    return dest


@pytest.fixture(scope="module")
def two_scene_asset(tmp_path_factory) -> AssetRecord:
    path = _render_two_scene_video(tmp_path_factory.mktemp("m1") / "two_scene.mp4")
    return AssetRecord(kind="video", path=str(path), duration_s=6.0, width=320, height=240, fps=24)


@pytest.fixture(scope="module")
def image_asset(tmp_path_factory) -> AssetRecord:
    path = _render_image(tmp_path_factory.mktemp("m1img") / "still.png")
    return AssetRecord(kind="image", path=str(path), meta={"caption": "colorful test pattern card"})


FAKE_SCENES = [
    {"start_timestamp": 0.0, "end_timestamp": 3.0,
     "visual_summary": "A colorful animated test pattern with a moving bar.",
     "key_actions": "bar sweeps across the pattern", "mood": "energetic"},
    {"start_timestamp": 3.0, "end_timestamp": 6.0,
     "visual_summary": "A flat gray card with no movement at all.",
     "key_actions": "nothing moves", "mood": "calm and still"},
]


# -------------------------------------------------------------------- domain
def test_domain_round_trips_json() -> None:
    plan = RecomposePlan(hypothesis="test", knobs=Knobs(cut_density="sparse"))
    again = RecomposePlan.model_validate_json(plan.model_dump_json())
    assert again.plan_id == plan.plan_id
    assert again.knobs.cut_density == "sparse"
    sheet = MusicSheet(track_path="t.wav", tempo_bpm=120.0, duration_s=60.0,
                       window_start_s=0.5, window_s=45.0, beats=[0.5, 1.0], beat_energy=[0.1, 0.9])
    assert MusicSheet.model_validate_json(sheet.model_dump_json()).beats == [0.5, 1.0]


def test_knobs_reject_unknown_density() -> None:
    with pytest.raises(Exception):
        Knobs(cut_density="frantic")  # type: ignore[arg-type]


def test_add_children_rejects_double_split(two_scene_asset: AssetRecord) -> None:
    tree = build_segment_tree(two_scene_asset)
    parent = tree.root
    if not parent.child_ids:  # ensure a split exists to double-split
        tree.add_children(parent.node_id, [3.0])
    with pytest.raises(ValueError):
        tree.add_children(tree.root_id, [2.0])


# -------------------------------------------------------------- segmentation
def test_level1_finds_the_visual_cut(two_scene_asset: AssetRecord) -> None:
    tree = build_segment_tree(two_scene_asset)
    leaves = tree.leaves()
    assert len(leaves) >= 2, "hard cut at 3.0s should split the root"
    boundary = leaves[0].end_s
    assert 2.5 <= boundary <= 3.5


def test_semantic_scenes_attach_by_overlap(two_scene_asset: AssetRecord) -> None:
    tree = build_segment_tree(two_scene_asset, analysis_scenes=FAKE_SCENES)
    first, last = tree.leaves()[0], tree.leaves()[-1]
    assert "test pattern" in first.summary
    assert "gray card" in last.summary
    assert first.entities and not first.text_inherited


def test_split_node_inherits_text_and_respects_min_leaf(two_scene_asset: AssetRecord) -> None:
    tree = build_segment_tree(two_scene_asset, analysis_scenes=FAKE_SCENES)
    leaf = tree.leaves()[0]
    child_ids = split_node(tree, two_scene_asset, leaf.node_id, target_dur_s=1.0)
    if child_ids:  # detection may find nothing inside a synthetic clip -> synthetic split
        for cid in child_ids:
            child = tree.node(cid)
            assert child.dur_s >= MIN_LEAF_S - 1e-6
            assert child.summary == leaf.summary and child.text_inherited
            assert child.level == leaf.level + 1


def test_ensure_leaves_deepens_on_demand(two_scene_asset: AssetRecord) -> None:
    tree = build_segment_tree(two_scene_asset)
    before = len(tree.leaves())
    usable = ensure_leaves_for(tree, two_scene_asset, target_dur_s=0.8)
    assert usable, "must return usable leaves"
    assert all(n.is_still or n.dur_s >= 0.8 for n in usable)
    assert len(tree.leaves()) >= before  # deepening never removes leaves


def test_no_demand_no_deepening(two_scene_asset: AssetRecord) -> None:
    """Owner requirement: dial density down -> segmentation stays coarse."""

    tree = build_segment_tree(two_scene_asset)
    before = {n.node_id for n in tree.leaves()}
    ensure_leaves_for(tree, two_scene_asset, target_dur_s=2.5)
    after = {n.node_id for n in tree.leaves()}
    assert before == after, "coarse targets must not deepen the tree"


def test_image_tree_is_single_still_leaf(image_asset: AssetRecord) -> None:
    tree = build_segment_tree(image_asset)
    leaves = tree.leaves()
    assert len(leaves) == 1 and leaves[0].is_still


# ------------------------------------------------------------------- signals
def test_signals_separate_motion_from_stillness(two_scene_asset: AssetRecord) -> None:
    tree = build_segment_tree(two_scene_asset)
    measured = annotate_tree_signals(tree, two_scene_asset, max_workers=2)
    assert measured >= 2
    moving, still = tree.leaves()[0], tree.leaves()[-1]
    assert moving.motion is not None and still.motion is not None
    assert moving.motion > still.motion, "testsrc must out-move a flat card"
    assert still.brightness is not None


# ------------------------------------------------------------- understanding
def test_extract_entities_keeps_subjects_drops_stopwords() -> None:
    ents = extract_entities(
        "A young child walks through rubble past a damaged building. "
        "The child looks at the camera near the damaged building."
    )
    assert any("child" in e for e in ents)
    assert any("building" in e or "rubble" in e for e in ents)
    assert not any(e in {"the", "a", "camera", "looks"} for e in ents)


def test_understand_video_uses_observation(two_scene_asset: AssetRecord) -> None:
    obs = {"scenes": FAKE_SCENES, "video_description": "test video",
           "music_prompt": {"global_mood": "calm"}}
    tree = asyncio.run(understand_asset(two_scene_asset, observation=obs))
    assert any("test pattern" in n.summary for n in tree.leaves())


def test_understand_image_uses_caption(image_asset: AssetRecord) -> None:
    tree = asyncio.run(understand_asset(image_asset))
    leaf = tree.leaves()[0]
    assert "test pattern" in leaf.summary
    assert leaf.entities
