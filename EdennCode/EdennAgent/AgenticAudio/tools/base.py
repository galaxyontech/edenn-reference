from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from EdennCode.Deployment.error_codes import scrub_provider_names

from ..domain import CandidateGraph, SessionState, User
from ..events import EventType
from ..models import (
    AgenticAudioSession,
    AgenticAudioWebSocketEvent,
    AgenticMessageRole,
    ToolSpec,
    TOOL_SPECS_BY_NAME,
)
from ..persistence.repositories import AgenticAudioRepository
from ..stages import transition
from .media import AgenticAudioTools


class ApprovalRequiredError(ValueError):
    """Raised by a paid tool when the user has not yet approved the spend.

    ``args[0]`` speaks to the AGENT (what to do instead of spending);
    ``user_message``, when set, is what the loop shows the USER — some gates
    unlock on a chat "yes", others only on a card button, and the recovery
    text must promise the right one.

    Subclasses ``ValueError`` so the API still surfaces it as 400 if it ever
    escapes the loop, but the reasoning loop catches it specifically to re-ask
    for approval gracefully instead of failing the turn. Lives here (the tool
    domain) so tool implementations and the loop share one definition.
    """

    def __init__(self, message: str, *, user_message: str | None = None) -> None:
        super().__init__(message)
        self.user_message = user_message


class ToolError(Exception):
    """A recoverable tool failure carrying a user-facing message.

    Tools raise this for bad/missing args instead of a raw ``KeyError`` so the
    loop can surface a friendly re-ask. (Phase 1 keeps the existing graceful
    in-tool handling; this is the shared type the loop can grow to recover from.)
    """

    def __init__(self, message: str, *, kind: str = "tool_error") -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


@dataclass
class ToolResult:
    """What a tool returns: the events to stream + the raw result fed back to
    the reasoning loop's scratchpad."""

    events: list[AgenticAudioWebSocketEvent] = field(default_factory=list)
    data: Any = None


