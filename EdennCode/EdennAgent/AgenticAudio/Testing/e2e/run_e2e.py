"""Run the scenario catalog through the REAL agent loop and *watch the session*.

    .venv/bin/python -m EdennCode.EdennAgent.AgenticAudio.Testing.e2e.run_e2e

Prints a turn-by-turn transcript + per-scenario gate metrics + an aggregate gate
summary. Exits non-zero if the hard safety gate (unauthorized_generation == 0) or
any gold deliverable gate fails. Requires AGENTIC_AUDIO_AZURE_* or AZURE_*; only
the agent's reasoning hits a model (no Postgres/providers)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

from EdennCode.env import load_env
from EdennCode.EdennAgent.AgenticAudio.agent import build_agentic_audio_agent_client

from .driver import E2EDriver, run_scenario
from .gates import compute_metrics, gate_summary
from .scenarios import SCENARIOS
from .watch import render_transcript


def real_llm_available() -> bool:
    def _has(*names: str) -> bool:
        return any((os.getenv(n) or "").strip() for n in names)

    return (
        _has("AGENTIC_AUDIO_AZURE_ENDPOINT", "AZURE_ENDPOINT")
        and _has("AGENTIC_AUDIO_AZURE_MODEL", "AZURE_MODEL")
        and _has("AGENTIC_AUDIO_AZURE_API_KEY", "AZURE_API_KEY")
    )


def main(argv: Optional[list[str]] = None) -> int:
    load_env()
    if not real_llm_available():
        print("SKIP: real agent LLM env (AGENTIC_AUDIO_AZURE_* / AZURE_*) not configured.")
        return 0
    client = build_agentic_audio_agent_client()
    rows = []
    for scenario in SCENARIOS:
        with tempfile.TemporaryDirectory() as tmp:
            driver = E2EDriver(Path(tmp), client)
            trajectory = run_scenario(driver, scenario)
        metrics = compute_metrics(trajectory, scenario)
        rows.append(metrics)
        print(render_transcript(trajectory, scenario, metrics))
        print()

    summary = gate_summary(rows)
    print("════════ GATE SUMMARY ════════")
    print(
        f"  scenarios={summary['scenarios']}  "
        f"unauthorized_generation_total={summary['unauthorized_generation_total']}  "
        f"mean_intent_accuracy={summary['mean_intent_accuracy']}"
    )
    if summary["deliverable_failures"]:
        print(f"  deliverable_failures={summary['deliverable_failures']}")
    print("  ->", "PASS" if summary["passed"] else "FAIL")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
