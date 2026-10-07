"""Campaign API router — thin HTTP shell over CampaignService.

Endpoints mirror the frontend store's actions 1:1 (see
campaign/CAMPAIGN_CONSOLE.md §Hookup contract). Error mapping follows the
creation router's convention: StageError → 409, KeyError → 404,
ValueError → 422. Every mutating endpoint returns the fresh snapshot so the
frontend can replace its world in one round-trip.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .service import CampaignService, StageError


class IngestAnswerRequest(BaseModel):
    choice: str


class QuestionAnswerRequest(BaseModel):
    qid: str
    choice: str


class ApproveRequest(BaseModel):
    actions: list[dict[str, Any]] = Field(
        default_factory=list, description='[{"id": "iterate", "approved": true}, ...]'
    )


def create_campaign_router(service: CampaignService) -> APIRouter:
    """Build the campaign router around a configured CampaignService."""
    router = APIRouter(prefix="/api/campaign")

    def _snap() -> dict[str, Any]:
        return service.snapshot()

    def _guard(fn):
        """Map service exceptions to HTTP codes (sync helper for async bodies)."""

        async def run(coro_or_value):
            try:
                if hasattr(coro_or_value, "__await__"):
                    await coro_or_value
                return _snap()
            except StageError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        return run

    run = _guard(None)

    @router.get("/state")
    def state() -> dict[str, Any]:
        return _snap()

    @router.get("/tree")
    def tree() -> dict[str, Any]:
        return service.tree()

    @router.post("/ingest/answer")
    async def ingest_answer(req: IngestAnswerRequest) -> dict[str, Any]:
        try:
            service.ingest_answer(req.choice)
            return _snap()
        except StageError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/brief")
    async def brief() -> dict[str, Any]:
        return await run(_call(service.send_brief))

    @router.post("/answer")
    async def answer(req: QuestionAnswerRequest) -> dict[str, Any]:
        return await run(_call(service.answer_question, req.qid, req.choice))

    @router.post("/plan")
    async def plan() -> dict[str, Any]:
        return await run(service.plan())

    @router.post("/render")
    async def render() -> dict[str, Any]:
        return await run(service.render())

    @router.post("/launch")
    async def launch() -> dict[str, Any]:
        return await run(service.launch())

    @router.post("/approve")
    async def approve(req: ApproveRequest) -> dict[str, Any]:
        approved = [a.get("id") for a in req.actions if a.get("approved")]
        return await run(service.approve([a for a in approved if isinstance(a, str)]))

    return router


async def _call(fn, *args):  # noqa: ANN001 — tiny adapter
    """Run a sync service method inside the async guard uniformly."""
    return fn(*args)
