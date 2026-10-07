"""The surfaces a capability has to reach, and the gate that says it reached them.

Adding one verb to this agent means touching about seven places: the tool table,
the implementation, the argument rules, the decision schema, the prompt that
teaches the model to use it, the canvas affordance, and the mock backend the
browser suite runs against. Exactly one of those — membership in the registry —
was ever enforced. The rest were remembered, and the last round of work proves
how that goes: an intent added to the enum and never to the prompt, a tool with
no argument rules, a pair of verbs the canvas cannot offer, a mock that never
learned the new fields existed.

This file is the cheap half of the fix: it walks the tables that already exist
and fails, by name, when a surface has been left behind. The expensive half —
generating those surfaces from one manifest so they cannot be left behind — is
what these assertions are meant to make obviously worth doing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from EdennCode.EdennAgent.AgenticAudio.agent.prompts import SYSTEM_PROMPT
from EdennCode.EdennAgent.AgenticAudio.models import (
    AGENT_DECISION_SCHEMA,
    AGENT_INTENTS,
    TOOL_NAMES,
)


# ---------------------------------------------------------------------------#
# intents: the enum the model is scored against, and the list it is shown      #
# ---------------------------------------------------------------------------#


def test_the_prompt_offers_every_intent_the_schema_accepts() -> None:
    """`plan_audio` lived in the enum for months without appearing in the
    prompt, so the model chose from fourteen of fifteen labels — and the missing
    one was how a director says "here is how these layers go together"."""
    missing = [intent for intent in AGENT_INTENTS if intent not in SYSTEM_PROMPT]
    assert not missing, f"intents the model is never shown: {missing}"


def test_the_intent_list_is_generated_rather_than_retyped() -> None:
    """A parity test catches the drift; generation prevents it. The roster
    beside it has worked this way from the start."""
    source = Path("EdennCode/EdennAgent/AgenticAudio/agent/prompts.py").read_text()

    assert "{INTENT_LIST}" in source, "the prompt names a placeholder"
    assert '.replace("{INTENT_LIST}", _intent_list_block())' in source
    # ...and no hand-typed copy of the vocabulary survives beside it.
    assert "new_variation, select_final, compare" not in source


def test_the_schema_and_the_prompt_agree_on_the_vocabulary() -> None:
    enum = AGENT_DECISION_SCHEMA["schema"]["properties"]["intent"]["enum"]
    assert set(AGENT_INTENTS) <= set(enum)


# ---------------------------------------------------------------------------#
# tools: the registry, the prompt, and the argument rules                      #
# ---------------------------------------------------------------------------#


def test_every_registered_tool_is_taught_in_the_prompt() -> None:
    """A tool the model is never told about is a tool it will never call —
    which is how two free verbs shipped and stayed invisible."""
    missing = [name for name in TOOL_NAMES if name not in SYSTEM_PROMPT]
    assert not missing, f"tools the prompt never mentions: {missing}"


def test_every_registered_tool_has_an_implementation_bound_to_it() -> None:
    from EdennCode.EdennAgent.AgenticAudio.tools.impls import build_tool_registry

    registry = build_tool_registry()
    missing = [name for name in TOOL_NAMES if registry.get(name) is None]
    assert not missing, f"tools with no implementation: {missing}"


def test_every_tool_the_model_can_name_is_a_tool_that_exists() -> None:
    """The inverse direction: the decision schema's enum derives from the same
    table, so a tool cannot be offered to the model without being registered."""
    enum = [
        name
        for name in AGENT_DECISION_SCHEMA["schema"]["properties"]["action"]["properties"][
            "tool_name"
        ]["enum"]
        if name
    ]
    assert set(enum) == set(TOOL_NAMES)


# ---------------------------------------------------------------------------#
# an accepted suggestion keeps the shape the suggester computed               #
# ---------------------------------------------------------------------------#


def test_an_accepted_transition_keeps_its_length_and_its_authority() -> None:
    """The suggester computes where a whoosh starts AND ends, what kind of
    moment it is, and that its timing was snapped to a scene cut. Folding in
    only the start turned it into a zero-length hit the renderer floors to a
    click — with no authority for the refinement stage to recognise it by, so it
    was treated as hand-placed and excluded from the very re-snapping it exists
    to receive."""
    from EdennCode.EdennAgent.AgenticAudio.agent.agent import _suggestion_as_plan_row

    row = _suggestion_as_plan_row(
        {
            "suggestion_id": "sug_1",
            "start_time": 4.85,
            "end_time": 5.25,
            "sound_prompt": "airy whoosh",
            "rationale": "detected scene cut at 5.00s",
            "event_type": "TRANSITION",
            "timing_authority": "cut_snap",
        },
        position=3,
    )

    assert row["start_s"] == 4.85
    assert row["duration_s"] == 0.4, "an effect with no duration renders as a click"
    assert row["event_type"] == "TRANSITION"
    assert row["timing_authority"] == "cut_snap"
    assert row["id"] == "sfx_ev_3"
    assert row["label"] == row["prompt"] == "airy whoosh"


def test_the_authority_it_carries_is_one_the_render_stage_recognises() -> None:
    """The two vocabularies have to match or the carry-through is decorative."""
    from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.planned_run import (
        SNAPPABLE_AUTHORITIES,
    )
    from EdennCode.EdennAgent.AgenticAudio.agent.agent import _suggestion_as_plan_row
    from EdennCode.EdennAgent.AgenticAudio.tools.sfx_suggest import (
        TRANSITION_SUGGESTION_DURATION_S,
    )

    row = _suggestion_as_plan_row(
        {"start_time": 1.0, "end_time": 1.0 + TRANSITION_SUGGESTION_DURATION_S,
         "sound_prompt": "whoosh", "timing_authority": "cut_snap"},
        position=1,
    )
    assert row["timing_authority"] in SNAPPABLE_AUTHORITIES


def test_a_suggestion_with_no_end_time_is_left_for_the_render_to_default() -> None:
    """Inventing a duration would be worse than the renderer's own default."""
    from EdennCode.EdennAgent.AgenticAudio.agent.agent import _suggestion_as_plan_row

    row = _suggestion_as_plan_row(
        {"start_time": 2.0, "description": "door", "end_time": None}, position=1
    )
    assert "duration_s" not in row

    backwards = _suggestion_as_plan_row(
        {"start_time": 5.0, "end_time": 4.0, "description": "door"}, position=1
    )
    assert "duration_s" not in backwards, "a negative length is not a length"


