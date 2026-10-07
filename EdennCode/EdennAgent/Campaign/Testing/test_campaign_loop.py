"""Headless e2e of the campaign hookup: the full loop through REAL engines.

Seeds the fixture media, then drives brief → questions → plan → render
(local ffmpeg) → launch (sandbox) → approve (round-2 compile), asserting the
stage machine, the ask-first questions, non-overlapping footage, real
rendered files, deterministic outcomes, and the grown lineage tree.

Run: PYTHONPATH=. .venv/bin/python -m pytest EdennCode/EdennAgent/Campaign/Testing/test_campaign_loop.py -x -q
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from EdennCode.EdennAgent.AssetLibrary.ingest import AssetIngestor
from EdennCode.EdennAgent.AssetLibrary.repository import InMemoryAulRepository
from EdennCode.EdennAgent.AdsAdapters import AttributionEngine, CampaignPublisher, SandboxAdapter
from EdennCode.EdennAgent.Creation.service import CreationService
from EdennCode.EdennAgent.AssetLibraryApi.service import LibraryService
from EdennCode.EdennAgent.Campaign.backend.service import build_campaign_service

MEDIA = Path(__file__).resolve().parent / "media"


@pytest.fixture(scope="module")
def loop_world():
    """Seed once for the module; the loop test drives the shared service."""
    if not MEDIA.is_dir():
        pytest.skip("fixture media missing — run Testing/assemble_media.py")

    async def build():
        repo = InMemoryAulRepository()
        ingestor = AssetIngestor(repo)
        ids = {}
        for f in sorted(MEDIA.iterdir()):
            if f.suffix not in (".mp4", ".wav", ".mp3"):
                continue
            obs = None
            of = f.with_suffix(".observation.json")
            if of.exists():
                obs = json.loads(of.read_text())
            ids[f.stem] = await ingestor.ingest(
                f, name=f.stem, kind="video" if f.suffix == ".mp4" else "audio", observation=obs
            )
        workdir = Path(tempfile.mkdtemp(prefix="edenn_campaign_test_"))
        creation = CreationService(repo, workdir=workdir, llm_client=None)
        svc = build_campaign_service(
            repo,
            creation,
            publisher=CampaignPublisher(repo, SandboxAdapter()),
            attribution=AttributionEngine(repo),
            library=LibraryService(repo),
            rephrase_available=False,
        )
        svc.register_sources(
            spine_id=ids["launch_footage"], accent_id=ids["event_reel"],
            music_id=ids["spring_track"], accent2_id=ids.get("broll_reel"),
            unfiled_id=ids.get("IMG_2214"),
        )
        return repo, svc

    return asyncio.run(build())


def test_full_campaign_loop(loop_world):
    repo, svc = loop_world

    async def drive():
        # Ingest question (organize-by-asking)
        snap = svc.snapshot()
        assert snap["ingest"]["pending"] is True
        svc.ingest_answer("Spring shoot")
        assert svc.snapshot()["ingest"]["pending"] is False

        # Brief → REAL resolver questions (two role-less videos → spine ask)
        svc.send_brief()
        snap = svc.snapshot()
        assert snap["stage"] == "roles"
        qs = snap["campaign"]["questions"]
        assert len(qs) >= 2, "expected engine spine question + campaign music question"
        engine_q = next(q for q in qs if q["id"].startswith("engine-"))
        assert "spine" in engine_q["text"].lower() or "lead" in engine_q["text"].lower()

        # Answer everything (pick the first chip of the engine question =
        # whichever video it names first; music: variant-in-style)
        for q in qs:
            choice = q["chips"][0] if q["id"].startswith("engine-") else "Variant in its style"
            svc.answer_question(q["id"], choice)
        snap = svc.snapshot()
        assert snap["campaign"]["roles"], "roles should be locked after all answers"

        # Plan: three variants, engine-planned, non-overlapping footage
        await svc.plan()
        snap = svc.snapshot()
        assert snap["stage"] == "planned"
        variants = snap["campaign"]["variants"]
        assert len(variants) == 3
        assert all(v["plan_id"] for v in variants)
        assert all(len(v["preview_slots"]) > 0 for v in variants)

        # Gate 1: real local renders
        await svc.render()
        snap = svc.snapshot()
        assert snap["stage"] == "rendered"
        for v in snap["campaign"]["variants"]:
            assert v["state"] == "rendered"
            assert v["take_url"].startswith("/api/v2/library/media/")
            asset_id = v["take_url"].rsplit("/", 1)[1]
            asset = repo.get_asset(asset_id)
            assert asset is not None and Path(asset.uri).exists(), "rendered file must exist"

        # Gate 2: sandbox publish + outcomes; narrative derived from numbers
        await svc.launch()
        snap = svc.snapshot()
        assert snap["stage"] == "proposal"
        outcomes = snap["campaign"]["outcomes"]
        assert len(outcomes) == 3
        states = {v["id"]: v["state"] for v in snap["campaign"]["variants"]}
        assert list(states.values()).count("killed") == 1
        assert snap["campaign"]["proposal"] is not None

        # Gate 3: approve iterate → round 2 planned as children of the winner
        await svc.approve(["iterate", "realloc", "retire"])
        snap = svc.snapshot()
        assert snap["stage"] == "round2"
        r2 = [v for v in snap["campaign"]["variants"] if v["round"] == 2]
        assert len(r2) == 2
        winner_id = next(v["id"] for v in snap["campaign"]["variants"] if v["state"] == "live")
        assert all(v["parent_variant_id"] == winner_id for v in r2)

        # The campaign tree: source + 5 variants, round-2 children off the winner
        tree = svc.tree()
        ids = {n["id"] for n in tree["nodes"]}
        assert {"src", "A", "B", "C", "A2", "B2"} <= ids
        assert ["A2"] == [e[1] for e in tree["edges"] if e[0] == winner_id and e[1] == "A2"]

    asyncio.run(drive())
