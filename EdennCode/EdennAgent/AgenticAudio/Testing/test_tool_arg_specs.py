"""Refusing the tool calls that used to crash — or worse, quietly misfire.

The decision schema ships with strict off, so nothing guarantees the model's
arguments have the shapes the tools expect. Each case below was verified against
the tools' own coercions: either it raised something nothing caught (a 500), or
it succeeded while meaning the opposite of what was asked.

The silent ones are the reason this exists. A bad number is a 500 and somebody
notices; ``allow_overlap: "false"`` is truthy, walks through the gate that
protects narration, and produces a mix nobody reviews.
"""

from __future__ import annotations

from typing import Any

import pytest

from EdennCode.EdennAgent.AgenticAudio.tools.arg_specs import (
    ToolArgsInvalid,
    validate_tool_args,
)


# ---------------------------------------------------------------------------#
# the crashes (uncaught TypeError -> 500)                                     #
# ---------------------------------------------------------------------------#


@pytest.mark.parametrize(
    "tool,args",
    [
        ("generate_candidates", {"count": {"n": 2}}),   # int({}) -> TypeError
        ("generate_candidates", {"count": [2]}),
        ("set_production_plan", {"layers": 5}),          # `for layer in 5` -> TypeError
        ("generate_voiceover", {"speed": {"x": 1}}),     # float({}) -> TypeError
        ("propose_script", {"hold_silent": 3}),
    ],
)
def test_arguments_that_used_to_crash_are_refused(tool: str, args: dict[str, Any]) -> None:
    with pytest.raises(ToolArgsInvalid) as caught:
        validate_tool_args(tool, args)
    assert caught.value.instruction, "a refusal without a repair instruction is a dead end"


# ---------------------------------------------------------------------------#
# the silent wrong answers                                                    #
# ---------------------------------------------------------------------------#


def test_a_quoted_false_is_refused_where_it_would_mean_yes() -> None:
    # bool("false") is True. Both of these flags turn a protection OFF, so the
    # string form does the opposite of what it says.
    with pytest.raises(ToolArgsInvalid):
        validate_tool_args("plan_sfx", {"allow_overlap": "false"})
    with pytest.raises(ToolArgsInvalid):
        validate_tool_args("set_production_plan", {"force_music": "false"})


def test_real_booleans_are_fine() -> None:
    validate_tool_args("plan_sfx", {"allow_overlap": True})
    validate_tool_args("set_production_plan", {"force_music": False})


def test_an_unreadable_start_time_is_refused_instead_of_becoming_zero() -> None:
    # "soon" coerced to 0.0, putting an effect meant for 0:12 on the first frame,
    # with nothing reported anywhere.
    with pytest.raises(ToolArgsInvalid) as caught:
        validate_tool_args(
            "plan_sfx",
            {"sfx_events": [{"label": "door", "prompt": "a door", "start_s": "soon"}]},
        )
    assert "sfx_events[0].start_s" in str(caught.value)


def test_an_infinite_start_time_is_refused() -> None:
    # Survived the clamp and was written into the plan, then forwarded to render.
    with pytest.raises(ToolArgsInvalid):
        validate_tool_args(
            "plan_sfx", {"sfx_events": [{"start_s": float("inf")}]},
        )


def test_a_string_where_a_list_of_layers_belongs_is_refused() -> None:
    # "music" iterates per character, every character is dropped, and the plan
    # silently becomes voiceover-only — the opposite of the request.
    with pytest.raises(ToolArgsInvalid):
        validate_tool_args("set_production_plan", {"layers": "music"})


# ---------------------------------------------------------------------------#
# what must STILL be accepted (the validator must not widen a refusal)        #
# ---------------------------------------------------------------------------#


@pytest.mark.parametrize(
    "tool,args",
    [
        # Numeric strings work today and must keep working.
        ("plan_sfx", {"sfx_events": [{"start_s": "3.5"}]}),
        ("generate_candidates", {"count": "2"}),
        ("generate_voiceover", {"speed": 1.08}),
        # A documented silent fallback: an unknown mode really is meant to become
        # music_first, and a test pins that.
        ("set_production_plan", {"mode": "nonsense"}),
        # Unknown keys are ignored by the tools, and the spend fingerprint hashes
        # the args verbatim — refusing them here would change what counts as a
        # duplicate generation.
        ("generate_sfx", {"unexpected": "value"}),
        ("plan_sfx", {"sfx_events": [{"label": "x", "prompt": "y", "start_s": 4, "extra": 1}]}),
        # Absent and empty are always fine; every arg is optional.
        ("plan_sfx", {}),
        ("set_production_plan", {"layers": []}),
        ("generate_candidates", {"count": None}),
        # A tool with no rules at all passes anything.
        ("analyze_video", {"whatever": ["anything"]}),
        ("finalize", {"candidate_id": "c1"}),
    ],
)
def test_working_calls_are_left_alone(tool: str, args: dict[str, Any]) -> None:
    validate_tool_args(tool, args)


def test_validation_never_rewrites_the_arguments() -> None:
    """The spend guard fingerprints tool_args verbatim — generate_sfx reads no
    arguments at all, yet a stray key still changes its fingerprint. Normalising
    here would quietly change which repeat calls count as duplicates."""
    args = {"count": "2", "extra": {"nested": [1, 2]}}
    before = repr(args)

    validate_tool_args("generate_candidates", args)

    assert repr(args) == before


# ---------------------------------------------------------------------------#
# enums                                                                       #
# ---------------------------------------------------------------------------#


def test_an_unknown_edit_kind_is_named_with_its_options() -> None:
    with pytest.raises(ToolArgsInvalid) as caught:
        validate_tool_args("edit_audio", {"edit_kind": "remix"})
    message = str(caught.value)
    assert "regenerate" in message and "extend" in message, (
        "a refusal should say what the acceptable values are"
    )


