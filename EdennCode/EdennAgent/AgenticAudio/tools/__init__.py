"""Agent-facing tools + the media/job toolkit. Re-exports public names so
``agentic_audio.tools`` is unchanged after the restructure.

Import order matters: ``media`` (leaf) before ``base`` (which pulls ``domain``,
whose ``session_state`` imports ``..tools.media`` — already loaded by then).
"""

from __future__ import annotations

from .media import AgenticAudioTools, normalize_music_modelspec
from .base import (
    ApprovalRequiredError,
    Tool,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolResult,
)
from .impls import build_tool_registry

__all__ = [
    "AgenticAudioTools",
    "ApprovalRequiredError",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "build_tool_registry",
    "normalize_music_modelspec",
]