def test_a_hand_placed_suggestion_carries_no_authority_it_was_not_given() -> None:
    from EdennCode.EdennAgent.AgenticAudio.agent.agent import _suggestion_as_plan_row

    row = _suggestion_as_plan_row(
        {"start_time": 2.0, "sound_prompt": "door slam"}, position=1
    )
    assert "timing_authority" not in row
    assert "event_type" not in row
    assert row["start_s"] == 2.0


def test_a_garbled_start_time_lands_at_zero_rather_than_crashing_the_accept() -> None:
    from EdennCode.EdennAgent.AgenticAudio.agent.agent import _suggestion_as_plan_row

    assert _suggestion_as_plan_row({"start_time": "soon"}, position=1)["start_s"] == 0.0


# ---------------------------------------------------------------------------#
# a choice the canvas can offer reaches every surface, or none                #
# ---------------------------------------------------------------------------#


def _choice_types() -> list[str]:
    """The canvas vocabulary, read off the request model that validates it."""
    import typing

    from EdennCode.EdennAgent.AgenticAudio.models import AgenticAudioChoiceRequest

    annotation = AgenticAudioChoiceRequest.model_fields["choice_type"].annotation
    return list(typing.get_args(annotation))


def test_every_choice_the_model_accepts_is_one_the_agent_can_act_on() -> None:
    """A choice type the request model accepts but no branch handles is a
    silent no-op: the click records, the user waits, and nothing happens."""
    agent_source = Path(
        "EdennCode/EdennAgent/AgenticAudio/agent/agent.py"
    ).read_text()

    # Two dispatch styles are in use and both are legitimate: an equality test,
    # and set membership where one branch serves several choices.
    missing = [
        choice for choice in _choice_types()
        if f'request.choice_type == "{choice}"' not in agent_source
        and f'"{choice}",' not in agent_source
        and f'"{choice}"}}' not in agent_source
    ]
    assert not missing, f"choice types with no dispatch branch: {missing}"


