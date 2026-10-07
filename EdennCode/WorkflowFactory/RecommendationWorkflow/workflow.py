"""RecommendationWorkflow orchestrator.

Combines the three stages into one async entry point used by the API route:

    profile = build_user_profile(user_id)            # only when not global
    if user mode and history_count == 0:
        return cold_start_empty_response()
    candidates = retrieve_candidates(profile, limit) # global mode → profile=None
    items = hydrate(candidates, debug)
    return items + metadata
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from EdennCode.WorkflowFactory.RecommendationWorkflow.build_user_profile_stage import (
    BuildUserProfileStage,
    UserProfile,
)
from EdennCode.WorkflowFactory.RecommendationWorkflow.hydrate_stage import (
    HydrateStage,
    RecommendationItem,
)
from EdennCode.WorkflowFactory.RecommendationWorkflow.retrieve_candidates_stage import (
    Candidate,
    RetrieveCandidatesStage,
)


@dataclass(frozen=True)
class RecommendationWorkflowInput:
    user_id: Optional[str] = None
    global_mode: bool = False
    limit: int = 10
    debug: bool = False

    def __post_init__(self) -> None:
        if self.global_mode and self.user_id:
            raise ValueError("Pass exactly one of user_id or global_mode=True.")
        if not self.global_mode and not self.user_id:
            raise ValueError("Pass exactly one of user_id or global_mode=True.")


@dataclass(frozen=True)
class RecommendationWorkflowResult:
    recommendations: list[dict[str, Any]]
    is_cold_start: bool
    profile_history_count: int
    mode: str  # "user" | "global"


class RecommendationWorkflow:
    """End-to-end orchestrator: profile → retrieve → hydrate."""

    def __init__(self, *, dsn: str) -> None:
        self._dsn = dsn
        self._build_profile = BuildUserProfileStage(dsn=dsn)
        self._retrieve = RetrieveCandidatesStage(dsn=dsn)
        self._hydrate = HydrateStage(dsn=dsn)

    async def run(self, input: RecommendationWorkflowInput) -> RecommendationWorkflowResult:
        profile: Optional[UserProfile] = None
        history_count = 0
        mode = "global" if input.global_mode else "user"

        if not input.global_mode:
            assert input.user_id is not None  # validated by __post_init__
            profile = await self._build_profile.run(user_id=input.user_id)
            history_count = profile.history_count
            if profile.history_count == 0:
                return RecommendationWorkflowResult(
                    recommendations=[],
                    is_cold_start=True,
                    profile_history_count=0,
                    mode=mode,
                )

        candidates: list[Candidate] = await self._retrieve.run(
            profile=profile, limit=input.limit
        )
        items: list[RecommendationItem] = await self._hydrate.run(
            candidates=candidates, debug=input.debug
        )
        return RecommendationWorkflowResult(
            recommendations=[item.payload for item in items],
            is_cold_start=False,
            profile_history_count=history_count,
            mode=mode,
        )
