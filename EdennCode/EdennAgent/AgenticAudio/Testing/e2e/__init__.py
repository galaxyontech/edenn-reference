"""Robust agent E2E foundation.

Drives the real agent loop end-to-end over in-memory fakes with a pluggable LLM
(scripted for CI, or the real model for runtime "watch the session" runs), scores
each trajectory against safety / intent / deliverable gates, and can render a
readable transcript of the session.

Layout:
- fakes.py      : the single import seam for the in-memory fakes
- scenarios.py  : Turn/Scenario model + the persona + happy/unhappy catalog
- driver.py     : E2EDriver (builds the real router over fakes) + run_scenario
- gates.py      : metrics + safety / deliverable gates + LLM judge
- watch.py      : render a turn-by-turn transcript ("watch the session")
- run_e2e.py    : CLI runner (--scripted | --real), prints watch + gate summary
"""

from __future__ import annotations

from .driver import E2EDriver, Trajectory, TurnResult, run_scenario
from .gates import compute_metrics, deliverable_reached, gate_summary
from .scenarios import SCENARIOS, Scenario, Turn
from .watch import render_transcript

__all__ = [
    "E2EDriver",
    "SCENARIOS",
    "Scenario",
    "Trajectory",
    "Turn",
    "TurnResult",
    "compute_metrics",
    "deliverable_reached",
    "gate_summary",
    "render_transcript",
    "run_scenario",
]
