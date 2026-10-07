"""Recompose audio layer: generated music, generated narration, and respect
for narration the FOOTAGE already carries.

Three concerns (owner directive 2026-07-15):
- ``music: generate`` — no track provided: build a SectionPlan from the
  visual passages + the asset's analyzed music_prompt and generate a real
  track through MusicGenerationCore (same providers as the video-music
  pipeline), then cut to ITS beat grid.
- ``narration: generate`` — one bounded LLM call writes a script timed to
  the window from the passage arcs; TTS through the same synthesizer the
  voiceover worker uses; mixed with ducking by the assembly.
- source speech — segments whose original audio carries narration/dialogue
  are measured (signals.measure_speech) and surfaced to the planner; when
  ``source_audio="duck"`` the original bed survives under the music instead
  of being stripped.

All model/provider clients are injectable; defaults resolve lazily so
importing this module stays cheap.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .domain import MusicSheet, RecomposePlan, new_id

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ narration
NARRATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"script": {"type": "string"}},
    "required": ["script"],
    "additionalProperties": False,
}

NARRATION_SYSTEM = """You write short voice-over scripts for edited videos.
You get the edit's passages (each with a semantic focus and editorial intent)
and a total duration. Write narration that lands those beats in order.
Rules: roughly 2.2 words per second of duration — never more; plain warm
prose, no headings or stage directions; do not describe the music; land the
final sentence before the end. Respond with JSON only."""


async def narration_script_from_plan(
    llm_client: Any,
    plan: RecomposePlan,
    window_s: float,
    *,
    max_tokens: int = 800,
) -> str:
    import json as _json

    word_budget = int(window_s * 2.2)
    payload = {
        "duration_s": round(window_s, 1),
        "word_budget": word_budget,
        "hypothesis": plan.hypothesis,
        "passages": [
            {"index": p.index, "focus": p.semantic_focus, "intent": p.arc_note}
            for p in plan.passages if p.slot_indices
        ],
    }
    decision, _usage = await llm_client.complete_messages(
        [
            {"role": "system", "content": NARRATION_SYSTEM},
            {"role": "user", "content": _json.dumps(payload, ensure_ascii=False)},
        ],
        json_schema={"name": "narration_script", "strict": True, "schema": NARRATION_SCHEMA},
        max_tokens=max_tokens,
    )
    script = str(decision.get("script") or "").strip()
    if not script:
        raise ValueError("narration model returned an empty script")
    words = script.split()
    if len(words) > int(word_budget * 1.3):  # keep TTS inside the window
        script = " ".join(words[: int(word_budget * 1.2)]).rstrip(",;:") + "."
    return script


TtsFn = Callable[..., Awaitable[Path]]


async def synthesize_narration(
    script: str,
    out_path: Path,
    *,
    voice: str = "nova",
    tone: str = "calm, warm, unhurried",
    speed: float = 1.0,
    tts_fn: Optional[TtsFn] = None,
) -> Path:
    """TTS via the same synthesizer the voiceover worker uses (injectable)."""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if tts_fn is not None:
        return await tts_fn(script=script, voice=voice, instructions=tone,
                            speed=speed, out_path=out_path)

    from EdennCode.ModelFactory.VoiceOverModelFactory.model_gateway_base_model import (
        AzureModelGatewayTTS4OMini,
    )

    tts = AzureModelGatewayTTS4OMini()
    return await asyncio.to_thread(
        tts.send_request_streaming, tone, speed, script, voice, out_path
    )


# ---------------------------------------------------------------- music (gen)
def _section_plan_from_passages(
    plan: RecomposePlan,
    observation: Optional[dict[str, Any]],
    duration_s: float,
) -> Any:
    from EdennCode.MusicGenerationCore.models import (
        MusicSection,
        NarrativeCue,
        SectionPlan,
    )

    mp = (observation or {}).get("music_prompt") or {}
    passages = [p for p in plan.passages if p.slot_indices] or plan.passages
    total = sum(max(p.end_s - p.start_s, 0.0) for p in passages) or duration_s
    cues, sections = [], []
    for p in passages:
        dur = max(p.end_s - p.start_s, 1.0) * (duration_s / total)
        slot_energies = [
            s.spec.energy for s in plan.slots if s.spec.passage_index == p.index
        ] or [0.5]
        cue = NarrativeCue(
            cue_id=f"cue_{p.index}",
            label=p.semantic_focus or f"passage {p.index}",
            role="section",
            target_duration_s=round(dur, 2),
            emotion=p.semantic_focus,
            description=p.arc_note,
        )
        cues.append(cue)
        sections.append(MusicSection(
            section_id=f"sec_{p.index}",
            label=p.semantic_focus or f"passage {p.index}",
            target_duration_s=round(dur, 2),
            objective=p.arc_note or "carry the visual passage",
            energy_start=round(float(slot_energies[0]), 3),
            energy_end=round(float(slot_energies[-1]), 3),
            cue_ids=[cue.cue_id],
            instrumentation_focus=[str(i) for i in (mp.get("instruments") or [])][:6],
        ))
    return SectionPlan(
        summary=plan.hypothesis or "recomposed edit",
        total_duration_s=round(duration_s, 2),
        overall_mood=str(mp.get("global_mood") or "cinematic, cohesive"),
        target_bpm=mp.get("tempo_bpm"),
        primary_instruments=[str(i) for i in (mp.get("instruments") or [])][:8],
        cues=cues,
        sections=sections,
        music_prompt_summary=str(mp.get("global_music_prompt") or "")[:500],
    )


async def generate_music_for_plan(
    plan: RecomposePlan,
    observation: Optional[dict[str, Any]],
    duration_s: float,
    out_dir: Path,
    *,
    modelspec: str = "edenn_enhanced",
    service: Any = None,
    water_mark: bool = False,
) -> Path:
    """Generate a real track shaped by the visual passages. Returns the audio
    path; the caller re-builds the MusicSheet from it and re-plans the cuts
    to the ACTUAL beat grid (music leads, always — DESIGN.md)."""

    from EdennCode.MusicGenerationCore.models import (
        MusicGenerationOptions,
        MusicGenerationRequest,
        normalize_modelspec,
    )

    if service is None:
        from EdennCode.MusicGenerationCore.provider_registry import (
            build_default_music_generation_service,
        )

        service = build_default_music_generation_service()

    out_dir.mkdir(parents=True, exist_ok=True)
    request = MusicGenerationRequest(
        request_id=new_id("recompose_music"),
        modelspec=normalize_modelspec(modelspec),
        section_plan=_section_plan_from_passages(plan, observation, duration_s),
        options=MusicGenerationOptions(
            include_vocals=False,
            max_variants=1,
            require_word_timestamps=False,
            water_mark=water_mark,
        ),
        output_dir=out_dir,
    )
    result = await service.generate(request)
    path = Path(result.primary.audio_path)
    logger.info("generated track: %s (%.1fs, %s)", path.name,
                result.primary.duration_s or -1, result.used_modelspec)
    return path
