"""First-class domain entities for agentic audio.

The redesign's User / Agent / Artifacts separation, MVP form: typed entities over
the existing ``sessions.state_json`` blob — no new DB tables (deferred). Reads are
typed and the candidate branching graph (parent/version/edit_kind) is a queryable
object; the canonical store remains the dict, so the wire/persistence contract is
unchanged.
"""

from __future__ import annotations

from .artifacts import (
    Candidate,
    CandidateGraph,
    FinalArtifact,
    Mix,
    ProductionPlan,
    Proposal,
    VoiceoverLayer,
)
from .session_state import SessionState
from .user import User

__all__ = [
    "Candidate",
    "CandidateGraph",
    "FinalArtifact",
    "Mix",
    "ProductionPlan",
    "Proposal",
    "SessionState",
    "User",
    "VoiceoverLayer",
]
