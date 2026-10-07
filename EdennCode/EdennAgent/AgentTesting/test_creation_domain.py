"""CP1 tests: creation domain contracts + BundleResolver ask-first behavior.

Hermetic — the resolver takes an explicit ``kinds`` mapping, so no store or
network is involved. The planner-cap test uses the existing multi-scene fixture
to prove 3- and 4-source plans are accepted and 5 is rejected.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from EdennCode.EdennAgent.Creation import (
    AmbiguityQuestion,
    BundleCapExceededError,
    BundleItem,
    BundleResolver,
    MusicSource,
    RequestBundle,
    SourceRole,
    TreatmentKind,
    TreatmentSpec,
)

RESOLVER = BundleResolver()


def _bundle(intent: str = "", **items: SourceRole | None) -> RequestBundle:
    """Bundle from ``ref=role`` kwargs (None = infer)."""

    return RequestBundle(
        intent=intent,
        items=[BundleItem(ref=ref, role=role) for ref, role in items.items()])


# ------------------------------------------------------------------ resolution
def test_single_video_plus_audio_resolves_without_questions() -> None:
    res = RESOLVER.resolve(
        _bundle("20s hook-first short", asset_v=None, asset_t=None),
        kinds={"asset_v": "video", "asset_t": "audio"})
    assert res.status == "resolved" and res.bundle is not None
    roles = {s.ref: s.role for s in res.bundle.sources}
    assert roles == {"asset_v": SourceRole.SPINE, "asset_t": SourceRole.MUSIC}
    assert all(s.reason for s in res.bundle.sources), "every role carries a why"
    assert res.bundle.spine_ref == "asset_v"


def test_two_videos_ask_first_for_spine() -> None:
    res = RESOLVER.resolve(_bundle("a short", asset_a=None, asset_b=None),
                           kinds={"asset_a": "video", "asset_b": "video"})
    assert res.status == "needs_input" and res.bundle is None
    q = res.questions[0]
    assert q.kind == "role_conflict"
    assert {o.key for o in q.options} == {"asset_a", "asset_b"}


def test_explicit_spine_makes_remaining_videos_accents() -> None:
    """The spine is singular: once one is chosen, other videos are b-roll —
    exactly what the ask-first question promises the user."""

    res = RESOLVER.resolve(
        _bundle("a short", asset_a=SourceRole.SPINE, asset_b=None),
        kinds={"asset_a": "video", "asset_b": "video"})
    assert res.status == "resolved"
    roles = {s.ref: s.role for s in res.bundle.sources}
    assert roles["asset_a"] is SourceRole.SPINE
    assert roles["asset_b"] is SourceRole.ACCENT
    assert [s for s in res.bundle.sources if s.ref == "asset_a"][0].explicit


def test_two_explicit_spines_ask_which_leads() -> None:
    res = RESOLVER.resolve(
        _bundle("a short", asset_a=SourceRole.SPINE, asset_b=SourceRole.SPINE),
        kinds={"asset_a": "video", "asset_b": "video"})
    assert res.status == "needs_input"
    assert {o.key for o in res.questions[0].options} == {"asset_a", "asset_b"}


def test_voice_intent_with_audio_asks_music_vs_voice() -> None:
    res = RESOLVER.resolve(
        _bundle("rephrase the announcer over it", asset_v=None, asset_t=None),
        kinds={"asset_v": "video", "asset_t": "audio"})
    assert res.status == "needs_input"
    q = res.questions[0]
    assert {o.key for o in q.options} == {"music", "voice"}


def test_fresh_music_against_referenced_track_is_a_contradiction() -> None:
    res = RESOLVER.resolve(
        _bundle("cut this with fresh music please",
                asset_v=None, asset_t=SourceRole.MUSIC),
        kinds={"asset_v": "video", "asset_t": "audio"})
    assert res.status == "needs_input"
    kinds = {q.kind for q in res.questions}
    assert "contradiction" in kinds
    contradiction = [q for q in res.questions if q.kind == "contradiction"][0]
    assert {o.key for o in contradiction.options} == {"provided", "variant_of",
                                                      "generate"}


def test_images_are_accents_and_excludes_are_kept_but_inactive() -> None:
    res = RESOLVER.resolve(
        _bundle("short", asset_v=None, asset_i=None, asset_x=SourceRole.EXCLUDE),
        kinds={"asset_v": "video", "asset_i": "image", "asset_x": "video"})
    assert res.status == "resolved"
    roles = {s.ref: s.role for s in res.bundle.sources}
    assert roles["asset_i"] is SourceRole.ACCENT
    assert roles["asset_x"] is SourceRole.EXCLUDE


def test_cap_counts_active_sources_only() -> None:
    ok = _bundle("s", a=None, b=SourceRole.EXCLUDE, c=SourceRole.ACCENT,
                 d=SourceRole.ACCENT, e=SourceRole.ACCENT)
    kinds = {k: ("video" if k == "a" else "image") for k in "abcde"}
    kinds["b"] = "video"
    assert RESOLVER.resolve(ok, kinds=kinds).status == "resolved"

    too_many = _bundle("s", a=None, b=None, c=SourceRole.ACCENT,
                       d=SourceRole.ACCENT, e=SourceRole.ACCENT)
    kinds2 = {"a": "video", "b": "video", "c": "image", "d": "image", "e": "image"}
    with pytest.raises(BundleCapExceededError):
        RESOLVER.resolve(too_many, kinds=kinds2)


def test_impossible_explicit_role_is_an_error_not_a_question() -> None:
    with pytest.raises(ValueError, match="impossible"):
        RESOLVER.resolve(_bundle("s", asset_i=SourceRole.MUSIC),
                         kinds={"asset_i": "image"})


def test_bad_ref_grammar_rejected_at_the_boundary() -> None:
    with pytest.raises(ValidationError):
        BundleItem(ref="not a ref!!", role=None)


# ------------------------------------------------------------------ treatments
def test_treatment_music_ref_requirement() -> None:
    with pytest.raises(ValidationError):
        TreatmentSpec(kind=TreatmentKind.MUSIC_ONLY,
                      music=MusicSource.PROVIDED, music_ref=None)
    spec = TreatmentSpec(kind=TreatmentKind.REPHRASE_ORIGINAL,
                         music=MusicSource.PROVIDED, music_ref="asset_t")
    assert spec.wants_source_speech
    assert TreatmentSpec(music=MusicSource.GENERATE).music_ref is None


def test_ambiguity_question_requires_two_options() -> None:
    with pytest.raises(ValidationError):
        AmbiguityQuestion(kind="gate", question="?", options=[])


# ------------------------------------------------------------ planner N-source
def test_planner_accepts_four_sources_and_rejects_five(tmp_path) -> None:
    from EdennCode.EdennAgent.Recompose.cutspec import generate_cut_spec
    from EdennCode.EdennAgent.Recompose.domain import AssetRecord, Knobs
    from EdennCode.EdennAgent.Recompose.musicsheet import provisional_sheet
    from EdennCode.EdennAgent.Recompose.planner import plan_recompose
    from EdennCode.EdennAgent.Recompose.Testing.test_recompose_m4_m5 import (
        MULTI_SCENE_TEXTS,
        _render_multi_scene_video,
    )
    from EdennCode.EdennAgent.Recompose.understanding import understand_asset
    import asyncio

    video = _render_multi_scene_video(tmp_path / "src.mp4")
    observation = {"scenes": [
        {"scene_index": i, "start_timestamp": i * 4.5,
         "end_timestamp": (i + 1) * 4.5, "visual_summary": s,
         "key_actions": a, "mood": "energetic"}
        for i, (s, a) in enumerate(MULTI_SCENE_TEXTS)]}

    def source(n: int) -> tuple[AssetRecord, object]:
        asset = AssetRecord(kind="video", path=str(video), duration_s=36.0)
        asset.asset_id = f"asset_src{n}"
        tree = asyncio.run(understand_asset(asset, observation=observation))
        return asset, tree

    pairs = [source(i) for i in range(4)]
    assets = [a for a, _ in pairs]
    trees = {a.asset_id: t for (a, t) in pairs}
    sheet = provisional_sheet(16.0, tempo_bpm=120)
    spec = generate_cut_spec(sheet, Knobs(cut_density="sparse",
                                          coherence_mode="single_story",
                                          max_slots_per_scene=4))
    plan = plan_recompose(assets=assets, trees=trees, sheet=sheet, spec=spec)
    assert len({s.asset_id for s in plan.slots}) >= 1
    assert all(s.asset_id in trees for s in plan.slots)

    extra, extra_tree = source(4)
    with pytest.raises(ValueError, match="at most 4"):
        plan_recompose(assets=assets + [extra],
                       trees={**trees, extra.asset_id: extra_tree},
                       sheet=sheet, spec=spec)
