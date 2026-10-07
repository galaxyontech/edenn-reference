"""Beyond-visual SFX suggestions for the agentic console.

Bridges the hybrid sound-design loop's proposers
(:mod:`EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.proposers`) into the
console's lightweight SFX layer: the same style palettes, density caps and
spacing rules propose GHOST rows on the plan card. Accepting one is an
explicit, free plan edit — nothing in this module spends or renders, matching
the proposers' own never-auto-commit contract.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.proposers import (
    STYLE_PRESETS,
    TRANSITION_SUGGESTION_DURATION_S,
    propose_narrative,
    propose_stylistic,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.VideoSoundEffectDataModel.datamodel import (
    SfxSuggestion,
)

logger = logging.getLogger(__name__)

_MIN_GAP_S = 2.0


class _LiteEvent:
    """The attributes the proposers read from a committed event, sourced from a
    console plan row."""

    def __init__(self, ev: dict[str, Any]) -> None:
        self.effective_start = float(ev.get("start_s") or 0.0)
        self.end_time = self.effective_start + 0.8
        self.origin = "visual"
        self.event_description = str(ev.get("label") or ev.get("prompt") or "effect")


class _LiteProject:
    """Duck-typed stand-in for SfxProject: exactly what the proposers touch."""

    def __init__(
        self,
        *,
        video_path: str,
        duration: float,
        scene_summary: str,
        plan_events: list[dict[str, Any]],
        existing_count: int,
    ) -> None:
        self.video_path = video_path
        self.video_duration = float(duration or 0.0)
        self.scene_summary = scene_summary
        self.events = [_LiteEvent(e) for e in plan_events]
        self.suggestions: list[SfxSuggestion] = []
        self._n = int(existing_count)

    def next_suggestion_id(self) -> str:
        self._n += 1
        return f"sug_{self._n:03d}"


def _stylistic_from_scenes(
    project: _LiteProject, scenes: list[dict[str, Any]], style: str
) -> list[SfxSuggestion]:
    """Observation-scene fallback: the same palette/density/spacing rules as
    ``propose_stylistic``, with cut times taken from the analysis instead of
    ffmpeg scene detection — API pods don't hold the video file locally, but
    the observation's scene boundaries ARE the detected cuts."""

    preset = STYLE_PRESETS.get(style) or STYLE_PRESETS["cinematic"]
    if not preset["palette"]:
        return []
    # The live analyzer emits start_timestamp; fakes/mocks emit start_s.
    cut_times = [
        t
        for t in (
            float(s.get("start_s") or s.get("start_timestamp") or 0.0)
            for s in scenes[1:]
        )
        if 0.5 < t < project.video_duration - 0.5
    ]
    max_count = max(1, round(preset["density_per_min"] * project.video_duration / 60.0))
    existing = {round(e.effective_start, 1) for e in project.events}
    out: list[SfxSuggestion] = []
    last = -_MIN_GAP_S
    for idx, cut_time in enumerate(cut_times):
        if len(out) >= max_count:
            break
        if cut_time - last < _MIN_GAP_S or round(cut_time, 1) in existing:
            continue
        prompt = preset["palette"][idx % len(preset["palette"])]
        out.append(
            SfxSuggestion(
                suggestion_id=project.next_suggestion_id(),
                origin="stylistic",
                start_time=round(cut_time - 0.15, 3),  # whooshes lead the cut
                end_time=round(cut_time - 0.15 + TRANSITION_SUGGESTION_DURATION_S, 3),
                description=f"scene cut at {cut_time:.2f}s",
                sound_prompt=prompt,
                rationale=f"detected scene cut at {cut_time:.2f}s ({style} style)",
                style=style,
                event_type="TRANSITION",
                timing_authority="cut_snap",
            )
        )
        last = cut_time
    return out


async def propose_console_suggestions(
    *,
    kind: str,
    state: dict[str, Any],
    source_video_path: Optional[str] = None,
    direction: str = "",
    style: str = "cinematic",
) -> list[dict[str, Any]]:
    """Fresh ghost suggestions for the session's SFX plan (dicts, pending).

    ``stylistic`` is deterministic and free; ``narrative`` runs the proposer's
    own LLM call (rationale required, hard density cap). Already-stored
    suggestions are de-duplicated by (start, prompt); rejected ones stay
    rejected — re-proposing them would nag.
    """

    obs = state.get("observation") or {}
    layers = state.get("layers") or {}
    sfx = layers.get("sfx") if isinstance(layers.get("sfx"), dict) else {}
    stored = (sfx or {}).get("suggestions") or []
    project = _LiteProject(
        video_path=str(source_video_path or ""),
        duration=obs.get("duration_s") or 0.0,
        scene_summary=str(obs.get("video_summary") or obs.get("video_description") or ""),
        plan_events=(sfx or {}).get("events") or [],
        existing_count=len(stored),
    )
    if kind == "stylistic":
        if source_video_path and Path(source_video_path).exists():
            try:
                suggestions = propose_stylistic(project, style=style, min_gap_s=_MIN_GAP_S)
            except Exception:  # noqa: BLE001 - exotic files; the analysis still has cuts
                logger.exception("stylistic proposer on file failed — falling back to scenes")
                suggestions = _stylistic_from_scenes(project, obs.get("scenes") or [], style)
        else:
            suggestions = _stylistic_from_scenes(project, obs.get("scenes") or [], style)
    elif kind == "narrative":
        suggestions = await propose_narrative(project, direction=direction)
    else:
        raise ValueError(f"Unknown suggestion kind: {kind} (stylistic|narrative)")

    seen = {
        (round(float(s.get("start_time") or 0.0), 1), s.get("sound_prompt"))
        for s in stored
    }
    return [
        s.to_dict()
        for s in suggestions
        if (round(s.start_time, 1), s.sound_prompt) not in seen
    ]
