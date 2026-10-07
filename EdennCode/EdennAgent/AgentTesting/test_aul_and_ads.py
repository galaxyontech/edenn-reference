"""AUL core + ads loop tests — hermetic (InMemoryAulRepository, sandbox adapter,
rendered fixtures). The Postgres backend is exercised by the e2e validation
script (validate_loop.py) against the shared DB; both back the same
``AulRepository`` contract so the twin cannot silently drift."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from EdennCode.EdennAgent.AdsAdapters import (
    AttributionEngine,
    CampaignPublisher,
    SandboxAdapter,
)
from EdennCode.EdennAgent.AssetLibrary import (
    AssetIdentity,
    AssetIngestor,
    InMemoryAulRepository,
    LineageRecorder,
    Ref,
    SegmentTreeAssembler,
)
from EdennCode.EdennAgent.Recompose.cutspec import generate_cut_spec
from EdennCode.EdennAgent.Recompose.domain import AssetRecord, Knobs
from EdennCode.EdennAgent.Recompose.musicsheet import provisional_sheet
from EdennCode.EdennAgent.Recompose.planner import plan_recompose
from EdennCode.EdennAgent.Recompose.Testing.test_recompose_m4_m5 import (
    MULTI_SCENE_TEXTS,
    _render_multi_scene_video,
)


# ---------------------------------------------------------------------- refs
def test_ref_roundtrip_and_validation() -> None:
    r = Ref.parse("asset_ab12#t=193.2-203.5")
    assert (r.asset_id, r.start_s, r.end_s) == ("asset_ab12", 193.2, 203.5)
    assert str(r) == "asset_ab12#t=193.2-203.5"
    assert not Ref.parse("asset_ab12").is_span
    with pytest.raises(ValueError):
        Ref.parse("asset_ab12#t=5-5")  # empty span
    with pytest.raises(ValueError):
        Ref.parse("nope nope")
    assert str(Ref.span("a", 1.23456, 2.0)) == "a#t=1.235-2"
    assert Ref.asset_of("asset_ab12#t=1-2") == "asset_ab12"


def test_stable_ids_are_deterministic(tmp_path: Path) -> None:
    p = tmp_path / "x.bin"
    p.write_bytes(b"hello edenn")
    a1, sha1 = AssetIdentity.from_file(p)
    a2, sha2 = AssetIdentity.from_file(p)
    assert a1 == a2 and sha1 == sha2 and a1.startswith("asset_")
    assert AssetIdentity.node_id("asset_x", 1.0, 2.5) == AssetIdentity.node_id("asset_x", 1.0, 2.5)


# ------------------------------------------------------------ ingest -> view
@pytest.fixture(scope="module")
def media(tmp_path_factory):
    root = tmp_path_factory.mktemp("aul")
    video = _render_multi_scene_video(root / "spine.mp4")
    scenes = [
        {"scene_index": i, "start_timestamp": i * 4.5, "end_timestamp": (i + 1) * 4.5,
         "visual_summary": s, "key_actions": a,
         "mood": "energetic" if i % 2 == 0 else "calm and still"}
        for i, (s, a) in enumerate(MULTI_SCENE_TEXTS)
    ]
    return {"video": video, "scenes": scenes, "root": root}


@pytest.fixture(scope="module")
def ingested(media):
    repo = InMemoryAulRepository()
    asset_id = asyncio.run(AssetIngestor(repo).ingest(
        media["video"], name="spine.mp4",
        observation={"scenes": media["scenes"]}, with_signals=True))
    return {"repo": repo, "asset_id": asset_id}


def test_ingest_persists_all_layers(ingested) -> None:
    repo, aid = ingested["repo"], ingested["asset_id"]
    asset = repo.get_asset(aid)
    assert asset and asset.duration_s > 30
    assert repo.get_annotations(aid, layer="L0", kind="tech")
    shots = repo.get_annotations(aid, layer="L1", kind="shot")
    scenes = repo.get_annotations(aid, layer="L2", kind="scene")
    assert len(shots) >= 6 and len(scenes) >= 6
    listing = repo.list_assets()
    assert listing[0].layers == ["L0", "L1", "L2"]


def test_tree_view_rebuilds_with_stable_ids(ingested, media) -> None:
    repo, aid = ingested["repo"], ingested["asset_id"]
    assembler = SegmentTreeAssembler(repo)
    t1 = assembler.build(aid)
    t2 = assembler.build(aid)
    assert set(t1.nodes) == set(t2.nodes), "node ids must be identical across builds"
    leaves = t1.leaves()
    assert len(leaves) >= 6
    assert any("test pattern" in n.summary for n in leaves)
    assert all(n.node_id.startswith("seg_asset_") for n in leaves)
    # a plan made against view 1 references nodes resolvable in view 2
    sheet = provisional_sheet(20.0, tempo_bpm=120)
    asset = AssetRecord(kind="video", path=str(media["video"]), duration_s=36.0)
    asset.asset_id = aid  # plan directly on AUL identity
    spec = generate_cut_spec(sheet, Knobs(cut_density="sparse",
                                          coherence_mode="single_story",
                                          max_slots_per_scene=3))
    plan = plan_recompose(assets=[asset], trees={aid: t1}, sheet=sheet, spec=spec)
    assert all(s.node_id in t2.nodes for s in plan.slots)


def test_search_finds_scene_text(ingested) -> None:
    hits = ingested["repo"].search("gray card stillness")
    assert hits and any("gray" in str(h.payload).lower() for h in hits)


def test_reingest_is_idempotent(media) -> None:
    # re-ingesting the same file must not duplicate annotations (P1 fix)
    repo = InMemoryAulRepository()
    ingestor = AssetIngestor(repo)
    obs = {"scenes": media["scenes"]}
    aid = asyncio.run(ingestor.ingest(media["video"], name="spine.mp4",
                                      observation=obs, with_signals=False))
    n1 = len(repo.get_annotations(aid))
    again = asyncio.run(ingestor.ingest(media["video"], name="spine.mp4",
                                        observation=obs, with_signals=False))
    assert again == aid
    assert len(repo.get_annotations(aid)) == n1, "re-ingest must replace, not duplicate"
    shots = repo.get_annotations(aid, layer="L1", kind="shot")
    assert len(shots) == len({(s.span_start_s, s.span_end_s) for s in shots})


# --------------------------------------------------------- lineage + outcomes
@pytest.fixture(scope="module")
def loop_state(ingested, media):
    """Plan + fake-render two variants, record lineage, publish + outcomes."""

    repo, aid = ingested["repo"], ingested["asset_id"]
    tree = SegmentTreeAssembler(repo).build(aid)
    asset = AssetRecord(kind="video", path=str(media["video"]), duration_s=36.0)
    asset.asset_id = aid
    sheet = provisional_sheet(12.0, tempo_bpm=120)
    recorder = LineageRecorder(repo)

    out_ids = []
    used: dict[str, list[tuple[float, float]]] = {}
    for tag in ("hookfirst", "awefirst"):
        spec = generate_cut_spec(sheet, Knobs(cut_density="sparse",
                                              coherence_mode="single_story",
                                              max_slots_per_scene=4))
        plan = plan_recompose(assets=[asset], trees={aid: tree}, sheet=sheet,
                              spec=spec, hypothesis=tag,
                              exclude_intervals={k: list(v) for k, v in used.items()})
        for s in plan.slots:
            used.setdefault(s.asset_id, []).append((s.seg_in_s, s.seg_in_s + s.spec.dur_s))
        render = media["root"] / f"render_{tag}.mp4"
        # content-addressed ids: each variant needs distinct bytes
        dur = 3 + len(out_ids)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                        "-i", f"testsrc=duration={dur}:size=320x240:rate=24",
                        "-pix_fmt", "yuv420p", str(render)],
                       check=True, capture_output=True, timeout=60)
        from EdennCode.EdennAgent.Recompose.domain import CutReport, RenderedVariant
        variant = RenderedVariant(plan_id=plan.plan_id, path=str(render),
                                  cut_report=CutReport(duration_s=3.0, n_slots=len(plan.slots)))
        out_id = asyncio.run(recorder.record_variant(plan, variant, name=f"take_{tag}"))
        out_ids.append(out_id)

    publisher = CampaignPublisher(repo, SandboxAdapter(quality_prior={"": 1.0}))
    for out_id in out_ids:
        ref = asyncio.run(publisher.publish(out_id, title=out_id, campaign="test"))
        n = asyncio.run(publisher.collect_outcomes(
            out_id, ref, ["2026-07-13", "2026-07-14", "2026-07-15"]))
        assert n == 3
    return {"repo": repo, "asset_id": aid, "out_ids": out_ids}


def test_lineage_edges_recorded(loop_state) -> None:
    repo, aid = loop_state["repo"], loop_state["asset_id"]
    made = [e for e in repo.edges_from(aid) if e.operation == "slot_cut"]
    assert made, "slot_cut edges must exist"
    assert {e.dst_ref for e in made} == set(loop_state["out_ids"])
    # usage rollup reflects consumed footage
    assert repo.usage_spans(aid), "usage spans must roll up from edges"
    listing = {s.asset.asset_id: s for s in repo.list_assets()}
    assert listing[aid].used_fraction > 0


def test_outputs_are_generated_assets(loop_state) -> None:
    repo = loop_state["repo"]
    for out_id in loop_state["out_ids"]:
        a = repo.get_asset(out_id)
        assert a and a.generated is True
        assert a.meta.get("hypothesis")


def test_outcomes_annotations_and_totals(loop_state) -> None:
    engine = AttributionEngine(loop_state["repo"])
    t = engine.variant_totals(loop_state["out_ids"][0])
    assert t.days == 3 and t.impressions > 0 and 0 < t.ctr < 0.1


def test_component_attribution_rolls_down_lineage(loop_state) -> None:
    engine = AttributionEngine(loop_state["repo"])
    comps = engine.component_attribution(loop_state["out_ids"])
    assert comps, "attribution must produce ranked components"
    top = comps[0]
    assert "#t=" in top.ref and top.impressions > 0
    assert top.evidence == "observational"
    openings = engine.component_attribution(loop_state["out_ids"], role="opening")
    assert openings and all("opening" in c.roles for c in openings)


def test_sandbox_metrics_deterministic() -> None:
    sb = SandboxAdapter()
    m1 = asyncio.run(sb.pull_metrics("sb_abc", "2026-07-15"))
    m2 = asyncio.run(sb.pull_metrics("sb_abc", "2026-07-15"))
    assert m1 == m2
    assert 0.005 < m1.ctr < 0.06


def test_collect_outcomes_dedups_on_repull() -> None:
    from EdennCode.EdennAgent.AssetLibrary import Asset, InMemoryAulRepository

    repo = InMemoryAulRepository()
    repo.upsert_asset(Asset(asset_id="out1", kind="video", sha256="s", name="v1",
                            uri="/x.mp4", generated=True))

    class _Spy(SandboxAdapter):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def pull_metrics(self, channel_ref, date):
            self.calls += 1
            return await super().pull_metrics(channel_ref, date)

    spy = _Spy()
    pub = CampaignPublisher(repo, spy)
    days = ["2026-07-13", "2026-07-14", "2026-07-15"]
    assert asyncio.run(pub.collect_outcomes("out1", "sb_1", days)) == 3
    # a re-pull of the same days must add nothing and re-fetch nothing
    assert asyncio.run(pub.collect_outcomes("out1", "sb_1", days)) == 0
    assert spy.calls == 3
    assert AttributionEngine(repo).variant_totals("out1").days == 3


def test_performance_reports_first_published_channel_and_is_project_scoped() -> None:
    from EdennCode.EdennAgent.AssetLibrary import Asset, Edge, InMemoryAulRepository
    from EdennCode.EdennAgent.AssetLibraryApi import LibraryService

    repo = InMemoryAulRepository()
    repo.upsert_asset(Asset(asset_id="out1", kind="video", sha256="s", name="v1",
                            uri="/x.mp4", generated=True))
    # republished to a second channel later; the first publish must win the label
    repo.add_edge(Edge(src_ref="out1", dst_ref="tiktok:t1", operation="published_as",
                       params={"channel": "tiktok"}))
    repo.add_edge(Edge(src_ref="out1", dst_ref="sandbox:s1", operation="published_as",
                       params={"channel": "sandbox"}))
    # another project's variant must not leak into project 'default'
    repo.upsert_asset(Asset(asset_id="out2", project_id="other", kind="video",
                            sha256="s2", name="v2", uri="/y.mp4", generated=True))
    repo.add_edge(Edge(src_ref="out2", dst_ref="tiktok:t2", operation="published_as",
                       project_id="other", params={"channel": "tiktok"}))

    perf = LibraryService(repo).performance("default")
    assert [v["name"] for v in perf["variants"]] == ["v1"]        # project-scoped
    assert perf["variants"][0]["channel"] == "tiktok"            # first publish wins
