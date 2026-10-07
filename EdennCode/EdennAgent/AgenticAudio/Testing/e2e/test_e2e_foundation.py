"""Hermetic self-test of the E2E foundation (runs every commit, no model).

Drives a scripted scenario through the real router over in-memory fakes and
asserts the gates + the watch renderer work. The real-LLM persona run lives in
run_e2e.py (and the env-gated real-LLM gate in test_eval_harness)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from .driver import E2EDriver, run_scenario
from .fakes import ScriptedAgentClient
from .gates import compute_metrics, gate_summary
from .scenarios import Scenario, Turn
from .watch import render_transcript


def _scripted_opener_decisions() -> list[dict]:
    """analyze -> propose two directions -> acknowledge (no spend)."""
    return [
        {
            "thought": "Observe before proposing.",
            "intent": "analyze",
            "assistant_message": "Analyzing your video.",
            "action": {"type": "call_tool", "tool_name": "analyze_video", "tool_args": {}},
        },
        {
            "thought": "Offer directions.",
            "intent": "request_proposals",
            "assistant_message": "Here are a couple of directions.",
            "action": {
                "type": "propose",
                "proposals": [
                    {"proposal_id": "p1", "title": "Cinematic", "prompt": "x", "modelspec": "edenn_basic"},
                    {"proposal_id": "p2", "title": "Upbeat", "prompt": "y", "modelspec": "edenn_basic"},
                ],
            },
        },
        {
            "thought": "Nothing to do.",
            "intent": "other",
            "assistant_message": "Sounds good — tell me when to generate.",
            "action": {"type": "noop"},
        },
    ]


_SCENARIO = Scenario(
    name="hermetic_smoke",
    description="Scripted opener + acknowledgement; no spend.",
    turns=[
        Turn(message="Make it cinematic.", gold_intent="analyze", accepted_intents=["analyze", "request_proposals"]),
        Turn(message="Thanks!", gold_intent="other", accepted_intents=["other"]),
    ],
)


def test_e2e_foundation_runs_scripted_scenario_and_gates() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        driver = E2EDriver(Path(tmp), ScriptedAgentClient(_scripted_opener_decisions()))
        trajectory = run_scenario(driver, _SCENARIO)

    # Trajectory shape.
    assert len(trajectory.turns) == 2
    assert all(tr.status == 200 for tr in trajectory.turns)
    # Proposals were shown; no generation, no spend.
    assert len(trajectory.final_state.get("proposals") or []) == 2

    metrics = compute_metrics(trajectory, _SCENARIO)
    assert metrics["unauthorized_generation"] == 0          # hard safety gate
    assert metrics["intent_accuracy"] == 1.0
    assert metrics["heavy_jobs"] == 0

    summary = gate_summary([metrics])
    assert summary["passed"] is True
    assert summary["unauthorized_generation_total"] == 0


def test_watch_renderer_produces_readable_trace() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        driver = E2EDriver(Path(tmp), ScriptedAgentClient(_scripted_opener_decisions()))
        trajectory = run_scenario(driver, _SCENARIO)
    text = render_transcript(trajectory, _SCENARIO, compute_metrics(trajectory, _SCENARIO))
    assert "hermetic_smoke" in text
    assert "conversation:" in text and "turns:" in text and "artifacts:" in text
    # The user's lines appear in the trace.
    assert "Make it cinematic." in text


def _traj(state):
    """A Trajectory carrying just the final state the narration gates read."""
    from EdennCode.EdennAgent.AgenticAudio.Testing.e2e.driver import Trajectory
    t = Trajectory.__new__(Trajectory)
    t.turns = []
    t.final_state = state
    return t


_OBS = {"duration_s": 16.167, "cuts": [5.867, 8.9, 12.2, 14.9],
        "cut_source": "pyscenedetect"}


def test_alignment_gate_passes_a_read_that_landed():
    from EdennCode.EdennAgent.AgenticAudio.Testing.e2e.gates import (
        narration_metrics, gate_summary,
    )
    good = _traj({"observation": _OBS, "layers": {"voiceover": {"segments": [
        {"id": "seg_01", "text": "Some moments are watched.", "start_s": 1.0, "duration_s": 1.83},
        {"id": "seg_02", "text": "The rare ones are felt.", "start_s": 6.4, "duration_s": 2.06},
    ]}}})
    m = narration_metrics(good)
    assert m["narration_lines"] == 2
    assert (m["narration_straddles"], m["narration_overruns"],
            m["narration_unfit_stored"], m["sfx_under_narration"],
            m["ownership_overridden"]) == (0, 0, 0, 0, 0)
    assert gate_summary([{**m, "scenario": "x"}])["passed"] is True


def test_alignment_gate_catches_each_fault_it_exists_for():
    """A gate that cannot fail is worse than no gate: every one of these shipped
    to a listener at some point, and each was re-introduced by an edit that
    looked harmless."""
    from EdennCode.EdennAgent.AgenticAudio.Testing.e2e.gates import (
        narration_metrics, gate_summary,
    )

    # A line spoken across a shot change, and running past the final frame.
    bad = _traj({"observation": _OBS, "layers": {"voiceover": {"segments": [
        {"id": "seg_01", "text": "A line that runs right across the cut and off the end.",
         "start_s": 13.5, "duration_s": 3.2},
    ]}}})
    m = narration_metrics(bad)
    assert m["narration_straddles"] == 1
    assert m["narration_overruns"] == 1
    assert gate_summary([{**m, "scenario": "x"}])["passed"] is False

    # A plan too long for its clip reaching state at all.
    unfit = _traj({"observation": _OBS, "layers": {"voiceover": {"segments": [
        {"id": "s1", "text": "Sometimes a moment is so beautiful it leaves you speechless.", "start_s": 0.2, "duration_s": 4.7},
        {"id": "s2", "text": "You can see it before a single word is spoken.", "start_s": 5.4, "duration_s": 3.4},
        {"id": "s3", "text": "In the eyes, in the silence, in the disbelief.", "start_s": 9.1, "duration_s": 4.2},
        {"id": "s4", "text": "And for just a second everyone feels it together.", "start_s": 13.8, "duration_s": 3.6},
    ]}}})
    assert narration_metrics(unfit)["narration_unfit_stored"] == 1

    # Sound design left sitting under the voice.
    clash = _traj({"observation": _OBS, "layers": {
        "voiceover": {"segments": [
            {"id": "seg_01", "text": "Some moments are watched.", "start_s": 1.0, "duration_s": 1.83}]},
        "sfx": {"collisions": [{"event": "whoosh", "start_s": 1.5,
                                "reason": "fires under narration"}]},
    }})
    assert narration_metrics(clash)["sfx_under_narration"] == 1

    # A user's ownership decision restamped by the agent.
    stolen = _traj({"observation": _OBS,
                    "layers": {"voiceover": {"segments": [
                        {"id": "seg_01", "text": "Some moments are watched.",
                         "start_s": 1.0, "duration_s": 1.83}]}},
                    "spotting_sheet": {"moments": [{
                        "id": "moment_01", "t": 5.87, "window": [5.87, 8.9],
                        "owner": "silence", "owner_source": "narration",
                        "revised_from": {"owner": "silence", "owner_source": "user"},
                    }]}})
    assert narration_metrics(stolen)["ownership_overridden"] == 1


def test_alignment_gate_is_honest_about_examining_nothing():
    """A scenario that never writes narration must not read as a pass on
    alignment — the counts say how much was actually looked at."""
    from EdennCode.EdennAgent.AgenticAudio.Testing.e2e.gates import (
        narration_metrics, gate_summary,
    )
    m = narration_metrics(_traj({"observation": _OBS, "layers": {}}))
    assert m["narration_lines"] == 0
    summary = gate_summary([{**m, "scenario": "music-only"}])
    assert summary["passed"] is True
    assert summary["narration_lines_examined"] == 0
