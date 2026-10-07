"""Campaign console devserver — frontend + API on one origin (port 5601).

Follows the agentic_audio design-devserver mount pattern:
  - InMemoryAulRepository seeded at startup from the fixture media dir
    (Testing/media; assembled by Testing/assemble_media.py — gitignored),
    with hand-authored *.observation.json caches so understanding runs with
    ZERO model spend.
  - CreationService with a workdir tempdir; llm_client stays None here, so
    planning is deterministic and variant C degrades honestly to music-only
    (the service says so in the thread). Real renders via local ffmpeg.
  - CampaignPublisher over SandboxAdapter — full publish/outcome loop, zero
    real spend.
  - create_library_router serves /api/v2/library/media/{asset_id} so takes
    stream SAS-free from local paths.
  - The campaign frontend is mounted at "/" LAST (mount order is load-bearing).

Run: python EdennCode/EdennAgent/Campaign/devserver.py
  (or the `campaign-console` launch entry, which now points here).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from EdennCode.EdennAgent.AssetLibrary.ingest import AssetIngestor
from EdennCode.EdennAgent.AssetLibrary.repository import InMemoryAulRepository
from EdennCode.EdennAgent.AdsAdapters import AttributionEngine, CampaignPublisher, SandboxAdapter
from EdennCode.EdennAgent.Creation.service import CreationService
from EdennCode.EdennAgent.AssetLibraryApi import create_library_router
from EdennCode.EdennAgent.AssetLibraryApi.service import LibraryService

from EdennCode.EdennAgent.Campaign.backend.router import create_campaign_router
from EdennCode.EdennAgent.Campaign.backend.service import build_campaign_service

log = logging.getLogger("edenn.campaign.devserver")

HERE = Path(__file__).resolve().parent
FRONTEND_DIR = HERE / "frontend"
# The STABLE agentic-agentic audio (entrance -> chat <-> canvas). Mounted at
# /studio so the audio creation surface is reachable from the campaign
# console; it runs its own client-side mock backend exactly like the
# standalone 5599 console. Untouched code — served, not copied.
STUDIO_DIR = HERE.parent / "AgenticAudio" / "frontend"
MEDIA_DIR = Path(os.getenv("EDENN_CAMPAIGN_MEDIA_DIR", str(HERE / "Testing" / "media")))
HOST = os.getenv("EDENN_DEV_HOST", "127.0.0.1")
PORT = int(os.getenv("EDENN_CAMPAIGN_PORT", "5601"))


def build_app() -> FastAPI:
    app = FastAPI(title="Edenn campaign console (dev)")

    repo = InMemoryAulRepository()
    ingestor = AssetIngestor(repo)
    workdir = Path(tempfile.mkdtemp(prefix="edenn_campaign_"))
    creation = CreationService(repo, workdir=workdir / "creation", llm_client=None)
    publisher = CampaignPublisher(repo, SandboxAdapter())
    library = LibraryService(repo)
    campaign = build_campaign_service(
        repo,
        creation,
        publisher=publisher,
        attribution=AttributionEngine(repo),
        library=library,
        rephrase_available=False,
    )

    @app.on_event("startup")
    async def _seed() -> None:
        """Ingest the fixture media; then register roles with the campaign."""
        if not MEDIA_DIR.is_dir():
            log.warning("campaign media dir missing: %s — run Testing/assemble_media.py", MEDIA_DIR)
            return
        ingested: dict[str, str] = {}
        for f in sorted(MEDIA_DIR.iterdir()):
            if f.suffix not in (".mp4", ".wav", ".mp3"):
                continue
            obs_file = f.with_suffix(".observation.json")
            observation = None
            if obs_file.exists():
                try:
                    observation = json.loads(obs_file.read_text())
                except (OSError, json.JSONDecodeError):
                    observation = None
            kind = "video" if f.suffix == ".mp4" else "audio"
            try:
                asset_id = await ingestor.ingest(
                    f, name=f.stem, kind=kind, observation=observation
                )
                ingested[f.stem] = asset_id
                log.info("seeded %s -> %s", f.name, asset_id)
            except Exception:  # noqa: BLE001 — per-file resilience, devserver pattern
                log.exception("seed failed for %s", f.name)
        spine = ingested.get("launch_footage")
        accent = ingested.get("event_reel")
        music = ingested.get("spring_track")
        unfiled = ingested.get("IMG_2214")
        if spine and accent and music:
            campaign.register_sources(
                spine_id=spine, accent_id=accent, music_id=music,
                accent2_id=ingested.get("broll_reel"), unfiled_id=unfiled,
            )
            log.info("campaign sources registered (spine=%s)", spine)
        else:
            log.error("seeding incomplete — campaign actions will 409 (have: %s)", ingested)

    app.include_router(create_campaign_router(campaign))
    app.include_router(create_library_router(repo))
    # The stable agentic audio BEFORE the catch-all; campaign frontend LAST —
    # the "/" static mount must not shadow API routes or /studio.
    if STUDIO_DIR.is_dir():
        app.mount("/studio", StaticFiles(directory=str(STUDIO_DIR), html=True), name="studio")
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True, follow_symlink=True), name="frontend")
    return app


app = build_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
