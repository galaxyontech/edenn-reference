"""Channel adapter contract (CREATIVE_LOOP §4) — deliberately thin.

Two capabilities only: publish a rendered creative, pull its daily metrics.
No bidding, no budgets, no audience tooling. Authorization scope (owner):
our ads-API access covers ADS use cases on accounts we control.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class PublishResult:
    channel: str                 # "tiktok" | "meta" | "google" | "sandbox"
    channel_ref: str             # the channel's creative/ad id
    landing: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DailyMetrics:
    channel: str
    channel_ref: str
    date: str                    # YYYY-MM-DD
    impressions: int
    clicks: int
    conversions: int
    spend: float
    placement: str = ""

    @property
    def ctr(self) -> float:
        return self.clicks / self.impressions if self.impressions else 0.0

    @property
    def cvr(self) -> float:
        return self.conversions / self.clicks if self.clicks else 0.0


class ChannelAdapter(ABC):
    name: str = "abstract"

    @abstractmethod
    async def publish(self, video_path: Path, *, title: str,
                      campaign: str, meta: Optional[dict[str, Any]] = None) -> PublishResult:
        """Upload the creative to the channel; return its channel id."""

    @abstractmethod
    async def pull_metrics(self, channel_ref: str, date: str) -> Optional[DailyMetrics]:
        """One day's metrics for a published creative (None = not yet available)."""

    def is_configured(self) -> bool:
        return True
