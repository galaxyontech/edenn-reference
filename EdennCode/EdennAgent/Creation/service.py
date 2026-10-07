"""CreationService — the capability orchestrator (AGENTIC_CREATION.md).

Drives one creation request through the loop's first half:

    resolve (ask first) → plan each short under the hard rules → preview

The paid second half (treatment synthesis + ffmpeg render + lineage recording)
runs only behind an explicit lock — :meth:`CreationService.render_short` —
keeping the show-before-spend guarantee structural rather than conventional.

The service depends only on the ``AulRepository`` contract plus the recompose
machinery; it never touches HTTP types, so routers stay thin shells.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

from ..AssetLibrary.lineage import LineageRecorder
from ..AssetLibrary.refs import Ref
from ..AssetLibrary.repository import AulRepository
from ..AssetLibrary.treeview import SegmentTreeAssembler
from ..Recompose.assembly import render_variant
from ..Recompose.cutspec import generate_cut_spec
from ..Recompose.domain import (
    AssetRecord,
    Knobs,
    MusicSheet,
    RecomposePlan,
    RenderedVariant,
    SegmentTree,
)
from ..Recompose.llm_planner import plan_recompose_llm
from ..Recompose.musicsheet import build_music_sheet, provisional_sheet, truncate_sheet
from ..Recompose.planner import plan_recompose
from .bundle import BundleResolver
from .domain import (
    BundleResolution,
    MusicSource,
    RequestBundle,
    ResolvedBundle,
    ShortSpec,
    SourceRole,
    TreatmentKind,
    TreatmentSpec,
)
from .preview import PlanPreview, PlanPreviewBuilder
from .treatments import RephraseOriginalTreatment

#: Roles whose assets provide FOOTAGE to the planner (music/style/exclude don't).
_FOOTAGE_ROLES: frozenset[SourceRole] = frozenset(
    {SourceRole.SPINE, SourceRole.ACCENT, SourceRole.VOICE})


@dataclass(frozen=True)
class ShortPlan:
    """One planned-but-unrendered short: everything a lock needs to render.

    Held server-side between ``plan`` and ``render`` calls (keyed by plan id)
    so the lock step renders exactly what was previewed — the show-before-spend
    contract in data form.
    """

    plan: RecomposePlan
    sheet: MusicSheet
    assets: list[AssetRecord]
    trees: dict[str, SegmentTree]
    spec_name: str
    treatment: TreatmentSpec
    bundle: ResolvedBundle


class CreationService:
    """Orchestrates multi-source creation over the AUL store.

    Args:
        repo: the asset/lineage store (any ``AulRepository`` backend).
        workdir: scratch directory for renders and slot caches.
        llm_client: optional planning model client (``complete_messages``
            contract). When present, planning uses the model over the
            deterministic legality machinery; when absent (tests), planning is
            fully deterministic.
    """

    def __init__(self, repo: AulRepository, *, workdir: Path,
                 llm_client: Optional[Any] = None,
                 rephrase: Optional["RephraseOriginalTreatment"] = None) -> None:
        self.repo = repo
        self.workdir = Path(workdir)
        self.llm_client = llm_client
        self.resolver = BundleResolver()
        self.trees = SegmentTreeAssembler(repo)
        self.previews = PlanPreviewBuilder(repo)
        self.recorder = LineageRecorder(repo)
        self.rephrase = rephrase or RephraseOriginalTreatment(llm_client=llm_client)
        self._pending: dict[str, ShortPlan] = {}

    # -------------------------------------------------------------- resolve
    def resolve(self, bundle: RequestBundle) -> BundleResolution:
        """Resolve bundle roles, or return the ask-first questions.

        Asset kinds are looked up from the store; unknown refs raise
        ``KeyError`` (surfaces turn that into a 404).
        """

        kinds: dict[str, str] = {}
        for item in bundle.items:
            asset = self.repo.get_asset(item.asset_id)
            if asset is None:
                raise KeyError(f"unknown asset in bundle: {item.ref}")
            kinds[item.asset_id] = asset.kind
        return self.resolver.resolve(bundle, kinds)

    # ----------------------------------------------------------------- plan
    async def plan_short(
        self, bundle: ResolvedBundle, short: ShortSpec, *,
        exclude_intervals: Optional[dict[str, list[tuple[float, float]]]] = None,
    ) -> tuple[PlanPreview, ShortPlan]:
        """Plan ONE short from the resolved bundle; no rendering, no spend.

        Builds trees from persisted annotations (the store round-trip, not
        fresh analysis), derives the sheet from the treatment's music policy,
        applies the treatment's audio policy to the knobs, and plans under the
        hard rules. The returned :class:`ShortPlan` is retained so a later
        :meth:`render_short` renders exactly what was previewed.

        Args:
            bundle: fully-resolved sources (roles decided).
            short: the requested output (hypothesis, duration, treatment).
            exclude_intervals: source spans consumed by OTHER shorts of the
                same request — the cross-output no-reuse rule.

        Returns:
            ``(preview, short_plan)`` — the JSON-ready preview payload and the
            retained server-side plan state.
        """

        treatment = self._treatment_with_defaults(short.treatment, bundle)
        assets, trees = self._footage(bundle)
        sheet = self._sheet(treatment, short.duration_s)
        knobs = self._apply_treatment(treatment, short.treatment.knobs)
        spec = generate_cut_spec(sheet, knobs)

        # The resolved spine leads; bites override it toward the voice source
        # (bites must be carved from the asset whose speech carries the story).
        voice_refs = bundle.refs_with_role(SourceRole.VOICE)
        spine_ref = bundle.spine_ref
        spine_id: Optional[str] = Ref.asset_of(spine_ref) if spine_ref else None
        if knobs.sound_bites and voice_refs:
            spine_id = Ref.asset_of(voice_refs[0])
        common = dict(assets=assets, trees=trees, sheet=sheet, spec=spec,
                      hypothesis=short.hypothesis,
                      spine_asset_id=spine_id,
                      exclude_intervals=exclude_intervals or {})
        if self.llm_client is not None:
            plan = await plan_recompose_llm(llm_client=self.llm_client, **common)
        else:
            plan = plan_recompose(**common)

        name = short.name or f"cut · {short.hypothesis.split(':')[0][:24]}"
        preview = self.previews.build(plan, sheet, bundle, treatment, name=name)
        short_plan = ShortPlan(plan=plan, sheet=sheet, assets=assets,
                               trees=trees, spec_name=name,
                               treatment=treatment, bundle=bundle)
        self._pending[plan.plan_id] = short_plan
        return preview, short_plan

    # --------------------------------------------------------------- render
    async def render_short(self, plan_id: str, *,
                           project_id: str = "default",
                           session_id: Optional[str] = None) -> str:
        """LOCK: render the previewed plan and record its lineage. Paid step.

        Voice treatments synthesize here (never at preview time):
        ``rephrase_original`` transcribes the voice source and redubs in the
        house voice; when its ASR gate refuses, it falls back to
        ``new_narration`` honestly (the fallback is recorded in lineage meta).

        Returns the rendered output's AUL asset id. Raises ``KeyError`` when
        the plan id was never previewed (a lock must follow a show).
        """

        state = self._pending.get(plan_id)
        if state is None:
            raise KeyError(f"no previewed plan with id {plan_id!r} — "
                           f"plan (show) before rendering (lock)")
        narration_path, narration_text = await self._voice_layer(state)
        label = state.spec_name.replace(" ", "_").replace("·", "").strip("_")
        variant: RenderedVariant = render_variant(
            state.plan, state.sheet, state.assets, state.trees,
            self.workdir, label=label or plan_id,
            narration_path=narration_path)
        track_ref = state.treatment.music_ref
        out_id = await self.recorder.record_variant(
            state.plan, variant, project_id=project_id, name=state.spec_name,
            track_asset_id=Ref.asset_of(track_ref) if track_ref else None,
            narration_text=narration_text, session_id=session_id)
        return out_id

    async def _voice_layer(self, state: ShortPlan
                           ) -> tuple[Optional[Path], Optional[str]]:
        """Synthesize the treatment's voice layer, if any.

        Returns ``(narration_path, narration_text)`` — both ``None`` for
        music-only / keep-original (bites carry the source voice directly).
        """

        treatment = state.treatment
        if treatment.kind not in (TreatmentKind.REPHRASE_ORIGINAL,
                                  TreatmentKind.NEW_NARRATION):
            return None, None
        if self.llm_client is None:
            raise ValueError(
                f"treatment {treatment.kind} needs a model client for the script")

        duration = sum(s.spec.dur_s for s in state.plan.slots)
        if treatment.kind is TreatmentKind.REPHRASE_ORIGINAL:
            source_ref = treatment.voice_source_ref or state.bundle.spine_ref
            source = self.repo.get_asset(Ref.asset_of(source_ref)) if source_ref else None
            if source is not None:
                result = await self.rephrase.synthesize(
                    voice_source_path=Path(source.uri), plan=state.plan,
                    duration_s=duration, workdir=self.workdir)
                if result is not None:
                    return result.audio_path, result.script
            logger.info("rephrase unavailable — falling back to new narration")

        from ..Recompose.audio import narration_script_from_plan, synthesize_narration

        script = await narration_script_from_plan(
            self.llm_client, state.plan, duration)
        audio = self.workdir / f"narration_{state.plan.plan_id}.wav"
        await synthesize_narration(script, audio, voice=self.rephrase.voice,
                                   tts_fn=self.rephrase.tts_fn)
        return audio, script

    # -------------------------------------------------------------- helpers
    def _treatment_with_defaults(self, treatment: TreatmentSpec,
                                 bundle: ResolvedBundle) -> TreatmentSpec:
        """Fill treatment refs from the bundle where the user omitted them.

        ``music_ref`` defaults to the bundle's music-role source;
        ``voice_source_ref`` to its voice-role source. Copies rather than
        mutates — the caller's spec stays untouched.
        """

        updates: dict[str, Any] = {}
        music_refs = bundle.refs_with_role(SourceRole.MUSIC)
        if treatment.music_ref is None and \
                treatment.music in (MusicSource.PROVIDED, MusicSource.VARIANT_OF) \
                and music_refs:
            updates["music_ref"] = music_refs[0]
        voice_refs = bundle.refs_with_role(SourceRole.VOICE)
        if treatment.voice_source_ref is None and voice_refs:
            updates["voice_source_ref"] = voice_refs[0]
        return treatment.model_copy(update=updates) if updates else treatment

    def _footage(self, bundle: ResolvedBundle
                 ) -> tuple[list[AssetRecord], dict[str, SegmentTree]]:
        """Materialize planner inputs for every footage-role source.

        Trees are rebuilt FROM the store (stable node ids), so plans survive
        process boundaries; assets carry the AUL id so lineage needs no remap.
        """

        assets: list[AssetRecord] = []
        trees: dict[str, SegmentTree] = {}
        for source in bundle.sources:
            if source.role not in _FOOTAGE_ROLES:
                continue
            asset_id = Ref.asset_of(source.ref)
            stored = self.repo.get_asset(asset_id)
            if stored is None:
                raise KeyError(f"unknown footage asset: {source.ref}")
            record = AssetRecord(kind=stored.kind, path=stored.uri,
                                 duration_s=stored.duration_s)
            record.asset_id = asset_id
            assets.append(record)
            trees[asset_id] = self.trees.build(asset_id)
        if not assets:
            raise ValueError("bundle has no footage-role sources to cut from")
        return assets, trees

    def _sheet(self, treatment: TreatmentSpec, duration_s: float) -> MusicSheet:
        """Music sheet per the treatment's music policy.

        provided/variant_of → analyze the referenced track (variant generation
        itself is a later, spend-gated step; planning against the seed track's
        structure is correct for both). generate → provisional grid now; the
        real track lands at render time behind its own approval.
        """

        if treatment.music_ref is not None:
            track = self.repo.get_asset(Ref.asset_of(treatment.music_ref))
            if track is None:
                raise KeyError(f"unknown track: {treatment.music_ref}")
            full = build_music_sheet(track.uri, window_s=max(duration_s * 2, 45.0))
            return truncate_sheet(full, duration_s)
        return provisional_sheet(duration_s)

    @staticmethod
    def _apply_treatment(treatment: TreatmentSpec, knobs: Knobs) -> Knobs:
        """Project the audio policy onto cut-taste knobs (returns a copy).

        keep_original needs bites and ducking; rephrase/new narration mute the
        source (the voice layer is synthesized at render); music_only mutes.
        """

        updates: dict[str, Any] = {}
        if treatment.kind is TreatmentKind.KEEP_ORIGINAL:
            updates["sound_bites"] = max(knobs.sound_bites, 2)
            updates["source_audio"] = "mute"      # bites carry their own audio
        elif treatment.kind in (TreatmentKind.REPHRASE_ORIGINAL,
                                TreatmentKind.NEW_NARRATION):
            updates["sound_bites"] = 0
            updates["source_audio"] = "mute"
            updates["narration"] = "generate"
        else:
            updates["sound_bites"] = 0
            updates["source_audio"] = "mute"
            updates["narration"] = "none"
        return knobs.model_copy(update=updates)
