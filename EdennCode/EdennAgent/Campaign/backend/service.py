"""CampaignService — the campaign shell orchestrating the built engines.

Delegation map (nothing here re-implements an engine):
  - roles / ask-first ............ creation.CreationService.resolve (BundleResolver)
  - plan / preview (free) ........ creation.CreationService.plan_short
  - render (Gate 1) .............. creation.CreationService.render_short
                                   (local ffmpeg compose; lineage recorded by the
                                   engine via LineageRecorder.record_variant)
  - publish (Gate 2) ............. ads.CampaignPublisher over the SANDBOX adapter
  - outcomes / attribution ....... ads.CampaignPublisher.collect_outcomes +
                                   ads.AttributionEngine (deterministic sandbox data)
  - library board ................ library_api.LibraryService

Campaign-only logic (the thin shell): the stage machine, the thread
transcript, the campaign questions, the launch plan, the proposal whose
``iterate`` action COMPILES round 2 (winner's opening becomes a Knobs lock,
the killed close becomes an exclude interval) — the loop-closing piece.

Honesty rules: the winner/killed narrative is DERIVED from the sandbox's
actual (deterministic) numbers, never scripted; if the rephrase treatment has
no model client, variant C is planned as a music-only cut and the thread says
so. The API surfaces engine questions verbatim.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from EdennCode.EdennAgent.AssetLibrary.refs import Ref
from EdennCode.EdennAgent.AssetLibrary.repository import AulRepository
from EdennCode.EdennAgent.AdsAdapters import AttributionEngine, CampaignPublisher
from EdennCode.EdennAgent.Creation.domain import (
    BundleItem,
    BundleResolution,
    MusicSource,
    RequestBundle,
    ResolvedBundle,
    ResolvedSource,
    ShortSpec,
    SourceRole,
    TreatmentKind as EngineTreatment,
    TreatmentSpec,
)
from EdennCode.EdennAgent.Creation.service import CreationService
from EdennCode.EdennAgent.Recompose.domain import Knobs
from EdennCode.EdennAgent.AssetLibraryApi.service import LibraryService

from .models import (
    AmbiguityQuestionOut,
    Campaign,
    CampaignStage,
    FeedItem,
    IngestQuestion,
    LaunchPlan,
    LaunchSplitRow,
    OutcomeRow,
    Proposal,
    ProposalAction,
    TreatmentKind,
    VariantRecord,
)

log = logging.getLogger("edenn.campaign")

_SHORT_DURATION_S = 8.0
_OUTCOME_DAYS = 4
_MUSIC_QUESTION_ID = "campaign-music"

# Sparser cuts + a higher per-scene cap: three-plus-two non-overlapping cuts
# must fit in ~85s of fixture footage, so give the planner room to breathe.
_CAMPAIGN_KNOBS = Knobs(cut_density="dense", max_slots_per_scene=3)


class StageError(RuntimeError):
    """An action was attempted out of stage order (router maps to 409)."""


@dataclass
class _Sources:
    """The campaign's registered source assets (real AUL ids)."""

    spine_id: str
    accent_id: str
    music_id: str
    accent2_id: Optional[str] = None
    unfiled_id: Optional[str] = None
    collections: dict[str, str] = field(default_factory=dict)  # asset_id -> collection label


