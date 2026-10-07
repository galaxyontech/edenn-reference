"""Typed campaign domain — the NEW objects of the advertiser journey.

These models are deliberately decoupled from the engine packages: they refer
to engine artifacts (assets, plans, publications) by string id only, so the
campaign shell can evolve without touching the creation/aul/ads contracts.
The stage machine here is the single source of truth for what the frontend
may do next; the API router refuses actions that do not match the stage.
"""
from __future__ import annotations

import enum
from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class CampaignStage(enum.StrEnum):
    """Lifecycle of a campaign; mirrors the frontend store's stage machine.

    Order matters: transitions only move forward, one step at a time, via the
    dedicated service methods (never by writing the field directly).
    """

    BRIEF = "brief"          # composer open; nothing resolved yet
    ROLES = "roles"          # brief sent; ask-first questions outstanding/answered
    PLANNED = "planned"      # variants planned + previewable; render not approved
    RENDERED = "rendered"    # Gate 1 passed; takes rendered locally
    LIVE = "live"            # Gate 2 passed; sandbox publications live
    PROPOSAL = "proposal"    # outcomes produced a proposal awaiting Gate 3
    ROUND2 = "round2"        # proposal approved; next generation planned


class TreatmentKind(enum.StrEnum):
    """The three creation treatments this campaign's variants use.

    NOTE: these are campaign-level labels; the creation engine's own
    TreatmentSpec is constructed from them by the service. The campaign canvas
    renders DIFFERENT variants than the audio session canvas — each branch
    head is a differently-treated plan of the same source, not takes of one
    direction.
    """

    HOOK_FIRST = "hook_first"        # hook-first cut, music variant
    FOUNDER_VOICE = "founder_voice"  # source speech bite kept intact
    REPHRASE = "rephrase"            # transcript -> rewrite -> house voice


class AmbiguityQuestionOut(BaseModel):
    """An ask-first question surfaced to the advertiser (never defaulted)."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Stable question id, unique within the campaign")
    text: str = Field(description="The question, advertiser-facing wording")
    chips: tuple[str, ...] = Field(description="Mutually exclusive answer choices")
    answer: str | None = Field(default=None, description="Chosen chip, once answered")


class VariantRecord(BaseModel):
    """One campaign variant: a differently-treated plan of the campaign source.

    Engine linkage is by id only: ``plan_id`` points at the creation plan,
    ``publication_id`` at the ads publication. ``take_url`` is the locally
    composed render (media URL served by the devserver), never a provider URL.
    """

    id: str = Field(description='Campaign-local id ("A", "B", "C", "A2", ...)')
    label: str = Field(description="Display label, e.g. 'A · hook-first'")
    round: int = Field(ge=1, description="Campaign round (generation) this belongs to")
    treatment: TreatmentKind
    grounding: str = Field(description="Human grounding line (collection + moments)")
    relative_note: str | None = Field(
        default=None,
        description="Nearest PUBLISHED relative + its real metric; None = the new bet",
    )
    plan_id: str | None = Field(default=None, description="Creation-engine plan id")
    preview_slots: tuple[float, ...] = Field(
        default=(), description="Slot durations (s) from the plan preview, for strips"
    )
    state: str = Field(
        default="planned",
        description="planned | rendering | rendered | live | killed",
    )
    take_url: str | None = Field(default=None, description="Local media URL once rendered")
    publication_id: str | None = Field(default=None, description="Sandbox publication id")
    halo: str | None = Field(default=None, description="Performance one-liner once live")
    parent_variant_id: str | None = Field(
        default=None,
        description="Round-N children record which round-(N-1) variant they grow from",
    )


class LaunchSplitRow(BaseModel):
    """One row of the market agent's budget split, with its WHY."""

    model_config = ConfigDict(frozen=True)

    variant_id: str
    budget_label: str = Field(description='e.g. "$600 · 40%"')
    why: str = Field(description="The agent's stated reason for this share")


class LaunchPlan(BaseModel):
    """The market agent's first artifact: how real budget would be spent.

    In this hookup the channel is the SANDBOX adapter — the full loop runs
    with zero spend; the consent copy still treats Gate 2 as the real-budget
    boundary because that is the product behavior being prototyped.
    """

    model_config = ConfigDict(frozen=True)

    split: tuple[LaunchSplitRow, ...]
    pacing: str = Field(description="Pacing + promote/kill criteria, one line")
    autonomy: str = Field(description="Autonomous-vs-asks boundary, one line")
    flight: str = Field(description='Flight summary, e.g. "May 1–14 · TikTok 9:16 · $1,500"')


class OutcomeRow(BaseModel):
    """One variant's live numbers as shown on the console KPI table."""

    model_config = ConfigDict(frozen=True)

    variant_id: str
    headline: str = Field(description='Lead metric, e.g. "2.9% CTR"')
    detail: str = Field(description='Spend/pacing detail, e.g. "$412 · pacing on-track"')
    status: str = Field(description="live | killed")


class FeedItem(BaseModel):
    """One agent-feed entry; every observation must trace to outcome data."""

    model_config = ConfigDict(frozen=True)

    day: str
    text: str
    evidence_variant_id: str | None = Field(
        default=None, description="Variant whose outcomes justify this entry"
    )
    is_proposal: bool = Field(default=False)


class ProposalAction(BaseModel):
    """One typed, separately-approvable action inside a proposal."""

    id: str = Field(description="iterate | reallocate | retire")
    label: str
    cost: str = Field(description='"render cost" | "free"')
    approved: bool = Field(default=True, description="Checkbox state at approval time")


class Proposal(BaseModel):
    """The market agent's thesis: evidence, typed actions, one approval.

    Approving ``iterate`` COMPILES the next round: winners' spans become
    locks, the losing close becomes an exclude, and the service plans the
    round-2 variants as children of the winning variant — the lineage tree
    grows a generation.
    """

    evidence: tuple[str, ...]
    actions: tuple[ProposalAction, ...]


class IngestQuestion(BaseModel):
    """The organize-by-asking question raised during library ingest."""

    id: str
    text: str
    chips: tuple[str, ...]
    answer: str | None = None


class Campaign(BaseModel):
    """The spine object: everything user-visible hangs off it."""

    id: str
    name: str
    stage: CampaignStage = CampaignStage.BRIEF
    brief: str = Field(description="The (prefilled) natural-language brief")
    source_asset_id: str | None = Field(default=None, description="Spine source asset id")
    questions: list[AmbiguityQuestionOut] = Field(default_factory=list)
    roles: list[tuple[str, str]] = Field(
        default_factory=list, description="(role, resolution) pairs once locked"
    )
    variants: list[VariantRecord] = Field(default_factory=list)
    launch: LaunchPlan | None = None
    outcomes: list[OutcomeRow] = Field(default_factory=list)
    feed: list[FeedItem] = Field(default_factory=list)
    proposal: Proposal | None = None
    started_on: date | None = None
