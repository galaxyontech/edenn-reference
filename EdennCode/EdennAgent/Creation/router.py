"""HTTP shell for the creation capability — thin, typed, no business logic.

Endpoints mirror the loop's phases (AGENTIC_CREATION.md):

    POST /api/v2/creation/resolve   bundle → roles, or ask-first questions
    POST /api/v2/creation/plan      resolved request → previews (no spend)
    POST /api/v2/creation/render    lock one previewed plan → rendered asset id

Request/response bodies are the pydantic contracts from ``creation.domain`` /
``creation.preview``; FastAPI validates at the boundary so the service only
ever sees well-typed inputs.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..Recompose.planner import MaterialExhaustedError
from .domain import BundleResolution, CreationRequest, RequestBundle
from .preview import PlanPreview
from .service import CreationService


class PlanResponse(BaseModel):
    """Previews for every requested short, in request order."""

    previews: list[PlanPreview]


class RenderRequest(BaseModel):
    """Lock request: render exactly one previewed plan."""

    plan_id: str = Field(description="Plan id returned by /plan")
    project_id: str = "default"
    session_id: Optional[str] = None


class RenderResponse(BaseModel):
    """The rendered output, now a first-class library asset."""

    asset_id: str = Field(description="AUL id of the rendered short")


def create_creation_router(service: CreationService) -> APIRouter:
    """Mount the creation endpoints over a configured service instance."""

    router = APIRouter(prefix="/api/v2/creation")

    @router.post("/resolve", response_model=BundleResolution)
    def resolve(bundle: RequestBundle) -> BundleResolution:
        """Resolve roles for a bundle — or return the questions to ask first."""

        try:
            return service.resolve(bundle)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/plan", response_model=PlanResponse)
    async def plan(request: CreationRequest) -> PlanResponse:
        """Plan every requested short (show-before-spend; nothing renders).

        The bundle must resolve cleanly here — if it still needs input the
        caller gets 409 with the questions, enforcing ask-first at the API
        boundary too.
        """

        resolution = service.resolve(request.bundle)
        if resolution.status == "needs_input":
            raise HTTPException(
                409, detail={"message": "bundle needs input — ask first",
                             "questions": [q.model_dump()
                                           for q in resolution.questions]})
        assert resolution.bundle is not None  # guaranteed by the invariant
        previews: list[PlanPreview] = []
        used: dict[str, list[tuple[float, float]]] = {}
        try:
            for short in request.shorts:
                preview, state = await service.plan_short(
                    resolution.bundle, short, exclude_intervals=used)
                for slot in state.plan.slots:  # cross-short no-reuse
                    used.setdefault(slot.asset_id, []).append(
                        (slot.seg_in_s, slot.seg_in_s + slot.spec.dur_s))
                previews.append(preview)
        except MaterialExhaustedError as exc:
            # Running out of unused footage is the library being too small for
            # the ask, not a server fault. It arrived as an unhandled 500 with a
            # traceback, so the console could only say "planning hit a wall" —
            # while the exception itself carries exactly what to change.
            raise HTTPException(422, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return PlanResponse(previews=previews)

    @router.post("/render", response_model=RenderResponse)
    async def render(request: RenderRequest) -> RenderResponse:
        """LOCK: render one previewed plan and record lineage (the paid step)."""

        try:
            asset_id = await service.render_short(
                request.plan_id, project_id=request.project_id,
                session_id=request.session_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return RenderResponse(asset_id=asset_id)

    return router
