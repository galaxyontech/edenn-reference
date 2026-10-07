"""The agent brain (orchestrator + reasoning loop + dispatcher). Re-exports the
public names so ``agentic_audio.agent`` is unchanged after the restructure."""

from __future__ import annotations

from .agent import (
    AgenticAudioAgent,
    ApprovalRequiredError,
    SYSTEM_PROMPT,
    build_agentic_audio_agent_client,
)

__all__ = [
    "AgenticAudioAgent",
    "ApprovalRequiredError",
    "SYSTEM_PROMPT",
    "build_agentic_audio_agent_client",
]
