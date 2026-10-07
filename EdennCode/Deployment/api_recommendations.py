"""GET /api/v1/recommendations — Track-1 deterministic recommendation endpoint.

Accepts exactly one of:
    ?user_id=<X>             → RAG-style ranking against user's prompt centroid
    ?global=true             → top-K by alignment_score (no user history)

Plus:
    ?limit=<N>               (default 10, max 50)
    ?debug=true              expand response to include score breakdown,
                             description, lyrics, music_prompt_json, etc.

Returns:
    {
      "mode": "user" | "global",
      "is_cold_start": bool,
      "profile_history_count": int,
      "version": "dev-<image_tag>",
      "recommendations": [ ...slim or debug objects... ]
    }
"""
from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from EdennCode.Deployment.api_common import ApiContext, service_version
from EdennCode.Deployment.auth.middleware import resolve_user_id
from EdennCode.Deployment.error_codes import scrub_provider_names
from EdennCode.WorkflowFactory.RecommendationWorkflow import (
    RecommendationWorkflow,
    RecommendationWorkflowInput,
)


class RecommendationResponse(BaseModel):
    mode: str = Field(..., description="'user' or 'global'.")
    is_cold_start: bool = Field(
        ...,
        description="True when user_id mode finds no embedded history; recommendations is then [].",
    )
    profile_history_count: int = Field(
        ...,
        description="Number of generation_job rows the profile was built from. 0 in global mode.",
    )
    version: str = Field(
        default_factory=service_version,
        description="Service version, formatted 'dev-<IMAGE_TAG>'. Defaults to 'dev-local' when unset.",
    )
    recommendations: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Ranked items. Slim by default; ?debug=true expands to include "
                    "score breakdown, description, lyrics_text, music_prompt_json.",
    )


def _resolve_dsn() -> str:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TELEMETRY_DATABASE_URL")
    if not dsn:
        raise HTTPException(
            status_code=503,
            detail="DATABASE_URL is not configured; recommendations unavailable.",
        )
    return dsn


def create_recommendations_router(context: ApiContext) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/api/v1/recommendations",
        response_model=RecommendationResponse,
        summary="Return ranked music recommendations for a user_id, or top-K globally.",
    )
    async def get_recommendations(
        request: Request,
        user_id: Optional[str] = Query(
            None,
            description="Caller-supplied user identifier. Mutually exclusive with global=true.",
        ),
        global_: bool = Query(
            False,
            alias="global",
            description="When true, ignore user history and return top-K creatives by alignment_score.",
        ),
        limit: int = Query(10, ge=1, le=50),
        debug: bool = Query(False, description="Expand response items with score breakdown and metadata."),
    ) -> RecommendationResponse:
        if not global_:
            user_id = resolve_user_id(request, user_id)

        if bool(user_id) == bool(global_):
            raise HTTPException(
                status_code=400,
                detail="Pass exactly one of ?user_id=... or ?global=true.",
            )

        try:
            workflow_input = RecommendationWorkflowInput(
                user_id=user_id,
                global_mode=global_,
                limit=limit,
                debug=debug,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=scrub_provider_names(str(exc))
            ) from exc

        workflow = RecommendationWorkflow(dsn=_resolve_dsn())
        try:
            result = await workflow.run(workflow_input)
        except Exception as exc:
            context.logger.exception("Recommendation workflow failed: %s", exc)
            raise HTTPException(status_code=500, detail="Recommendation workflow failed.") from exc

        return RecommendationResponse(
            mode=result.mode,
            is_cold_start=result.is_cold_start,
            profile_history_count=result.profile_history_count,
            recommendations=result.recommendations,
        )

    return router


__all__ = ["create_recommendations_router"]