def test_every_choice_the_agent_acts_on_is_one_the_mock_can_answer() -> None:
    """Mock mirrors real. The browser suite is the only automated eyes on the
    canvas, and it is blind to any choice the mock has never heard of — which is
    how the last two free verbs shipped with no coverage at all."""
    mock_source = Path(
        "EdennCode/EdennAgent/AgenticAudio/frontend/mock-backend.js"
    ).read_text()

    missing = [
        choice for choice in _choice_types()
        if f'frame.choice_type === "{choice}"' not in mock_source
        and f'"{choice}"' not in mock_source
    ]
    assert not missing, f"choice types the mock backend cannot answer: {missing}"


def test_the_free_verbs_are_reachable_without_guessing_the_words() -> None:
    """Both of these existed for a while as chat-only phrasings. A capability a
    user has to guess the words for is one most users never find."""
    app_source = Path(
        "EdennCode/EdennAgent/AgenticAudio/frontend/js/app.js"
    ).read_text()

    assert 'choice_type: "sculpt"' in app_source
    assert 'choice_type: "compare"' in app_source
    # ...offered from the take itself, where the question comes up.
    assert "requestSculpt(c)" in app_source
    assert "requestCompare()" in app_source


def test_the_re_cut_control_cannot_offer_a_start_past_the_end_of_the_track() -> None:
    """Seeking past the end produces a "completed" re-cut of silence — the
    truncation failure arriving through a new door. The backend clamps too; the
    control simply never offers it."""
    app_source = Path(
        "EdennCode/EdennAgent/AgenticAudio/frontend/js/app.js"
    ).read_text()

    assert "Math.max(0, fullTrack - takeLength)" in app_source
    assert "range.max = String(latest.toFixed(1))" in app_source


def test_new_frontend_surface_is_versioned_so_browsers_pick_it_up() -> None:
    """A cached bundle is a shipped change nobody can see."""
    index = Path("EdennCode/EdennAgent/AgenticAudio/frontend/index.html").read_text()

    assert "v=e22" not in index, "the asset version was not bumped with this change"
    assert "sculpt-overlay" in index


# ---------------------------------------------------------------------------#
# the manifest: declaring a tool's contract is not optional                   #
# ---------------------------------------------------------------------------#


def test_a_tool_cannot_be_declared_without_saying_what_it_reads() -> None:
    """The gate, not the checklist. Two tools shipped with no argument rules at
    all because nothing forced the question to be answered — one of them turned
    a mistyped argument into a 500."""
    import pytest

    from EdennCode.EdennAgent.AgenticAudio.models import ArgField, ToolSpec

    with pytest.raises(ValueError) as caught:
        ToolSpec("silent_tool", "Doing something…")
    assert "args or reads_no_args" in str(caught.value)

    # ...and it is EITHER, not both: a tool that reads nothing has nothing to
    # declare, and one that declares fields is not argument-free.
    with pytest.raises(ValueError):
        ToolSpec(
            "confused_tool", "Doing something…",
            args=(ArgField("x", "number"),), reads_no_args=True,
        )

    # The two legal shapes.
    assert ToolSpec("reads_nothing", "…", reads_no_args=True).reads_no_args
    assert ToolSpec("reads_one", "…", args=(ArgField("x", "number"),)).args


def test_every_tool_in_the_table_has_answered_the_question() -> None:
    from EdennCode.EdennAgent.AgenticAudio.models import TOOL_SPECS

    for spec in TOOL_SPECS:
        assert bool(spec.args) != bool(spec.reads_no_args), spec.name


def test_the_validator_is_derived_from_the_table_not_retyped_beside_it() -> None:
    """A hand-written chain covered six of fourteen tools, and the two newest
    were among the eight it missed. Derivation is what makes that impossible."""
    source = Path("EdennCode/EdennAgent/AgenticAudio/tools/arg_specs.py").read_text()

    assert "TOOL_SPECS_BY_NAME" in source, "the rules come from the table"
    assert 'elif tool_name == "plan_sfx"' not in source, "no hand-written chain survives"
    assert 'if tool_name == "generate_candidates"' not in source


