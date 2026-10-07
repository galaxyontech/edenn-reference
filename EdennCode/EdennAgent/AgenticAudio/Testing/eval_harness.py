"""Back-compat shim.

The multi-turn eval harness moved into the structured ``Testing/e2e/`` package
(driver / scenarios / gates / watch). This module re-exports the previous public
surface so existing imports (run_eval, test_eval_harness) keep working unchanged.
New code should import from ``Testing.e2e`` directly.
"""

from __future__ import annotations

from EdennCode.EdennAgent.AgenticAudio.Testing.e2e.driver import (
    MemoryEvalDriver,
    Trajectory,
    TurnResult,
    run_scenario,
)
from EdennCode.EdennAgent.AgenticAudio.Testing.e2e.gates import (
    JUDGE_SCHEMA,
    JUDGE_SYSTEM,
    compute_metrics,
    judge_transcript,
)
from EdennCode.EdennAgent.AgenticAudio.Testing.e2e.scenarios import Scenario, Turn

__all__ = [
    "JUDGE_SCHEMA",
    "JUDGE_SYSTEM",
    "MemoryEvalDriver",
    "Scenario",
    "Trajectory",
    "Turn",
    "TurnResult",
    "compute_metrics",
    "judge_transcript",
    "run_scenario",
]
