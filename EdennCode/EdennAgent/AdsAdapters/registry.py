"""Channel resolution: the configured real channel, else the sandbox twin.

Adapters register by name; ``resolve`` returns the preferred channel when its
credentials are present, otherwise the sandbox on the identical contract — so
the full loop stays exercisable with zero spend.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from .base import ChannelAdapter

logger = logging.getLogger(__name__)

AdapterFactory = Callable[[], ChannelAdapter]


class ChannelRegistry:
    def __init__(self, *, adapters: Optional[dict[str, AdapterFactory]] = None,
                 fallback: Optional[AdapterFactory] = None) -> None:
        if adapters is None:
            from .tiktok import TikTokAdsAdapter

            adapters = {"tiktok": TikTokAdsAdapter}
        if fallback is None:
            from .sandbox import SandboxAdapter

            fallback = SandboxAdapter
        self._adapters = adapters
        self._fallback = fallback

    def resolve(self, prefer: str = "tiktok") -> ChannelAdapter:
        factory = self._adapters.get(prefer)
        if factory is not None:
            adapter = factory()
            if adapter.is_configured():
                return adapter
        logger.info("ads: no %s credentials — using sandbox adapter", prefer)
        return self._fallback()
