"""The creative loop's write + read sides (CREATIVE_LOOP §3).

``CampaignPublisher`` pushes a rendered variant to a channel and pulls its daily
outcomes into the store as ledger operations (a ``published_as`` edge, then
``outcome`` annotations). ``AttributionEngine`` reads those outcomes back and
rolls them DOWN the lineage edges to rank the components that carried them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..AssetLibrary.models import Annotation, Edge
from ..AssetLibrary.repository import AulRepository
from .base import ChannelAdapter, DailyMetrics


@dataclass(frozen=True)
class VariantTotals:
    """A published variant's outcomes summed over its live days."""

    asset_id: str
    days: int
    impressions: int
    clicks: int
    conversions: int
    spend: float

    @property
    def ctr(self) -> float:
        return round(self.clicks / self.impressions, 5) if self.impressions else 0.0

    @property
    def cvr(self) -> float:
        return round(self.conversions / self.clicks, 5) if self.clicks else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"asset_id": self.asset_id, "days": self.days,
                "impressions": self.impressions, "clicks": self.clicks,
                "conversions": self.conversions, "spend": self.spend,
                "ctr": self.ctr, "cvr": self.cvr}


@dataclass
class Component:
    """A source span/track with the outcomes rolled down to it, ranked by CTR."""

    ref: str
    impressions: int = 0
    clicks: int = 0
    conversions: int = 0
    variants: set[str] = field(default_factory=set)
    roles: set[str] = field(default_factory=set)
    evidence: str = "observational"

    @property
    def ctr(self) -> float:
        return round(self.clicks / self.impressions, 5) if self.impressions else 0.0

    @property
    def cvr(self) -> float:
        return round(self.conversions / self.clicks, 5) if self.clicks else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"ref": self.ref, "impressions": self.impressions,
                "ctr": self.ctr, "cvr": self.cvr,
                "n_variants": len(self.variants), "roles": sorted(self.roles),
                "evidence": self.evidence}


class CampaignPublisher:
    """Publishes a variant to a channel and collects its daily outcomes."""

    def __init__(self, repo: AulRepository, adapter: ChannelAdapter) -> None:
        self.repo = repo
        self.adapter = adapter

    async def publish(self, output_asset_id: str, *, title: str, campaign: str,
                      project_id: str = "default") -> str:
        asset = self.repo.get_asset(output_asset_id)
        if asset is None:
            raise KeyError(output_asset_id)
        result = await self.adapter.publish(Path(asset.uri), title=title, campaign=campaign,
                                            meta={"asset_id": output_asset_id})
        self.repo.add_edge(Edge(
            src_ref=output_asset_id, dst_ref=f"{self.adapter.name}:{result.channel_ref}",
            operation="published_as", project_id=project_id,
            params={"channel": self.adapter.name, "channel_ref": result.channel_ref,
                    "campaign": campaign, "title": title}))
        return result.channel_ref

    async def collect_outcomes(self, output_asset_id: str, channel_ref: str,
                               dates: list[str], *, project_id: str = "default") -> int:
        # Ledger is append-only per (channel_ref, date): skip days already pulled
        # so a re-pull can never double-count in totals/attribution.
        existing = self.repo.get_annotations(output_asset_id, layer="outcome",
                                             kind="outcome_daily")
        seen = {(o.payload.get("channel_ref"), o.payload.get("date")) for o in existing}
        rows: list[Annotation] = []
        for date in dates:
            if (channel_ref, date) in seen:
                continue
            m: Optional[DailyMetrics] = await self.adapter.pull_metrics(channel_ref, date)
            if m is None:
                continue
            rows.append(Annotation(
                asset_id=output_asset_id, project_id=project_id,
                layer="outcome", kind="outcome_daily",
                producer=f"{self.adapter.name}_ads@v1",
                payload={"date": m.date, "impressions": m.impressions, "clicks": m.clicks,
                         "conversions": m.conversions, "spend": m.spend,
                         "ctr": round(m.ctr, 5), "cvr": round(m.cvr, 5),
                         "placement": m.placement, "channel": m.channel,
                         "channel_ref": channel_ref}))
        if rows:
            self.repo.add_annotations(rows)
        return len(rows)


class AttributionEngine:
    """Reads outcomes back and attributes them to variants and components."""

    def __init__(self, repo: AulRepository) -> None:
        self.repo = repo

    def variant_totals(self, output_asset_id: str) -> VariantTotals:
        outs = self.repo.get_annotations(output_asset_id, layer="outcome", kind="outcome_daily")
        return VariantTotals(
            asset_id=output_asset_id, days=len(outs),
            impressions=sum(o.payload["impressions"] for o in outs),
            clicks=sum(o.payload["clicks"] for o in outs),
            conversions=sum(o.payload["conversions"] for o in outs),
            spend=round(sum(o.payload["spend"] for o in outs), 2))

    def component_attribution(self, output_asset_ids: list[str], *,
                              operation: str = "slot_cut",
                              role: Optional[str] = None) -> list[Component]:
        """Roll variant outcomes down lineage edges -> ranked components.

        Groups by source ref (span/track); reports weighted CTR/CVR + sample
        size. Labeled observational — the experiment upgrade is a variation-axis run.
        """

        totals = {aid: self.variant_totals(aid) for aid in output_asset_ids}
        comps: dict[str, Component] = {}
        for aid in output_asset_ids:
            t = totals[aid]
            for edge in self.repo.edges_to(aid):
                if edge.operation != operation:
                    continue
                if role and edge.params.get("role") != role:
                    continue
                c = comps.setdefault(edge.src_ref, Component(ref=edge.src_ref))
                c.impressions += t.impressions
                c.clicks += t.clicks
                c.conversions += t.conversions
                c.variants.add(aid)
                if edge.params.get("role"):
                    c.roles.add(edge.params["role"])
        return sorted(comps.values(), key=lambda c: -c.ctr)
