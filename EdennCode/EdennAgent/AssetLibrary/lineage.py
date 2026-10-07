"""Lineage: turn a rendered plan into a generated asset + its edges.

``record_variant`` frees lineage from per-run plan.json blobs — every slot's
source span becomes a ``slot_cut`` edge and the track a ``music_for`` edge, which
are exactly the queries the Connections screen runs. The output render is itself
ingested as a (generated) asset, so outputs are first-class in the graph.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..Recompose.domain import RecomposePlan, RenderedVariant
from .ingest import AssetIngestor
from .models import Edge
from .refs import Ref
from .repository import AulRepository


class LineageRecorder:
    """Records a rendered variant and its provenance edges into the store."""

    def __init__(self, repo: AulRepository, *, ingestor: Optional[AssetIngestor] = None) -> None:
        self.repo = repo
        self.ingestor = ingestor or AssetIngestor(repo)

    async def record_variant(
        self, plan: RecomposePlan, variant: RenderedVariant, *,
        project_id: str = "default", name: Optional[str] = None,
        track_asset_id: Optional[str] = None, narration_text: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> str:
        """Registers the render as a generated asset, writes its lineage, returns its id."""

        out_id = await self.ingestor.ingest(
            Path(variant.path), name=name or Path(variant.path).stem, kind="video",
            project_id=project_id, generated=True, with_signals=False, understand=False,
            meta={"plan_id": plan.plan_id, "hypothesis": plan.hypothesis,
                  "knobs": plan.knobs.model_dump(),
                  "narration_script": narration_text,
                  "cut_report": variant.cut_report.model_dump() if variant.cut_report else None},
        )

        for slot in plan.slots:
            # Slot source ids are recompose AssetRecord ids; the caller maps them
            # to AUL ids with remap_plan_assets when they differ.
            src = str(Ref.span(slot.asset_id, slot.seg_in_s, slot.seg_in_s + slot.spec.dur_s))
            self.repo.add_edge(Edge(
                src_ref=src, dst_ref=out_id, operation="slot_cut", project_id=project_id,
                params={"slot": slot.spec.index, "role": slot.spec.role,
                        "is_bite": slot.spec.is_bite, "why": slot.why,
                        "t_out": slot.spec.t_start, "plan_id": plan.plan_id,
                        "hypothesis": plan.hypothesis},
                session_id=session_id))
        if track_asset_id:
            self.repo.add_edge(Edge(
                src_ref=track_asset_id, dst_ref=out_id, operation="music_for",
                project_id=project_id, params={"plan_id": plan.plan_id}, session_id=session_id))
        return out_id

    @staticmethod
    def remap_plan_assets(plan: RecomposePlan, asset_map: dict[str, str]) -> RecomposePlan:
        """Rewrite a plan's recompose asset ids to AUL content-addressed ids."""

        plan = plan.model_copy(deep=True)
        for slot in plan.slots:
            slot.asset_id = asset_map.get(slot.asset_id, slot.asset_id)
        plan.source_asset_ids = [asset_map.get(a, a) for a in plan.source_asset_ids]
        if plan.spine_asset_id:
            plan.spine_asset_id = asset_map.get(plan.spine_asset_id, plan.spine_asset_id)
        return plan
