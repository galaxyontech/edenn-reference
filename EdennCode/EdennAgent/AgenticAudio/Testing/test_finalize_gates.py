"""What may be locked in as the finished piece.

Finalize is the point of no return: it is what the customer downloads, shares
and is billed for. It already refused to lock a mix built from a different take
— that one shipped a "final mix" with the music missing entirely. Two other ways
to ship something wrong were left open.

A mix can be missing a layer the user ASKED for, because composing while the
narration still renders is legitimate and the result looks exactly like a
finished one. And the critic that measures the master wrote down what was wrong
with it, and finalize never read that — so a deliverable the product itself had
judged truncated, or silent throughout, could still be locked.

Both refuse rather than forbid. The faults are mechanical and a user may
genuinely accept one; what they may not do is accept it without being told.
"""

from __future__ import annotations

from typing import Any

import pytest

from EdennCode.EdennAgent.AgenticAudio.design.memory_fakes import (
    _MemoryAgenticRepository,
)
from EdennCode.EdennAgent.AgenticAudio.models import (
    AGENT_TOOL_FINALIZE,
    AgenticSessionPhase,
)
from EdennCode.EdennAgent.AgenticAudio.tools.base import ToolContext
from EdennCode.EdennAgent.AgenticAudio.tools.media import mix_stems
from EdennCode.EdennAgent.AgenticAudio.tools.impls import build_tool_registry


def _session(repo: _MemoryAgenticRepository, mix: dict[str, Any]) -> Any:
    """A session whose mix was composed from the take that is selected.

    The mix carries a ``built_from`` stamp because a composed mix now does; a
    mix without one is treated as predating an edit and is refused, which is
    its own test below.
    """
    state = {
        "candidates": [{"candidate_id": "c1", "status": "completed",
                        "audio_url": "https://x/a.mp3"}],
        "selected_candidate_id": "c1",
        "production_plan": {"mode": "full_e2e",
                            "layers": ["music", "voiceover"]},
    }
    state["mix"] = {
        "music_candidate_id": "c1",
        "video_url": "/dev/media/m.mp4",
        "built_from": mix_stems(state),
        **mix,
    }
    return repo.create_session(
        source_video_artifact_id="art",
        phase=AgenticSessionPhase.AWAITING_CANDIDATE_CHOICE,
        state_json=state,
    )


class _StubMedia:
    """Just enough for the work that happens AFTER the gates."""

    @staticmethod
    def compose_final_mix(*, selected_candidate: dict[str, Any]) -> dict[str, Any]:
        return {"status": "completed",
                "audio_url": selected_candidate.get("audio_url"),
                "video_url": "/dev/media/final.mp4"}


async def _finalize(repo, session, **args: Any):
    tool = build_tool_registry().get(AGENT_TOOL_FINALIZE)
    return await tool.run(
        ToolContext(session_id=session.session_id, repository=repo,
                    media=_StubMedia(), max_candidates=3),
        {"candidate_id": "c1", **args},
    )


@pytest.mark.asyncio
async def test_a_mix_missing_a_layer_the_user_asked_for_is_not_locked() -> None:
    repo = _MemoryAgenticRepository()
    session = _session(repo, {"missing_layers": ["voiceover"]})

    result = await _finalize(repo, session)

    assert result.data["error"] == "mix_missing_requested_layer"
    assert result.data["missing_layers"] == ["voiceover"]
    assert "recompose" in result.data["instruction"].lower()


@pytest.mark.asyncio
async def test_a_measurably_broken_master_is_not_locked() -> None:
    """The critic measured this and wrote it down. Locking it anyway is what
    made the critic decorative."""
    repo = _MemoryAgenticRepository()
    session = _session(repo, {
        "listen_report": {"clean": False,
                          "notes": ["the mix runs 6.0s short of the video"],
                          "observations": [], "measured": {}},
    })

    result = await _finalize(repo, session)

    assert result.data["error"] == "mix_has_faults"
    assert "6.0s short" in " ".join(result.data["faults"])


@pytest.mark.asyncio
async def test_a_user_who_has_heard_the_problem_may_still_have_it() -> None:
    """Refused, not forbidden. Their piece, their call — once they know."""
    repo = _MemoryAgenticRepository()
    session = _session(repo, {
        "listen_report": {"clean": False, "notes": ["it ends on 2.0s of silence"],
                          "observations": [], "measured": {}},
    })

    result = await _finalize(repo, session, acknowledge_faults=True)

    assert result.data.get("error") != "mix_has_faults"


