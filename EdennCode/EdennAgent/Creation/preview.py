"""Show-before-spend: the plan-preview contract (AGENTIC_CREATION.md §Show).

:class:`PlanPreviewBuilder` converts a planned-but-unrendered
:class:`~..recompose.domain.RecomposePlan` + its :class:`MusicSheet` into a
typed, JSON-ready payload the console renders as a timeline strip and plays
client-side (clock-master pattern absorbed from the OpenCut study — the
playhead owns time; the source video and track slave to it). Nothing here
renders or spends; the paid ffmpeg render happens only after an explicit lock.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from ..AssetLibrary.refs import Ref
from ..AssetLibrary.repository import AulRepository
from ..Recompose.domain import MusicSheet, RecomposePlan
from .domain import ResolvedBundle, SourceRole, TreatmentSpec


class PreviewSlot(BaseModel):
    """One cut in output time, with enough provenance to seek the source.

    ``t_out``/``dur_s`` position the block on the strip; ``asset_id`` +
    ``seg_in_s`` let the player seek the right source at the right frame.
    """

    index: int = Field(description="Slot index in output order")
    t_out: float = Field(description="Output-time start (seconds)")
    dur_s: float = Field(description="Slot duration (seconds)")
    asset_id: str = Field(description="Source asset (AUL id)")
    seg_in_s: float = Field(description="Seek position inside the source")
    role: str = Field(description="Slot role: opening | body | closer | ...")
    is_bite: bool = Field(default=False,
                          description="True = source speech plays intact")
    why: str = Field(default="", description="Planner's one-line rationale")


class PreviewSource(BaseModel):
    """A source the player must be able to load, with its resolved role."""

    asset_id: str
    name: str
    kind: str = Field(description="video | image | audio")
    role: SourceRole
    duration_s: Optional[float] = None
    media_url: str = Field(description="Streamable URL for the player")


class PlanPreview(BaseModel):
    """Everything the console needs to draw and play one planned short.

    ``beats_out`` are beat positions translated into OUTPUT time (the sheet's
    window start subtracted), pre-filtered to the short's duration — the strip
    draws them as ticks and the player may snap its scrubber to them.
    """

    plan_id: str
    name: str
    hypothesis: str
    duration_s: float
    tempo_bpm: float
    music_start_s: float = Field(
        description="Offset into the track where the window begins — the "
                    "player sets the track's currentTime to this at t=0")
    beats_out: list[float]
    slots: list[PreviewSlot]
    sources: list[PreviewSource]
    music_url: Optional[str] = Field(
        default=None, description="Streamable track URL (None = provisional)")
    treatment_kind: str
    music_mode: str


class PlanPreviewBuilder:
    """Builds :class:`PlanPreview` payloads from planner output.

    Stateless apart from the repository used to look up source names and the
    URL template used to point the player at the media-streaming endpoint.
    """

    def __init__(self, repo: AulRepository,
                 media_url_template: str = "/api/v2/library/media/{asset_id}") -> None:
        self.repo = repo
        self.media_url_template = media_url_template

    def build(self, plan: RecomposePlan, sheet: MusicSheet,
              bundle: ResolvedBundle, treatment: TreatmentSpec,
              *, name: str) -> PlanPreview:
        """Assemble the preview for one planned short.

        Args:
            plan: the (unrendered) recompose plan.
            sheet: the music sheet the plan was made against; supplies tempo,
                beats, and the window offset for track sync.
            bundle: the resolved request bundle — supplies each source's role.
            treatment: the short's audio policy (shown, and used to decide
                whether a music URL is attached).
            name: display name for this short.

        Returns:
            A fully-typed :class:`PlanPreview`, JSON-serializable via pydantic.
        """

        duration = sum(s.spec.dur_s for s in plan.slots)
        role_of = {Ref.asset_of(s.ref): s.role for s in bundle.sources}

        sources: list[PreviewSource] = []
        for asset_id in dict.fromkeys([s.asset_id for s in plan.slots]):
            sources.append(self._source(asset_id, role_of))
        music_url: Optional[str] = None
        if treatment.music_ref is not None:
            music_asset_id = Ref.asset_of(treatment.music_ref)
            sources.append(self._source(music_asset_id, role_of))
            music_url = self.media_url_template.format(asset_id=music_asset_id)

        beats_out = [round(b - sheet.window_start_s, 4) for b in sheet.beats
                     if 0.0 <= b - sheet.window_start_s <= duration]

        return PlanPreview(
            plan_id=plan.plan_id, name=name, hypothesis=plan.hypothesis,
            duration_s=round(duration, 3), tempo_bpm=sheet.tempo_bpm,
            music_start_s=sheet.window_start_s, beats_out=beats_out,
            slots=[PreviewSlot(index=s.spec.index, t_out=s.spec.t_start,
                               dur_s=s.spec.dur_s, asset_id=s.asset_id,
                               seg_in_s=s.seg_in_s, role=s.spec.role,
                               is_bite=s.spec.is_bite, why=s.why)
                   for s in plan.slots],
            sources=sources, music_url=music_url,
            treatment_kind=treatment.kind.value,
            music_mode=treatment.music.value)

    def _source(self, asset_id: str,
                role_of: dict[str, SourceRole]) -> PreviewSource:
        """Look up one source asset and attach its resolved role + URL."""

        asset = self.repo.get_asset(asset_id)
        if asset is None:
            raise KeyError(f"preview references unknown asset: {asset_id}")
        return PreviewSource(
            asset_id=asset_id, name=asset.name, kind=asset.kind,
            role=role_of.get(asset_id, SourceRole.ACCENT),
            duration_s=asset.duration_s,
            media_url=self.media_url_template.format(asset_id=asset_id))
