from __future__ import annotations

from typing import Any, Optional

from .agent.agent import AgenticAudioAgent
from .models import (
    AgenticAudioChoiceRequest,
    AgenticAudioSession,
    AgenticAudioWebSocketEvent,
)
from .persistence.repositories import AgenticAudioRepository
from .tools.media import AgenticAudioTools


class AgenticAudioPlanner:
    """Backwards-compatible facade over the LLM-driven :class:`AgenticAudioAgent`.

    The previous deterministic state machine has been replaced by the agent; this
    class keeps the historical constructor/method surface so callers and tests
    that referenced the planner continue to work. All planning decisions are now
    made by the agent's LLM loop.
    """

    def __init__(
        self,
        *,
        repository: AgenticAudioRepository,
        tools: AgenticAudioTools,
        llm_client: Any,
        agent: Optional[AgenticAudioAgent] = None,
        max_steps_per_turn: Optional[int] = None,
        max_candidates: Optional[int] = None,
        require_intent_gate: bool = False,
    ) -> None:
        self.repository = repository
        self.tools = tools
        self.agent = agent or AgenticAudioAgent(
            repository=repository,
            tools=tools,
            llm_client=llm_client,
            max_steps_per_turn=max_steps_per_turn,
            max_candidates=max_candidates,
            require_intent_gate=require_intent_gate,
        )

    async def bootstrap_session(
        self,
        *,
        session: AgenticAudioSession,
        initial_message: Optional[str] = None,
        emit: Any = None,
    ) -> list[AgenticAudioWebSocketEvent]:
        return await self.agent.bootstrap_session(
            session=session, initial_message=initial_message, emit=emit
        )

    async def handle_user_message(
        self,
        *,
        session_id: str,
        content: str,
        payload: Optional[dict[str, Any]] = None,
        emit: Any = None,
    ) -> list[AgenticAudioWebSocketEvent]:
        return await self.agent.handle_user_message(
            session_id=session_id, content=content, payload=payload, emit=emit
        )

    async def handle_choice(
        self,
        *,
        session_id: str,
        request: AgenticAudioChoiceRequest,
        emit: Any = None,
    ) -> list[AgenticAudioWebSocketEvent]:
        return await self.agent.handle_choice(
            session_id=session_id, request=request, emit=emit
        )

    def refresh_session_state(self, session_id: str) -> AgenticAudioSession:
        return self.agent.refresh_session_state(session_id)


__all__ = ["AgenticAudioPlanner"]
