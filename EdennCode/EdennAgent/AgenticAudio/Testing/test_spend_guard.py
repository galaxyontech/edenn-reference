"""Not spending twice for one request.

Every generating tool costs real provider credit, and every path that reaches
one retries: a client resends a POST it never saw answered, a socket drops
mid-turn and the console reconnects, the loop re-proposes after a transient
failure. Each of those produced a second render, a second charge, and a take
nobody asked for.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from EdennCode.EdennAgent.AgenticAudio.agent import spend_guard


# ---------------------------------------------------------------------------#
# what counts as the same request                                             #
# ---------------------------------------------------------------------------#


def test_the_same_call_fingerprints_the_same() -> None:
    a = spend_guard.fingerprint("generate_candidates", {"proposal_id": "p1", "n": 2})
    b = spend_guard.fingerprint("generate_candidates", {"n": 2, "proposal_id": "p1"})
    assert a == b, "argument order changed the identity of the request"


def test_a_different_ask_fingerprints_differently() -> None:
    a = spend_guard.fingerprint("generate_candidates", {"proposal_id": "p1"})
    b = spend_guard.fingerprint("generate_candidates", {"proposal_id": "p2"})
    c = spend_guard.fingerprint("generate_voiceover", {"proposal_id": "p1"})
    assert len({a, b, c}) == 3


def test_unserialisable_arguments_still_produce_a_fingerprint() -> None:
    """A guard that throws on an odd argument is a guard that stops guarding."""
    assert spend_guard.fingerprint("generate_sfx", {"when": object()})


# ---------------------------------------------------------------------------#
# the window                                                                  #
# ---------------------------------------------------------------------------#


def test_an_immediate_repeat_is_recognised() -> None:
    state = spend_guard.remember({}, tool_name="generate_candidates", tool_args={"p": 1})
    assert spend_guard.recent_match(
        state, tool_name="generate_candidates", tool_args={"p": 1}
    ) is not None


def test_a_different_call_is_not_recognised() -> None:
    state = spend_guard.remember({}, tool_name="generate_candidates", tool_args={"p": 1})
    assert spend_guard.recent_match(
        state, tool_name="generate_candidates", tool_args={"p": 2}
    ) is None


def test_the_same_ask_much_later_is_allowed() -> None:
    """"Give me another take like that one" is a real thing people do; refusing
    it an hour later would be wrong. The window only covers the retry storm."""
    now = time.time()
    state = spend_guard.remember(
        {}, tool_name="generate_candidates", tool_args={"p": 1}, now=now - 10_000
    )
    assert spend_guard.recent_match(
        state, tool_name="generate_candidates", tool_args={"p": 1}, now=now
    ) is None


def test_the_record_does_not_grow_without_bound() -> None:
    """It lives inside the session document, which is read and written on every
    turn — an unbounded list there costs every future request."""
    state: dict = {}
    for i in range(200):
        state = spend_guard.remember(
            state, tool_name="generate_candidates", tool_args={"p": i}
        )
    assert len(state[spend_guard.STATE_KEY]) <= spend_guard.MAX_RECORDS


def test_expired_records_are_swept_rather_than_kept() -> None:
    now = time.time()
    state = spend_guard.remember(
        {}, tool_name="generate_candidates", tool_args={"p": 1}, now=now - 10_000
    )
    state = spend_guard.remember(
        state, tool_name="generate_candidates", tool_args={"p": 2}, now=now
    )
    assert len(state[spend_guard.STATE_KEY]) == 1


def test_the_guard_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero disables it — an escape hatch for a load test that means to repeat."""
    monkeypatch.setenv("AGENTIC_AUDIO_SPEND_DEDUPE_WINDOW_S", "0")
    assert spend_guard.window_seconds() == 0


# ---------------------------------------------------------------------------#
# through the dispatcher                                                      #
# ---------------------------------------------------------------------------#


class _Spec:
    def __init__(self, generation: bool) -> None:
        self.is_generation = generation
        self.is_heavy = generation


