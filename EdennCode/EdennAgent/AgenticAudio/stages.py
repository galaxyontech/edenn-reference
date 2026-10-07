"""Explicit session stage model.

Replaces the bare ``phase`` string with a declared :class:`Stage` enum plus the
canonical transition graph, and routes every phase write through a single
``transition`` function. Validation is currently SOFT (it logs an undeclared
transition rather than raising) because a session is intentionally never-ending —
after a final mix the user keeps iterating (re-edit / re-mix), which legitimately
loops earlier stages. Centralizing the writes here is what makes the graph
auditable and gives one place to tighten to hard validation later.

``Stage`` values are byte-identical to the old ``AgenticAudioSessionPhase``
strings, so persisted phases and the wire contract are unchanged.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class Stage(str, Enum):
    CREATED = "created"
    OBSERVING = "observing"
    PROPOSING = "proposing"
    AWAITING_PLAN_CHOICE = "awaiting_plan_choice"
    GENERATING_CANDIDATES = "generating_candidates"
    AWAITING_CANDIDATE_CHOICE = "awaiting_candidate_choice"
    COMPOSING = "composing"
    COMPLETED = "completed"
    FAILED = "failed"

    def __str__(self) -> str:
        return self.value


# Canonical forward happy-path graph. Iterative re-entry (e.g. COMPLETED ->
# GENERATING_CANDIDATES when the user edits after finalizing) is expected and
# allowed; this map documents the intended flow and powers soft validation.
LEGAL_TRANSITIONS: dict[Stage, frozenset[Stage]] = {
    Stage.CREATED: frozenset({Stage.OBSERVING, Stage.PROPOSING, Stage.AWAITING_PLAN_CHOICE}),
    Stage.OBSERVING: frozenset({Stage.PROPOSING, Stage.AWAITING_PLAN_CHOICE}),
    Stage.PROPOSING: frozenset({Stage.AWAITING_PLAN_CHOICE, Stage.OBSERVING}),
    Stage.AWAITING_PLAN_CHOICE: frozenset({Stage.GENERATING_CANDIDATES, Stage.PROPOSING}),
    Stage.GENERATING_CANDIDATES: frozenset({Stage.AWAITING_CANDIDATE_CHOICE, Stage.FAILED}),
    Stage.AWAITING_CANDIDATE_CHOICE: frozenset(
        {Stage.COMPOSING, Stage.GENERATING_CANDIDATES, Stage.AWAITING_CANDIDATE_CHOICE}
    ),
    Stage.COMPOSING: frozenset(
        {Stage.COMPLETED, Stage.COMPOSING, Stage.GENERATING_CANDIDATES, Stage.AWAITING_CANDIDATE_CHOICE}
    ),
    # Never-ending session: keep iterating after a final mix.
    Stage.COMPLETED: frozenset(
        {Stage.GENERATING_CANDIDATES, Stage.COMPOSING, Stage.AWAITING_CANDIDATE_CHOICE}
    ),
    Stage.FAILED: frozenset({Stage.OBSERVING, Stage.PROPOSING, Stage.GENERATING_CANDIDATES}),
}


class StageMachine:
    """Declared transition graph over :class:`Stage`."""

    @staticmethod
    def is_legal(src: Optional[str], dst: str) -> bool:
        try:
            src_stage = Stage(src) if src is not None else None
            dst_stage = Stage(dst)
        except ValueError:
            return False
        if src_stage is None:
            return True
        return dst_stage in LEGAL_TRANSITIONS.get(src_stage, frozenset())

    @classmethod
    def validate(cls, src: Optional[str], dst: str) -> bool:
        """Soft validation: log an undeclared transition, never block it."""

        ok = cls.is_legal(src, dst)
        if not ok:
            logger.debug("agentic_audio: undeclared stage transition %s -> %s", src, dst)
        return ok


def transition(
    repository: Any,
    session_id: str,
    to_phase: str,
    **update_kwargs: Any,
) -> Any:
    """Move a session to ``to_phase`` (validated) and apply any other field
    updates in the same write. The single choke point for phase changes."""

    session = repository.get_session(session_id)
    current = session.phase if session is not None else None
    StageMachine.validate(current, to_phase)
    return repository.update_session(session_id, phase=to_phase, **update_kwargs)


__all__ = ["Stage", "StageMachine", "LEGAL_TRANSITIONS", "transition"]
