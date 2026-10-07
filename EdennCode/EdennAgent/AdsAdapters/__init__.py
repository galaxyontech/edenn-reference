"""Ads channels + the creative loop (CREATIVE_LOOP §3-4).

Contract (base) -> concrete adapters (tiktok, sandbox) -> resolution (registry)
-> loop services (campaign: publish/collect/attribute).
"""

from .base import ChannelAdapter, DailyMetrics, PublishResult
from .campaign import AttributionEngine, CampaignPublisher, Component, VariantTotals
from .registry import ChannelRegistry
from .sandbox import SandboxAdapter
from .tiktok import TikTokAdsAdapter

__all__ = [
    "ChannelAdapter", "PublishResult", "DailyMetrics",
    "TikTokAdsAdapter", "SandboxAdapter", "ChannelRegistry",
    "CampaignPublisher", "AttributionEngine", "VariantTotals", "Component",
]
