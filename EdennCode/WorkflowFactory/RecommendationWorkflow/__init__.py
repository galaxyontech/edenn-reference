"""Track 1 recommendation workflow: deterministic SQL + RAG cosine ranking.

Three stages, each runnable as `python -m
EdennCode.WorkflowFactory.RecommendationWorkflow.<stage> --help` so Track 2's
LLM-driven progressive-disclosure skill can shell out to them as scripts.

  build_user_profile_stage   reads generation_job + creative for one user_id,
                             returns preferred_language/vocals + prompt centroid.
  retrieve_candidates_stage  scores creative_feature_snapshot rows in either
                             user-RAG mode (cosine + alignment) or global mode
                             (alignment_score DESC).
  hydrate_stage              joins creative + music_asset + snapshot for the
                             slim or debug response payload.
"""

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
from EdennCode.WorkflowFactory.RecommendationWorkflow.workflow import (
    RecommendationWorkflow,
    RecommendationWorkflowInput,
    RecommendationWorkflowResult,
)

__all__ = [
    "BuildUserProfileStage",
    "Candidate",
    "HydrateStage",
    "RecommendationItem",
    "RecommendationWorkflow",
    "RecommendationWorkflowInput",
    "RecommendationWorkflowResult",
    "RetrieveCandidatesStage",
    "UserProfile",
]
