"""
Beyond-visual sound proposers.

Each proposer emits `SfxSuggestion`s (ghost events with rationale) — never
committed events. Accepting a suggestion is an explicit editor op, keeping the
user in charge of every designed sound.

- Edit-geometry: deterministic, frame-accurate — scene cuts become styled
  transition candidates. No ML uncertainty.
- Narrative: LLM proposes offscreen/emotional sounds from the scene summary
  plus the user's direction, density-capped with a rationale per suggestion.
- Engine-mined: onsets heard in a video-native engine's track that our event
  list does not cover become review suggestions.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List

from EdennCode.Util.MediaUtils import detect_scene_cuts
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.VideoSoundEffectDataModel.datamodel import (
    SfxProject,
    SfxSuggestion,
)

logger = logging.getLogger(__name__)

# Style presets: prompt palette + max suggestions per minute of video.
STYLE_PRESETS: Dict[str, Dict] = {
    "clean": {"density_per_min": 0, "palette": []},
    "vlog": {
        "density_per_min": 6,
        "palette": [
            "short airy whoosh transition",
            "soft swish transition",
            "subtle pop transition accent",
        ],
    },
    "cinematic": {
        "density_per_min": 4,
        "palette": [
            "deep cinematic whoosh transition",
            "low riser swelling into the next shot",
            "soft cinematic boom accent",
        ],
    },
    "meme": {
        "density_per_min": 10,
        "palette": [
            "quick cartoon slide whistle transition",
            "snappy vinyl scratch accent",
            "fast comedic whoosh",
        ],
    },
}

TRANSITION_SUGGESTION_DURATION_S = 0.7
NARRATIVE_MAX_PER_30S = 2


def propose_stylistic(
    project: SfxProject,
    *,
    style: str = "cinematic",
    min_gap_s: float = 2.0,
) -> List[SfxSuggestion]:
    """Scene cuts → styled transition suggestions. Deterministic and frame-accurate."""
    preset = STYLE_PRESETS.get(style)
    if preset is None:
        raise ValueError(f"unknown style {style!r}; options: {sorted(STYLE_PRESETS)}")
    if not preset["palette"]:
        return []

    # detect_scene_cuts returns sorted cut timestamps (seconds); keep interior
    # cuts only, then respect density + spacing caps.
    cut_times = [
        float(t)
        for t in detect_scene_cuts(Path(project.video_path))
        if 0.5 < float(t) < project.video_duration - 0.5
    ]
    # Short videos still get at least one styled transition (unless style=clean).
    max_count = max(1, round(preset["density_per_min"] * project.video_duration / 60.0))
    existing = {round(e.effective_start, 1) for e in project.events}
    suggestions: List[SfxSuggestion] = []
    last_time = -min_gap_s
    for idx, cut_time in enumerate(cut_times):
        if len(suggestions) >= max_count:
            break
        if cut_time - last_time < min_gap_s or round(cut_time, 1) in existing:
            continue
        prompt = preset["palette"][idx % len(preset["palette"])]
        suggestion = SfxSuggestion(
            suggestion_id=project.next_suggestion_id(),
            origin="stylistic",
            start_time=round(cut_time - 0.15, 3),  # whooshes lead the cut slightly
            end_time=round(cut_time - 0.15 + TRANSITION_SUGGESTION_DURATION_S, 3),
            description=f"scene cut at {cut_time:.2f}s",
            sound_prompt=prompt,
            rationale=f"detected scene cut at {cut_time:.2f}s ({style} style)",
            style=style,
            event_type="TRANSITION",
            timing_authority="cut_snap",
        )
        project.suggestions.append(suggestion)
        suggestions.append(suggestion)
        last_time = cut_time
    return suggestions


NARRATIVE_SUGGESTION_SCHEMA = {
    "name": "sfx_narrative_suggestions",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "suggestions": {
                "type": "array",
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "properties": {
                        "origin": {"type": "string", "enum": ["offscreen", "narrative"]},
                        "start_timestamp": {"type": "number", "minimum": 0},
                        "end_timestamp": {"type": "number", "minimum": 0},
                        "description": {"type": "string"},
                        "sound_prompt": {"type": "string"},
                        "rationale": {
                            "type": "string",
                            "description": "Why this sound serves the scene — shown to the user.",
                        },
                    },
                    "required": [
                        "origin",
                        "start_timestamp",
                        "end_timestamp",
                        "description",
                        "sound_prompt",
                        "rationale",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["suggestions"],
        "additionalProperties": False,
    },
}


async def propose_narrative(
    project: SfxProject,
    *,
    direction: str = "",
    llm_client=None,
) -> List[SfxSuggestion]:
    """
    LLM-proposed offscreen/emotional sounds. Conservative by design: hard
    density cap, rationale required, nothing auto-committed.
    """
    if llm_client is None:
        from EdennCode.Util.MediaUtils.pipeline_util import build_azure_client

        llm_client = build_azure_client()

    max_suggestions = max(1, int(NARRATIVE_MAX_PER_30S * project.video_duration / 30.0))
    events_text = "\n".join(
        f"- {e.effective_start:.1f}-{e.end_time:.1f}s [{e.origin}] {e.event_description}"
        for e in project.events
    ) or "(none)"
    instruction = (
        "You are a film sound designer proposing sounds BEYOND what is visible: "
        "offscreen world sounds and narrative/emotional sounds. The video already has the "
        "events listed below — do not duplicate them. Propose at most "
        f"{max_suggestions} suggestions for a {project.video_duration:.1f}s video. "
        "Prefer few, purposeful sounds over coverage; every suggestion needs a rationale "
        "the creator will read. Timestamps must lie within the video."
    )
    user_text = (
        f"Scene summary: {project.scene_summary or '(none)'}\n"
        f"Creator direction: {direction or '(none given)'}\n"
        f"Existing sounds:\n{events_text}"
    )
    payload, _usage = await llm_client.complete_messages(
        messages=[
            {"role": "system", "content": [{"type": "text", "text": instruction}]},
            {"role": "user", "content": [{"type": "text", "text": user_text}]},
        ],
        json_schema=NARRATIVE_SUGGESTION_SCHEMA,
        max_tokens=1200,
    )
    suggestions: List[SfxSuggestion] = []
    for item in (payload.get("suggestions") or [])[:max_suggestions]:
        start = max(0.0, min(float(item["start_timestamp"]), project.video_duration))
        end = max(start + 0.3, min(float(item["end_timestamp"]), project.video_duration))
        suggestion = SfxSuggestion(
            suggestion_id=project.next_suggestion_id(),
            origin=str(item["origin"]),
            start_time=round(start, 3),
            end_time=round(end, 3),
            description=str(item["description"]).strip(),
            sound_prompt=str(item["sound_prompt"]).strip(),
            rationale=str(item["rationale"]).strip(),
            timing_authority="design",
        )
        project.suggestions.append(suggestion)
        suggestions.append(suggestion)
    return suggestions


def propose_from_engine_track(
    project: SfxProject,
    *,
    onset_times: List[float],
    tolerance_s: float = 0.5,
    max_suggestions: int = 5,
) -> List[SfxSuggestion]:
    """
    Cross-check: onsets heard in a video-native engine's track with no event
    of ours nearby become review suggestions ("the engine heard something").
    """
    covered = [e.effective_start for e in project.events if not e.muted]
    suggestions: List[SfxSuggestion] = []
    for onset in sorted(onset_times):
        if len(suggestions) >= max_suggestions:
            break
        if any(abs(onset - t) <= tolerance_s for t in covered):
            continue
        if not 0.0 <= onset <= project.video_duration:
            continue
        suggestion = SfxSuggestion(
            suggestion_id=project.next_suggestion_id(),
            origin="offscreen",
            start_time=round(onset, 3),
            end_time=round(min(onset + 1.0, project.video_duration), 3),
            description=f"engine heard a sound at {onset:.2f}s that has no event",
            sound_prompt="",
            rationale="cross-check against a video-native engine's track; review the moment",
            timing_authority="design",
        )
        project.suggestions.append(suggestion)
        suggestions.append(suggestion)
        covered.append(onset)
    return suggestions
