"""Tool dispatch: the one place a tool name becomes a running tool.

Used by both the orchestrator (deterministic ``/choices`` + intent gate) and the
:class:`ReasoningLoop` (the ``call_tool`` action), so the registry lookup and the
``ToolContext`` construction live in exactly one spot.
"""

from __future__ import annotations

from typing import Any

import logging
import time

from . import spend_guard
from ..api import audit
from ..api.observability import principal_var
from ..persistence.repositories import AgenticAudioRepository
from ..tools.arg_specs import validate_tool_args
from ..tools.arg_specs import ToolArgsInvalid
from ..tools.base import ApprovalRequiredError, ToolContext, ToolRegistry
from ..tools.media import AgenticAudioTools


logger = logging.getLogger(__name__)


class DuplicateGeneration(ValueError):
    """The same generation was asked for twice inside the dedupe window.

    A ValueError so it surfaces as a 400 rather than a server fault: the request
    is the problem, and the honest answer is that the work is already happening.
    """


class ToolDispatcher:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        repository: AgenticAudioRepository,
        media: AgenticAudioTools,
        max_candidates: int,
    ) -> None:
        self.registry = registry
        self.repository = repository
        self.media = media
        self.max_candidates = max_candidates

    def _context(self, session_id: str, *, invoked_by: str = "agent") -> ToolContext:
        return ToolContext(
            session_id=session_id,
            repository=self.repository,
            media=self.media,
            max_candidates=self.max_candidates,
            invoked_by=invoked_by,
        )

    async def dispatch_collecting(
        self, session_id: str, tool_name: str, tool_args: dict[str, Any],
        *, invoked_by: str = "agent",
    ) -> tuple[list[Any], Any]:
        """Run a tool; returns (events, raw_result).

        Unknown tools raise ValueError (surfaced as 400); a paid tool without
        prior approval raises ApprovalRequiredError, which the loop catches.
        """

        tool = self.registry.get(tool_name)

        # Check the arguments BEFORE the spend guard records anything. A refused
        # call never ran, so a fingerprint left behind for it would reject the
        # user's corrected retry as a duplicate of the attempt that was blocked —
        # the same reasoning as the ApprovalRequiredError rollback below, applied
        # one step earlier. Validation never rewrites tool_args: the fingerprint
        # hashes them verbatim, so normalising here would quietly change which
        # repeat calls count as the same request.
        validate_tool_args(tool_name, tool_args)

        # A generating tool costs real provider credit, and every path that
        # reaches one retries: a resent POST, a reconnecting socket, a loop
        # re-proposing after a transient failure. Each of those used to produce
        # a second render and a second charge.
        if getattr(tool, "requires_approval", False):
            window = spend_guard.window_seconds()
            if window:
                session = self.repository.get_session(session_id)
                state = dict(getattr(session, "state_json", None) or {}) if session else {}
                seen = spend_guard.recent_match(
                    state, tool_name=tool_name, tool_args=tool_args, window_s=window
                )
                if seen is not None:
                    logger.warning(
                        "refusing a duplicate %s for session %s (last ran %.0fs ago)",
                        tool_name, session_id, max(0.0, time.time() - float(seen.get("at") or 0)),
                    )
                    raise DuplicateGeneration(
                        "That generation is already running — I won't start it twice."
                    )
                # Recorded BEFORE the run, not after: the window has to cover the
                # tool being slow, which is exactly when a client gives up and
                # retries.
                self.repository.update_session(
                    session_id,
                    state_json=spend_guard.remember(
                        state, tool_name=tool_name, tool_args=tool_args, window_s=window
                    ),
                )

        if getattr(tool, "requires_approval", False):
            # The caller IS knowable here. The dispatcher is not handed one, but
            # the router binds the resolved principal into the request context
            # for logging, and a spend line is exactly what that context is for.
            # Without it every billed generation on an auth-required deployment
            # recorded no actor — four out of four during the live audit, by a
            # signed-in owner, over both transports — which is the one question
            # an audit log exists to answer.
            audit.record(
                f"generation.{tool_name}", kind=audit.SPEND,
                actor=principal_var.get("") or None, session_id=session_id,
            )

        try:
            result = await tool.run(
                self._context(session_id, invoked_by=invoked_by), tool_args
            )
        except Exception as exc:
            # Release the fingerprint only for failures we KNOW happened before
            # anything was bought. Refusals and argument rejections are raised
            # by the tool's own gates before it reaches a provider; a tool may
            # also say so explicitly by setting `spent = False` on what it
            # raises. Everything else keeps its fingerprint, because the
            # alternative error is the expensive one: a provider that timed out
            # after taking the money, retried immediately, and charged twice.
            # The cost of being wrong this way is that the user waits out the
            # window; the cost of the other way is their money.
            pre_spend = isinstance(exc, (ApprovalRequiredError, ToolArgsInvalid)) or (
                getattr(exc, "spent", None) is False
            )
            if pre_spend and getattr(tool, "requires_approval", False) and spend_guard.window_seconds():
                session = self.repository.get_session(session_id)
                state = dict(getattr(session, "state_json", None) or {}) if session else {}
                self.repository.update_session(
                    session_id,
                    state_json=spend_guard.forget(
                        state, tool_name=tool_name, tool_args=tool_args
                    ),
                )
            raise
        return result.events, result.data

    async def dispatch(
        self, session_id: str, tool_name: str, tool_args: dict[str, Any],
        *, invoked_by: str = "agent",
    ) -> list[Any]:
        events, _ = await self.dispatch_collecting(
            session_id, tool_name, tool_args, invoked_by=invoked_by
        )
        return events

    def is_heavy(self, tool_name: str) -> bool:
        return self.registry.get(tool_name).is_heavy


__all__ = ["ToolDispatcher"]
