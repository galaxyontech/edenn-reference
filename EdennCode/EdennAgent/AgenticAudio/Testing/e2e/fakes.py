"""The single import seam for the in-memory fakes used by the E2E foundation.

The driver/harness import the fakes from HERE, not from the test module — so the
foundation no longer reaches into test internals. The definitions currently live
in ``test_agentic_audio_api`` and are re-exported here under clean names;
physically relocating them into this module later won't change any importer.
"""

from __future__ import annotations

from EdennCode.EdennAgent.AgenticAudio.Testing.test_agentic_audio_api import (
    _MemoryAgenticRepository as MemoryAgenticRepository,
    _MemoryAsyncRepository as MemoryAsyncRepository,
    _MemoryQueue as MemoryQueue,
    _RecordingCompose as RecordingCompose,
    _ScriptedAgentClient as ScriptedAgentClient,
    _context as build_context,
    _fake_analyze as fake_analyze,
    _fake_remix as fake_remix,
    _seed_music_and_voiceover as seed_music_and_voiceover,
    _seed_music_only as seed_music_only,
    _seed_source_video as seed_source_video,
)

__all__ = [
    "MemoryAgenticRepository",
    "MemoryAsyncRepository",
    "MemoryQueue",
    "RecordingCompose",
    "ScriptedAgentClient",
    "build_context",
    "fake_analyze",
    "fake_remix",
    "seed_music_and_voiceover",
    "seed_music_only",
    "seed_source_video",
]
