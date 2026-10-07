"""HTTP/WS surface for agentic audio. Re-exports the public router API so the
external path ``agentic_audio.api`` is unchanged after the restructure."""

from __future__ import annotations

from .router import (
    AgenticAudioActionResponse,
    agentic_audio_enabled,
    create_agentic_audio_router,
    mount_agentic_audio_router,
)

__all__ = [
    "AgenticAudioActionResponse",
    "agentic_audio_enabled",
    "create_agentic_audio_router",
    "mount_agentic_audio_router",
]
