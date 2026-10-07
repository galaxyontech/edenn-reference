"""Sandbox channel — the identical contract, deterministic synthetic metrics.

Lets the FULL loop (publish -> outcomes -> attribution -> UI) run e2e with
zero spend and zero credentials. Metrics are seeded from the channel_ref so
runs are reproducible; a small quality prior can be injected per creative
(e.g. hypothesis) so attribution tests have real signal to find.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Optional

from .base import ChannelAdapter, DailyMetrics, PublishResult


def _h(seed: str, lo: float, hi: float) -> float:
    v = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return lo + v * (hi - lo)


class SandboxAdapter(ChannelAdapter):
    name = "sandbox"

    def __init__(self, quality_prior: Optional[dict[str, float]] = None) -> None:
        # channel_ref substring -> CTR multiplier (test signal injection)
        self.quality_prior = quality_prior or {}

    async def publish(self, video_path: Path, *, title: str, campaign: str,
                      meta: Optional[dict[str, Any]] = None) -> PublishResult:
        ref = "sb_" + hashlib.sha256(
            (str(video_path) + title + campaign).encode()).hexdigest()[:12]
        return PublishResult(channel=self.name, channel_ref=ref,
                             raw={"title": title, "campaign": campaign,
                                  "meta": meta or {}})

    async def pull_metrics(self, channel_ref: str, date: str) -> Optional[DailyMetrics]:
        boost = 1.0
        for key, mult in self.quality_prior.items():
            if key in channel_ref:
                boost = mult
        seed = f"{channel_ref}:{date}"
        impressions = int(_h(seed + ":imp", 30_000, 90_000))
        ctr = _h(seed + ":ctr", 0.014, 0.026) * boost
        clicks = int(impressions * ctr)
        cvr = _h(seed + ":cvr", 0.005, 0.011) * (0.6 + 0.4 * boost)
        conversions = int(clicks * cvr)
        spend = round(impressions / 1000 * _h(seed + ":cpm", 4.5, 9.0), 2)
        return DailyMetrics(channel=self.name, channel_ref=channel_ref, date=date,
                            impressions=impressions, clicks=clicks,
                            conversions=conversions, spend=spend,
                            placement="sandbox_feed")
