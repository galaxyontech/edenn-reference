"""The single source of truth for the agent's event vocabulary.

Every event the backend streams (over WS, and in each REST ``events[]`` batch)
is one of these types. ``EventType`` is a ``str`` enum, so a member IS the wire
string (``EventType.TOOL_STARTED == "tool.started"``) and serializes unchanged —
but emit sites reference the member, so renaming one fails the tests instead of
silently breaking the frontend that keys off the same names. Keep this list in
sync with the frontend's event handler (see FRONTEND_INTEGRATION.md).
"""

from __future__ import annotations

from enum import Enum


class EventType(str, Enum):
    # Session lifecycle / transport.
    SESSION_OPENED = "session.opened"
    ERROR = "error"

    # Conversation.
    MESSAGE_CREATED = "message.created"
    AGENT_REASONING = "agent.reasoning"

    # Tool execution beats.
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"

    # Cards / panels the workbench renders.
    PROPOSAL_CARDS = "proposal.cards"
    CANDIDATE_CARDS = "candidate.cards"
    CANDIDATE_SELECTED = "candidate.selected"
    CLARIFY_CARDS = "clarify.cards"
    PRODUCTION_PLAN = "production.plan"
    VOICEOVER_SCRIPT = "voiceover.script"
    VOICEOVER_GENERATING = "voiceover.generating"
    SPOTTING_SHEET = "spotting.sheet"
    SFX_PLAN = "sfx.plan"
    SFX_GENERATING = "sfx.generating"
    MIX_UPDATED = "mix.updated"
    FINAL_ARTIFACT = "final.artifact"

    # Direction / phase / choice signals.
    DIRECTION_APPROVED = "direction.approved"
    PHASE_CHANGED = "phase.changed"
    CHOICE_RECORDED = "choice.recorded"

    # Collab mode (comment threads on the lineage canvas). Broadcast to every
    # live WS on the session so all viewers' pins/panels update together.
    COMMENT_THREAD_CREATED = "comment.thread.created"
    COMMENT_THREAD_UPDATED = "comment.thread.updated"  # resolve / reopen
    COMMENT_CREATED = "comment.created"
    COMMENT_UPDATED = "comment.updated"  # edit / delete / reactions
    PARTICIPANT_UPDATED = "participant.updated"

    def __str__(self) -> str:  # so f-strings / logs show the wire value
        return self.value


# All wire strings, handy for contract tests asserting FE<->BE agree.
EVENT_TYPES: frozenset[str] = frozenset(member.value for member in EventType)


__all__ = ["EventType", "EVENT_TYPES"]