def test_the_known_edit_kinds_pass() -> None:
    for kind in ("regenerate", "extend", "creative_edit"):
        validate_tool_args("edit_audio", {"edit_kind": kind})


# ---------------------------------------------------------------------------#
# the contract with the two call paths                                        #
# ---------------------------------------------------------------------------#


def test_the_refusal_is_a_value_error() -> None:
    """The deterministic /choices path maps ValueError to 400 — a malformed card
    submission IS a bad request — while the model's path catches it in the loop
    and repairs the call. One exception type serves both."""
    assert issubclass(ToolArgsInvalid, ValueError)


# ---------------------------------------------------------------------------#
# the two verbs that shipped without any rules at all                         #
# ---------------------------------------------------------------------------#


def test_comparing_takes_refuses_a_non_list_instead_of_raising_a_500() -> None:
    """`[str(cid) for cid in 5]` is a TypeError nothing catches."""
    with pytest.raises(ToolArgsInvalid) as caught:
        validate_tool_args("compare_takes", {"candidate_ids": 5})
    assert "candidate_ids" in str(caught.value)
    assert caught.value.instruction


def test_a_single_id_passed_as_a_string_is_refused_not_spelled_out() -> None:
    """The quiet one: "c1" iterates per character, matches no take, and the tool
    reports having nothing to compare — in the tool whose whole job is telling
    takes apart."""
    with pytest.raises(ToolArgsInvalid):
        validate_tool_args("compare_takes", {"candidate_ids": "c1"})


def test_an_id_that_is_not_an_id_is_named_by_position() -> None:
    with pytest.raises(ToolArgsInvalid) as caught:
        validate_tool_args("compare_takes", {"candidate_ids": ["c1", 7]})
    assert "candidate_ids[1]" in str(caught.value)


def test_comparing_every_finished_take_needs_no_arguments() -> None:
    validate_tool_args("compare_takes", {})
    validate_tool_args("compare_takes", {"candidate_ids": []})
    validate_tool_args("compare_takes", {"candidate_ids": ["c1", "c2"]})


def test_sculpting_to_a_word_is_refused_before_it_becomes_the_top_of_the_track() -> None:
    """"soon" coerced to 0.0 hands back the window the user is moving away
    from, and calls it done."""
    with pytest.raises(ToolArgsInvalid) as caught:
        validate_tool_args("sculpt_audio", {"window_start_s": "soon"})
    assert "window_start_s" in str(caught.value)


def test_an_unknown_sculpt_kind_is_named_with_the_kinds_that_exist() -> None:
    with pytest.raises(ToolArgsInvalid) as caught:
        validate_tool_args("sculpt_audio", {"sculpt_kind": "fade_out"})
    assert "sculpt_kind" in str(caught.value)


def test_a_plain_re_cut_passes_untouched() -> None:
    validate_tool_args("sculpt_audio", {"sculpt_kind": "shift_window",
                                        "window_start_s": 42.5})
    validate_tool_args("sculpt_audio", {"window_start_s": "42.5"})   # numeric strings work
    validate_tool_args("sculpt_audio", {})


def test_the_new_branches_still_never_mutate_what_they_check() -> None:
    """The spend fingerprint hashes tool_args verbatim."""
    import copy

    for tool, args in (
        ("compare_takes", {"candidate_ids": ["c1", "c2"], "unknown": 1}),
        ("sculpt_audio", {"sculpt_kind": "shift_window", "window_start_s": "42.5"}),
    ):
        before = copy.deepcopy(args)
        validate_tool_args(tool, args)
        assert args == before, f"{tool} arguments were rewritten"


# ---------------------------------------------------------------------------#
# the gaps a full inventory of the tools turned up                            #
# ---------------------------------------------------------------------------#


def test_asking_for_no_vocals_as_a_string_does_not_buy_a_vocal_take() -> None:
    """The bool-ish trap on a PAID path: "false" is truthy, so a take the user
    asked to keep instrumental came back sung."""
    with pytest.raises(ToolArgsInvalid) as caught:
        validate_tool_args("generate_candidates", {"include_vocals": "false"})
    assert "include_vocals" in str(caught.value)

    validate_tool_args("generate_candidates", {"include_vocals": False})
    validate_tool_args("generate_candidates", {"include_vocals": True})


def test_keeping_the_original_audio_is_a_boolean_not_a_word() -> None:
    """Same trap, on the flag that decides whether the video's own audio stays
    under the music."""
    for tool in ("adjust_remix", "compose_mix"):
        with pytest.raises(ToolArgsInvalid):
            validate_tool_args(tool, {"preserve_original_audio": "no"})
        validate_tool_args(tool, {"preserve_original_audio": True})


def test_a_direction_that_is_not_text_never_reaches_a_paid_render() -> None:
    """str() accepts anything, so a list arrived at the provider as its own
    Python repr and was generated from."""
    for tool in ("generate_candidates", "edit_audio"):
        with pytest.raises(ToolArgsInvalid):
            validate_tool_args(tool, {"prompt": ["warm", "cinematic"]})
        validate_tool_args(tool, {"prompt": "warm and cinematic"})


def test_the_tool_that_reads_nothing_refuses_nothing() -> None:
    """generate_sfx renders the plan already on the session. A stray key still
    changes its spend fingerprint, which is why the table says so out loud —
    but there is nothing here to refuse."""
    validate_tool_args("generate_sfx", {"anything": "at all"})
    validate_tool_args("generate_sfx", {})


def test_an_unknown_tool_is_the_registrys_refusal_to_make_not_ours() -> None:
    validate_tool_args("no_such_tool", {"whatever": object()})