def test_the_prompt_teaches_the_argument_names_the_dispatcher_enforces() -> None:
    """The model was being taught argument names by prose nobody re-checked
    against the code that refuses them."""
    from EdennCode.EdennAgent.AgenticAudio.models import TOOL_SPECS

    for spec in TOOL_SPECS:
        for field in spec.args:
            if field.enforced:
                assert field.name in SYSTEM_PROMPT, f"{spec.name}.{field.name}"


def test_a_spec_with_no_implementation_fails_the_registry() -> None:
    """The inverse drift: a tool the model can name, the schema accepts, and
    the dispatcher cannot run."""
    import pytest

    from EdennCode.EdennAgent.AgenticAudio.tools.base import Tool, ToolRegistry
    from EdennCode.EdennAgent.AgenticAudio.models import TOOL_NAMES

    class _OnlyOne(Tool):
        name = TOOL_NAMES[0]

        async def run(self, ctx: object, args: dict) -> object:  # pragma: no cover
            raise NotImplementedError

    with pytest.raises(ValueError) as caught:
        ToolRegistry([_OnlyOne()])
    assert "no implementation bound" in str(caught.value)


# ---------------------------------------------------------------------------#
# the class bodies are still class bodies                                     #
# ---------------------------------------------------------------------------#


def test_the_media_service_still_has_every_verb_bound_to_it() -> None:
    """A module-level def inserted before a method silently ENDS the class
    body: Python imports it happily, and everything below becomes a loose
    function. Nothing failed at import — the first sign was an unrelated test
    losing a method it had always had. Cheap to assert, expensive to debug."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import AgenticAudioTools

    for verb in (
        "analyze_video",
        "route_style_prompt",
        "generate_music_candidates",
        "edit_audio",
        "adjust_remix",
        "sculpt_window",
        "compose_mix",
        "hydrate_candidate_results",
        "hydrate_voiceover_layer",
        "hydrate_sfx_layer",
    ):
        assert hasattr(AgenticAudioTools, verb), f"AgenticAudioTools lost {verb}"


def test_the_orchestrator_still_has_every_entrypoint_bound_to_it() -> None:
    from EdennCode.EdennAgent.AgenticAudio.agent.agent import AgenticAudioAgent

    for entry in (
        "bootstrap_session",
        "handle_user_message",
        "handle_choice",
        "refresh_session_state",
        "_act_on_sfx_suggestion",
        "_select_sfx_variant",
    ):
        assert hasattr(AgenticAudioAgent, entry), f"AgenticAudioAgent lost {entry}"


# ---------------------------------------------------------------------------#
# a choice must forward what its tool reads                                   #
# ---------------------------------------------------------------------------#


def test_a_choice_forwards_every_argument_its_tool_declares() -> None:
    """The gap that let two finished features ship unreachable.

    Each choice branch used to pick arguments out of the payload with a tuple
    written at the call site. A tool grows an argument, the tuple does not, and
    the capability is live everywhere except the one place a user could use it:
    splice and the volume envelope both shipped that way. Every layer was
    correct in isolation, which is why only a journey caught it.
    """
    from EdennCode.EdennAgent.AgenticAudio.agent.agent import _tool_args_from_payload
    from EdennCode.EdennAgent.AgenticAudio.models import TOOL_SPECS

    for spec in TOOL_SPECS:
        if not spec.args:
            continue
        # Everything the tool says it reads, offered at once.
        payload = {field.name: "x" for field in spec.args}
        forwarded = _tool_args_from_payload(spec.name, payload)
        missing = sorted(set(payload) - set(forwarded))
        assert not missing, f"{spec.name} choice would drop: {missing}"


def test_a_choice_forwards_nothing_its_tool_never_reads() -> None:
    """The other direction: a payload is client-controlled, and a tool should
    receive only what it has declared."""
    from EdennCode.EdennAgent.AgenticAudio.agent.agent import _tool_args_from_payload

    forwarded = _tool_args_from_payload(
        "sculpt_audio", {"window_start_s": 4.0, "not_an_argument": "surprise"}
    )
    assert "not_an_argument" not in forwarded
    assert forwarded["window_start_s"] == 4.0


def test_the_choice_branches_no_longer_hand_pick_arguments() -> None:
    """A hand-written tuple beside a manifest is the drift back."""
    source = Path("EdennCode/EdennAgent/AgenticAudio/agent/agent.py").read_text()

    assert "_tool_args_from_payload(" in source
    for retyped in (
        'for k in ("window_start_s", "sculpt_kind")',
        'for k in ("candidate_id", "music_volume", "preserve_original_audio")',
    ):
        assert retyped not in source, f"an argument list is being retyped: {retyped}"


# ---------------------------------------------------------------------------#
# the repairs that save a customer money have a control                       #
# ---------------------------------------------------------------------------#


def test_one_line_and_one_hit_can_be_fixed_from_the_page() -> None:
    """Both were built, both were reachable only by typing the right words into
    chat. They are the two verbs that exist specifically so a customer does not
    re-pay for work they were happy with, which makes them the worst two to
    leave without a control."""
    app = Path("EdennCode/EdennAgent/AgenticAudio/frontend/js/app.js").read_text()

    assert "segment_id: s.id" in app, "no way to re-read a single narration line"
    assert "event_ids: [ev.id]" in app, "no way to redo a single sound effect"
    # Both spend, so both go through the same confirmation as any generation.
    retake = app.split("Re-read this line?")[1][:400]
    assert "confirmSpend" in app and "startThinking()" in retake


def test_the_sfx_choice_carries_which_hits_to_redo() -> None:
    """A control that sends event_ids into a branch that drops them is the same
    unreachable feature with extra steps."""
    agent = Path("EdennCode/EdennAgent/AgenticAudio/agent/agent.py").read_text()
    sfx_dispatch = agent.split("AGENT_TOOL_GENERATE_SFX,")[-1][:400]

    assert "_tool_args_from_payload(AGENT_TOOL_GENERATE_SFX, payload)" in agent
    assert "{}" not in sfx_dispatch.split("invoked_by")[0], (
        "the sfx dispatch still sends an empty payload"
    )


# ---------------------------------------------------------------------------#
# the prompt against the gates it is supposed to agree with                    #
# ---------------------------------------------------------------------------#


def _tools_that_refuse_agent_invocation() -> list[str]:
    """Which tools only the USER can start, read off the gates themselves.

    Derived rather than listed, because a list is the thing that drifted. A
    tool qualifies when its implementation raises on `ctx.invoked_by != "user"`
    with nothing else in the test — an unconditional refusal, as opposed to
    the conditional nudges (propose_script wants timed segments on long
    footage, set_production_plan protects a user-owned plan) that shape a call
    the agent is still allowed to make.
    """

    import ast
    import inspect
    import textwrap

    from EdennCode.EdennAgent.AgenticAudio.tools import impls
    from EdennCode.EdennAgent.AgenticAudio.tools.base import Tool

    def refuses(cls: type) -> bool:
        tree = ast.parse(textwrap.dedent(inspect.getsource(cls)))
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if not (
                isinstance(test, ast.Compare)
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.NotEq)
                and isinstance(test.left, ast.Attribute)
                and test.left.attr == "invoked_by"
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == "user"
            ):
                continue
            if any(isinstance(n, ast.Raise) for n in ast.walk(node)):
                return True
        return False

    return sorted(
        obj.name
        for obj in vars(impls).values()
        if isinstance(obj, type)
        and issubclass(obj, Tool)
        and getattr(obj, "name", "")
        and refuses(obj)
    )


_CALL_VERB = re.compile(
    r"\b(call|calling|invoke|invoking|run|re-run|rerun|trigger|fire|use|pass)\b",
    re.I,
)
_STOOD_DOWN = re.compile(
    r"never|not |n't|refus|blocked|only runs from|do not|don't|won't|cannot|"
    r"yourself|instead of|rather than|without",
    re.I,
)


def _clauses_instructing_a_call(prompt: str, tool: str) -> list[str]:
    flat = re.sub(r"\s+", " ", prompt)
    offending: list[str] = []
    for match in re.finditer(re.escape(tool), flat):
        if not _CALL_VERB.search(flat[max(0, match.start() - 70): match.start()]):
            continue
        window = flat[max(0, match.start() - 160): match.end() + 160]
        if _STOOD_DOWN.search(window):
            continue
        offending.append(window)
    return offending


def test_no_clause_tells_the_model_to_call_a_tool_the_gate_refuses() -> None:
    """The gate kept narration spend behind the user's click; the prose told
    the model to call the tool anyway — twice, once for a tone change and once
    to re-read a single line. Both are real requests, so the model tried, and
    every attempt burned a step and ended the turn on a refusal the user never
    asked for. The fix is the prose, never the gate: these clauses now point at
    the control the user presses."""
    user_only = _tools_that_refuse_agent_invocation()
    assert user_only, "the invoker gate vanished — it is what keeps spend behind a click"

    for tool in user_only:
        offending = _clauses_instructing_a_call(SYSTEM_PROMPT, tool)
        assert not offending, (
            f"the prompt tells the model to call {tool}, which the gate refuses "
            f"for agent invocation: {offending}"
        )


def test_the_prompt_splits_free_from_paid_the_way_the_manifest_does() -> None:
    """Both halves were hand-typed and both had drifted: the free list named
    five of ten (so the model asked permission for work that spends nothing),
    and the rule naming what MAY NOT run without approval named three of four
    — leaving generate_sfx outside the sentence that guards every other way to
    spend."""
    from EdennCode.EdennAgent.AgenticAudio.models import TOOL_SPECS

    flat = re.sub(r"\s+", " ", SYSTEM_PROMPT)
    spending = flat.split("The tools that spend are:")[1].split(".")[0]
    free = flat.split("is free and may run without asking:")[1].split(".")[0]

    named_paid = {n.strip() for n in spending.split(",")}
    named_free = {n.strip() for n in free.split(",")}

    assert named_paid == {s.name for s in TOOL_SPECS if s.is_generation}
    assert named_free == {s.name for s in TOOL_SPECS if not s.is_generation}
    assert not (named_paid & named_free), "a tool cannot be on both sides"


def test_the_spend_split_is_generated_rather_than_retyped() -> None:
    """Same reason as the intent list: a list the model is shown, maintained by
    hand beside the table it is supposed to mirror, drifts. Both halves now
    come out of `is_generation`."""
    prompts_src = Path(
        "EdennCode/EdennAgent/AgenticAudio/agent/prompts.py"
    ).read_text()
    assert '.replace(\n    "{PAID_TOOLS}"' in prompts_src
    assert '.replace(\n    "{FREE_TOOLS}"' in prompts_src
    assert "spec.is_generation is generation" in prompts_src
    # ...and no hand-typed copy of either half survives beside it: the rendered
    # lists must exist nowhere but the block that generates them.
    for half in ("The tools that spend are:", "is free and may run without asking:"):
        rendered = re.sub(r"\s+", " ", SYSTEM_PROMPT).split(half)[1].split(".")[0]
        first_two = ", ".join(rendered.strip().split(", ")[:2])
        assert first_two not in prompts_src, f"the list is retyped in the prose: {first_two}"


# ---------------------------------------------------------------------------#
# the schema the model answers against, and the table that defines the tools   #
# ---------------------------------------------------------------------------#


def _schema_tool_args() -> dict[str, object]:
    return AGENT_DECISION_SCHEMA["schema"]["properties"]["action"]["properties"][
        "tool_args"
    ]["properties"]


def test_every_declared_argument_is_in_the_schema_the_model_answers_against() -> None:
    """Eight arguments the validator enforces were absent from the schema, so
    the model was never shown the shape of a call it is refused for getting
    wrong — including every argument of the two free verbs added most recently
    (splice's `segments`, the fade `music_envelope`, the per-hit `event_ids`)."""
    from EdennCode.EdennAgent.AgenticAudio.models import TOOL_SPECS

    properties = _schema_tool_args()
    missing = sorted(
        {field.name for spec in TOOL_SPECS for field in spec.args}
        - set(properties)
    )
    assert not missing, f"declared but never shown to the model: {missing}"


def test_every_schema_argument_belongs_to_a_tool_that_reads_it() -> None:
    """The other direction: a property nothing reads is a call the model can be
    taught to make and no tool can honour."""
    from EdennCode.EdennAgent.AgenticAudio.models import TOOL_SPECS

    declared = {field.name for spec in TOOL_SPECS for field in spec.args}
    orphaned = sorted(set(_schema_tool_args()) - declared)
    assert not orphaned, f"schema properties no tool declares: {orphaned}"


def test_the_schema_offers_every_way_to_re_cut_a_take() -> None:
    """The concrete drift this derivation removes: a second sculpt kind shipped
    with its tool, its validator rule and its canvas control, and the schema
    kept offering one — so the model reading the schema could not name it."""
    from EdennCode.EdennAgent.AgenticAudio.models import SCULPT_KINDS

    offered = set(_schema_tool_args()["sculpt_kind"]["enum"]) - {""}
    assert offered == set(SCULPT_KINDS)


def test_the_number_that_reaches_a_paid_render_is_bounded() -> None:
    """A length typed into a card and a length invented by a model both land
    here, and nothing downstream clamps it: "30000" is a paid render of eight
    hours of music nobody can use."""
    from EdennCode.EdennAgent.AgenticAudio.models import MAX_EXTEND_SECONDS
    from EdennCode.EdennAgent.AgenticAudio.tools.arg_specs import (
        ToolArgsInvalid,
        validate_tool_args,
    )

    assert _schema_tool_args()["extend_seconds"]["maximum"] == MAX_EXTEND_SECONDS
    validate_tool_args("edit_audio", {"extend_seconds": MAX_EXTEND_SECONDS})
    with pytest.raises(ToolArgsInvalid):
        validate_tool_args("edit_audio", {"extend_seconds": MAX_EXTEND_SECONDS + 1})


def test_every_argument_a_tool_reads_is_one_it_declares() -> None:
    """Under-declaration is the quiet half of the drift. A tool reading an
    argument nobody declared means the prompt never names it, the schema never
    shows it, the validator never checks it, and the choice branches cannot
    forward it — which is exactly how the per-line re-record became
    unreachable from the card that offers it."""
    import ast
    import inspect
    import textwrap

    from EdennCode.EdennAgent.AgenticAudio.models import TOOL_SPECS_BY_NAME
    from EdennCode.EdennAgent.AgenticAudio.tools import impls
    from EdennCode.EdennAgent.AgenticAudio.tools.base import Tool

    def keys_read(cls: type) -> set[str]:
        tree = ast.parse(textwrap.dedent(inspect.getsource(cls)))
        found: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"get", "pop", "setdefault"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "args"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                found.add(node.args[0].value)
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == "args"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                found.add(node.slice.value)
            if (
                isinstance(node, ast.Compare)
                and len(node.ops) == 1
                and isinstance(node.ops[0], ast.In)
                and isinstance(node.left, ast.Constant)
                and isinstance(node.left.value, str)
                and isinstance(node.comparators[0], ast.Name)
                and node.comparators[0].id == "args"
            ):
                found.add(node.left.value)
        return found

    undeclared: dict[str, list[str]] = {}
    for obj in vars(impls).values():
        if not (
            isinstance(obj, type)
            and issubclass(obj, Tool)
            and getattr(obj, "name", "")
        ):
            continue
        spec = TOOL_SPECS_BY_NAME.get(obj.name)
        if spec is None:
            continue
        missing = sorted(keys_read(obj) - {field.name for field in spec.args})
        if missing:
            undeclared[obj.name] = missing
    assert not undeclared, f"arguments read but never declared: {undeclared}"


def test_no_choice_branch_still_hand_picks_the_arguments_it_forwards() -> None:
    """Every branch derives what it forwards. The hand-written tuples did not
    merely risk drift — they had already drifted: the voice-over branch dropped
    `segment_id` (so the per-line re-record re-recorded everything), and the
    script branch could not carry `hold_silent` at all."""
    source = Path("EdennCode/EdennAgent/AgenticAudio/agent/agent.py").read_text()

    # A dispatch whose args come from a comprehension over a literal tuple of
    # names is the shape that drifts.
    retyped = re.findall(r"for k in \((?:\s*\"[a-z_]+\",?)+\s*\)", source)
    assert not retyped, f"argument lists still retyped beside the manifest: {retyped}"