class _CountingTool:
    """A tool that records how many times it actually ran."""

    def __init__(self, name: str, *, generation: bool) -> None:
        self.name = name
        self.requires_approval = generation
        self.runs = 0

    async def run(self, ctx, args):
        from EdennCode.EdennAgent.AgenticAudio.tools.base import ToolResult

        self.runs += 1
        return ToolResult(events=[], data={"ok": True})


class _Registry:
    def __init__(self, tool) -> None:
        self._tool = tool

    def get(self, name: str):
        return self._tool


def _dispatcher(tool, repo):
    from EdennCode.EdennAgent.AgenticAudio.agent.dispatcher import ToolDispatcher

    return ToolDispatcher(
        registry=_Registry(tool), repository=repo, media=None, max_candidates=3
    )


@pytest.fixture()
def repo():
    from EdennCode.EdennAgent.AgenticAudio.design.memory_fakes import (
        _MemoryAgenticRepository,
    )

    r = _MemoryAgenticRepository()
    r.create_session(
        session_id="sess_1",
        source_video_artifact_id="artifact_1",
        creator_user_id="alice",
        phase="observing",
        state_json={},
    )
    return r


def test_a_repeated_generation_runs_only_once(repo) -> None:
    import asyncio

    from EdennCode.EdennAgent.AgenticAudio.agent.dispatcher import DuplicateGeneration

    tool = _CountingTool("generate_candidates", generation=True)
    d = _dispatcher(tool, repo)

    asyncio.run(d.dispatch_collecting("sess_1", "generate_candidates", {"p": 1}))
    with pytest.raises(DuplicateGeneration):
        asyncio.run(d.dispatch_collecting("sess_1", "generate_candidates", {"p": 1}))
    assert tool.runs == 1, "the second attempt still spent"


def test_a_different_generation_is_not_blocked(repo) -> None:
    import asyncio

    tool = _CountingTool("generate_candidates", generation=True)
    d = _dispatcher(tool, repo)

    asyncio.run(d.dispatch_collecting("sess_1", "generate_candidates", {"p": 1}))
    asyncio.run(d.dispatch_collecting("sess_1", "generate_candidates", {"p": 2}))
    assert tool.runs == 2


def test_a_free_tool_is_never_blocked(repo) -> None:
    """Only spending is guarded. Re-reading or re-planning is free and people do
    it constantly."""
    import asyncio

    tool = _CountingTool("analyze_video", generation=False)
    d = _dispatcher(tool, repo)

    for _ in range(4):
        asyncio.run(d.dispatch_collecting("sess_1", "analyze_video", {"same": True}))
    assert tool.runs == 4


def test_the_guard_records_before_the_tool_runs(repo) -> None:
    """The window has to cover the tool being SLOW — which is exactly when a
    client gives up and retries. Recording after the run would leave the whole
    render window unguarded."""
    import asyncio

    from EdennCode.EdennAgent.AgenticAudio.agent.dispatcher import DuplicateGeneration

    seen: dict = {}

    class _SlowTool(_CountingTool):
        async def run(self, ctx, args):
            # While "running", a retry arrives.
            state = repo.get_session("sess_1").state_json
            seen["during"] = spend_guard.recent_match(
                state, tool_name="generate_candidates", tool_args={"p": 9}
            )
            return await super().run(ctx, args)

    tool = _SlowTool("generate_candidates", generation=True)
    d = _dispatcher(tool, repo)
    asyncio.run(d.dispatch_collecting("sess_1", "generate_candidates", {"p": 9}))

    assert seen["during"] is not None, "a retry during the run would have spent again"


def test_the_refusal_is_a_client_error_not_a_server_fault(repo) -> None:
    """The request is the problem, and the honest answer is that the work is
    already happening."""
    from EdennCode.EdennAgent.AgenticAudio.agent.dispatcher import DuplicateGeneration

    assert issubclass(DuplicateGeneration, ValueError)


# ---------------------------------------------------------------------------#
# a retry versus asking again                                                 #
# ---------------------------------------------------------------------------#