class ToolContext:
    """Everything a tool needs to run, with no back-reference to the agent.

    Bundles the session id, the persistence repository, and the media/job
    toolkit, plus the small shared helpers tools used to reach for on the agent
    (event construction, message append, candidate/proposal resolution). Built
    fresh per dispatch by the reasoning loop.
    """

    def __init__(
        self,
        *,
        session_id: str,
        repository: AgenticAudioRepository,
        media: AgenticAudioTools,
        max_candidates: int,
        invoked_by: str = "agent",
    ) -> None:
        self.session_id = session_id
        self.repository = repository
        self.media = media
        self.max_candidates = max_candidates
        # Who asked for this call: "user" only when a real user control (a
        # choice endpoint — a card button) submitted it; "agent" for everything
        # the reasoning loop decides on its own. Spend gates key off this,
        # because a precondition the agent can satisfy itself (like "a script
        # exists") is not an approval — the agent drafted a script and spent on
        # it in the same turn, and a user watched a paid render they never
        # asked for.
        self.invoked_by = invoked_by

    @property
    def actor_user_id(self) -> Optional[str]:
        """The principal whose request this is, when there is one.

        NOT the session's owner. Every job row was stamped with the owner
        whoever actually ran it, so a collaborator's render was attributed to
        somebody else — while the rate limiter and the audit log, reading this
        same context, recorded the truth. Two records of one action that
        disagree cannot become a bill.

        Empty outside a request (a background sweep, a test), which is exactly
        when the owner IS the best available answer.
        """

        from ..api.observability import principal_var

        return principal_var.get("") or None

    # ----- session + events --------------------------------------------------

    def require_session(self) -> AgenticAudioSession:
        session = self.repository.get_session(self.session_id)
        if session is None:
            raise KeyError(self.session_id)
        return session

    def event(self, event_type: str, payload: dict[str, Any]) -> AgenticAudioWebSocketEvent:
        return AgenticAudioWebSocketEvent(
            event_type=event_type, session_id=self.session_id, payload=payload
        )

    def append_message(
        self,
        role: str,
        content: str,
        *,
        payload_json: Optional[dict[str, Any]] = None,
    ) -> list[AgenticAudioWebSocketEvent]:
        """Same contract as ``session_io.append_message``, including the scrub.

        A tool's line is our own copy, but it quotes model-authored material
        (a drafted script, a direction title), so it leaves by the same door.
        It cannot literally share that function: importing the agent package
        from a tool closes an import cycle.
        """

        if str(role).lower() != "user":
            content = scrub_provider_names(content)
        message = self.repository.append_message(
            session_id=self.session_id,
            role=role,
            content=content,
            payload_json=payload_json or {},
        )
        return [
            self.event(
                EventType.MESSAGE_CREATED,
                {
                    "message_id": message.message_id,
                    "role": message.role,
                    "content": message.content,
                },
            )
        ]

    def transition_to(self, to_phase: str, **update_kwargs: Any) -> Any:
        """Move the session to ``to_phase`` (validated by the StageMachine) and
        apply any other field updates in the same write — the single choke point
        for a tool's phase changes."""

        return transition(self.repository, self.session_id, to_phase, **update_kwargs)

    def no_target_message(self, verb: str) -> ToolResult:
        """Graceful in-conversation reply when there is no music track to act on.

        Edits that arrive before any music exists (e.g. "make it quieter" on a
        fresh session) used to 404; instead we keep the conversation going.
        """

        events = self.append_message(
            AgenticMessageRole.ASSISTANT,
            f"There's no music track to {verb} yet — want me to put together a "
            "direction and generate one first?",
        )
        return ToolResult(events=events, data={"status": "no_candidate"})

    # ----- first-class domain views ------------------------------------------

    @property
    def user(self) -> User:
        """The session's creator as a first-class entity."""

        return User.from_session(self.require_session())

    def session_state(self) -> SessionState:
        """A typed accessor over the current session's state_json."""

        return SessionState(self.require_session().state_json)

    # ----- candidate / proposal resolution (via the typed domain layer) -------

    @staticmethod
    def find_by_id(
        items: list[dict[str, Any]], key: str, value: str
    ) -> Optional[dict[str, Any]]:
        for item in items:
            if str(item.get(key)) == value:
                return dict(item)
        return None

    def resolve_candidate(
        self,
        session: AgenticAudioSession,
        candidates: list[dict[str, Any]],
        tool_args: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        """Target candidate from tool args, selection, or (if unambiguous) the
        only one — resolved by the typed candidate graph."""

        candidate_id = str(
            tool_args.get("candidate_id") or session.selected_candidate_id or ""
        )
        return CandidateGraph(candidates).resolve(candidate_id)

    def resolve_proposal(
        self, session: AgenticAudioSession, tool_args: dict[str, Any]
    ) -> dict[str, Any]:
        return SessionState(session.state_json).resolve_proposal(tool_args)

    @staticmethod
    def next_version(
        candidates: list[dict[str, Any]], parent_id: str, parent: dict[str, Any]
    ) -> int:
        return CandidateGraph(candidates).next_version(parent_id, parent)


class Tool(ABC):
    """An agent-facing tool: one capability the reasoning loop can dispatch.

    Identity/metadata (heavy?, generation?, status label) is owned by the
    matching :class:`ToolSpec` in ``models.TOOL_SPECS`` — the single source of
    truth — so a tool never re-declares its own classification.
    """

    #: Must match a ToolSpec name in models.TOOL_SPECS.
    name: str = ""

    @property
    def spec(self) -> ToolSpec:
        return TOOL_SPECS_BY_NAME[self.name]

    @property
    def is_heavy(self) -> bool:
        return self.spec.is_heavy

    @property
    def requires_approval(self) -> bool:
        return self.spec.is_generation

    @abstractmethod
    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        """Execute the tool and return the events + raw result."""


class ToolRegistry:
    """The one place tools are looked up. Built once from the Tool instances;
    every tool must have a matching ToolSpec, so the registry and the derived
    schema/heavy/generation/status maps can never drift apart."""

    def __init__(self, tools: Iterable[Tool]) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if not tool.name:
                raise ValueError(f"Tool {tool!r} has no name")
            if tool.name in self._tools:
                raise ValueError(f"Duplicate tool registered: {tool.name}")
            if tool.name not in TOOL_SPECS_BY_NAME:
                raise ValueError(f"Tool {tool.name!r} has no ToolSpec in TOOL_SPECS")
            self._tools[tool.name] = tool

        # ...and the other direction. A spec with no implementation is a tool
        # the model can name, the schema will accept, and the dispatcher cannot
        # run — a promise the product does not keep.
        missing = [name for name in TOOL_SPECS_BY_NAME if name not in self._tools]
        if missing:
            raise ValueError(
                "ToolSpec declared with no implementation bound: "
                + ", ".join(sorted(missing))
            )

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise ValueError(f"Unsupported tool: {name}")
        return tool

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)


__all__ = [
    "ApprovalRequiredError",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
]