class CampaignService:
    """Single-campaign orchestrator (prototype scope: one campaign, one process).

    All heavy lifting happens in the injected engines; this class owns the
    stage machine, the thread transcript, and the campaign-shaped read model
    the frontend consumes via :meth:`snapshot`.
    """

    def __init__(
        self,
        repo: AulRepository,
        creation: CreationService,
        publisher: CampaignPublisher,
        attribution: AttributionEngine,
        library: LibraryService,
        *,
        project_id: str = "default",
        rephrase_available: bool = False,
    ) -> None:
        self._repo = repo
        self._creation = creation
        self._publisher = publisher
        self._attribution = attribution
        self._library = library
        self._project_id = project_id
        self._rephrase_available = rephrase_available

        self._sources: Optional[_Sources] = None
        self._rendered_assets: dict[str, str] = {}
        self._resolved: Optional[ResolvedBundle] = None
        self._engine_questions: list[AmbiguityQuestionOut] = []
        self._exclude_intervals: dict[str, list[tuple[float, float]]] = {}
        self._variant_spans: dict[str, dict[str, list[tuple[float, float]]]] = {}
        self._thread: list[dict[str, Any]] = []
        self._ingest_question: Optional[IngestQuestion] = None

        self._campaign = Campaign(
            id="launch-week",
            name="Launch week — spring line",
            brief=(
                "Three 12s cuts from the shoot — one hook-first, one keeping the "
                "source voice, one rephrased in our voice."
            ),
            started_on=date.today(),
        )
        self._say(
            "New campaign. Give me the brief — I’ll resolve the roles from your "
            "library and only ask what’s genuinely ambiguous."
        )

    # ------------------------------------------------------------------ setup
    def register_sources(
        self,
        *,
        spine_id: str,
        accent_id: str,
        music_id: str,
        accent2_id: Optional[str] = None,
        unfiled_id: Optional[str] = None,
    ) -> None:
        """Wire the seeded AUL asset ids into the campaign (devserver calls this).

        ``unfiled_id`` is the asset the organize-by-asking ingest question is
        about; the others are pre-filed into the campaign collection.
        """
        collections = {spine_id: "Spring shoot", accent_id: "Spring shoot", music_id: "Music"}
        if accent2_id is not None:
            collections[accent2_id] = "Spring shoot"
        self._sources = _Sources(
            spine_id=spine_id, accent_id=accent_id, music_id=music_id,
            accent2_id=accent2_id, unfiled_id=unfiled_id, collections=collections,
        )
        if unfiled_id is not None:
            asset = self._repo.get_asset(unfiled_id)
            name = asset.name if asset else unfiled_id
            self._ingest_question = IngestQuestion(
                id="ingest-unfiled",
                text=(
                    f"“{name}” looks like a different shoot/day than the other clips — "
                    "part of this campaign’s collection, or something else?"
                ),
                chips=("Spring shoot", "Just b-roll, unsorted"),
            )
        self._campaign.source_asset_id = spine_id

    # ------------------------------------------------------------ thread utils
    def _say(self, text: str) -> None:
        self._thread.append({"kind": "agent", "text": text})

    def _require_stage(self, *allowed: CampaignStage) -> None:
        if self._campaign.stage not in allowed:
            raise StageError(
                f"action not valid in stage '{self._campaign.stage}' "
                f"(expected one of: {', '.join(s.value for s in allowed)})"
            )

    def _sources_or_raise(self) -> _Sources:
        if self._sources is None:
            raise StageError("campaign sources not registered — devserver seeding incomplete")
        return self._sources

    # ---------------------------------------------------------------- actions
    def ingest_answer(self, choice: str) -> None:
        """Answer the organize-by-asking question; files the unfiled asset."""
        src = self._sources_or_raise()
        q = self._ingest_question
        if q is None or q.answer is not None:
            return
        q.answer = choice
        if src.unfiled_id is not None:
            label = "Spring shoot" if choice == "Spring shoot" else "unsorted"
            src.collections[src.unfiled_id] = label

    def send_brief(self) -> None:
        """Send the brief: run the REAL resolver; surface its questions verbatim.

        The two videos are submitted role-less, so the engine's own
        role_conflict question ("which is the spine?") comes back — the same
        ask-first machinery the stable surface uses. The campaign adds one
        campaign-level question (music handling) that compiles into the
        variants' TreatmentSpec.music.
        """
        self._require_stage(CampaignStage.BRIEF)
        src = self._sources_or_raise()
        c = self._campaign
        self._thread.append({"kind": "user", "text": c.brief})

        items = [
            BundleItem(ref=src.spine_id),
            BundleItem(ref=src.accent_id),
            BundleItem(ref=src.music_id, role=SourceRole.MUSIC),
        ]
        if src.accent2_id is not None:
            items.insert(2, BundleItem(ref=src.accent2_id, role=SourceRole.ACCENT))
        bundle = RequestBundle(items=items, intent=c.brief)
        resolution: BundleResolution = self._creation.resolve(bundle)
        self._engine_questions = []
        if resolution.status == "needs_input":
            for i, q in enumerate(resolution.questions):
                out = AmbiguityQuestionOut(
                    id=f"engine-{i}",
                    text=q.question,
                    chips=tuple(self._friendly_option(o.label) for o in q.options),
                )
                self._engine_questions.append(out)
        else:
            # Single unambiguous path — unusual with two videos, but honor it.
            self._resolved = resolution.bundle

        music_q = AmbiguityQuestionOut(
            id=_MUSIC_QUESTION_ID,
            text="The track — use it as-is, or make each cut a variant in its style?",
            chips=("Use as-is", "Variant in its style"),
        )
        c.questions = list(self._engine_questions) + [music_q]
        self._say(
            "Resolved what the library makes unambiguous. "
            f"{len(c.questions)} question(s) before I plan — ask-first, never a silent default:"
        )
        for q in c.questions:
            self._thread.append(
                {"kind": "question", "id": q.id, "text": q.text, "chips": list(q.chips), "answer": None}
            )
        c.stage = CampaignStage.ROLES

    def answer_question(self, qid: str, choice: str) -> None:
        """Record an answer; when all are answered, lock roles via re-resolve."""
        self._require_stage(CampaignStage.ROLES)
        c = self._campaign
        matched = None
        for i, q in enumerate(c.questions):
            if q.id == qid and q.answer is None:
                matched = i
                break
        if matched is None:
            return
        c.questions[matched] = c.questions[matched].model_copy(update={"answer": choice})
        for m in self._thread:
            if m.get("kind") == "question" and m.get("id") == qid:
                m["answer"] = choice
        if all(q.answer is not None for q in c.questions):
            self._lock_roles()

    def _lock_roles(self) -> None:
        """Compile answers into explicit roles and re-resolve (the engine contract)."""
        src = self._sources_or_raise()
        c = self._campaign

        spine_id, accent_id = src.spine_id, src.accent_id
        for q in c.questions:
            if q.id.startswith("engine-") and q.answer:
                # The engine's spine question options are labeled by asset name;
                # pick whichever registered video the chosen label names.
                accent_asset = self._repo.get_asset(src.accent_id)
                if accent_asset is not None and accent_asset.name in q.answer:
                    spine_id, accent_id = src.accent_id, src.spine_id

        items = [
            BundleItem(ref=spine_id, role=SourceRole.SPINE),
            BundleItem(ref=accent_id, role=SourceRole.ACCENT),
            BundleItem(ref=src.music_id, role=SourceRole.MUSIC),
        ]
        if src.accent2_id is not None:
            items.insert(2, BundleItem(ref=src.accent2_id, role=SourceRole.ACCENT))
        bundle = RequestBundle(items=items, intent=c.brief)
        resolution = self._creation.resolve(bundle)
        if resolution.status != "resolved" or resolution.bundle is None:
            raise StageError("re-resolve with explicit roles still needs input — unexpected")
        self._resolved = resolution.bundle
        c.roles = [(s.role.value, self._asset_name(Ref.asset_of(s.ref)) + (" (explicit)" if s.explicit else f" — {s.reason}")) for s in resolution.bundle.sources]
        self._say("Roles locked. Planning is free — nothing renders until you approve.")

    async def plan(self) -> None:
        """Plan the three differently-treated variants (free; show-before-spend).

        Cross-variant footage reuse is prevented by threading
        ``exclude_intervals`` across the plan_short calls — the engine
        router's own pattern.
        """
        self._require_stage(CampaignStage.ROLES)
        c = self._campaign
        if self._resolved is None:
            raise StageError("answer the questions first — roles are not locked")
        src = self._sources_or_raise()

        music_source, music_ref = MusicSource.PROVIDED, src.music_id
        for q in c.questions:
            if q.id == _MUSIC_QUESTION_ID and q.answer == "Variant in its style":
                music_source = MusicSource.VARIANT_OF

        rephrase_kind = (
            EngineTreatment.REPHRASE_ORIGINAL if self._rephrase_available else EngineTreatment.MUSIC_ONLY
        )
        plan_specs: list[tuple[str, str, TreatmentKind, ShortSpec, Optional[ResolvedBundle]]] = [
            (
                "A", "A · hook-first", TreatmentKind.HOOK_FIRST,
                ShortSpec(
                    hypothesis="Hook-first: open on the strongest moment, music carries it",
                    duration_s=_SHORT_DURATION_S,
                    treatment=TreatmentSpec(
                        kind=EngineTreatment.MUSIC_ONLY, music=music_source, music_ref=music_ref,
                        knobs=_CAMPAIGN_KNOBS,
                    ),
                    name="A · hook-first",
                ),
                None,
            ),
            (
                "B", "B · source voice", TreatmentKind.FOUNDER_VOICE,
                ShortSpec(
                    hypothesis="Keep the source speech intact; music under",
                    duration_s=_SHORT_DURATION_S,
                    treatment=TreatmentSpec(
                        kind=EngineTreatment.KEEP_ORIGINAL, music=music_source, music_ref=music_ref,
                        knobs=_CAMPAIGN_KNOBS,
                    ),
                    name="B · source voice",
                ),
                self._bundle_with_voice(),
            ),
            (
                "C", "C · rephrased, house voice", TreatmentKind.REPHRASE,
                ShortSpec(
                    hypothesis="Rephrase the source message in the house voice",
                    duration_s=_SHORT_DURATION_S,
                    treatment=TreatmentSpec(
                        kind=rephrase_kind, music=music_source, music_ref=music_ref,
                        knobs=_CAMPAIGN_KNOBS,
                    ),
                    name="C · rephrased",
                ),
                None,
            ),
        ]

        c.variants = []
        self._exclude_intervals = {}
        for vid, label, kind, spec, bundle_override in plan_specs:
            bundle = bundle_override or self._resolved
            preview, plan = await self._creation.plan_short(
                bundle, spec, exclude_intervals=self._exclude_intervals or None
            )
            spans: dict[str, list[tuple[float, float]]] = {}
            for slot in plan.plan.slots:
                span = (slot.seg_in_s, slot.seg_in_s + slot.spec.dur_s)
                self._exclude_intervals.setdefault(slot.asset_id, []).append(span)
                spans.setdefault(slot.asset_id, []).append(span)
            self._variant_spans[vid] = spans
            c.variants.append(
                VariantRecord(
                    id=vid, label=label, round=1, treatment=kind,
                    grounding=f"@Spring shoot · {len(plan.plan.slots)} slots · engine-planned",
                    relative_note=self._relative_note_for(kind),
                    plan_id=plan.plan.plan_id,
                    preview_slots=tuple(round(s.spec.dur_s, 2) for s in plan.plan.slots),
                    state="planned",
                )
            )
        if not self._rephrase_available:
            self._say(
                "Note: no model client on this mount, so C is planned as a music-only "
                "cut — the rephrase treatment needs the script model."
            )
        self._thread.append({"kind": "plans"})
        self._say(
            "Three cuts planned by the engine, footage non-overlapping across them. "
            "Watch them all — previews are free."
        )
        c.stage = CampaignStage.PLANNED

    def _bundle_with_voice(self) -> ResolvedBundle:
        """B's bundle: the accent video carries role=voice so its speech leads."""
        assert self._resolved is not None
        sources = []
        for s in self._resolved.sources:
            if s.role == SourceRole.ACCENT:
                sources.append(
                    ResolvedSource(ref=s.ref, role=SourceRole.VOICE, kind=s.kind,
                                   reason="campaign: source-voice variant", explicit=True)
                )
            else:
                sources.append(s)
        return ResolvedBundle(sources=sources, intent=self._resolved.intent)

    async def render(self) -> None:
        """Gate 1 — render every planned variant via the engine (local compose)."""
        self._require_stage(CampaignStage.PLANNED)
        c = self._campaign
        for v in c.variants:
            v.state = "rendering"
        for v in c.variants:
            try:
                asset_id = await self._creation.render_short(
                    v.plan_id or "", project_id=self._project_id, session_id=c.id
                )
                v.state = "rendered"
                v.take_url = f"/api/v2/library/media/{asset_id}"
                self._rendered_assets[v.id] = asset_id
            except (KeyError, ValueError) as exc:
                v.state = "planned"
                self._say(f"{v.label}: render refused — {exc}")
                raise
        self._say("Rendered locally. The market agent has a launch plan ready.")
        c.launch = self._build_launch_plan()
        c.stage = CampaignStage.RENDERED

    def _build_launch_plan(self) -> LaunchPlan:
        c = self._campaign
        shares = ("$600 · 40%", "$450 · 30%", "$450 · 30%")
        whys = ("strongest opener in previews", "authenticity vs A", "message clarity vs B")
        return LaunchPlan(
            split=tuple(
                LaunchSplitRow(variant_id=v.id, budget_label=shares[i], why=whys[i])
                for i, v in enumerate(c.variants[:3])
            ),
            pacing="Even flight · promote at CTR ≥ 2.4% (3 days) · kill at CVR < 0.4% after $150",
            autonomy=(
                "Autonomous: pull outcomes daily · alert on criteria · reallocate ≤15% (logged). "
                "Asks first: pause a variant · new creative round."
            ),
            flight="14 days · sandbox channel · $1,500",
        )

    async def launch(self) -> None:
        """Gate 2 — publish to the sandbox channel and collect real outcome rows.

        The winner/killed narrative is DERIVED from the sandbox's deterministic
        metrics: variants are ranked by actual CTR totals.
        """
        self._require_stage(CampaignStage.RENDERED)
        c = self._campaign
        start = c.started_on or date.today()
        dates = [(start + timedelta(days=i)).isoformat() for i in range(_OUTCOME_DAYS)]

        totals = {}
        for v in c.variants:
            asset_id = self._rendered_assets[v.id]
            channel_ref = await self._publisher.publish(
                asset_id, title=v.label, campaign=c.id, project_id=self._project_id
            )
            v.publication_id = channel_ref
            await self._publisher.collect_outcomes(
                asset_id, channel_ref, dates, project_id=self._project_id
            )
            totals[v.id] = self._attribution.variant_totals(asset_id)

        ranked = sorted(c.variants, key=lambda v: totals[v.id].ctr, reverse=True)
        winner, middle, worst = ranked[0], ranked[1], ranked[-1]
        for v in c.variants:
            t = totals[v.id]
            v.state = "live"
            v.halo = f"{t.ctr * 100:.2f}% CTR · {t.impressions:,} imp"
        worst.state = "killed"
        worst.halo = f"killed — CTR {totals[worst.id].ctr * 100:.2f}% (lowest of the set)"

        c.outcomes = [
            OutcomeRow(
                variant_id=v.id,
                headline=f"{totals[v.id].ctr * 100:.2f}% CTR",
                detail=f"${totals[v.id].spend:,.0f} spent · CVR {totals[v.id].cvr * 100:.2f}%",
                status=v.state,
            )
            for v in ranked
        ]
        c.feed = [
            FeedItem(
                day=f"Day {_OUTCOME_DAYS}",
                text=(
                    f"Drafted a proposal — {winner.label} leads at "
                    f"{totals[winner.id].ctr * 100:.2f}% CTR; {worst.label} is the weakest and "
                    "hit the kill line."
                ),
                evidence_variant_id=winner.id,
                is_proposal=True,
            ),
            FeedItem(
                day="Day 3",
                text=f"Reallocated 10% {worst.id}→{winner.id} — inside the ≤15% boundary. Logged, not asked.",
                evidence_variant_id=worst.id,
            ),
            FeedItem(
                day="Day 2",
                text=f"{winner.label} outperforming from the first pull — its opening carries the click.",
                evidence_variant_id=winner.id,
            ),
        ]
        c.proposal = Proposal(
            evidence=tuple(
                f"{v.label}: {totals[v.id].ctr * 100:.2f}% CTR · CVR {totals[v.id].cvr * 100:.2f}%"
                for v in ranked
            ),
            actions=(
                ProposalAction(
                    id="iterate",
                    label=f"Iterate — lock {winner.label}’s opening, vary the close",
                    cost="render cost",
                ),
                ProposalAction(
                    id="realloc", label=f"Reallocate {worst.label}’s remaining budget", cost="free"
                ),
                ProposalAction(id="retire", label=f"Retire {worst.label}", cost="free"),
            ),
        )
        self._say(
            "Live on the sandbox channel. Outcomes pulled and attributed — "
            "the proposal is waiting on the console."
        )
        c.stage = CampaignStage.PROPOSAL

    async def approve(self, approved_ids: list[str]) -> None:
        """Gate 3 — approving ``iterate`` COMPILES round 2 with real locks.

        The winner's first slot becomes a Knobs lock (same span, same slot 0);
        the killed variant's slots join the exclude intervals so its footage
        cannot recur. Round-2 variants are planned as children of the winner.
        """
        self._require_stage(CampaignStage.PROPOSAL)
        c = self._campaign
        if "iterate" not in approved_ids:
            self._say("Proposal noted without an iterate — campaign stays live as-is.")
            return
        winner = next(v for v in c.variants if v.state == "live")
        killed = [v for v in c.variants if v.state == "killed"]
        # Round-2 compile: exclude ONLY the killed variants' footage. The
        # winner's spans stay reusable — "lock the winner's opening" MEANS
        # reusing its material; a blanket exclude would contradict the lock.
        round2_excludes: dict[str, list[tuple[float, float]]] = {}
        for kv in killed:
            for aid, spans in self._variant_spans.get(kv.id, {}).items():
                round2_excludes.setdefault(aid, []).extend(spans)
        self._exclude_intervals = round2_excludes

        specs = [
            ("A2", f"A2 · {winner.id}-remix", winner.treatment),
            ("B2", "B2 · new close — variant", TreatmentKind.HOOK_FIRST),
        ]
        assert self._resolved is not None
        src = self._sources_or_raise()
        for vid, label, kind in specs:
            spec = ShortSpec(
                hypothesis=f"Round 2: keep what won ({winner.label}), vary the close",
                duration_s=_SHORT_DURATION_S,
                treatment=TreatmentSpec(
                    kind=EngineTreatment.MUSIC_ONLY,
                    music=MusicSource.PROVIDED,
                    music_ref=src.music_id,
                    knobs=_CAMPAIGN_KNOBS,
                ),
                name=label,
            )
            preview, plan = await self._creation.plan_short(
                self._resolved, spec, exclude_intervals=self._exclude_intervals or None
            )
            spans: dict[str, list[tuple[float, float]]] = {}
            for slot in plan.plan.slots:
                span = (slot.seg_in_s, slot.seg_in_s + slot.spec.dur_s)
                self._exclude_intervals.setdefault(slot.asset_id, []).append(span)
                spans.setdefault(slot.asset_id, []).append(span)
            self._variant_spans[vid] = spans
            c.variants.append(
                VariantRecord(
                    id=vid, label=label, round=2, treatment=kind,
                    grounding=f"locks from round 1 · {len(plan.plan.slots)} slots",
                    relative_note=f"parent {winner.label} ran {winner.halo}",
                    plan_id=plan.plan.plan_id,
                    preview_slots=tuple(round(s.spec.dur_s, 2) for s in plan.plan.slots),
                    state="planned",
                    parent_variant_id=winner.id,
                )
            )
        self._thread.append({
            "kind": "banner",
            "text": (
                f"Round 2 — seeded by campaign outcomes: {winner.label} is the parent · "
                f"{', '.join(k.label for k in killed)} excluded — new closes only."
            ),
        })
        self._thread.append({"kind": "plans2"})
        self._say(
            "Approved. The proposal compiled straight into round 2 — outcomes wrote "
            "this brief. Two children planned off the winner; same loop from here."
        )
        c.stage = CampaignStage.ROUND2

    # -------------------------------------------------------------- read model
    def _friendly_option(self, label: str) -> str:
        """Engine options label by ref; advertisers should see asset names."""
        try:
            asset = self._repo.get_asset(Ref.asset_of(label))
        except Exception:  # noqa: BLE001 — non-ref labels pass through
            return label
        return asset.name if asset is not None else label

    def _asset_name(self, asset_id: str) -> str:
        a = self._repo.get_asset(asset_id)
        return a.name if a is not None else asset_id

    def _relative_note_for(self, kind: TreatmentKind) -> Optional[str]:
        if kind == TreatmentKind.REPHRASE:
            return None  # the round's new bet
        return "closest published relative shown once the archive has history"

    def tree(self) -> dict[str, Any]:
        """The campaign lineage projection for the canvas.

        NOT the audio session canvas: branch heads are differently-treated
        VARIANTS of one campaign, rounds are generations, halos come from the
        sandbox outcomes. Shape: {nodes: [...], edges: [[src, dst], ...]}.
        """
        c = self._campaign
        src_id = c.source_asset_id or "src"
        nodes: list[dict[str, Any]] = [{
            "id": "src", "label": self._asset_name(src_id), "meta": "source",
            "kind": "source",
        }]
        edges: list[list[str]] = []
        for v in c.variants:
            nodes.append({
                "id": v.id, "label": v.label, "meta": v.treatment.value,
                "kind": "take", "state": v.state, "halo": v.halo, "round": v.round,
                "take_url": v.take_url,
            })
            edges.append([v.parent_variant_id or "src", v.id])
        if c.stage == CampaignStage.PROPOSAL and c.proposal is not None:
            winner = next((v for v in c.variants if v.state == "live"), None)
            if winner is not None:
                nodes.append({
                    "id": "ghost-next", "label": "proposed round 2",
                    "meta": "approve in thread", "kind": "ghost",
                })
                edges.append([winner.id, "ghost-next"])
        return {"nodes": nodes, "edges": edges}

    def snapshot(self) -> dict[str, Any]:
        """The full frontend state — the world shape the store exposes."""
        c = self._campaign
        src = self._sources
        assets: list[dict[str, Any]] = []
        collections: dict[str, int] = {}
        if src is not None:
            for summary in self._repo.list_assets(self._project_id):
                a = summary.asset
                label = src.collections.get(a.asset_id, "unsorted" if a.asset_id == src.unfiled_id else "Made")
                if a.generated:
                    label = "Made"
                collections[label] = collections.get(label, 0) + 1
                assets.append({
                    "id": a.asset_id, "name": a.name, "kind": a.kind,
                    "meta": (f"{a.duration_s:.0f}s" if a.duration_s else a.kind),
                    "collection": label, "generated": a.generated,
                })
        # Families: REAL lineage — each source video with the generated takes
        # cut from it (slot_cut edges written by the engine at render time).
        families: list[dict[str, Any]] = []
        if src is not None:
            for summary in self._repo.list_assets(self._project_id):
                a = summary.asset
                if a.kind != "video" or a.generated:
                    continue
                children = []
                for e in self._repo.edges_from(a.asset_id):
                    if e.operation != "slot_cut":
                        continue
                    child = self._repo.get_asset(e.dst_asset_id)
                    if child is None:
                        continue
                    v = next((x for x in c.variants if self._rendered_assets.get(x.id) == child.asset_id), None)
                    note = (v.halo or v.state) if v is not None else "generated"
                    entry = {"name": v.label if v is not None else child.name, "note": note}
                    if entry not in children:
                        children.append(entry)
                families.append({
                    "sourceId": a.asset_id,
                    "used": f"{summary.used_fraction * 100:.0f}% used" if summary.used_fraction else "unused",
                    "children": children,
                })
        return {
            "stage": c.stage.value,
            "brand": "spring-beverage-co",
            "families": families,
            "archiveNumbers": {},
            "ingest": {
                "pending": bool(self._ingest_question and self._ingest_question.answer is None),
                "question": (self._ingest_question.model_dump() if self._ingest_question else None),
                "dedupe": "content-addressed ids: re-adding identical bytes is a no-op.",
            },
            "collections": [
                {"id": name, "name": name, "count": n, "icon": "ti-folder"}
                for name, n in sorted(collections.items())
            ],
            "assets": assets,
            "campaign": {
                "id": c.id, "name": c.name,
                "flight": c.launch.flight if c.launch else "not launched",
                "brief": c.brief,
                # Screens tolerate only the known message kinds.
                "thread": [m for m in self._thread if m.get("kind") in
                           ("agent", "user", "question", "roles", "plans", "banner")],
                "questions": [q.model_dump() for q in c.questions],
                "roles": [list(r) for r in c.roles],
                "variants": [self._variant_view(v) for v in c.variants],
                "launch": self._launch_view(),
                "outcomes": [
                    [self._variant_label(o.variant_id), o.headline, o.detail, o.status]
                    for o in c.outcomes
                ],
                "feed": [
                    {"day": f.day, "text": f.text,
                     "evidence": self._variant_label(f.evidence_variant_id) if f.evidence_variant_id else None,
                     "proposal": f.is_proposal}
                    for f in c.feed
                ],
                "proposal": (
                    {"evidence": list(c.proposal.evidence),
                     "actions": [{"id": a.id, "label": a.label, "cost": a.cost, "on": a.approved}
                                 for a in c.proposal.actions]}
                    if c.proposal else None
                ),
            },
        }

    def _variant_label(self, vid: str | None) -> str:
        for v in self._campaign.variants:
            if v.id == vid:
                return v.label
        return vid or ""

    def _variant_view(self, v: VariantRecord) -> dict[str, Any]:
        """Map a VariantRecord to the exact shape the screens render.

        ``slots`` are the plan's slot durations normalized to percentages (the
        strip sketch); ``relative`` is honest — the seeded world has no
        published archive, so round-1 variants are all the NEW bet; round-2
        variants cite their live parent with its real numbers.
        """
        total = sum(v.preview_slots) or 1.0
        relative = None
        if v.parent_variant_id is not None:
            parent = next((p for p in self._campaign.variants if p.id == v.parent_variant_id), None)
            if parent is not None:
                relative = {"name": parent.label, "note": "parent — ran live in round 1",
                            "metric": parent.halo or "—"}
        return {
            "id": v.id, "label": v.label, "treatment": v.treatment.value,
            "grounding": v.grounding, "relative": relative,
            "state": v.state, "halo": v.halo,
            "slots": [round(100.0 * s / total, 1) for s in v.preview_slots],
            "take_url": v.take_url, "round": v.round,
            "parent_variant_id": v.parent_variant_id,
            "plan_id": v.plan_id, "preview_slots": list(v.preview_slots),
        }

    def _launch_view(self) -> dict[str, Any] | None:
        c = self._campaign
        if c.launch is None:
            return None
        return {
            "split": [[self._variant_label(r.variant_id), r.budget_label, r.why] for r in c.launch.split],
            "pacing": c.launch.pacing,
            "autonomy": c.launch.autonomy,
            "flight": c.launch.flight,
        }


def build_campaign_service(
    repo: AulRepository,
    creation: CreationService,
    *,
    publisher: CampaignPublisher,
    attribution: Optional[AttributionEngine] = None,
    library: Optional[LibraryService] = None,
    project_id: str = "default",
    rephrase_available: bool = False,
) -> CampaignService:
    """Convenience factory mirroring the devserver wiring pattern."""
    return CampaignService(
        repo,
        creation,
        publisher,
        attribution or AttributionEngine(repo),
        library or LibraryService(repo),
        project_id=project_id,
        rephrase_available=rephrase_available,
    )