def test_asking_again_after_a_result_arrives_is_allowed() -> None:
    """"Another variant" sends identical arguments — which take it is, is
    implicit. Blocking that would break a real thing people do."""
    before = {"layers": {"sfx": {"variants": [{"variant_id": "v1"}]}}}
    state = spend_guard.remember(before, tool_name="generate_sfx", tool_args={})

    after = dict(state)
    after["layers"] = {"sfx": {"variants": [{"variant_id": "v1"}, {"variant_id": "v2"}]}}
    assert spend_guard.recent_match(after, tool_name="generate_sfx", tool_args={}) is None


def test_a_retry_before_the_result_exists_is_blocked() -> None:
    """The dangerous case: the first call is still in flight, so the session has
    not moved, and both would spend."""
    state = {"layers": {"sfx": {"variants": [{"variant_id": "v1"}]}}}
    state = spend_guard.remember(state, tool_name="generate_sfx", tool_args={})
    assert spend_guard.recent_match(state, tool_name="generate_sfx", tool_args={}) is not None


def test_a_new_take_changes_the_witness() -> None:
    a = spend_guard.witness({"candidates": [{"candidate_id": "c1"}]})
    b = spend_guard.witness({"candidates": [{"candidate_id": "c1"}, {"candidate_id": "c2"}]})
    assert a != b


def test_a_legacy_list_shaped_sfx_layer_does_not_break_the_witness() -> None:
    """The layer has had two shapes; a guard that throws on the old one stops
    guarding for exactly the sessions that predate the change."""
    assert spend_guard.witness({"layers": {"sfx": []}})


def test_forget_releases_a_refused_spend_but_only_that_one() -> None:
    """A blocked call never spent; its fingerprint must not make the user's
    immediately-following click read as a duplicate. Only the refused entry is
    released — a genuinely-running generation stays remembered."""

    from EdennCode.EdennAgent.AgenticAudio.agent import spend_guard

    state: dict = {}
    state = spend_guard.remember(state, tool_name="generate_voiceover",
                                 tool_args={"voice_id": "calm_male"})
    state = spend_guard.remember(state, tool_name="generate_sfx", tool_args={})
    assert spend_guard.recent_match(state, tool_name="generate_voiceover",
                                    tool_args={"voice_id": "calm_male"}) is not None

    state = spend_guard.forget(state, tool_name="generate_voiceover",
                               tool_args={"voice_id": "calm_male"})
    assert spend_guard.recent_match(state, tool_name="generate_voiceover",
                                    tool_args={"voice_id": "calm_male"}) is None
    assert spend_guard.recent_match(state, tool_name="generate_sfx",
                                    tool_args={}) is not None


# ---------------------------------------------------------------------------#
# who spent it                                                                #
# ---------------------------------------------------------------------------#


@pytest.mark.asyncio
async def test_a_spend_is_attributed_to_the_caller(repo, caplog) -> None:
    """The audit line names the signed-in caller, not "unauthenticated".

    On the deployed console every billed generation logged no actor at all —
    four out of four during the live audit, by a signed-in owner, over both
    transports. The dispatcher was never handed a caller, so it recorded that it
    had none; the router had already resolved one and bound it for logging.
    """

    import json
    import logging

    from EdennCode.EdennAgent.AgenticAudio.api.observability import bind_request

    tool = _CountingTool("generate_candidates", generation=True)
    bind_request(session_id="sess_1", principal="alice")
    with caplog.at_level(logging.INFO, logger="edenn.agentic_audio.audit"):
        await _dispatcher(tool, repo).dispatch("sess_1", "generate_candidates", {})

    lines = [
        json.loads(r.getMessage().split("audit ", 1)[1])
        for r in caplog.records
        if r.name == "edenn.agentic_audio.audit"
    ]
    assert lines and lines[0]["action"] == "generation.generate_candidates"
    assert lines[0]["actor"] == "alice"
    assert lines[0]["kind"] == "spend"