@pytest.mark.asyncio
async def test_a_clean_master_locks_without_ceremony() -> None:
    repo = _MemoryAgenticRepository()
    session = _session(repo, {
        "listen_report": {"clean": True, "notes": [], "observations": [],
                          "measured": {}},
        "missing_layers": [],
    })

    result = await _finalize(repo, session)

    assert "error" not in result.data


def test_the_override_is_a_declared_argument() -> None:
    """Otherwise the validator would refuse the very escape hatch the
    instruction tells the agent to use."""
    from EdennCode.EdennAgent.AgenticAudio.models import TOOL_SPECS_BY_NAME

    spec = TOOL_SPECS_BY_NAME[AGENT_TOOL_FINALIZE]
    assert any(field.name == "acknowledge_faults" for field in spec.args)


def test_compose_records_what_is_missing_rather_than_deciding_alone() -> None:
    """Composing music-only while narration renders is legitimate, so compose
    records and finalize decides. Putting the refusal in compose would block a
    normal step."""
    from pathlib import Path

    impls = Path("EdennCode/EdennAgent/AgenticAudio/tools/impls.py").read_text()
    assert '"missing_layers": [' in impls
    assert "mix_missing_requested_layer" in impls


# --------------------------------------------------------------------------- #
# The master must be the one the user last heard                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_master_composed_before_a_re_cut_is_not_locked() -> None:
    """compose -> free re-cut -> finalize used to lock the PRE-cut file while
    the mix's listen report honestly described something else entirely."""
    repo = _MemoryAgenticRepository()
    session = _session(repo, {})

    # The user re-cuts the take. Only the candidate's render epoch moves.
    state = dict(session.state_json)
    state["candidates"] = [dict(state["candidates"][0], render_epoch=1)]
    repo.update_session(session.session_id, state_json=state)

    result = await _finalize(repo, repo.get_session(session.session_id))

    assert result.data["error"] == "mix_predates_edit"
    assert "compose again" in result.data["instruction"].lower()


@pytest.mark.asyncio
async def test_a_master_composed_before_a_retaken_line_is_not_locked() -> None:
    """The same hole reached through narration: one line re-synthesised, a new
    voice-over file, and the master still points at the old one."""
    repo = _MemoryAgenticRepository()
    state: dict[str, Any] = {
        "candidates": [{"candidate_id": "c1", "status": "completed",
                        "audio_url": "https://x/a.mp3"}],
        "selected_candidate_id": "c1",
        "production_plan": {"mode": "full_e2e", "layers": ["music", "voiceover"]},
        "layers": {"voiceover": {"audio_url": "https://x/vo-1.wav"}},
    }
    state["mix"] = {"music_candidate_id": "c1", "video_url": "/dev/media/m.mp4",
                    "built_from": mix_stems(state)}
    session = repo.create_session(
        source_video_artifact_id="art",
        phase=AgenticSessionPhase.AWAITING_CANDIDATE_CHOICE, state_json=state)

    moved = dict(session.state_json)
    moved["layers"] = {"voiceover": {"audio_url": "https://x/vo-2.wav"}}
    repo.update_session(session.session_id, state_json=moved)

    result = await _finalize(repo, repo.get_session(session.session_id))

    assert result.data["error"] == "mix_predates_edit"


@pytest.mark.asyncio
async def test_a_user_who_wants_the_older_master_may_still_have_it() -> None:
    """Refused, not forbidden — the same contract as the other two gates."""
    repo = _MemoryAgenticRepository()
    session = _session(repo, {})
    state = dict(session.state_json)
    state["candidates"] = [dict(state["candidates"][0], render_epoch=1)]
    repo.update_session(session.session_id, state_json=state)

    result = await _finalize(repo, repo.get_session(session.session_id),
                             acknowledge_faults=True)

    assert "error" not in result.data


@pytest.mark.asyncio
async def test_a_mix_that_still_describes_its_stems_locks() -> None:
    repo = _MemoryAgenticRepository()
    session = _session(repo, {})

    result = await _finalize(repo, session)

    assert "error" not in result.data


@pytest.mark.asyncio
async def test_a_mix_with_no_stamp_at_all_is_refused() -> None:
    """A master composed before provenance existed cannot prove it is current.
    One free re-compose is the cheaper error than handing over the wrong file."""
    repo = _MemoryAgenticRepository()
    session = _session(repo, {"built_from": None})

    result = await _finalize(repo, session)

    assert result.data["error"] == "mix_predates_edit"
