"""Artifact entities + the candidate branching graph.

Proposals and Candidates already have rich Pydantic models (reused here as
``Proposal`` / ``Candidate``); ``Mix`` / ``VoiceoverLayer`` / ``FinalArtifact`` /
``ProductionPlan`` formalize dict shapes that previously floated untyped inside
``state_json``. All artifact models allow extra keys so parsing then dumping is
loss-free (the canonical store stays the dict).
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

from ..models import MusicCandidateCard, MusicProposalCard

# A direction the user can pick, and a generated take. These are THE artifact
# types the workbench renders; keep them as the existing cards.
Proposal = MusicProposalCard
Candidate = MusicCandidateCard


class _Artifact(BaseModel):
    # Never drop unknown keys — a typed view must round-trip the dict byte-for-byte.
    model_config = ConfigDict(extra="allow")


class Mix(_Artifact):
    """A composed music + voice-over mix over the source video (compose_mix)."""

    music_candidate_id: Optional[str] = None
    music_volume: float = 0.85
    voiceover_volume: float = 1.0
    voiceover_start_s: float = 0.0
    duck_gain_db: float = -9.0
    preserve_original_audio: bool = False
    status: Optional[str] = None
    video_url: Optional[str] = None
    message: Optional[str] = None


class VoiceoverLayer(_Artifact):
    """A narration layer: a drafted script + chosen voice, then its TTS audio."""

    script: Optional[str] = None
    voice_id: Optional[str] = None
    language: Optional[str] = None
    tone: Optional[str] = None
    speed: Optional[float] = None
    status: Optional[str] = None
    linked_job_id: Optional[str] = None
    audio_url: Optional[str] = None


class SfxEvent(_Artifact):
    """One spotted sound-effect moment in the SFX plan (editable)."""

    id: Optional[str] = None
    label: Optional[str] = None
    prompt: Optional[str] = None
    start_s: Optional[float] = None


class SfxVariant(_Artifact):
    """One rendered SFX take (the user can generate several and pick one)."""

    variant_id: Optional[str] = None
    label: Optional[str] = None
    status: Optional[str] = None
    linked_job_id: Optional[str] = None
    audio_url: Optional[str] = None
    video_url: Optional[str] = None
    placeholder: Optional[bool] = None


class SfxLayer(_Artifact):
    """A sound-effects layer: a spotted event plan + one or more rendered
    variants over the source video. Parallel to :class:`VoiceoverLayer`, but
    carries a list of variants (A/B) rather than a single track."""

    status: Optional[str] = None
    summary: Optional[str] = None
    ambience: Optional[str] = None
    events: list[SfxEvent] = []
    variants: list[SfxVariant] = []
    selected_variant_id: Optional[str] = None


class FinalArtifact(_Artifact):
    """The session's resolved deliverable."""

    status: Optional[str] = None
    video_url: Optional[str] = None
    audio_url: Optional[str] = None
    deliverable: Optional[str] = None


class ProductionPlan(_Artifact):
    """How the session sequences its audio layers."""

    mode: Optional[str] = None
    layers: list[str] = []


class CandidateGraph:
    """The candidate branching graph (parent/version/edit_kind) as a queryable
    object over the raw candidate dicts stored in ``state_json``.

    Operates on dicts (the canonical store); ``typed()`` lifts them to
    ``Candidate`` models when a typed view is wanted.
    """

    def __init__(self, candidates: Optional[list[dict[str, Any]]]) -> None:
        self._candidates = [dict(c) for c in (candidates or [])]

    def __len__(self) -> int:
        return len(self._candidates)

    @property
    def candidates(self) -> list[dict[str, Any]]:
        return [dict(c) for c in self._candidates]

    def typed(self) -> list[Candidate]:
        return [Candidate.model_validate(c) for c in self._candidates]

    def find(self, candidate_id: str) -> Optional[dict[str, Any]]:
        for c in self._candidates:
            if str(c.get("candidate_id")) == str(candidate_id):
                return dict(c)
        return None

    def children_of(self, parent_id: str) -> list[dict[str, Any]]:
        return [
            dict(c)
            for c in self._candidates
            if str(c.get("parent_candidate_id")) == str(parent_id)
        ]

    def next_version(self, parent_id: str, parent: dict[str, Any]) -> int:
        versions = [int(parent.get("version") or 1)]
        for c in self._candidates:
            if str(c.get("parent_candidate_id")) == str(parent_id):
                versions.append(int(c.get("version") or 1))
        return max(versions) + 1

    def resolve(self, candidate_id: str) -> tuple[dict[str, Any], str]:
        """Target candidate from an id (or the only one if unambiguous)."""

        if candidate_id:
            candidate = self.find(candidate_id)
            if candidate is not None:
                return candidate, str(candidate_id)
        if len(self._candidates) == 1:
            only = dict(self._candidates[0])
            return only, str(only.get("candidate_id"))
        raise KeyError(f"Candidate not found: {candidate_id!r}")


__all__ = [
    "Candidate",
    "CandidateGraph",
    "FinalArtifact",
    "Mix",
    "ProductionPlan",
    "Proposal",
    "VoiceoverLayer",
]
