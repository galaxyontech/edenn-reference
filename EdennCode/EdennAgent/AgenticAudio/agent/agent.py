"""The agentic-audio orchestrator.

Thin by design: it owns the public entry points (bootstrap / message / choice /
refresh) and the DETERMINISTIC paths (the intent gate and ``/choices`` routing),
and delegates every model-driven turn to :class:`ReasoningLoop` and every tool
run to :class:`ToolDispatcher`. The reasoning, memory, stages, tools, and events
each live in their own module — see loop.py / stages.py / tool_impls.py / events.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable, Optional

from EdennCode.ModelFactory.LanguageModelFactory.azure_based_model_model_gateway import (
    AzureMultimodalClient,
)

from .dispatcher import ToolDispatcher
from ..events import EventType
from .loop import ReasoningLoop
from ..models import (
    AGENT_ACTION_CALL_TOOL,
    AGENT_EDIT_KIND_REGENERATE,
    AGENT_PLAN_MODE_FULL_E2E,
    AGENT_PLAN_MODE_MUSIC_FIRST,
    AGENT_TOOL_ADJUST_REMIX,
    AGENT_TOOL_ANALYZE_VIDEO,
    AGENT_TOOL_COMPARE_TAKES,
    AGENT_TOOL_COMPOSE_MIX,
    AGENT_TOOL_EDIT_AUDIO,
    AGENT_TOOL_FINALIZE,
    AGENT_TOOL_GENERATE_CANDIDATES,
    AGENT_TOOL_GENERATE_SFX,
    AGENT_TOOL_GENERATE_VOICEOVER,
    AGENT_TOOL_PLAN_SFX,
    AGENT_TOOL_PROPOSE_SCRIPT,
    AGENT_TOOL_SCULPT_AUDIO,
    AGENT_TOOL_SET_PRODUCTION_PLAN,
    AUDIO_LAYER_MUSIC,
    AUDIO_LAYER_SFX,
    AUDIO_LAYER_VOICEOVER,
    AUDIO_LAYERS,
    AgenticAudioChoiceRequest,
    AgenticAudioSession,
    AgenticAudioWebSocketEvent,
    AgenticMessageRole,
    usable_music_modelspec,
)
from .prompts import SYSTEM_PROMPT
from .turn_lock import SessionBusy, TurnLock
from ..persistence.repositories import AgenticAudioRepository
from .session_io import (
    append_message,
    find_by_id,
    make_event,
    push,
    reasoning_event,
    record_choice_event,
    require_session,
)
from ..tools.base import ApprovalRequiredError
from ..tools.impls import build_tool_registry
from ..tools.media import AgenticAudioTools, normalize_music_modelspec
from ..tools.spotting import OWNERS, set_moment_owner


logger = logging.getLogger(__name__)


# Optional async sink the WS endpoint passes in to stream each event LIVE as the
# turn unfolds (so the UI shows real reasoning beats, not a static placeholder).
# When None (the REST path), events are simply returned in a batch as before.
EventSink = Callable[[AgenticAudioWebSocketEvent], Awaitable[None]]


# Deterministic "intent gate" (enabled by the production mount, off by default so
# existing tests keep their scripted analyze->propose flow). When on, a fresh
# session first OBSERVES the video, then asks the user which modality to build
# before the agent proposes any music. The answer is recorded as a production
# plan, so the modality choice is explicit and durable — independent of the LLM.
INTENT_GATE_OPTIONS = [
    {"id": "full_audio", "label": "Full audio", "hint": "Music + voiceover + SFX"},
    {"id": "music_only", "label": "Music only", "hint": "Soundtrack first"},
    {"id": "voiceover_only", "label": "Voiceover only", "hint": "Narration over the video"},
    {"id": "sound_design", "label": "Sound FX", "hint": "Effects matched to the action"},
]
# SFX isn't a shipped layer yet, so it is intentionally NOT planned — the canvas
# only ever shows tracks the session can actually deliver.
INTENT_GATE_PLAN = {
    "full_audio": ("full_e2e", ["music", "voiceover", "sfx"]),
    "music_only": ("music_first", ["music"]),
    "voiceover_only": ("music_first", ["voiceover"]),
    "sound_design": ("music_first", ["sfx"]),
}
INTENT_GATE_QUESTION = "What are you adding today?"
# Phrases that name ONE layer unmistakably. Used only to skip re-asking a
# question the user has already answered in their own words — never to guess.
INTENT_GATE_PHRASES = {
    AUDIO_LAYER_MUSIC: (
        r"\bmusic\b", r"\bsoundtrack\b", r"\bscore\b", r"\bsong\b",
        r"\bbgm\b", r"\bbacking track\b",
    ),
    AUDIO_LAYER_VOICEOVER: (
        r"\bvoice[\s-]?overs?\b", r"\bvoiceovers?\b", r"\bnarrat(?:e|ion|or)\b",
        r"\bvo\b",
    ),
    AUDIO_LAYER_SFX: (
        r"\bsound effects?\b", r"\bsfx\b", r"\bfoley\b", r"\bsound design\b",
        r"\bambien(?:ce|t)\b",
    ),
}


def layer_named_by(message: str) -> Optional[str]:
    """The single layer this message names, or None if it names none or several.

    The intent gate asks which modality to build, which is the right question to
    ask someone who has not said. Asked of someone who opened with "add
    background music to this clip" it is the product not listening: their answer
    was the first thing they typed. Conservative on purpose — two layers named,
    or none, and the gate still asks, so the failure mode is the old behaviour
    rather than a modality nobody chose.
    """

    import re

    text = str(message or "").lower()
    named = [
        layer
        for layer, patterns in INTENT_GATE_PHRASES.items()
        if any(re.search(pattern, text) for pattern in patterns)
    ]
    return named[0] if len(named) == 1 else None
# The clarify card carries the question, so the greeting must NOT repeat it.
INTENT_GATE_GREETING = "Got it — I had a look at your video."


def build_agentic_audio_agent_client() -> AzureMultimodalClient:
    """Build the dedicated LLM client used only for agent reasoning.

    Reads ``AGENTIC_AUDIO_AZURE_*`` env vars and falls back to the shared
    ``AZURE_*`` configuration, so the agent can run a different deployment from
    the video-music pipeline without extra setup.
    """

    def _pick(specific: str, shared: str, default: str = "") -> str:
        return (
            os.getenv(specific, "").strip()
            or os.getenv(shared, "").strip()
            or default
        )

    endpoint = _pick("AGENTIC_AUDIO_AZURE_ENDPOINT", "AZURE_ENDPOINT")
    model = _pick("AGENTIC_AUDIO_AZURE_MODEL", "AZURE_MODEL")
    api_key = _pick("AGENTIC_AUDIO_AZURE_API_KEY", "AZURE_API_KEY")
    api_version = _pick(
        "AGENTIC_AUDIO_AZURE_API_VERSION", "AZURE_API_VERSION", "2024-12-01-preview"
    )
    api_mode = _pick("AGENTIC_AUDIO_AZURE_API_MODE", "AZURE_API_MODE", "chat_completions")
    timeout = int(_pick("AGENTIC_AUDIO_AZURE_API_TIMEOUT", "AZURE_API_TIMEOUT", "60"))

    if not endpoint or not model or not api_key:
        raise RuntimeError(
            "Agentic audio agent client requires AGENTIC_AUDIO_AZURE_* or AZURE_* "
            "endpoint/model/api_key to be configured."
        )

    return AzureMultimodalClient(
        azure_endpoint=endpoint,
        azure_api_version=api_version,
        azure_model=model,
        api_key=api_key,
        timeout=timeout,
        label="agentic_audio",
        api_mode=api_mode,
    )


def _tool_args_from_payload(
    tool_name: str, payload: Optional[dict[str, Any]], **overrides: Any
) -> dict[str, Any]:
    """The arguments a canvas choice forwards to its tool.

    Derived from the tool's own declared arguments rather than a tuple written
    out at the call site. Those tuples are how a new capability arrives
    unreachable: the tool grows an argument, the choice branch keeps forwarding
    the three it always did, and the feature is live everywhere except the
    place a user could actually use it. Both of the verbs added most recently
    shipped that way and a journey test caught it, not a unit test — because
    every layer in isolation was correct.

    Unknown keys are dropped, which is the same contract the validator has:
    a payload is client-controlled, and a tool should receive only what it says
    it reads.
    """

    from ..models import TOOL_SPECS_BY_NAME

    spec = TOOL_SPECS_BY_NAME.get(tool_name)
    declared = {field.name for field in (spec.args if spec else ())}
    args = {
        key: value for key, value in (payload or {}).items() if key in declared
    }
    args.update({k: v for k, v in overrides.items() if v is not None})
    return args


def _suggestion_as_plan_row(
    suggestion: dict[str, Any], *, position: int
) -> dict[str, Any]:
    """Turn an accepted SFX suggestion into a committed plan row.

    A suggestion is not a bare timestamp: the suggester computed where the
    effect starts AND ends, what kind of moment it is, and — for a whoosh
    leading a scene cut — that its timing was snapped to that cut and is
    allowed to be re-snapped later. Folding in only the start threw all of
    that away, so an accepted transition arrived as a zero-length hit that
    the renderer floors into a click, carrying no authority for the
    refinement stage to recognise it by.

    Field names change at this boundary, which is the whole reason the drop
    went unnoticed: the suggester speaks ``start_time``/``end_time``, and the
    plan speaks ``start_s``/``duration_s``.
    """

    label = str(
        suggestion.get("sound_prompt") or suggestion.get("description") or "Effect"
    )
    try:
        start_s = max(0.0, float(suggestion.get("start_time") or 0.0))
    except (TypeError, ValueError):
        start_s = 0.0

    row: dict[str, Any] = {
        "id": f"sfx_ev_{position}",
        "label": label,
        "prompt": label,
        "start_s": start_s,
        "reason": str(suggestion.get("rationale") or ""),
    }

    # How long it runs is the difference between a sound and a click.
    try:
        end_s = float(suggestion.get("end_time"))
    except (TypeError, ValueError):
        end_s = None
    if end_s is not None and end_s > start_s:
        row["duration_s"] = round(end_s - start_s, 3)

    # What kind of moment it is, and whether its timing may be re-snapped to
    # nearby motion. A row that loses its authority is treated as hand-placed
    # and excluded from refinement — the opposite of what a cut-snapped
    # suggestion is asking for.
    for source_key, plan_key in (
        ("event_type", "event_type"),
        ("timing_authority", "timing_authority"),
    ):
        value = str(suggestion.get(source_key) or "").strip()
        if value:
            row[plan_key] = value
    return row


class AgenticAudioAgent:
    """Orchestrator: entry points + deterministic paths; delegates reasoning to
    :class:`ReasoningLoop` and tool execution to :class:`ToolDispatcher`."""

    def __init__(
        self,
        *,
        repository: AgenticAudioRepository,
        tools: AgenticAudioTools,
        llm_client: Any,
        max_steps_per_turn: Optional[int] = None,
        max_candidates: Optional[int] = None,
        require_intent_gate: bool = False,
    ) -> None:
        self.repository = repository
        self.tools = tools
        self.llm_client = llm_client
        # Serialises turns per session. Distributed when the repository can hand
        # us a database connection; in-process otherwise.
        # Counted so a shutdown can wait for turns instead of killing them:
        # a turn interrupted mid-render has usually already spent, so the user
        # loses a take they were charged for.
        self.turns_in_flight = 0

        def _enter() -> None:
            self.turns_in_flight += 1

        def _exit() -> None:
            self.turns_in_flight = max(0, self.turns_in_flight - 1)

        self._turn_lock_impl = TurnLock(
            client_factory=getattr(repository, "_client_factory", None),
            on_enter=_enter,
            on_exit=_exit,
        )
        # When True (production mount), bootstrap deterministically asks the user
        # which modality to build before the agent proposes. See the gate below.
        self.require_intent_gate = bool(require_intent_gate)
        self.max_steps_per_turn = int(
            max_steps_per_turn
            if max_steps_per_turn is not None
            else os.getenv("AGENTIC_AUDIO_MAX_STEPS_PER_TURN", "4")
        )
        self.max_candidates = int(
            max_candidates
            if max_candidates is not None
            else os.getenv("AGENTIC_AUDIO_MAX_CANDIDATES", "3")
        )
        # Single registry -> dispatcher -> loop. Tools are stateless (state flows
        # through ToolContext), so one shared registry instance is fine.
        self.dispatcher = ToolDispatcher(
            registry=build_tool_registry(),
            repository=repository,
            media=tools,
            max_candidates=self.max_candidates,
        )
        self.loop = ReasoningLoop(
            repository=repository,
            llm_client=llm_client,
            dispatcher=self.dispatcher,
            max_steps_per_turn=self.max_steps_per_turn,
        )

    # ----- public entry points (mirror the previous planner surface) ---------

    async def bootstrap_session(
        self,
        *,
        session: AgenticAudioSession,
        initial_message: Optional[str] = None,
        emit: Optional[EventSink] = None,
    ) -> list[AgenticAudioWebSocketEvent]:
        events: list[AgenticAudioWebSocketEvent] = []
        if initial_message:
            await push(
                emit,
                events,
                append_message(
                    self.repository, session.session_id, AgenticMessageRole.USER, initial_message
                ),
            )
        # Intent gate (production): observe the video, then ask which modality to
        # build BEFORE proposing. Skipped when already observed (idempotent).
        if self.require_intent_gate and not (session.state_json or {}).get("observation"):
            named = layer_named_by(initial_message)
            if named:
                # They already answered it. Analyse, record THEIR layer as the
                # plan (source "user", so a later model plan cannot drop it), and
                # get on with the work.
                events.extend(
                    await self._bootstrap_with_named_layer(
                        session.session_id, layer=named, emit=emit
                    )
                )
            else:
                events.extend(
                    await self._bootstrap_with_intent_gate(session.session_id, emit=emit)
                )
                return events
        # The loop streams its own events live; just collect them here.
        events.extend(
            await self.loop.run(
                session.session_id, user_message=initial_message, emit=emit
            )
        )
        return events

    async def _bootstrap_with_named_layer(
        self, session_id: str, *, layer: str, emit: Optional[EventSink] = None
    ) -> list[AgenticAudioWebSocketEvent]:
        """Observe the video and record the layer the user already named.

        The same two steps the gate performs, minus the question — and the plan
        is marked the user's own, because it is: they wrote it.
        """

        events: list[AgenticAudioWebSocketEvent] = []
        await push(
            emit,
            events,
            [
                reasoning_event(
                    session_id,
                    intent="analyze",
                    action_type=AGENT_ACTION_CALL_TOOL,
                    tool_name=AGENT_TOOL_ANALYZE_VIDEO,
                    thought="Observing the video before deciding the audio.",
                )
            ],
        )
        await push(
            emit, events, await self.dispatcher.dispatch(session_id, AGENT_TOOL_ANALYZE_VIDEO, {})
        )
        plan_args = {
            "mode": AGENT_PLAN_MODE_MUSIC_FIRST,
            "layers": [layer],
            "source": "user",
        }
        if layer != AUDIO_LAYER_MUSIC:
            plan_args["force_music"] = False
        await push(
            emit,
            events,
            await self.dispatcher.dispatch(
                session_id, AGENT_TOOL_SET_PRODUCTION_PLAN, plan_args, invoked_by="user"
            ),
        )
        return events

    async def _bootstrap_with_intent_gate(
        self, session_id: str, *, emit: Optional[EventSink] = None
    ) -> list[AgenticAudioWebSocketEvent]:
        """Deterministic first turn: observe the video, then ask the modality
        (full audio / music only / voice-over) before proposing any music. The
        choice is recorded as a production plan when answered (see handle_choice).
        Runs no LLM step, so the modality is captured explicitly every time."""

        events: list[AgenticAudioWebSocketEvent] = []
        await push(
            emit,
            events,
            [
                reasoning_event(
                    session_id,
                    intent="analyze",
                    action_type=AGENT_ACTION_CALL_TOOL,
                    tool_name=AGENT_TOOL_ANALYZE_VIDEO,
                    thought="Observing the video before deciding the audio.",
                )
            ],
        )
        await push(
            emit, events, await self.dispatcher.dispatch(session_id, AGENT_TOOL_ANALYZE_VIDEO, {})
        )
        await push(
            emit,
            events,
            append_message(
                self.repository, session_id, AgenticMessageRole.ASSISTANT, INTENT_GATE_GREETING
            ),
        )
        clarification = {
            "question": INTENT_GATE_QUESTION,
            "options": [dict(option) for option in INTENT_GATE_OPTIONS],
            "gate": "intent",
        }
        state = dict(require_session(self.repository, session_id).state_json)
        state["pending_clarification"] = clarification
        self.repository.update_session(session_id, state_json=state)
        await push(
            emit, events, [make_event(session_id, EventType.CLARIFY_CARDS, clarification)]
        )
        return events

    def _turn_lock(self, session_id: str):
        """Per-session turn serialisation, across replicas when there is a
        database to hold the lock in.

        A turn reads the session, reasons, calls tools and writes it back many
        times over. Two turns interleaving is not a race to tidy up afterwards —
        it is two agents editing the same document with different ideas of what
        is in it, and the loser's work disappears.

        See turn_lock.py: a Postgres advisory lock when a client factory is
        available, an in-process lock otherwise (which is exactly right for the
        single-replica standalone).
        """
        return self._turn_lock_impl.hold(session_id)

    async def handle_user_message(
        self,
        *,
        session_id: str,
        content: str,
        payload: Optional[dict[str, Any]] = None,
        emit: Optional[EventSink] = None,
    ) -> list[AgenticAudioWebSocketEvent]:
        require_session(self.repository, session_id)
        async with self._turn_lock(session_id):
            events: list[AgenticAudioWebSocketEvent] = []
            await push(
                emit,
                events,
                append_message(
                    self.repository, session_id, AgenticMessageRole.USER, content, payload_json=payload or {}
                ),
            )
            # A comment-originated turn tags BOTH its user + assistant messages with
            # source="comment" so the console shows them only in the comment thread,
            # not the main chat transcript (keeps turn ordering clean).
            message_payload = (
                {"source": "comment"} if (payload or {}).get("source") == "comment" else None
            )
            # The loop streams its own events live; just collect them here.
            events.extend(
                await self.loop.run(
                    session_id, user_message=content, emit=emit, message_payload=message_payload
                )
            )
            return events

    async def handle_choice(
        self,
        *,
        session_id: str,
        request: AgenticAudioChoiceRequest,
        emit: Optional[EventSink] = None,
    ) -> list[AgenticAudioWebSocketEvent]:
        """Explicit UI choices are dispatched deterministically (no LLM round-trip)."""

        async with self._turn_lock(session_id):
            events = await self._handle_choice_locked(
                session_id=session_id, request=request, emit=emit
            )
            # Structured choices change exactly the state the durable memory is
            # derived from — the approved direction, the takes, the lock. Folding
            # only on chat turns left Director's notes describing a session that
            # no longer existed (live audit, 2026-08-30).
            self.loop.fold_memory(session_id)
            return events

    async def _handle_choice_locked(
        self,
        *,
        session_id: str,
        request: AgenticAudioChoiceRequest,
        emit: Optional[EventSink] = None,
    ) -> list[AgenticAudioWebSocketEvent]:
        session = require_session(self.repository, session_id)

        if request.choice_type == "proposal":
            proposal = find_by_id(
                session.state_json.get("proposals") or [], "proposal_id", request.target_id
            )
            if proposal is None:
                raise KeyError(f"Proposal not found: {request.target_id}")
            # Guard: generate_candidates REPLACES state["candidates"], so a second
            # proposal approval would both double-spend and wipe every existing
            # take/branch. Refuse deterministically while any usable take exists
            # (all-failed sessions may regenerate). The chat UI hides consumed
            # proposal cards; this closes the same door for the canvas/REST paths.
            existing = session.state_json.get("candidates") or []
            usable = [
                c for c in existing
                if isinstance(c, dict) and c.get("status") not in {"failed", "error"}
            ]
            if usable:
                raise ApprovalRequiredError(
                    "This session already has generated takes — branch a variation "
                    "from one of them instead. (Re-generating a direction would "
                    "replace your existing takes.)"
                )
            # Picking a proposal IS the explicit human approval that unlocks the
            # paid generation gate (the high-assurance, non-LLM approval path).
            state = dict(session.state_json)
            # The tier rides the approval click. Across every organic session on
            # the deployment the director chose the two premium tiers 26 times
            # out of 26 while the analysis suggested the basic one every time,
            # and there was no control anywhere in the console to say otherwise —
            # the only way to reach the basic tier was to say the words in chat.
            # Same principle as the layer picker: the model may suggest, the user
            # decides, and the decision is written where generation reads it.
            chosen_spec = str((request.payload or {}).get("modelspec") or "").strip()
            if chosen_spec:
                wanted = normalize_music_modelspec(chosen_spec)
                # Availability is settled once, here, and the ask is preserved so
                # the card can show a substitution rather than quietly making one.
                usable_spec = usable_music_modelspec(wanted)
                proposals = [dict(p) for p in (state.get("proposals") or [])]
                for entry in proposals:
                    if str(entry.get("proposal_id")) != str(request.target_id):
                        continue
                    entry["modelspec"] = usable_spec
                    entry["modelspec_source"] = "user"
                    if usable_spec != wanted:
                        entry["requested_modelspec"] = wanted
                    else:
                        entry.pop("requested_modelspec", None)
                    proposal = dict(entry)
                state["proposals"] = proposals
            state["approved_direction"] = True
            state["approved_proposal_id"] = request.target_id
            self.repository.update_session(session_id, state_json=state)
            events: list[AgenticAudioWebSocketEvent] = []
            await push(
                emit, events, [record_choice_event(self.repository, session_id, request)]
            )
            await push(
                emit,
                events,
                await self.dispatcher.dispatch(
                    session_id,
                    AGENT_TOOL_GENERATE_CANDIDATES,
                    {"proposal_id": request.target_id},
                ),
            )
            # Approval used to be wordless: the button dimmed and nothing said
            # what was spent or how long it takes. Acknowledge deterministically
            # (no LLM call) with honest expectations.
            title = str(proposal.get("title") or "your direction")
            await push(
                emit,
                events,
                append_message(
                    self.repository,
                    session_id,
                    AgenticMessageRole.ASSISTANT,
                    f"Generating takes of “{title}” now — this spends generation credits "
                    "and usually takes a few minutes. They'll appear here and on the "
                    "canvas as they finish.",
                ),
            )
            return events

        if request.choice_type in {"candidate", "compose"}:
            if request.choice_type == "compose":
                candidate_id = session.selected_candidate_id
                if not candidate_id:
                    raise ValueError("Select a candidate before composing the final mix.")
            else:
                candidate_id = request.target_id
            candidate = find_by_id(
                session.state_json.get("candidates") or [], "candidate_id", candidate_id
            )
            if candidate is None:
                raise KeyError(f"Candidate not found: {candidate_id}")
            events = []
            await push(
                emit, events, [record_choice_event(self.repository, session_id, request)]
            )
            await push(
                emit,
                events,
                await self.dispatcher.dispatch(
                    session_id, AGENT_TOOL_FINALIZE, {"candidate_id": candidate_id}
                ),
            )
            # Locking used to happen SILENTLY (no assistant reply), which made a
            # full-audio session look stalled: the plan still owes a voice-over but
            # nothing said so. Acknowledge the lock deterministically (no LLM call)
            # and, when the plan's voiceover layer is still outstanding, say what
            # comes next instead of leaving the session in a wordless "composing".
            title = str(candidate.get("title") or "your take")
            plan_layers = list(
                (session.state_json.get("production_plan") or {}).get("layers") or []
            )
            voiceover = (session.state_json.get("layers") or {}).get("voiceover") or {}
            if "voiceover" in plan_layers and voiceover.get("status") != "completed":
                ack = (
                    f"Locked in “{title}”. The music is set — next up is the "
                    "voice-over: tell me what the narration should say (or ask me to draft it)."
                )
            else:
                ack = f"Locked in “{title}”. Your final mix is ready — you can keep iterating any time."
            await push(
                emit,
                events,
                append_message(
                    self.repository, session_id, AgenticMessageRole.ASSISTANT, ack
                ),
            )
            return events

        if request.choice_type == "clarification":
            # Closing the clarify loop: tapping a quick-choice chip records the
            # answer, clears the outstanding question, and feeds the chosen option
            # back in as the user's turn so the agent acts on it with full context.
            pending = session.state_json.get("pending_clarification") or {}
            option = find_by_id(pending.get("options") or [], "id", request.target_id)
            # The layer picker offers subsets that no single option id names, so
            # the option's own label ("Music only") can describe something other
            # than what the person ticked. Say back what they actually chose —
            # this text becomes their turn in the transcript.
            picked_layers = [
                layer
                for layer in (request.payload.get("layers") or [])
                if layer in AUDIO_LAYERS
            ]
            layer_answer = ""
            if picked_layers and (pending.get("gate") == "intent"):
                names = {
                    AUDIO_LAYER_MUSIC: "Music",
                    AUDIO_LAYER_VOICEOVER: "Voice-over",
                    AUDIO_LAYER_SFX: "Sound effects",
                }
                chosen = [names[l] for l in AUDIO_LAYERS if l in picked_layers]
                layer_answer = " + ".join(chosen)
            answer = str(
                request.payload.get("text")
                or layer_answer
                or (option or {}).get("label")
                or request.target_id
            )
            events = []
            await push(
                emit, events, [record_choice_event(self.repository, session_id, request)]
            )
            state = dict(session.state_json)
            state["pending_clarification"] = None
            # SFX treatment card: record the answered treatment durably so the
            # agent never re-asks and plan_sfx's gate opens (see PlanSfxTool).
            if pending.get("topic") == "sfx_treatment":
                label = str(
                    (option or {}).get("label") or request.payload.get("text") or ""
                ).strip()
                if not label:
                    # A no-answer answer (empty target_id, stale option id, no
                    # text) must not settle the gate with garbage — keep the
                    # card outstanding and change nothing.
                    return events
                state["sfx_treatment"] = {
                    "id": request.target_id,
                    "label": label,
                    "hint": (option or {}).get("hint"),
                    "notes": str(request.payload.get("text") or "").strip(),
                    "source": "card",
                }
            self.repository.update_session(session_id, state_json=state)
            await push(
                emit,
                events,
                append_message(
                    self.repository,
                    session_id,
                    AgenticMessageRole.USER,
                    answer,
                    payload_json={"clarification_option_id": request.target_id},
                ),
            )
            # Intent gate: record the chosen modality as a production plan before
            # the agent proposes, so the choice is durable and routes the layers.
            if pending.get("gate") == "intent":
                mode, layers = INTENT_GATE_PLAN.get(
                    request.target_id, (AGENT_PLAN_MODE_MUSIC_FIRST, [AUDIO_LAYER_MUSIC])
                )
                # The picker lets the user choose ANY subset of the three layers,
                # but there are only four intent ids, so several subsets collapse
                # onto an id whose canned layer list is not what was ticked
                # (music+voiceover lands on "full_audio" and silently re-adds
                # sound effects; music+sfx lands on "music_only" and silently
                # drops them). The client sends the actual selection alongside the
                # id — honour it, and keep the id mapping only as the fallback for
                # a caller that sends no layers.
                chosen = [
                    layer
                    for layer in (request.payload.get("layers") or [])
                    if layer in AUDIO_LAYERS
                ]
                if chosen:
                    layers = list(dict.fromkeys(chosen))
                    mode = (
                        AGENT_PLAN_MODE_FULL_E2E
                        if AUDIO_LAYER_MUSIC in layers and AUDIO_LAYER_VOICEOVER in layers
                        else AGENT_PLAN_MODE_MUSIC_FIRST
                    )
                # Marked as the user's own choice so a later model-driven plan
                # cannot quietly drop a layer they asked for.
                plan_args = {"mode": mode, "layers": list(layers), "source": "user"}
                # A session that did not ask for music must not have one forced
                # on it — true for the legacy ids and for an explicit selection.
                if (
                    request.target_id in {"voiceover_only", "sound_design"}
                    or AUDIO_LAYER_MUSIC not in layers
                ):
                    plan_args["force_music"] = False
                await push(
                    emit,
                    events,
                    await self.dispatcher.dispatch(
                        session_id, AGENT_TOOL_SET_PRODUCTION_PLAN, plan_args,
                        invoked_by="user",
                    ),
                )
            # The loop streams its own events live.
            events.extend(await self.loop.run(session_id, user_message=answer, emit=emit))
            return events

        if request.choice_type == "mix":
            # Structured mix adjustment from slider values (replaces free-text
            # "set the music to 80%"). Route to the multi-layer compose when a
            # voice-over has rendered, otherwise the cheap music-only re-mux.
            payload = dict(request.payload or {})
            layers = session.state_json.get("layers") or {}
            voiceover = layers.get("voiceover") or {}
            has_voiceover_audio = bool(voiceover.get("audio_url"))
            # SFX joins the master mix too: any completed variant makes compose
            # the right path (music-only stays on the cheap re-mux).
            sfx_layer = layers.get("sfx")
            has_sfx_audio = isinstance(sfx_layer, dict) and any(
                isinstance(v, dict) and v.get("status") == "completed" and v.get("audio_url")
                for v in (sfx_layer.get("variants") or [])
            )
            events = []
            await push(
                emit, events, [record_choice_event(self.repository, session_id, request)]
            )
            if has_voiceover_audio or has_sfx_audio:
                tool_name = AGENT_TOOL_COMPOSE_MIX
                tool_args = _tool_args_from_payload(AGENT_TOOL_COMPOSE_MIX, payload)
            else:
                tool_name = AGENT_TOOL_ADJUST_REMIX
                tool_args = _tool_args_from_payload(AGENT_TOOL_ADJUST_REMIX, payload)
            await push(
                emit, events, await self.dispatcher.dispatch(
                    session_id, tool_name, tool_args, invoked_by="user"
                )
            )
            return events

        if request.choice_type == "sculpt":
            # Move where this take sits inside its own longer track. Free: it
            # re-presents audio the session already paid for, so it carries no
            # approval and no spend fingerprint.
            events = []
            await push(
                emit, events, [record_choice_event(self.repository, session_id, request)]
            )
            tool_args = _tool_args_from_payload(
                AGENT_TOOL_SCULPT_AUDIO,
                request.payload,
                candidate_id=request.target_id or None,
            )
            await push(
                emit, events, await self.dispatcher.dispatch(
                    session_id, AGENT_TOOL_SCULPT_AUDIO, tool_args, invoked_by="user"
                )
            )
            return events

        if request.choice_type == "compare":
            # Side by side, on what the takes sound like rather than what they
            # were asked to be. Free, and answers the question a user with more
            # than one finished take actually has.
            events = []
            await push(
                emit, events, [record_choice_event(self.repository, session_id, request)]
            )
            tool_args = _tool_args_from_payload(
                AGENT_TOOL_COMPARE_TAKES, request.payload
            )
            await push(
                emit, events, await self.dispatcher.dispatch(
                    session_id, AGENT_TOOL_COMPARE_TAKES, tool_args, invoked_by="user"
                )
            )
            return events

        if request.choice_type == "variation":
            # Branch a new take from a candidate (a fresh regenerate by default).
            events = []
            await push(
                emit, events, [record_choice_event(self.repository, session_id, request)]
            )
            tool_args = _tool_args_from_payload(
                AGENT_TOOL_EDIT_AUDIO,
                request.payload,
                candidate_id=request.target_id,
                edit_kind=str(
                    (request.payload or {}).get("edit_kind")
                    or AGENT_EDIT_KIND_REGENERATE
                ),
            )
            await push(
                emit, events, await self.dispatcher.dispatch(
                    session_id, AGENT_TOOL_EDIT_AUDIO, tool_args, invoked_by="user"
                )
            )
            return events

        if request.choice_type == "voiceover":
            # (Re)draft the script (free) if the user edited it, then generate the
            # narration. propose_script satisfies generate_voiceover's script gate.
            payload = dict(request.payload or {})
            # A CLEARED script must not silently re-fire a paid TTS job off the
            # previously-stored script (that would spend without approval). If the
            # choice explicitly carries an empty script, reject before dispatch.
            if "script" in payload and not str(payload.get("script") or "").strip():
                raise ValueError("Write a script before generating the voice-over.")
            events = []
            await push(
                emit, events, [record_choice_event(self.repository, session_id, request)]
            )
            script = str(payload.get("script") or "").strip()
            has_segments = bool(payload.get("narration_segments"))
            # The card sends its textarea back on EVERY Generate click, edited
            # or not. Re-proposing an UNCHANGED script used to replace the timed
            # draft with a flat one — the agent's 3 placed segments and the
            # held-silent beat wiped by the very click meant to record them, and
            # the render came out as one continuous read parked at 0:00 (live,
            # twice, 2026-08-27). Only a real edit re-drafts.
            stored_vo = dict(
                (require_session(self.repository, session_id).state_json.get("layers") or {})
                .get("voiceover") or {}
            )
            script_unchanged = bool(script) and script == str(
                stored_vo.get("script") or ""
            ).strip()
            if has_segments or (script and not script_unchanged):
                propose_events, propose_result = await self.dispatcher.dispatch_collecting(
                    session_id,
                    AGENT_TOOL_PROPOSE_SCRIPT,
                    _tool_args_from_payload(AGENT_TOOL_PROPOSE_SCRIPT, payload),
                    invoked_by="user",
                )
                await push(emit, events, propose_events)
                # The script gate refused this plan, so the draft was never
                # stored. Falling through would fire a paid TTS job against the
                # PREVIOUS script and report a stale "draft a script first" —
                # tell the user what is actually wrong with the one they sent.
                if isinstance(propose_result, dict) and propose_result.get("error"):
                    raise ValueError(
                        str(propose_result.get("instruction")
                            or propose_result.get("error"))
                    )
            await push(
                emit,
                events,
                await self.dispatcher.dispatch(
                    session_id,
                    AGENT_TOOL_GENERATE_VOICEOVER,
                    _tool_args_from_payload(AGENT_TOOL_GENERATE_VOICEOVER, payload),
                    invoked_by="user",
                ),
            )
            return events

        if request.choice_type == "spotting":
            # The user taking a moment back. Ownership is the one decision that
            # governs every layer, so it has to be reachable directly rather than
            # only through whatever the agent infers from a sentence.
            payload = dict(request.payload or {})
            events = []
            await push(
                emit, events, [record_choice_event(self.repository, session_id, request)]
            )
            session_state = require_session(self.repository, session_id).state_json
            state = dict(session_state)
            sheet = dict(state.get("spotting_sheet") or {})
            moment_id = str(request.target_id or payload.get("moment_id") or "").strip()
            owner = str(payload.get("owner") or "").strip()
            if not sheet.get("moments"):
                raise ValueError("No spotting sheet yet — analyse the video first.")
            updated = set_moment_owner(
                sheet, moment_id, owner,
                source="user", reason=str(payload.get("reason") or "").strip(),
            )
            if updated is None:
                raise ValueError(
                    f"Cannot set moment {moment_id!r} to owner {owner!r}. "
                    f"Owners are: {', '.join(OWNERS)}."
                )
            state["spotting_sheet"] = sheet
            self.repository.update_session(session_id, state_json=state)
            await push(emit, events, [make_event(
                session_id,
                EventType.SPOTTING_SHEET,
                {"spotting_sheet": sheet, "changed": updated},
            )])
            return events

        if request.choice_type == "sfx":
            # Three shapes: (re)plan the events, select a variant, or generate.
            # A bare select carries `select_variant_id` and spends nothing.
            payload = dict(request.payload or {})
            events = []
            await push(
                emit, events, [record_choice_event(self.repository, session_id, request)]
            )
            # Ghost suggestions (free, never auto-committed): propose
            # beyond-visual ideas onto the plan, or act on one.
            suggest_kind = str(payload.get("sfx_suggest") or "").strip()
            if suggest_kind:
                await push(
                    emit,
                    events,
                    await self._propose_sfx_suggestions(
                        session_id,
                        kind=suggest_kind,
                        style=str(payload.get("style") or "cinematic"),
                    ),
                )
                return events
            suggestion_action = str(payload.get("suggestion_action") or "").strip()
            if suggestion_action:
                await push(
                    emit,
                    events,
                    self._act_on_sfx_suggestion(
                        session_id,
                        suggestion_id=str(payload.get("suggestion_id") or ""),
                        action=suggestion_action,
                    ),
                )
                return events
            select_id = str(payload.get("select_variant_id") or "").strip()
            if select_id and not payload.get("sfx_events"):
                await push(emit, events, self._select_sfx_variant(session_id, select_id))
                return events
            # Key-presence (not truthiness) decides re-planning: an explicitly
            # EMPTY sfx_events list is a plan edit ("I deleted every row"), and
            # must be validated as one — never skipped so that generate fires
            # against the stale previous plan the user just deleted.
            if "sfx_events" in payload or "sfx_ambience" in payload:
                plan_args = _tool_args_from_payload(AGENT_TOOL_PLAN_SFX, payload)
                # Explicit events from the plan editor ARE the user's treatment
                # (the strongest possible statement of intent) — open the
                # treatment gate inline when the card hasn't been answered yet.
                session_state = require_session(self.repository, session_id).state_json
                if not session_state.get("sfx_treatment") and not plan_args.get("treatment"):
                    plan_args["treatment"] = {
                        "label": "Direct plan edit",
                        "notes": "events submitted through the plan editor",
                    }
                await push(
                    emit,
                    events,
                    await self.dispatcher.dispatch(
                        session_id, AGENT_TOOL_PLAN_SFX, plan_args, invoked_by="user"
                    ),
                )
            if payload.get("plan_only"):
                # Plan-editor save: record the edited plan WITHOUT spending.
                # Generation stays behind the card's explicit Generate button —
                # fixing a typo in a row must never render (and bill) a take.
                return events
            await push(
                emit,
                events,
                await self.dispatcher.dispatch(
                    session_id,
                    AGENT_TOOL_GENERATE_SFX,
                    # Redoing named hits keeps every other effect in the bed;
                    # an empty payload still means "make the whole thing".
                    _tool_args_from_payload(AGENT_TOOL_GENERATE_SFX, payload),
                    invoked_by="user",
                ),
            )
            return events

        raise ValueError(f"Unsupported choice_type: {request.choice_type}")

    def _sfx_layer_or_error(self, session_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """(state, layers, sfx) with a crafted error when no plan exists yet."""
        session = require_session(self.repository, session_id)
        state = dict(session.state_json)
        layers = dict(state.get("layers") or {})
        sfx = layers.get("sfx")
        if not isinstance(sfx, dict) or not (sfx.get("events") or sfx.get("ambience")):
            raise ValueError("Plan the sound effects first — then I can suggest more ideas.")
        return state, layers, dict(sfx)

    async def _propose_sfx_suggestions(
        self, session_id: str, *, kind: str, style: str
    ) -> list[dict[str, Any]]:
        """Free: run the hybrid-loop proposers over this session's plan and
        store the results as pending ghost rows."""
        from ..tools.sfx_suggest import propose_console_suggestions

        state, layers, sfx = self._sfx_layer_or_error(session_id)
        session = require_session(self.repository, session_id)
        source_path = None
        try:
            artifact = self.tools.get_source_video_artifact(session.source_video_artifact_id)
            local = getattr(artifact, "local_path", None)
            source_path = local if local else None
        except Exception:  # noqa: BLE001 - the scenes fallback covers it
            source_path = None
        direction = str((state.get("memory") or {}).get("creative_direction") or "")
        fresh = await propose_console_suggestions(
            kind=kind,
            state=state,
            source_video_path=source_path,
            direction=direction,
            style=style,
        )
        stored = list(sfx.get("suggestions") or [])
        stored.extend(fresh)
        sfx["suggestions"] = stored
        layers["sfx"] = sfx
        state["layers"] = layers
        self.repository.update_session(session_id, state_json=state)
        return [make_event(session_id, EventType.SFX_PLAN, sfx)]

    def _act_on_sfx_suggestion(
        self, session_id: str, *, suggestion_id: str, action: str
    ) -> list[dict[str, Any]]:
        """Free: accept folds the ghost into the committed plan; reject keeps
        it from ever being re-proposed. Both are explicit user calls."""
        if action not in ("accept", "reject"):
            raise ValueError(f"Unsupported suggestion action: {action} (accept|reject)")
        state, layers, sfx = self._sfx_layer_or_error(session_id)
        stored = list(sfx.get("suggestions") or [])
        hit = next((s for s in stored if s.get("suggestion_id") == suggestion_id), None)
        if hit is None:
            raise ValueError(f"Suggestion not found: {suggestion_id}")
        if action == "accept" and hit.get("status") != "accepted":
            events_plan = list(sfx.get("events") or [])
            events_plan.append(_suggestion_as_plan_row(hit, position=len(events_plan) + 1))
            events_plan.sort(key=lambda e: float(e.get("start_s") or 0.0))
            sfx["events"] = events_plan
            sfx["over_budget"] = len(events_plan) > int(sfx.get("density_cap") or 3)
        hit["status"] = "accepted" if action == "accept" else "rejected"
        sfx["suggestions"] = stored
        layers["sfx"] = sfx
        state["layers"] = layers
        self.repository.update_session(session_id, state_json=state)
        return [make_event(session_id, EventType.SFX_PLAN, sfx)]

    def _select_sfx_variant(self, session_id: str, variant_id: str) -> list[dict[str, Any]]:
        """In-process, free: point the SFX layer at a chosen variant."""
        session = require_session(self.repository, session_id)
        state = dict(session.state_json)
        layers = dict(state.get("layers") or {})
        sfx = layers.get("sfx")
        if not isinstance(sfx, dict):
            return []
        sfx = dict(sfx)
        exists = any(v.get("variant_id") == variant_id for v in (sfx.get("variants") or []))
        if not exists:
            return []
        sfx["selected_variant_id"] = variant_id
        layers["sfx"] = sfx
        state["layers"] = layers
        self.repository.update_session(session_id, state_json=state)
        return [make_event(session_id, EventType.SFX_GENERATING, sfx)]

    #: A session hydrating without settling for this long is not converging.
    #: The poll is every 2.5s, so this is roughly a minute of a session being
    #: rewritten on every tick.
    _HYDRATION_CHURN_WRITES = 24

    def _note_hydration(self, session_id: str, *, wrote: bool) -> None:
        """Count CONSECUTIVE writes the poll makes, and say so when they stop stopping.

        Hydration is supposed to converge: it re-derives a projection, and once
        the underlying jobs are finished the result stops changing, the equality
        guard sees no difference, and nothing is written. A value that is not
        byte-stable — an unrounded float, a timestamp, a set iterated in a new
        order — breaks that and the session is rewritten forever, quietly, at
        the cost of a database write every 2.5 seconds.

        That failure has no symptom a user could report and no error anywhere.
        The last one was found by reading the code. This is the cheapest
        possible tripwire: it does not fix anything, it just refuses to let the
        next one be silent.
        """

        counts = getattr(self, "_hydration_writes", None)
        if counts is None:
            counts = {}
            self._hydration_writes = counts
        if not wrote:
            # It settled. That is what convergence looks like, and it is the
            # normal case: a poll that finds nothing new writes nothing.
            counts.pop(session_id, None)
            return
        counts[session_id] = counts.get(session_id, 0) + 1
        if counts[session_id] == self._HYDRATION_CHURN_WRITES:
            logger.warning(
                "agentic audio: session %s has been rewritten by hydration %d "
                "times without settling — something derived on the poll is not "
                "byte-stable and the session is churning",
                session_id,
                self._HYDRATION_CHURN_WRITES,
            )

    def refresh_session_state(self, session_id: str) -> AgenticAudioSession:
        """Fold finished job results into the session's state.

        This runs on the 2.5-second poll, OUTSIDE the per-session turn lock, so
        it races every turn on a session that is generating something. It used
        to read the whole state document, hydrate a branch of it, and write the
        whole thing back — which meant "your take finished" and whatever the
        agent recorded in the meantime were two writers overwriting each other,
        last one winning, with no error anywhere.

        The hydration now happens inside a locked read-modify-write, so the
        document it edits is the current one.
        """

        def hydrate(state: dict[str, Any]) -> Optional[dict[str, Any]]:
            changed = False
            candidates = self.tools.hydrate_candidate_results(
                candidates=list(state.get("candidates") or []),
                observation=state.get("observation") or {},
            )
            if candidates != state.get("candidates"):
                state["candidates"] = candidates
                changed = True
            layers = dict(state.get("layers") or {})
            voiceover = layers.get("voiceover")
            if voiceover and voiceover.get("linked_job_id"):
                hydrated = self.tools.hydrate_voiceover_layer(
                    voiceover=voiceover, observation=state.get("observation") or {},
                )
                if hydrated != voiceover:
                    layers["voiceover"] = hydrated
                    state["layers"] = layers
                    changed = True
            sfx = layers.get("sfx")
            if isinstance(sfx, dict) and any(
                v.get("linked_job_id") for v in (sfx.get("variants") or [])
            ):
                hydrated_sfx = self.tools.hydrate_sfx_layer(sfx=sfx)
                if hydrated_sfx != sfx:
                    layers["sfx"] = hydrated_sfx
                    state["layers"] = layers
                    changed = True
            # None means "nothing had finished" — the common case on a poll, and
            # it must not cost a write or a version bump.
            self._note_hydration(session_id, wrote=changed)
            return state if changed else None

        mutate = getattr(self.repository, "mutate_session_state", None)
        if mutate is None:
            # An in-memory repository has no transactions to hold and no
            # concurrency to lose an update to.
            session = require_session(self.repository, session_id)
            updated = hydrate(dict(session.state_json))
            if updated is not None:
                session = self.repository.update_session(session_id, state_json=updated)
            return session
        return mutate(session_id, hydrate)


__all__ = [
    "AgenticAudioAgent",
    "ApprovalRequiredError",
    "build_agentic_audio_agent_client",
    "SYSTEM_PROMPT",
]
