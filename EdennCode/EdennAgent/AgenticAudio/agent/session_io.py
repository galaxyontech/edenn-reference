"""Shared session I/O + reasoning-beat helpers.

Small, dependency-light functions used by both the orchestrator
(:class:`AgenticAudioAgent`) and the :class:`ReasoningLoop`, so neither owns the
other and there is one implementation of "append a message", "build an event",
"emit the live reasoning beat", etc.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

from typing import Any, Optional

from EdennCode.Deployment.error_codes import scrub_provider_names

from ..events import EventType
from ..models import (
    AGENT_ACTION_ASK,
    AGENT_ACTION_CALL_TOOL,
    AGENT_ACTION_CLARIFY,
    AGENT_ACTION_NOOP,
    AGENT_ACTION_PROPOSE,
    STATUS_BY_TOOL,
    AgenticAudioChoiceRequest,
    AgenticAudioSession,
    AgenticAudioWebSocketEvent,
)


def make_event(
    session_id: str, event_type: str, payload: dict[str, Any]
) -> AgenticAudioWebSocketEvent:
    return AgenticAudioWebSocketEvent(
        event_type=event_type, session_id=session_id, payload=payload
    )


async def push(
    emit: Optional[Any],
    events: list[AgenticAudioWebSocketEvent],
    new: list[AgenticAudioWebSocketEvent],
) -> None:
    """Append events and, when streaming, emit each live as it is produced.

    A failed emit does NOT end the turn. The sink is a browser socket: a page
    refresh mid-render used to raise straight through the reasoning loop, which
    skipped the turn record, the token usage and the resume context — so the
    work was done, the money was spent, and the session had no memory of any of
    it. The events are still collected, so the snapshot the client gets when it
    reconnects is complete; only the live streaming stops.
    """

    sink: Optional[Any] = emit
    for event in new:
        events.append(event)
        if sink is not None:
            try:
                await sink(event)
            except Exception:  # noqa: BLE001 — a dead socket is not a failed turn
                logger.info(
                    "agentic audio: event sink is gone; finishing the turn without "
                    "streaming (the snapshot will carry it)"
                )
                sink = None


def require_session(repository: Any, session_id: str) -> AgenticAudioSession:
    session = repository.get_session(session_id)
    if session is None:
        raise KeyError(session_id)
    return session


def append_message(
    repository: Any,
    session_id: str,
    role: str,
    content: str,
    *,
    payload_json: Optional[dict[str, Any]] = None,
) -> list[AgenticAudioWebSocketEvent]:
    """Persist one message and return the event that announces it.

    Anything the MODEL wrote is scrubbed on the way through. The house rule —
    never name an upstream vendor where a user can see it — was resting on the
    system prompt asking the model nicely, which is not a control: the same
    free text is persisted, emitted to every collaborator on the socket, and
    replayed in the snapshot. The user's own words are left exactly as typed;
    scrubbing those would edit the brief rather than our own output.
    """

    if str(role).lower() != "user":
        content = scrub_provider_names(content)
    message = repository.append_message(
        session_id=session_id,
        role=role,
        content=content,
        payload_json=payload_json or {},
    )
    return [
        make_event(
            session_id,
            EventType.MESSAGE_CREATED,
            {
                "message_id": message.message_id,
                "role": message.role,
                "content": message.content,
            },
        )
    ]


def find_by_id(
    items: list[dict[str, Any]], key: str, value: str
) -> Optional[dict[str, Any]]:
    for item in items:
        if str(item.get(key)) == value:
            return dict(item)
    return None


def record_choice_event(
    repository: Any, session_id: str, request: AgenticAudioChoiceRequest
) -> AgenticAudioWebSocketEvent:
    choice = repository.record_choice(
        session_id=session_id,
        choice_type=request.choice_type,
        target_id=request.target_id,
        payload_json=request.payload,
    )
    return make_event(
        session_id,
        EventType.CHOICE_RECORDED,
        {
            "choice_id": choice.choice_id,
            "choice_type": choice.choice_type,
            "target_id": choice.target_id,
        },
    )


# Live status shown for non-tool actions (tool statuses come from STATUS_BY_TOOL).
_STATUS_BY_ACTION = {
    AGENT_ACTION_PROPOSE: "Lining up directions…",
    AGENT_ACTION_ASK: "Thinking it through…",
    AGENT_ACTION_CLARIFY: "Checking a detail with you…",
    AGENT_ACTION_NOOP: "Got it…",
}


def reasoning_event(
    session_id: str,
    *,
    intent: str,
    action_type: str,
    tool_name: str,
    thought: str,
) -> AgenticAudioWebSocketEvent:
    """The live 'agent.reasoning' beat: a short status plus the model's thought.

    The thought is raw model free text on its way to a browser, so it goes
    through the same scrub as the message.
    """

    if action_type == AGENT_ACTION_CALL_TOOL:
        status = STATUS_BY_TOOL.get(tool_name, "Working on it…")
    else:
        status = _STATUS_BY_ACTION.get(action_type, "Thinking…")
    return make_event(
        session_id,
        EventType.AGENT_REASONING,
        {
            "status": status,
            "intent": intent or None,
            # Raw model reasoning, truncated; UI may show it in a details view.
            "thought": scrub_provider_names(thought[:400]) or None,
        },
    )


__all__ = [
    "append_message",
    "find_by_id",
    "make_event",
    "push",
    "reasoning_event",
    "record_choice_event",
    "require_session",
]
