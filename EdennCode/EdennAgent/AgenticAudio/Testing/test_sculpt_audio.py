"""Moving the music, without paying to generate it again.

A take is one window of a longer piece of music: the matcher picks where to cut,
and until now that choice was final. If the drop landed after the cut, or the
take opened mid-phrase, or it ended on two seconds of silence, the only recourse
was a whole new generation — money and minutes for a problem that is a seek.

The full track has been on record the whole time, and both mixers already knew
how to seek into it. The offset the matcher chose was even computed and carried
all the way to the render result, then dropped one hop before it reached the
take, so nothing knew its own window to move relative to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from EdennCode.EdennAgent.AgenticAudio.models import (
    SCULPT_KINDS,
    SCULPT_KIND_SHIFT_WINDOW,
    AGENT_GENERATION_TOOLS,
    AGENT_HEAVY_TOOLS,
    AGENT_TOOL_SCULPT_AUDIO,
)
from EdennCode.EdennAgent.AgenticAudio.tools.impls import build_tool_registry


# ---------------------------------------------------------------------------#
# the spend class is the whole point                                          #
# ---------------------------------------------------------------------------#


def test_sculpting_is_free_and_does_not_end_the_turn() -> None:
    """If this were an edit_audio kind it would inherit that tool's spend class:
    a duplicate-generation fingerprint, a SPEND audit line, a 'this costs money'
    dialog, and an ended turn — for an ffmpeg seek."""
    assert AGENT_TOOL_SCULPT_AUDIO not in AGENT_GENERATION_TOOLS
    assert AGENT_TOOL_SCULPT_AUDIO not in AGENT_HEAVY_TOOLS

    tool = build_tool_registry().get(AGENT_TOOL_SCULPT_AUDIO)
    assert tool.requires_approval is False
    assert tool.is_heavy is False


def test_the_tool_is_registered_against_its_spec() -> None:
    # The registry refuses a tool with no matching ToolSpec, and the decision
    # schema's tool enum derives from the same table.
    assert build_tool_registry().get(AGENT_TOOL_SCULPT_AUDIO) is not None
    assert SCULPT_KIND_SHIFT_WINDOW in SCULPT_KINDS


# ---------------------------------------------------------------------------#
# the media layer                                                             #
# ---------------------------------------------------------------------------#


class _Tools:
    """AgenticAudioTools with just enough wired for the window path."""

    @staticmethod
    def build(window_fn: Any = None) -> Any:
        from EdennCode.EdennAgent.AgenticAudio.design.memory_fakes import (
            _MemoryAsyncRepository,
        )
        from EdennCode.EdennAgent.AgenticAudio.tools import AgenticAudioTools

        return AgenticAudioTools(
            async_repository=_MemoryAsyncRepository(),
            settings=None,
            window_fn=window_fn,
        )


@pytest.mark.asyncio
async def test_a_take_with_no_longer_track_refuses_with_the_reason() -> None:
    """The basic tier renders video-length audio directly: there is no other
    window to move to. Rendering an identical file and calling it a change would
    be worse than saying so."""
    tools = _Tools.build()
    result = await tools.sculpt_window(
        candidate={"candidate_id": "c1", "audio_url": "https://x/a.mp3",
                   "complete_audio_url": "https://x/a.mp3"},
        source_video_artifact_id="art",
        window_start_s=5.0,
    )

    assert result["status"] == "unavailable"
    assert result["reason"] == "no_full_track"
    assert "length of the video" in result["message"]


@pytest.mark.asyncio
async def test_a_take_with_a_full_track_re_cuts_from_the_new_point() -> None:
    calls: list[dict[str, Any]] = []

    async def window_fn(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"status": "completed", "remixed_video_url": "/dev/media/w.mp4"}

    tools = _Tools.build(window_fn)
    result = await tools.sculpt_window(
        candidate={"candidate_id": "c1", "audio_url": "https://x/cut.mp3",
                   "complete_audio_url": "https://x/full.mp3"},
        source_video_artifact_id="art",
        window_start_s=42.5,
    )

    assert result["status"] == "completed"
    assert calls[0]["window_start_s"] == 42.5


@pytest.mark.asyncio
async def test_a_non_numeric_offset_is_refused_before_any_work() -> None:
    tools = _Tools.build()
    with pytest.raises(ValueError):
        await tools.sculpt_window(
            candidate={"complete_audio_url": "https://x/full.mp3",
                       "audio_url": "https://x/cut.mp3"},
            source_video_artifact_id="art",
            window_start_s="somewhere",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------#
# the two-renderer trap                                                       #
# ---------------------------------------------------------------------------#


def test_the_master_mixer_can_seek_into_the_music() -> None:
    """The single-layer overlay has taken a music offset for a long time; the
    master mix had no such parameter at all. A window chosen while the session
    was music-only reset the moment narration or SFX joined and the mix switched
    renderers."""
    import inspect

    from EdennCode.Util.MediaUtils.ffmpeg_utils import compose_master_mix_on_video

    assert "music_start_s" in inspect.signature(compose_master_mix_on_video).parameters


def test_compose_reads_the_window_off_the_candidate_not_the_call() -> None:
    """Persisted on the take, so every renderer picks it up. Passed per call, it
    would survive exactly until the next thing the user did."""
    source = Path(
        "EdennCode/EdennAgent/AgenticAudio/tools/impls.py"
    ).read_text()
    assert 'window = candidate.get("window") or {}' in source
    assert "music_start_s=music_start_s," in source


def test_the_devserver_has_its_own_window_renderer() -> None:
    """Every media verb needs its devserver twin: the injected functions bypass
    the in-process implementations entirely on the deployment that ships."""
    source = Path(
        "EdennCode/EdennAgent/AgenticAudio/design/devserver.py"
    ).read_text()
    assert "async def _dev_window(" in source
    assert "window_fn=_dev_window," in source
    # It must source the FULL track: seeking into the video-length cut would run
    # the music out before the picture ends.
    assert '_local_media_file(candidate.get("complete_audio_url"))' in source


# ---------------------------------------------------------------------------#
# comparing takes                                                             #
# ---------------------------------------------------------------------------#


@pytest.mark.asyncio
async def test_comparing_takes_reports_what_they_sound_like() -> None:
    """The state summary carries ids, statuses and URLs and nothing about how a
    take SOUNDS, so "which of these is better?" used to be answered from the
    direction text — the same words for every take in the group."""
    from EdennCode.EdennAgent.AgenticAudio.design.memory_fakes import _MemoryAgenticRepository
    from EdennCode.EdennAgent.AgenticAudio.models import (
        AGENT_TOOL_COMPARE_TAKES, AgenticSessionPhase,
    )
    from EdennCode.EdennAgent.AgenticAudio.tools.base import ToolContext

    repo = _MemoryAgenticRepository()
    session = repo.create_session(
        source_video_artifact_id="art", phase=AgenticSessionPhase.AWAITING_CANDIDATE_CHOICE,
        state_json={"candidates": [
            {"candidate_id": "c1", "title": "Velvet", "status": "completed", "version": 1,
             "prompt": "warm strings",
             "listen_report": {"clean": False, "notes": ["it ends with 2.0s of silence"],
                               "observations": ["energy settles across the take"],
                               "measured": {"cut_duration_s": 20.6}},
             "window": {"start_s": 96.0, "source": "matcher"}},
            {"candidate_id": "c2", "title": "Velvet", "status": "completed", "version": 2,
             "parent_candidate_id": "c1", "edit_kind": "regenerate", "prompt": "warm strings",
             "listen_report": {"clean": True, "notes": [],
                               "observations": ["energy builds across the take"],
                               "measured": {"cut_duration_s": 20.6}}},
            {"candidate_id": "c3", "status": "queued"},
        ]},
    )
    tool = build_tool_registry().get(AGENT_TOOL_COMPARE_TAKES)
    result = await tool.run(ToolContext(session_id=session.session_id, repository=repo,
                                        media=None, max_candidates=3), {})

    rows = result.data["takes"]
    assert [r["candidate_id"] for r in rows] == ["c1", "c2"], "an unfinished take is not comparable"
    assert rows[0]["faults"] == ["it ends with 2.0s of silence"]
    assert rows[1]["shape"] == ["energy builds across the take"]
    assert rows[1]["parent_candidate_id"] == "c1", "lineage is part of the comparison"
    assert result.data["any_faults"] is True


@pytest.mark.asyncio
async def test_the_comparison_outlives_the_turn() -> None:
    """The scratchpad is turn-local, so a comparison the user refers to two
    messages later ("go with the second one") has to be on the session."""
    from EdennCode.EdennAgent.AgenticAudio.design.memory_fakes import _MemoryAgenticRepository
    from EdennCode.EdennAgent.AgenticAudio.models import (
        AGENT_TOOL_COMPARE_TAKES, AgenticSessionPhase,
    )
    from EdennCode.EdennAgent.AgenticAudio.tools.base import ToolContext

    repo = _MemoryAgenticRepository()
    session = repo.create_session(
        source_video_artifact_id="art", phase=AgenticSessionPhase.AWAITING_CANDIDATE_CHOICE,
        state_json={"candidates": [{"candidate_id": "c1", "status": "completed"}]},
    )
    tool = build_tool_registry().get(AGENT_TOOL_COMPARE_TAKES)
    await tool.run(ToolContext(session_id=session.session_id, repository=repo,
                               media=None, max_candidates=3), {})

    stored = repo.get_session(session.session_id).state_json.get("last_comparison")
    assert stored and stored["compared"] == 1


@pytest.mark.asyncio
async def test_comparing_with_nothing_finished_says_so_instead_of_failing() -> None:
    from EdennCode.EdennAgent.AgenticAudio.design.memory_fakes import _MemoryAgenticRepository
    from EdennCode.EdennAgent.AgenticAudio.models import (
        AGENT_TOOL_COMPARE_TAKES, AgenticSessionPhase,
    )
    from EdennCode.EdennAgent.AgenticAudio.tools.base import ToolContext

    repo = _MemoryAgenticRepository()
    session = repo.create_session(
        source_video_artifact_id="art", phase=AgenticSessionPhase.GENERATING_CANDIDATES,
        state_json={"candidates": [{"candidate_id": "c1", "status": "queued"}]},
    )
    tool = build_tool_registry().get(AGENT_TOOL_COMPARE_TAKES)
    result = await tool.run(ToolContext(session_id=session.session_id, repository=repo,
                                        media=None, max_candidates=3), {})

    assert result.data["error"] == "nothing_to_compare"
    assert result.data["instruction"]


def test_a_comparison_never_carries_vendor_identity() -> None:
    # The rows go into events, the scratchpad, and the model's prose. Excluding
    # provider fields outright beats relying on the egress scrub to catch them.
    source = Path("EdennCode/EdennAgent/AgenticAudio/tools/impls.py").read_text()
    body = source.split("class CompareTakesTool")[1].split("class SculptAudioTool")[0]
    for leak in ("provider", "modelspec"):
        assert leak not in body, f"the comparison exposes {leak}"
