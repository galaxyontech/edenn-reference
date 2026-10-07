"""Typed accessor over a session's ``state_json``.

Reads are typed and the candidate branching graph is a queryable object; the
canonical store remains the dict (MVP typed layer — no new tables). Mutation
still happens dict-side in the tools, so the persisted/wire shape is unchanged.
"""

from __future__ import annotations

from typing import Any, Optional

from ..models import MusicProposalCard
from ..tools.media import normalize_music_modelspec
from .artifacts import (
    CandidateGraph,
    FinalArtifact,
    Mix,
    ProductionPlan,
    SfxLayer,
    VoiceoverLayer,
)


class SessionState:
    def __init__(self, state_json: Optional[dict[str, Any]]) -> None:
        self._raw = dict(state_json or {})

    @property
    def raw(self) -> dict[str, Any]:
        return self._raw

    # ----- artifact collections ----------------------------------------------

    @property
    def candidates(self) -> list[dict[str, Any]]:
        return list(self._raw.get("candidates") or [])

    @property
    def proposals(self) -> list[dict[str, Any]]:
        return list(self._raw.get("proposals") or [])

    @property
    def graph(self) -> CandidateGraph:
        return CandidateGraph(self.candidates)

    # ----- typed singletons ---------------------------------------------------

    @property
    def approved_direction(self) -> bool:
        return bool(self._raw.get("approved_direction"))

    @property
    def production_plan(self) -> Optional[ProductionPlan]:
        plan = self._raw.get("production_plan")
        return ProductionPlan.model_validate(plan) if plan else None

    @property
    def mix(self) -> Optional[Mix]:
        mix = self._raw.get("mix")
        return Mix.model_validate(mix) if mix else None

    @property
    def voiceover(self) -> Optional[VoiceoverLayer]:
        layer = (self._raw.get("layers") or {}).get("voiceover")
        return VoiceoverLayer.model_validate(layer) if layer else None

    @property
    def sfx(self) -> Optional[SfxLayer]:
        layer = (self._raw.get("layers") or {}).get("sfx")
        # Legacy shape was a list ([]); only a dict is a real SFX layer.
        return SfxLayer.model_validate(layer) if isinstance(layer, dict) else None

    @property
    def final_artifact(self) -> Optional[FinalArtifact]:
        final = self._raw.get("final_artifact")
        return FinalArtifact.model_validate(final) if final else None

    # ----- resolution ---------------------------------------------------------

    def find_proposal(self, proposal_id: str) -> Optional[dict[str, Any]]:
        for proposal in self.proposals:
            if str(proposal.get("proposal_id")) == str(proposal_id):
                return dict(proposal)
        return None

    def resolve_proposal(self, tool_args: dict[str, Any]) -> dict[str, Any]:
        """A stored proposal by id, or an inline plan from tool args."""

        proposal_id = tool_args.get("proposal_id")
        if proposal_id:
            proposal = self.find_proposal(str(proposal_id))
            if proposal is not None:
                return proposal
        if tool_args.get("prompt"):
            return MusicProposalCard(
                proposal_id=str(proposal_id or "proposal_inline"),
                title=str(tool_args.get("title") or "Music Direction"),
                prompt=str(tool_args.get("prompt")),
                modelspec=normalize_music_modelspec(str(tool_args.get("modelspec") or "")),
                include_vocals=bool(tool_args.get("include_vocals", False)),
                vocal_gender=str(tool_args.get("vocal_gender") or "female"),
                music_volume=float(tool_args.get("music_volume") or 0.85),
            ).model_dump(mode="json")
        raise KeyError(
            f"Proposal not found and no inline plan provided: {proposal_id!r}"
        )


__all__ = ["SessionState"]
