"""The reasoning loop: one bounded LLM turn.

Build context -> get one structured decision -> stream the reasoning beat ->
dispatch the action (tool / propose / clarify / ask / noop) -> on a heavy tool or
a terminal action, end the turn -> persist turn + durable memory. The orchestrator
(:class:`AgenticAudioAgent`) owns entrypoints and deterministic choices and calls
into this loop for every model-driven turn. All tool execution goes through the
shared :class:`ToolDispatcher`; all phase writes go through :mod:`stages`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Optional

from EdennCode.Deployment.error_codes import scrub_provider_names

from .dispatcher import DuplicateGeneration, ToolDispatcher
from ..events import EventType
from ..models import (
    usable_music_modelspec,
    AGENT_ACTION_CALL_TOOL,
    AGENT_ACTION_CLARIFY,
    AGENT_ACTION_NOOP,
    AGENT_ACTION_PROPOSE,
    AGENT_DECISION_SCHEMA,
    AGENT_HEAVY_TOOLS,
    AGENT_INTENT_OTHER,
    AGENT_MEMORY_RECENT_TURNS,
    AgenticAudioSession,
    AgenticAudioWebSocketEvent,
    AgenticMessageRole,
    AgenticToolStatus,
    MusicProposalCard,
)
from .prompts import PROMPT_HASH, SYSTEM_PROMPT
from ..persistence.repositories import AgenticAudioRepository
from .session_io import (
    append_message,
    make_event,
    push,
    reasoning_event,
    require_session,
)
from ..stages import Stage, transition
from ..tools.base import ApprovalRequiredError
from ..tools.media import comparison_still_describes, normalize_music_modelspec

logger = logging.getLogger(__name__)

EventSink = Callable[[AgenticAudioWebSocketEvent], Awaitable[None]]

SCRATCH_BUDGET_CHARS = 4000
_SCRATCH_MAX_ITEMS = 5
_SCRATCH_MAX_STR = 400


def _prune_for_scratch(value: Any, *, depth: int = 0) -> Any:
    """Shrink a tool result by structure: drop URL values, cap arrays and strings."""

    if depth > 6:
        return "…"
    if isinstance(value, dict):
        pruned: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if isinstance(item, str) and item and (
                "url" in name.lower() or item.startswith(("http://", "https://", "/dev/media"))
            ):
                pruned[name] = "<url>"
                continue
            pruned[name] = _prune_for_scratch(item, depth=depth + 1)
        return pruned
    if isinstance(value, (list, tuple)):
        items = list(value)
        kept: list[Any] = [
            _prune_for_scratch(item, depth=depth + 1) for item in items[:_SCRATCH_MAX_ITEMS]
        ]
        if len(items) > _SCRATCH_MAX_ITEMS:
            kept.append(f"…+{len(items) - _SCRATCH_MAX_ITEMS} more")
        return kept
    if isinstance(value, str) and len(value) > _SCRATCH_MAX_STR:
        return value[:_SCRATCH_MAX_STR] + "…"
    return value


def compact_tool_result(tool_result: Any, *, budget: int = SCRATCH_BUDGET_CHARS) -> str:
    """Serialize a tool result for the scratchpad: always parseable, always bounded.

    Slicing serialized JSON (the previous approach) cuts mid-string and hands the
    model broken syntax exactly when a result is interesting enough to be long.
    Prune the structure first, and if it still doesn't fit, say so truthfully
    rather than emitting a corrupt fragment.
    """

    # Prune only on overflow. Pruning unconditionally would clip a plan's events
    # or a script's text that fit the budget perfectly well — and for a tool
    # whose output the state summary does not re-serve, the scratch copy is the
    # model's only view of what it just did.
    verbatim = json.dumps(tool_result, default=str)
    if len(verbatim) <= budget:
        return verbatim

    pruned = _prune_for_scratch(tool_result)
    text = json.dumps(pruned, default=str)
    if len(text) <= budget:
        return text
    if isinstance(pruned, dict):
        squeezed = {
            key: (f"[{len(val)} items]" if isinstance(val, list) else val)
            for key, val in pruned.items()
        }
        text = json.dumps(squeezed, default=str)
        if len(text) <= budget:
            return text
        keys = ", ".join(sorted(str(key) for key in squeezed))
        return _summary_json(f"result too large to show; keys: {keys}", budget)
    return _summary_json(f"result too large to show ({len(text)} chars)", budget)


#: Fields of a candidate that are cheap, stable and useful on every step.
_CANDIDATE_DIGEST_FIELDS: tuple[str, ...] = (
    "candidate_id",
    "title",
    "status",
    "audio_url",
    "video_url",
    "remixed_video_url",
    "music_volume",
    "version",
    "parent_candidate_id",
    "edit_kind",
    "extend_mode",
)


def _candidate_digest(candidate: dict[str, Any]) -> dict[str, Any]:
    """One take, as the model sees it on every step of every turn.

    Deliberately thin: this summary is rebuilt and re-sent for each reasoning
    step, so anything included is paid for many times over. Two things earn
    their place beyond the identifiers:

    * ``listen_notes`` — the COUNT of faults, enough to notice a take has a
      problem worth calling ``compare_takes`` about, without swamping the
      summary with every report in the session.
    * the window — where this cut sits inside its own longer track, and how
      long that track is. Without these the agent is asked to move a take to a
      better moment while being shown no coordinates to move it with, so its
      only honest option is to guess.
    """

    digest: dict[str, Any] = {
        key: candidate.get(key) for key in _CANDIDATE_DIGEST_FIELDS
    }

    report = candidate.get("listen_report") or {}
    if report:
        digest["listen_notes"] = len(report.get("notes") or [])

    # The section map, not the beat grid: a few labelled stretches are what
    # lets the agent say "the drop lands at 0:14", while hundreds of beat
    # timestamps would swamp a summary that rides every step. The grid stays on
    # the candidate for snapping, which happens in the tool, not in the prompt.
    structure = candidate.get("music_structure") or {}
    if structure.get("sections"):
        digest["sections"] = [
            {"label": s.get("label"), "start_s": s.get("start_s")}
            for s in structure["sections"][:8]
        ]
    if structure.get("tempo_bpm"):
        digest["tempo_bpm"] = structure["tempo_bpm"]

    window = candidate.get("window") or {}
    if window:
        digest["window_start_s"] = window.get("start_s")
        full_track_s = window.get("full_duration_s") or (
            report.get("measured") or {}
        ).get("full_duration_s")
        if full_track_s is not None:
            digest["full_track_s"] = full_track_s
    return digest


#: How many recent messages always survive the window. Comfortably more than
#: the turn history the state summary carries, so the model never sees a
#: summarised turn whose messages it cannot also read.
TRANSCRIPT_WINDOW_MESSAGES = 40

#: ...and a character ceiling for the same window, because message LENGTH is
#: unbounded even when the count is not. Sized for the worst case rather than
#: the average: there is no tokenizer in this stack, and CJK text runs close to
#: one token per character, so this ceiling is roughly its own token count in
#: the languages where that matters and generous everywhere else.
TRANSCRIPT_WINDOW_CHARS = 24_000


def _window_transcript(messages: list[Any]) -> tuple[list[Any], int]:
    """The tail of the conversation that rides every step, and what was left out.

    Returns ``(kept, elided_count)``. Walks backwards so the most recent
    exchange is never the thing dropped, and stops on whichever limit binds
    first. A short session — which is every session today — comes back
    completely unchanged, so the behaviour this alters is only the behaviour of
    a session long enough to have a problem.
    """

    kept: list[Any] = []
    budget = TRANSCRIPT_WINDOW_CHARS
    for message in reversed(messages):
        if len(kept) >= TRANSCRIPT_WINDOW_MESSAGES:
            break
        budget -= len(str(getattr(message, "content", "") or ""))
        if budget < 0 and kept:
            # Always keep at least one: a single enormous message is still the
            # thing the user just said.
            break
        kept.append(message)
    kept.reverse()
    return kept, len(messages) - len(kept)


def _accumulate_usage(
    total: dict[str, int], usage: Optional[dict[str, Any]]
) -> dict[str, int]:
    """Add one decision call's token cost to the turn's running total.

    The client has always returned this and the loop has always dropped it on
    the floor — which is why two deferred structural decisions, whether the
    decision schema should carry per-tool argument shapes and whether the
    four-step budget actually binds, are both still waiting on "measure the
    cost first". Four calls a turn, every turn, and nothing was counting.

    Integers only, and only three of them: this rides in the turn record, which
    is capped and re-served to the model.
    """

    if not isinstance(usage, dict):
        return total
    # A call happened either way, and how many round-trips a turn takes is worth
    # knowing on its own.
    total["calls"] = total.get("calls", 0) + 1
    for source, key in (
        ("prompt_tokens", "prompt"),
        ("completion_tokens", "completion"),
    ):
        raw = usage.get(source)
        if raw is None:
            # Not reported is not zero, and recording it as zero would make an
            # unmeasured turn look free.
            continue
        try:
            total[key] = total.get(key, 0) + int(raw)
        except (TypeError, ValueError):
            continue
    return total


def _resume_context(
    scratch: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    *,
    budget: int = 1200,
) -> dict[str, Any]:
    """What the next turn needs in order to genuinely pick up where this left off.

    A turn that runs out of steps promises exactly that, and nothing made it
    true: the scratchpad dies with the turn, and the recent-turns projection
    carries neither the actions nor the fact that the turn was cut short, so the
    next turn could not tell it had been interrupted — let alone where. "Keep
    going" restarted from durable state and re-derived a half-finished plan,
    which in a session where the next step is often a billed generation is a
    spend question, not a tidiness one.

    Deliberately small, and it rides the prompt only on the turn after a
    truncation. Tool output is scrubbed on the way in: the scratchpad holds raw
    results, and this is about to become durable session state.
    """

    tail = ""
    for entry in reversed(scratch):
        content = str(entry.get("content") or "")
        if content.startswith("[tool_result") or content.startswith("[tool_error"):
            tail = scrub_provider_names(content)[:budget]
            break

    context: dict[str, Any] = {
        "reason": "step_budget_exhausted",
        # What the turn DID. The marker recording that it stopped is already the
        # reason above, and repeating it here reads as work performed.
        "actions": [
            dict(action)
            for action in actions
            if action.get("action") and action.get("action") != "budget_exhausted"
        ][-4:],
    }
    if tail:
        context["last_tool_result"] = tail
    return context


def _summary_json(message: str, budget: int) -> str:
    """A truthful stand-in that stays inside the budget, JSON wrapper included."""

    room = max(0, budget - len(json.dumps({"_summary": ""})))
    text = json.dumps({"_summary": message[:room]})
    while len(text) > budget and room > 0:
        room = max(0, room - max(1, len(text) - budget))
        text = json.dumps({"_summary": message[:room]})
    return text


#: Stable names for the ways a tool call fails that are not the model's fault.
#: They are a vocabulary for humans and for support, not an error taxonomy for
#: the model: the user sees the sentence, the turn record keeps the code.
_FAILURE_MESSAGE = {
    "provider_unavailable": (
        "The service that makes this didn't answer just now, so nothing was "
        "produced. Nothing has been lost — try again in a moment."
    ),
    "render_failed": (
        "Something went wrong while putting the audio together, so I don't "
        "have a result to show you. Worth trying again."
    ),
    "storage_unavailable": (
        "I couldn't reach the place the finished file is kept, so I've stopped "
        "rather than hand you something I can't store. Try again shortly."
    ),
    "client_gone": (
        "The connection dropped while that was running. Anything already "
        "started kept going — reopen the session to see where it got to."
    ),
    "tool_failed": (
        "That didn't work and I'd rather say so than guess at why. Nothing was "
        "delivered. Try again, or ask me for something different."
    ),
}


def _failure_code(exc: BaseException) -> str:
    """Classify a tool failure into one of a handful of stable names.

    Deliberately shallow. The point is not a precise taxonomy — it is that the
    turn record says something a human can act on, and that the customer gets a
    sentence rather than a stack trace's worth of nothing.
    """

    # Match on the type name AND the message: the useful signal is in one or
    # the other depending on how far from the provider the failure was raised.
    haystack = f"{type(exc).__name__} {exc}".lower()
    for code, markers in (
        ("client_gone", ("socketgone", "disconnect", "clientgone", "connectionreset")),
        ("render_failed", ("calledprocess", "ffmpeg", "ffprobe")),
        ("storage_unavailable", ("pool", "operationalerror", "database", "blob", "storage")),
        ("provider_unavailable", ("timeout", "timedout", "connection", "unavailable",
                                  "502", "503", "504")),
    ):
        if any(marker in haystack for marker in markers):
            return code
    return "tool_failed"


class ReasoningLoop:
    """Runs the bounded per-turn agent loop against the LLM + tool dispatcher."""

    def __init__(
        self,
        *,
        repository: AgenticAudioRepository,
        llm_client: Any,
        dispatcher: ToolDispatcher,
        max_steps_per_turn: int,
    ) -> None:
        self.repository = repository
        self.llm_client = llm_client
        self.dispatcher = dispatcher
        self.max_steps_per_turn = int(max_steps_per_turn)

    async def run(
        self,
        session_id: str,
        *,
        user_message: Optional[str] = None,
        emit: Optional[EventSink] = None,
        message_payload: Optional[dict[str, Any]] = None,
    ) -> list[AgenticAudioWebSocketEvent]:
        events: list[AgenticAudioWebSocketEvent] = []
        # Tag every assistant message this turn produces with its origin (e.g.
        # source="comment" for an @agent comment pickup), so the frontend can keep
        # comment-thread turns out of the MAIN chat and render them only in the
        # thread — otherwise a comment turn's reply interleaves into the chat
        # transcript, scrambling the visible turn order.
        def _assistant(content: str) -> list[AgenticAudioWebSocketEvent]:
            return append_message(
                self.repository, session_id, AgenticMessageRole.ASSISTANT, content,
                payload_json=message_payload or {},
            )
        scratch: list[dict[str, Any]] = []
        turn_intent: Optional[str] = None
        turn_actions: list[dict[str, Any]] = []
        memory_updates: list[dict[str, Any]] = []
        turn_clarification: Optional[dict[str, Any]] = None
        # Step accounting. Skips (redundancy guards, a retried unusable decision)
        # consume budget while recording no action, so a turn record built only
        # from `actions` under-counts what the turn actually cost.
        steps_used = 0
        # Skips consume budget while recording no action, and they do it for
        # four quite different reasons. One number could not tell them apart, so
        # "how often does the model emit something unusable?" — the question
        # that decides whether the decision shape needs changing at all — had no
        # answer anywhere in the system.
        skips_by_cause: dict[str, int] = {}

        def _charge_skip(cause: str) -> None:
            """Charge one consumed step to a named cause."""

            skips_by_cause[cause] = skips_by_cause.get(cause, 0) + 1

        token_usage: dict[str, int] = {}
        resume_context: Optional[dict[str, Any]] = None
        degenerate_retried = False
        budget_exhausted = False
        # Liveness: each step's reasoning beat is part of the model's OWN output,
        # so it can only land after that step's LLM call returns (seconds). Emit a
        # deterministic beat immediately so the thinking trail has content WHILE
        # the first step runs, not only after it.
        if user_message:
            await push(
                emit,
                events,
                [
                    make_event(
                        session_id,
                        EventType.AGENT_REASONING,
                        {
                            "status": "Reading your direction",
                            "intent": None,
                            "thought": f"“{str(user_message)[:160]}” — working out the right next step.",
                        },
                    )
                ],
            )
        for _ in range(self.max_steps_per_turn):
            steps_used += 1
            session = require_session(self.repository, session_id)
            messages = self._build_messages(session, scratch)
            try:
                decision, usage = await self.llm_client.complete_messages(
                    messages, json_schema=AGENT_DECISION_SCHEMA
                )
                _accumulate_usage(token_usage, usage)
            except Exception as exc:  # noqa: BLE001 - surface as assistant message
                logger.exception("Agentic audio decision call failed: %s", exc)
                await push(
                    emit,
                    events,
                    _assistant("I hit an error while planning. Please try again."),
                )
                break

            # A "successful" call can still carry nothing usable: the client hands
            # back {} when the model returned no content and {"_raw": ...} when the
            # JSON did not parse, and json.loads can yield a non-dict (a bare
            # string or list) that would crash the .get() calls below. All three
            # used to end the turn in total silence — no reply at all, which reads
            # as a dead product rather than a limited one. Retry the step once with
            # a corrective nudge, then apologise the way a hard failure does.
            if not isinstance(decision, dict) or not decision or set(decision) == {"_raw"}:
                if not degenerate_retried:
                    degenerate_retried = True
                    _charge_skip("unusable_decision")
                    logger.warning(
                        "agentic audio: unusable decision for session %s — retrying once",
                        session_id,
                    )
                    scratch.append(
                        {
                            "role": "user",
                            "content": (
                                "[system] Your last reply was empty or was not a valid "
                                "JSON decision. Reply again with exactly ONE decision "
                                "object matching the schema."
                            ),
                        }
                    )
                    continue
                logger.error(
                    "agentic audio: unusable decision twice for session %s — ending turn",
                    session_id,
                )
                await push(
                    emit,
                    events,
                    _assistant("I hit an error while planning. Please try again."),
                )
                break

            # Intent detection + durable memory are captured per step; the first
            # classified intent of the turn labels the turn.
            step_intent = str(decision.get("intent") or "").strip()
            if step_intent and turn_intent is None:
                turn_intent = step_intent
            step_memory = decision.get("memory_update")
            if isinstance(step_memory, dict) and step_memory:
                memory_updates.append(step_memory)

            # NOTE: we deliberately do NOT auto-unlock generation from a free-text
            # approval-intent turn. Approval must come from the deterministic
            # /choices pick or an explicit approve_direction tool call; otherwise
            # the cost gate inside the tool re-asks instead of spending.

            assistant_message = str(decision.get("assistant_message") or "").strip()
            # The schema is advisory (strict is off), so the model can hand back a
            # legal-looking decision whose nested shapes are wrong —
            # {"action": "call_tool"} as a bare string is the common one. dict()
            # on that raises, and the raw ValueError text became the user's error
            # message. Treat a wrong-shaped part as absent and let the turn go on.
            raw_action = decision.get("action")
            action = raw_action if isinstance(raw_action, dict) else {}
            action_type = str(action.get("type") or AGENT_ACTION_NOOP)

            # The modality is captured up front by the intent gate. If the model
            # tries to re-ask it (production_plan already set), skip the whole step.
            if action_type == AGENT_ACTION_CLARIFY and self._is_redundant_modality_clarify(
                session, self._normalize_clarification(action.get("clarification"))
            ):
                scratch.append(
                    {
                        "role": "user",
                        "content": (
                            "[system] The modality is already chosen (production_plan "
                            "is set). Do NOT ask about modality/layers again — propose "
                            "music directions now."
                        ),
                    }
                )
                _charge_skip("redundant_modality_clarify")
                continue

            # The treatment answer is durable too: once state.sfx_treatment is
            # set, re-asking the card is a bug, not diligence.
            if action_type == AGENT_ACTION_CLARIFY and self._is_redundant_sfx_treatment_clarify(
                session, self._normalize_clarification(action.get("clarification"))
            ):
                scratch.append(
                    {
                        "role": "user",
                        "content": (
                            "[system] The SFX treatment is already chosen "
                            "(state.sfx_treatment). Do NOT re-ask — call plan_sfx "
                            "within that treatment now."
                        ),
                    }
                )
                _charge_skip("redundant_sfx_treatment_clarify")
                continue

            # Surface the agent's intermediate thinking so the UI can show a live
            # status (one live beat per step) before the action's effect lands.
            await push(
                emit,
                events,
                [
                    reasoning_event(
                        session_id,
                        intent=step_intent,
                        action_type=action_type,
                        tool_name=str(action.get("tool_name") or ""),
                        thought=str(decision.get("thought") or ""),
                    )
                ],
            )

            if assistant_message:
                await push(emit, events, _assistant(assistant_message))

            if action_type == AGENT_ACTION_CLARIFY:
                turn_clarification = self._normalize_clarification(
                    action.get("clarification")
                )
                turn_actions.append({"action": AGENT_ACTION_CLARIFY})
                await push(
                    emit,
                    events,
                    [make_event(session_id, EventType.CLARIFY_CARDS, turn_clarification)],
                )
                break

            if action_type == AGENT_ACTION_CALL_TOOL:
                tool_name = str(action.get("tool_name") or "")
                raw_args = action.get("tool_args")
                tool_args = raw_args if isinstance(raw_args, dict) else {}
                if not tool_name:
                    # "" is in the schema's tool_name enum, so this is a legal
                    # decision that would otherwise reach the registry and raise.
                    scratch.append(
                        {
                            "role": "user",
                            "content": (
                                "[system] You chose call_tool without naming a tool. "
                                "Name one of the available tools, or choose a "
                                "different action."
                            ),
                        }
                    )
                    _charge_skip("unnamed_tool")
                    continue
                turn_actions.append({"action": "call_tool", "tool_name": tool_name})
                try:
                    tool_events, tool_result = await self.dispatcher.dispatch_collecting(
                        session_id, tool_name, tool_args
                    )
                except ApprovalRequiredError as exc:
                    # (A) Degrade safely: the model tried to spend before approval
                    # was on record. Don't 400 — surface the gate's own words and
                    # wait. The blocked tool did NOT run, so record it as an
                    # approval prompt (not a call). The tool's message matters:
                    # some gates unlock on a chat "yes", others only on a card
                    # button, and a canned "say the word" promises the wrong one.
                    turn_actions[-1] = {"action": "approval_prompt", "blocked_tool": tool_name}
                    gate_message = (getattr(exc, "user_message", None) or "").strip() or (
                        "Just to confirm before I spend anything — want me to go "
                        "ahead and generate this? Say the word and I'll start."
                    )
                    await push(emit, events, _assistant(gate_message))
                    break
                except DuplicateGeneration as exc:
                    # (B) The spend guard refused a repeat of a generation that is
                    # already running. Nothing was spent and nothing needs to be —
                    # say so in-chat instead of tearing the turn down as a 400,
                    # which also lost the turn record after events had streamed.
                    # Loop-only: the deterministic /choices path keeps its 400,
                    # where a duplicate click IS a request-level error.
                    turn_actions[-1] = {
                        "action": "duplicate_refused",
                        "blocked_tool": tool_name,
                    }
                    await push(
                        emit,
                        events,
                        _assistant(
                            scrub_provider_names(str(exc))
                            or "That generation is already running — I won't start it twice."
                        ),
                    )
                    break
                except ValueError as exc:
                    # (C) The tool rejected the model's ARGUMENTS — an unknown
                    # tool name, a script that was empty, a volume that wasn't a
                    # number. These were the most reachable version of the silent
                    # turn: a plain ValueError escaped the loop, the turn record
                    # was never written, and the user got a 400 whose body was a
                    # sentence written for the model. Hand it back as a tool
                    # result instead — repairing a bad argument on the next step
                    # is exactly what the step budget is for.
                    detail = scrub_provider_names(str(exc)) or "the arguments were rejected"
                    repair = getattr(exc, "instruction", "")
                    if repair:
                        detail = f"{detail} {scrub_provider_names(str(repair))}"
                    logger.warning(
                        "agentic audio: %s rejected its arguments in session %s: %s",
                        tool_name, session_id, detail,
                    )
                    turn_actions[-1] = {"action": "tool_error", "blocked_tool": tool_name}
                    scratch.append(
                        {
                            "role": "user",
                            "content": (
                                f"[tool_error {tool_name}] {detail} — fix the arguments "
                                "and try again, or choose a different action."
                            ),
                        }
                    )
                    continue
                except Exception as exc:  # noqa: BLE001
                    # (D) Everything else: the provider was down, ffmpeg failed,
                    # the pool was exhausted, the socket died mid-render. These
                    # are the failures production actually has, and they were
                    # the only ones with no handler — so they escaped the loop
                    # entirely, and with them went the turn record, the token
                    # usage, and any resume context. The user got a bare 500
                    # while the spend guard kept a fingerprint recorded before
                    # the tool ran, so their immediate retry was refused as a
                    # duplicate of an attempt that had already failed.
                    code = _failure_code(exc)
                    logger.exception(
                        "agentic audio: %s failed in session %s (%s)",
                        tool_name, session_id, code,
                    )
                    turn_actions[-1] = {
                        "action": "tool_error",
                        "blocked_tool": tool_name,
                        "error_code": code,
                    }
                    await push(emit, events, _assistant(_FAILURE_MESSAGE[code]))
                    break
                await push(emit, events, tool_events)
                if tool_name in AGENT_HEAVY_TOOLS:
                    break  # async job queued; result hydrates later
                scratch.append(
                    {
                        "role": "user",
                        "content": (
                            f"[tool_result {tool_name}] {compact_tool_result(tool_result)}"
                        ),
                    }
                )
                continue

            if action_type == AGENT_ACTION_PROPOSE:
                turn_actions.append({"action": AGENT_ACTION_PROPOSE})
                await push(
                    emit,
                    events,
                    self._store_proposals(
                        session_id,
                        raw_proposals
                        if isinstance(raw_proposals := action.get("proposals"), list)
                        else [],
                    ),
                )
                break

            # ask / noop / unknown -> end the turn, await the user.
            turn_actions.append({"action": action_type})
            break
        else:
            # The step budget ran out mid-plan. Falling silent here is the same
            # dead-product silence as an unusable decision: the user asked for
            # something and got a thinking trail that simply stops. Say what
            # happened so continuing is one message away.
            budget_exhausted = True
            logger.info(
                "agentic audio: step budget (%d) exhausted for session %s",
                self.max_steps_per_turn,
                session_id,
            )
            turn_actions.append({"action": "budget_exhausted"})
            resume_context = _resume_context(scratch, turn_actions)
            await push(
                emit,
                events,
                _assistant(
                    "I've used all my planning steps for this turn. Tell me to keep "
                    "going and I'll pick up right where I left off."
                ),
            )

        # Record the turn + fold durable memory once per user-driven turn.
        if user_message is not None:
            self._record_turn_and_memory(
                session_id=session_id,
                user_message=user_message,
                intent=turn_intent or AGENT_INTENT_OTHER,
                actions=turn_actions,
                memory_updates=memory_updates,
                clarification=turn_clarification,
                steps_used=steps_used,
                skips_by_cause=skips_by_cause,
                budget_exhausted=budget_exhausted,
                token_usage=token_usage,
                resume_context=resume_context,
            )
        return events

    # ----- context construction ---------------------------------------------

    def _build_messages(
        self, session: AgenticAudioSession, scratch: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.append(
            {
                "role": "system",
                "content": "Current session state:\n"
                + json.dumps(self._state_summary(session), default=str),
            }
        )
        # Window the transcript HERE, never in the repository: build_snapshot and
        # the UI transcript both depend on list_messages returning everything,
        # and a LIMIT down there would quietly truncate what the user can read.
        #
        # A session never ends by design, so replaying every message on every
        # step of every turn grows linearly and is paid up to four times a turn.
        # The compressed forms already exist and already ride along — durable
        # memory, the last turns, the whole session state — so the older
        # messages are not lost, only re-served in a smaller shape.
        conversation = [
            message
            for message in self.repository.list_messages(session.session_id)
            if message.role in {AgenticMessageRole.USER, AgenticMessageRole.ASSISTANT}
        ]
        kept, elided = _window_transcript(conversation)
        if elided:
            messages.append({
                "role": "system",
                "content": (
                    f"[{elided} earlier message(s) in this session are not shown. "
                    "What was decided in them is in the session state and memory "
                    "above; ask the user rather than guessing at specifics.]"
                ),
            })
        for message in kept:
            content = message.content
            # Canvas mode attaches the @-referenced version as payload context_refs.
            # Render it into the LLM's view of the turn (the persisted message stays
            # clean) so an instruction like "make the drop harder" unambiguously
            # targets the referenced take instead of forcing a "which version?" ask.
            if message.role == AgenticMessageRole.USER:
                note = self._context_ref_note(session, message.payload_json)
                if note:
                    content = f"{content}\n{note}"
            messages.append({"role": message.role, "content": content})
        messages.extend(scratch)
        return messages

    def _context_ref_note(
        self, session: AgenticAudioSession, payload_json: Optional[dict[str, Any]]
    ) -> str:
        """Render a user message's canvas @-reference for the model's context."""
        # Client-controlled: dedupe and cap so a hostile payload can't bloat the
        # prompt (matches this file's bounding conventions, e.g. [:1000] turns).
        refs = list(dict.fromkeys(str(r) for r in ((payload_json or {}).get("context_refs") or []) if r))[:8]
        if not refs:
            return ""
        candidates = session.state_json.get("candidates") or []
        by_id = {str(c.get("candidate_id")): c for c in candidates if isinstance(c, dict)}
        names: list[str] = []
        for ref in refs:
            if ref == "__source__":
                names.append("the source video")
            elif ref in by_id:
                title = str(by_id[ref].get("title") or ref)[:120]
                names.append(f'"{title}" (candidate_id={ref})')
            # Unresolvable ids (stale/foreign) are dropped rather than confusing the model.
        if not names:
            return ""
        return (
            "[UI context: the user attached a reference to "
            + "; ".join(names)
            + " — their instruction refers to that version. Target it directly; do not ask which version.]"
        )

    def _state_summary(self, session: AgenticAudioSession) -> dict[str, Any]:
        state = session.state_json or {}
        observation = state.get("observation") or {}
        return {
            "phase": session.phase,
            "status": session.status,
            # A source video is ALWAYS attached to a session at creation time — the
            # user already uploaded/selected it. The agent must never ask for one.
            "source_video_attached": True,
            "source_video_artifact_id": session.source_video_artifact_id,
            "has_observation": bool(observation),
            "observation": observation,
            # The shared moment list both planners write against. Present every
            # turn so neither layer can plan without seeing the other's claims.
            "spotting_sheet": state.get("spotting_sheet") or {},
            "proposals": [
                {k: p.get(k) for k in ("proposal_id", "title", "modelspec")}
                for p in (state.get("proposals") or [])
            ],
            "candidates": [
                _candidate_digest(c) for c in (state.get("candidates") or [])
            ],
            # A comparison is held durably because the scratchpad is turn-local,
            # which means it outlives the takes it measured. Serve it only while
            # it still describes them.
            "last_comparison": (
                state.get("last_comparison")
                if comparison_still_describes(
                    state.get("last_comparison"), state.get("candidates")
                )
                else None
            ),
            "selected_candidate_id": session.selected_candidate_id,
            "approved_direction": bool(state.get("approved_direction")),
            "production_plan": state.get("production_plan"),
            "sfx_treatment": state.get("sfx_treatment"),
            "layers": state.get("layers") or {},
            "mix": state.get("mix"),
            "pending_clarification": state.get("pending_clarification"),
            # Durable conversational memory + recent turn history so the agent
            # stays consistent across a multi-turn session.
            "memory": state.get("memory") or {},
            "recent_turns": [
                {
                    "turn": t.get("turn"),
                    "intent": t.get("intent"),
                    "tools": t.get("tools"),
                    "user_message": t.get("user_message"),
                    # A turn that was cut short mid-plan looked identical to one
                    # that finished, so "keep going" had nothing to go on.
                    **({"budget_exhausted": True} if t.get("budget_exhausted") else {}),
                }
                for t in (state.get("turns") or [])[-AGENT_MEMORY_RECENT_TURNS:]
            ],
            # Present only on the turn after a truncation, and cleared by the
            # next turn that finishes: what the interrupted turn had already
            # done, and the last tool result it saw.
            **(
                {"resume_context": state["resume_context"]}
                if state.get("resume_context") else {}
            ),
        }

    # ----- session memory + turn history -------------------------------------

    def _record_turn_and_memory(
        self,
        *,
        session_id: str,
        user_message: str,
        intent: str,
        actions: list[dict[str, Any]],
        memory_updates: list[dict[str, Any]],
        clarification: Optional[dict[str, Any]] = None,
        steps_used: int = 0,
        skips_by_cause: Optional[dict[str, int]] = None,
        budget_exhausted: bool = False,
        token_usage: Optional[dict[str, int]] = None,
        resume_context: Optional[dict[str, Any]] = None,
    ) -> None:
        session = require_session(self.repository, session_id)
        state = dict(session.state_json)
        turns = list(state.get("turns") or [])
        tools = [a.get("tool_name") for a in actions if a.get("tool_name")]
        record: dict[str, Any] = {
            "turn": len(turns) + 1,
            "user_message": str(user_message)[:1000],
            "intent": intent,
            "actions": actions,
            "tools": tools,
            "phase": session.phase,
            # Which build of the system prompt directed this turn. See
            # prompts.PROMPT_HASH.
            "prompt_hash": PROMPT_HASH,
        }
        # Additive telemetry: how much of the step budget this turn actually cost,
        # including the skips that record no action. Kept out of the record when
        # unremarkable so turn history stays readable.
        if steps_used:
            record["steps_used"] = steps_used
        skips = {cause: n for cause, n in (skips_by_cause or {}).items() if n}
        if skips:
            # The total keeps its name and meaning; the breakdown is what makes
            # it answerable why the budget went where it went.
            record["skipped_steps"] = sum(skips.values())
            record["skips_by_cause"] = skips
        if budget_exhausted:
            record["budget_exhausted"] = True
        if token_usage and token_usage.get("calls"):
            record["token_usage"] = dict(token_usage)
            # The same numbers, where a bill can be built from them. The turn
            # record is session state: it is trimmed to the last hundred turns,
            # it goes when the session goes, and it is not a place money can be
            # counted from. Recorded before the session write below, because
            # the calls have already happened either way and bookkeeping must
            # not be what decides whether they are known about.
            self._record_usage(session_id=session_id, token_usage=token_usage)
        turns.append(record)
        # Keep history bounded; the most recent turns matter most for continuity.
        state["turns"] = turns[-100:]
        state["memory"] = self._merge_memory(
            memory=state.get("memory") or {},
            intent=intent,
            memory_updates=memory_updates,
            state=state,
        )
        # The pending clarification is whatever this turn asked for; a turn that
        # acted instead of clarifying clears any prior outstanding question.
        state["pending_clarification"] = clarification
        # Same discipline for the continuation: a turn that finished describes
        # no interrupted work, and leaving the previous one on the session would
        # be a second stale-state bug of exactly the kind this branch just spent
        # its time removing.
        if resume_context:
            state["resume_context"] = resume_context
        else:
            state.pop("resume_context", None)
        self.repository.update_session(session_id, state_json=state)

    def _record_usage(self, *, session_id: str, token_usage: dict[str, int]) -> None:
        """One row for one turn's model calls.

        Best-effort on purpose: failing a user's turn to record a fraction of a
        cent is the wrong trade, and the money is gone either way. An
        unreported count stays NULL rather than becoming zero — a turn whose
        usage the endpoint did not return is unmeasured, not free.
        """

        from ..persistence.usage import (
            Measurement,
            SITE_REASONING_TURN,
            active_meter,
            turn_key,
        )

        meter = active_meter()
        if not meter.enabled:
            return
        try:
            meter.record_once(
                meter_key=turn_key(session_id),
                site=SITE_REASONING_TURN,
                measurement=Measurement(
                    provider_calls=token_usage.get("calls"),
                    lm_input_tokens=token_usage.get("prompt"),
                    lm_output_tokens=token_usage.get("completion"),
                    primary_unit="lm_input_tokens",
                ),
                session_id=session_id,
            )
        except Exception:  # noqa: BLE001 - never fail a turn over bookkeeping
            logger.warning("usage meter: turn not recorded", exc_info=True)

    def fold_memory(self, session_id: str) -> None:
        """Re-derive durable memory from state, with no turn to record.

        Structured choices — approve a direction, generate, lock a take — are
        dispatched deterministically and never pass through ``run``, so the fold
        only ever ran on free-text chat turns. In a session that proposed,
        rendered and billed the enhanced tier, Director's notes still read
        ``modelspec: edenn_basic``: the fold had last run at the propose turn,
        before any proposal existed, and fell through to the analysis default
        (live audit, 2026-08-30). Memory that only updates when the user happens
        to type is memory that goes stale beside the thing it describes.
        """

        session = require_session(self.repository, session_id)
        state = dict(session.state_json)
        state["memory"] = self._merge_memory(
            memory=state.get("memory") or {},
            intent=None,  # no turn happened; do not pad recent_intents
            memory_updates=[],
            state=state,
        )
        self.repository.update_session(session_id, state_json=state)

    def _merge_memory(
        self,
        *,
        memory: dict[str, Any],
        intent: Optional[str],
        memory_updates: list[dict[str, Any]],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        merged = dict(memory)
        preferences = dict(merged.get("preferences") or {})
        style_keywords = list(merged.get("style_keywords") or [])
        avoid = list(merged.get("avoid") or [])

        # 1) Qualitative memory the LLM chose to remember this turn.
        for update in memory_updates:
            direction = str(update.get("creative_direction") or "").strip()
            if direction:
                merged["creative_direction"] = direction[:500]
            for keyword in update.get("style_keywords") or []:
                keyword = str(keyword).strip()
                if keyword and keyword not in style_keywords:
                    style_keywords.append(keyword)
            for keyword in update.get("avoid") or []:
                keyword = str(keyword).strip()
                if keyword and keyword not in avoid:
                    avoid.append(keyword)
            for key, value in (update.get("preferences") or {}).items():
                preferences[str(key)] = value

        # 2) Deterministic preferences derived from concrete state signals, which
        #    are more reliable than free-text for numeric/categorical settings.
        for key, value in self._derive_preferences(state).items():
            if value is not None:
                preferences[key] = value

        recent_intents = list(merged.get("recent_intents") or [])
        if intent is not None:
            recent_intents.append(intent)

        merged["preferences"] = preferences
        merged["style_keywords"] = style_keywords[-20:]
        merged["avoid"] = avoid[-20:]
        merged["recent_intents"] = recent_intents[-AGENT_MEMORY_RECENT_TURNS:]
        return merged

    @staticmethod
    def _derive_preferences(state: dict[str, Any]) -> dict[str, Any]:
        preferences: dict[str, Any] = {}
        candidates = state.get("candidates") or []
        # The user's most recent explicit mix volume, recorded by adjust_remix.
        if state.get("last_mix_volume") is not None:
            preferences["music_volume"] = state.get("last_mix_volume")

        # Preferred modelspec: the approved proposal, else a generated candidate,
        # else the analysis suggestion.
        proposals = state.get("proposals") or []
        selected_proposal_id = state.get("selected_proposal_id")
        modelspec = None
        if selected_proposal_id:
            for proposal in proposals:
                if proposal.get("proposal_id") == selected_proposal_id:
                    modelspec = proposal.get("modelspec")
                    break
        if not modelspec and candidates:
            modelspec = candidates[0].get("modelspec")
        if not modelspec:
            modelspec = (state.get("observation") or {}).get("suggested_modelspec")
        if modelspec:
            preferences["modelspec"] = modelspec
        return preferences

    # ----- clarification helpers ---------------------------------------------

    @staticmethod
    def _normalize_clarification(raw: Any) -> dict[str, Any]:
        # Wrong-shaped parts are treated as absent: the schema is advisory, and a
        # bare string here used to raise out of the turn.
        raw = raw if isinstance(raw, dict) else {}
        options: list[dict[str, Any]] = []
        raw_options = raw.get("options")
        for index, option in enumerate(raw_options if isinstance(raw_options, list) else []):
            if not isinstance(option, dict):
                continue
            label = str(option.get("label") or "").strip()
            if not label:
                continue
            normalized = {
                "id": str(option.get("id") or f"option_{index + 1}"),
                "label": label,
                "hint": str(option.get("hint") or "") or None,
            }
            if option.get("recommended"):
                normalized["recommended"] = True
            options.append(normalized)
        clarification = {
            "question": str(raw.get("question") or "Could you clarify what you'd like?"),
            "options": options,
        }
        # Topic tags let the answer be recorded durably (sfx_treatment today).
        topic = str(raw.get("topic") or "").strip()
        if topic and topic != "general":
            clarification["topic"] = topic
        return clarification

    @staticmethod
    def _is_redundant_modality_clarify(
        session: AgenticAudioSession, clarification: dict[str, Any]
    ) -> bool:
        """True when the modality is already locked (production_plan set) and the
        model is trying to re-ask which modality/layers to build — a redundant
        repeat of the intent gate."""

        if not (session.state_json or {}).get("production_plan"):
            return False
        text = " ".join(
            [str(clarification.get("question") or "")]
            + [str(opt.get("label") or "") for opt in clarification.get("options") or []]
            + [str(opt.get("id") or "") for opt in clarification.get("options") or []]
        ).lower()
        keywords = (
            "modality", "music only", "music-only", "voice-over", "voiceover",
            "full mix", "full audio", "full_audio", "music_only", "voiceover_only",
            "with sfx", "adding today", "what are you adding",
        )
        return sum(1 for k in keywords if k in text) >= 2

    @staticmethod
    def _is_redundant_sfx_treatment_clarify(
        session: AgenticAudioSession, clarification: dict[str, Any]
    ) -> bool:
        """True when the SFX treatment is already chosen (state.sfx_treatment)
        and the model is trying to re-ask it — the answer is durable; re-asking
        a settled question erodes trust in the card."""

        if not (session.state_json or {}).get("sfx_treatment"):
            return False
        if str(clarification.get("topic") or "") == "sfx_treatment":
            return True
        # Fallback for a re-ask the model forgot to tag: the same question in
        # plain words is just as redundant as the tagged one.
        text = " ".join(
            [str(clarification.get("question") or "")]
            + [str(opt.get("label") or "") for opt in clarification.get("options") or []]
        ).lower()
        sfx_words = ("sfx", "sound effect", "sound design", "effects")
        treatment_words = ("feel like", "style", "register", "treatment", "how many", "density")
        return any(w in text for w in sfx_words) and any(w in text for w in treatment_words)

    # ----- proposals ----------------------------------------------------------

    def _store_proposals(
        self, session_id: str, raw_proposals: list[dict[str, Any]]
    ) -> list[AgenticAudioWebSocketEvent]:
        session = require_session(self.repository, session_id)
        proposals: list[dict[str, Any]] = []
        # Hard cap: at most 2 directions, regardless of what the model returns.
        for index, raw in enumerate(raw_proposals[:2]):
            # The tier is clamped to what THIS box can actually render. The
            # model proposed premium tiers whose provider had no key here;
            # render-time fallback then substituted a different provider than
            # every label on screen — and a slower one, which the user felt
            # before anyone saw it in a log.
            requested_spec = normalize_music_modelspec(str(raw.get("modelspec") or ""))
            usable_spec = usable_music_modelspec(requested_spec)
            card = MusicProposalCard(
                proposal_id=str(raw.get("proposal_id") or f"proposal_{index + 1}"),
                title=str(raw.get("title") or f"Direction {index + 1}"),
                prompt=str(raw.get("prompt") or ""),
                modelspec=usable_spec,
                include_vocals=bool(raw.get("include_vocals", False)),
                vocal_gender=str(raw.get("vocal_gender") or "female"),
                music_volume=float(raw.get("music_volume") or 0.85),
            )
            proposal_row = card.model_dump(mode="json")
            if usable_spec != requested_spec:
                proposal_row["requested_modelspec"] = requested_spec
                logger.warning(
                    "proposal %s asked for %s; no provider key here — pinned "
                    "to %s at propose time",
                    proposal_row["proposal_id"], requested_spec, usable_spec,
                )
            proposals.append(proposal_row)
        state = dict(session.state_json)
        state["proposals"] = proposals
        self.repository.record_tool_call(
            session_id=session_id,
            tool_name="propose_music_plan",
            status=AgenticToolStatus.COMPLETED,
            input_json={"observation": state.get("observation") or {}},
            output_json={"proposals": proposals},
            finished=True,
        )
        transition(
            self.repository,
            session_id,
            Stage.AWAITING_PLAN_CHOICE,
            state_json=state,
        )
        return [
            make_event(session_id, EventType.PROPOSAL_CARDS, {"proposals": proposals}),
            make_event(
                session_id,
                EventType.PHASE_CHANGED,
                {"phase": Stage.AWAITING_PLAN_CHOICE.value},
            ),
        ]


__all__ = ["ReasoningLoop", "EventSink"]
