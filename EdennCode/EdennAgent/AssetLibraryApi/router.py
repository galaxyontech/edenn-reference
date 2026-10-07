"""Library API — thin HTTP shell over LibraryService + ThumbnailRenderer.

All aggregation lives in service.py; this module only maps HTTP <-> service
calls. Mounted standalone by the e2e validation server; platform mounting
follows the agentic_audio pattern when this ships.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from ..AssetLibrary.repository import AulRepository
from .service import LibraryService
from .thumbnails import ThumbnailRenderer

FRONTEND_DIR = Path(__file__).parent / "frontend"


def create_library_router(repo: AulRepository) -> APIRouter:
    router = APIRouter(prefix="/api/v2/library")
    service = LibraryService(repo)
    thumbs = ThumbnailRenderer()

    @router.get("/assets")
    def list_assets(project_id: str = "default") -> dict:
        return {"assets": service.list_assets(project_id)}

    @router.get("/board")
    def board(project_id: str = "default") -> dict:
        return service.board(project_id)

    @router.get("/graph")
    def graph(project_id: str = "default") -> dict:
        return service.graph(project_id)

    @router.get("/plan")
    def plan(project_id: str = "default") -> dict:
        return service.plan(project_id)

    @router.get("/assets/{asset_id}")
    def asset_detail(asset_id: str) -> dict:
        detail = service.asset_detail(asset_id)
        if detail is None:
            raise HTTPException(404, "asset not found")
        return detail

    @router.get("/search")
    def search(q: str, project_id: str = "default") -> dict:
        return service.search(q, project_id)

    @router.get("/performance")
    def performance(project_id: str = "default") -> dict:
        return service.performance(project_id)

    @router.get("/thumb/{asset_id}")
    def thumb(asset_id: str, t: float = 1.0):
        asset = repo.get_asset(asset_id)
        if asset is None:
            raise HTTPException(404, "asset not found")
        try:
            return FileResponse(thumbs.render(asset, t), media_type="image/jpeg")
        except FileNotFoundError:
            raise HTTPException(404, "no thumb")

    @router.get("/media/{asset_id}")
    def media(asset_id: str):
        """Stream a source file for the plan-preview player (range-capable)."""

        asset = repo.get_asset(asset_id)
        if asset is None:
            raise HTTPException(404, "asset not found")
        path = Path(asset.uri)
        if not path.is_file():
            raise HTTPException(404, "media file not available")
        media_type = "audio/wav" if path.suffix == ".wav" else (
            "audio/mpeg" if path.suffix == ".mp3" else "video/mp4")
        return FileResponse(path, media_type=media_type)

    @router.get("/app", include_in_schema=False)
    def app() -> HTMLResponse:
        return HTMLResponse((FRONTEND_DIR / "index.html").read_text())

    return router
