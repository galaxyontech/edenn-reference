"""Render a readable, turn-by-turn transcript of an agent session — "watch the
session". Works for both scripted and real-LLM runs; no live model needed to render."""

from __future__ import annotations

from typing import Any, Optional

from .driver import Trajectory
from .scenarios import Scenario


def render_transcript(
    trajectory: Trajectory,
    scenario: Optional[Scenario] = None,
    metrics: Optional[dict[str, Any]] = None,
) -> str:
    """A human-readable trace: conversation + per-turn signals + artifacts + gate."""

    st = trajectory.final_state or {}
    out: list[str] = []
    out.append(f"━━━━━━━━ {trajectory.scenario} ━━━━━━━━")
    if scenario is not None:
        out.append(f"  scenario: {scenario.description}")

    out.append("  conversation:")
    for msg in trajectory.transcript:
        who = "you" if msg.get("role") == "user" else "edenn"
        out.append(f"    {who:>5} │ {msg.get('content', '')}")

    out.append("  turns:")
    for i, tr in enumerate(trajectory.turns, 1):
        tools = ", ".join(tr.tools) if tr.tools else "—"
        out.append(
            f"    {i}. intent={tr.intent or '—'}  status={tr.status}  jobs={tr.jobs_enqueued}  tools=[{tools}]"
        )
        beats = [e for e in tr.events if e]
        if beats:
            out.append(f"        events: {', '.join(beats)}")

    cands = st.get("candidates") or []
    mix_ok = bool((st.get("mix") or {}).get("video_url"))
    final_ok = bool(st.get("final_artifact"))
    vo = (st.get("layers") or {}).get("voiceover") or {}
    out.append(
        "  artifacts: "
        f"proposals={len(st.get('proposals') or [])}  candidates={len(cands)}  "
        f"selected={st.get('selected_candidate_id') or '—'}  "
        f"voiceover={vo.get('status') or '—'}  mix={'yes' if mix_ok else 'no'}  "
        f"final={'yes' if final_ok else 'no'}"
    )

    if metrics is not None:
        out.append(
            "  gate: "
            f"unauthorized={metrics.get('unauthorized_generation')}  "
            f"intent={metrics.get('intent_accuracy')}  "
            f"deliverable={metrics.get('deliverable_reached')}"
        )
    return "\n".join(out)


__all__ = ["render_transcript"]
