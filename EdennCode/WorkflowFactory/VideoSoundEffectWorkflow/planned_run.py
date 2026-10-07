"""Render the sound plan a person approved, instead of re-deciding it.

The end-to-end workflow spots its own events, which is the right behaviour when
nobody has said what they want. But the agentic console asks the user to approve
a specific plan — these effects, at these moments — and then handed that plan to
the workflow as a sentence of prose and let it spot again from scratch. The
timestamps on the card and the hits in the render were related only by
coincidence, and the plan card was, in effect, a suggestion box.

This module is the other mode: the plan IS the spotting. It reuses the workflow's
own steps 5-7 verbatim (per-event generation → optional ambience bed → shared
render/mux) and simply never constructs the analysis stage, so a planned render
also costs nothing in video-understanding calls.

Two deliberate differences from the analysed path:

* **User timing wins.** The timing-refinement stage snaps events to nearby motion
  by event type and duration; it does not read ``timing_authority``. Rows a
  person placed are therefore filtered out of refinement at the call site rather
  than exempted inside the stage, so the editor's explicit re-snap keeps working.
* **The bed prompt comes from the plan.** The analysed path takes its ambience
  description from the analysis it just ran. Here there is no analysis, so a
  planned bed carries the words the user approved.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.Stages.PreprocessStage.preprocess_stage import (
    PreprocessStage,
    PreprocessStageInput,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.Stages.SoundEffectGenerationStage.sound_effect_generation_stage import (
    SoundEffectGenerationStage,
    SoundEffectGenerationStageInput,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.Stages.TimingRefinementStage.timing_refinement_stage import (
    TimingRefinementStage,
    TimingRefinementStageInput,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.VideoSoundEffectDataModel.datamodel import (
    AmbienceBed,
    MixSettings,
    SfxProject,
    SfxVariant,
    SoundFXEvent,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.rendering import (
    RenderResult,
    render_project,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.video_sound_effect_workflow import (
    VideoSfxWorkflowOptions,
)

logger = logging.getLogger(__name__)

# A planned row carries a moment, not a duration. Long enough to be a sound
# rather than a click, short enough not to smear across the next cut; the
# generator's own floor is 0.2s, which is unusable.
DEFAULT_EVENT_DURATION_S = 1.2

# The shortest an effect may end up after clamping to the picture. Below this it
# is a click rather than a sound, which is how a hit placed on the final frame
# used to disappear.
MIN_AUDIBLE_EVENT_S = 0.35

# Rows a person placed. Anything else may be snapped to nearby motion.
USER_TIMING = "user"
# The authorities that ALLOW a moment to be moved. An allow-list, so anything
# unrecognised keeps the user's placement instead of being handed to the detector.
SNAPPABLE_AUTHORITIES = frozenset({"motion_snap", "cut_snap"})


@dataclass
class PlannedSfxInput:
    video_path: str
    uploaded_public_facing_url: str
    events: List[dict]
    ambience_prompt: str = ""
    run_dir: Optional[str] = None
    options: VideoSfxWorkflowOptions = field(default_factory=VideoSfxWorkflowOptions)
    #: event_id -> audio already rendered for it. Those effects are kept rather
    #: than generated again, which is what makes fixing ONE hit cost one
    #: generation instead of a whole bed's worth.
    reuse_event_audio: dict[str, str] = field(default_factory=dict)


@dataclass
class PlannedSfxOutput:
    project: SfxProject
    video_duration: float
    generated_sound_events: List[SoundFXEvent]
    mixed_audio_path: Path
    final_video_path: Path
    latency_seconds: float
    #: Whether a bed was ATTEMPTED — not whether one survived. A bed that was
    #: paid for and then failed sets ``enabled = False`` on its own object, so
    #: reading that flag afterwards reports a paid render as no render at all.
    ambience_attempted: bool = False
    #: The route the bed ACTUALLY ran on, after any runtime fallback: "text"
    #: means the engine was given a description of the video, "video_native"
    #: means it watched the footage. The two are different products at
    #: different prices, and the difference was visible only in a log line.
    ambience_route: Optional[str] = None
    #: Why the video-native route was not used, when it was not. Empty when it
    #: ran, or when nothing asked for it.
    ambience_route_reason: str = ""


def plan_rows_to_events(rows: List[dict], *, video_duration: float) -> List[SoundFXEvent]:
    """Turn approved plan rows into the events the generator renders.

    The plan's ``prompt`` becomes ``sound_prompt`` and its ``label`` becomes the
    description: the generator prefers the prompt and falls back to the
    description, so mapping both to the description would synthesise from
    "Effect 2".
    """

    events: List[SoundFXEvent] = []
    used_ids: set[str] = set()
    for index, row in enumerate(rows or []):
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or f"Effect {index + 1}").strip()
        prompt = str(row.get("prompt") or label).strip()
        try:
            start = max(0.0, float(row.get("start_s") or 0.0))
        except (TypeError, ValueError):
            start = 0.0

        duration = DEFAULT_EVENT_DURATION_S
        for key in ("duration_s", "duration"):
            if row.get(key) is not None:
                try:
                    duration = max(0.05, float(row[key]))
                except (TypeError, ValueError):
                    duration = DEFAULT_EVENT_DURATION_S
                break
        if not math.isfinite(duration):
            duration = DEFAULT_EVENT_DURATION_S

        if video_duration:
            # Guarantee the hit is long enough to be HEARD, without moving one
            # that already fits. The plan clamps its own starts to the duration
            # inclusively, so "an impact on the last frame" arrives exactly at
            # the end; clamping against a hair's-width epsilon turned that into
            # a 0.05s click — the very thing a default duration exists to
            # prevent. An event that simply runs past the end keeps its start
            # and gets a shorter tail, which is what the picture allows.
            start = min(start, max(0.0, video_duration - MIN_AUDIBLE_EVENT_S))
        end = start + duration
        if video_duration:
            end = min(end, video_duration)

        # Ids address events for refinement and for the render manifest, so a
        # duplicate makes two rows the same row: one moment silently replaced by
        # another, generated twice, and mutated from two coroutines at once.
        event_id = str(row.get("id") or f"planned_{index + 1:03d}")
        if event_id in used_ids:
            event_id = f"{event_id}_{index + 1:03d}"
        used_ids.add(event_id)

        events.append(
            SoundFXEvent(
                event_id=event_id,
                start_time=start,
                end_time=end,
                event_description=label,
                sound_event_local_path="",
                confidence=1.0,
                event_type=str(row.get("event_type") or ""),
                sound_prompt=prompt,
                # Provenance is the whole point: these came from a person, and
                # every downstream decision that asks "who chose this?" — timing
                # refinement here, ownership in the console — reads it.
                source="user",
                origin="user",
                # Fail CLOSED: only a recognised snapping authority may move a
                # moment. A typo or an unexpected value must leave the user's
                # placement alone, not hand it to the motion detector.
                timing_authority=(
                    str(row.get("timing_authority"))
                    if str(row.get("timing_authority") or "") in SNAPPABLE_AUTHORITIES
                    else USER_TIMING
                ),
            )
        )
    return events


class PlannedSfxRun:
    """Steps 5-7 of the end-to-end workflow, with the plan as the spotting."""

    def __init__(
        self,
        *,
        preprocess_stage: Optional[PreprocessStage] = None,
        timing_refinement_stage: Optional[TimingRefinementStage] = None,
        sound_effect_generation_stage: Optional[SoundEffectGenerationStage] = None,
    ) -> None:
        self._preprocess_stage = preprocess_stage
        self._timing_refinement_stage = timing_refinement_stage
        self._sound_effect_generation_stage = sound_effect_generation_stage

    @property
    def preprocess_stage(self) -> PreprocessStage:
        if self._preprocess_stage is None:
            self._preprocess_stage = PreprocessStage()
        return self._preprocess_stage

    @property
    def timing_refinement_stage(self) -> TimingRefinementStage:
        if self._timing_refinement_stage is None:
            self._timing_refinement_stage = TimingRefinementStage()
        return self._timing_refinement_stage

    @property
    def sound_effect_generation_stage(self) -> SoundEffectGenerationStage:
        if self._sound_effect_generation_stage is None:
            self._sound_effect_generation_stage = SoundEffectGenerationStage()
        return self._sound_effect_generation_stage

    async def execute(self, stage_input: PlannedSfxInput) -> PlannedSfxOutput:
        start_ts = time.time()
        options = stage_input.options
        video_path = Path(stage_input.video_path).expanduser().resolve()
        run_dir = Path(
            stage_input.run_dir
            or Path("outputs") / "video_sfx_runs" / time.strftime("%Y%m%d_%H%M%S")
        )
        run_dir.mkdir(parents=True, exist_ok=True)

        preprocess_output = await self.preprocess_stage.run(
            PreprocessStageInput(video_path=video_path)
        )
        video_duration = float(preprocess_output.metadata.duration)

        events = plan_rows_to_events(stage_input.events, video_duration=video_duration)
        logger.info(
            "Planned SFX run: %d approved event(s), ambience=%r, %.2fs video",
            len(events), (stage_input.ambience_prompt or "")[:60], video_duration,
        )

        # Only rows that did NOT come from a person may be snapped. The stage
        # decides by event type and duration and never reads timing_authority, so
        # filtering here is what keeps a user-placed hit where they put it —
        # while leaving the editor's explicit re-snap path untouched.
        snappable_at = [
            index for index, event in enumerate(events)
            if event.timing_authority in SNAPPABLE_AUTHORITIES
        ]
        if options.enable_timing_refinement and snappable_at:
            refinement = await self.timing_refinement_stage.run(
                TimingRefinementStageInput(
                    video_path=video_path,
                    events=[events[index] for index in snappable_at],
                    video_duration=video_duration,
                )
            )
            # Write refinements back POSITIONALLY. Matching by id would let a
            # duplicate id reach across the exclusion boundary and replace a
            # user-placed moment with an engine-snapped one.
            for slot, refined_event in zip(snappable_at, refinement.events):
                events[slot] = refined_event

        # Effects the caller already has audio for. Fixing one hit in a bed of
        # twelve used to re-synthesize all twelve — the user pays again for
        # eleven sounds they were happy with, and gets subtly different ones,
        # because each effect is its own paid generation. The mix is cheap local
        # ffmpeg; the synthesis is what costs.
        reuse = {
            str(event_id): Path(path)
            for event_id, path in (stage_input.reuse_event_audio or {}).items()
            if path and Path(path).exists()
        }
        pending: List[SoundFXEvent] = []
        pending_slots: List[int] = []
        for index, event in enumerate(events):
            kept = reuse.get(str(event.event_id))
            if kept is None:
                pending.append(event)
                pending_slots.append(index)
                continue
            event.variants = [SfxVariant(path=str(kept), prompt=event.sound_prompt)]
            event.selected_variant = 0

        if reuse:
            logger.info(
                "Planned SFX run: keeping %d effect(s) already rendered, "
                "generating %d", len(events) - len(pending), len(pending),
            )

        if pending:
            generation_output = await self.sound_effect_generation_stage.run(
                SoundEffectGenerationStageInput(
                    list_of_generation_packages=pending,
                    output_directory=run_dir / "sfx_assets",
                    sample_rate=options.sample_rate,
                    num_variants=options.num_variants,
                )
            )
            # Positionally, so a kept effect is never replaced by a generated
            # one — the same reasoning as the timing write-back above.
            for slot, generated in zip(pending_slots, generation_output.generated_sound_events):
                events[slot] = generated

        ambience: Optional[AmbienceBed] = None
        ambience_attempted = False
        ambience_route: Optional[str] = None
        ambience_route_reason = ""
        bed_prompt = (stage_input.ambience_prompt or "").strip()
        if options.enable_ambience and bed_prompt:
            generation_stage = self.sound_effect_generation_stage
            bed_route = options.bed_route
            if bed_route == "auto":
                bed_route = (
                    "video_native"
                    if generation_stage.video_conditioned_model is not None
                    and video_duration <= 60.0
                    else "text"
                )
            ambience = AmbienceBed(
                prompt=bed_prompt, gain_db=options.ambience_gain_db, route=bed_route
            )
            # Recorded on ATTEMPT, before the try below: a bed that was bought
            # and then failed must not read as a bed nobody asked for.
            ambience_attempted = True
            if bed_route != "video_native":
                ambience_route_reason = (
                    "no video-conditioned engine configured"
                    if generation_stage.video_conditioned_model is None
                    else f"clip is {video_duration:.0f}s, past the video-native limit"
                )
            elif not (stage_input.uploaded_public_facing_url or "").strip():
                # The engine FETCHES the clip, so a deployment with no
                # reachable URL cannot use it however good its key is.
                ambience_route_reason = "the clip has no URL the engine can fetch"
            bed_duration = (
                video_duration if bed_route == "video_native" else min(video_duration, 20.0)
            )
            try:
                ambience = await generation_stage.generate_ambience(
                    ambience=ambience,
                    output_directory=run_dir / "sfx_assets",
                    duration_seconds=bed_duration,
                    sample_rate=options.sample_rate,
                    video_url=stage_input.uploaded_public_facing_url,
                )
                ambience_route = getattr(ambience, "route", bed_route)
            except Exception:
                # The bed is an optional layer; a provider hiccup must not orphan
                # the event clips that were already paid for.
                logger.exception("Ambience generation failed; continuing without a bed.")
                ambience.enabled = False
                ambience_route = getattr(ambience, "route", bed_route)
                ambience_route_reason = ambience_route_reason or "the bed render failed"

        project = SfxProject(
            project_dir=str(run_dir),
            video_path=str(video_path),
            video_url=stage_input.uploaded_public_facing_url,
            video_duration=video_duration,
            user_prompt="",
            scene_summary="",
            events=events,
            ambience=ambience,
            mix=MixSettings(
                preserve_original_audio=options.preserve_original_audio,
                sfx_master_gain_db=options.sfx_master_gain_db,
                sample_rate=options.sample_rate,
            ),
        )
        render_result: RenderResult = render_project(
            project, render_sfx_only_debug=options.render_sfx_only_debug
        )
        project.save()

        return PlannedSfxOutput(
            project=project,
            video_duration=video_duration,
            generated_sound_events=events,
            mixed_audio_path=render_result.mixed_audio_path,
            final_video_path=render_result.final_video_path,
            latency_seconds=time.time() - start_ts,
            ambience_attempted=ambience_attempted,
            ambience_route=ambience_route,
            ambience_route_reason=ambience_route_reason,
        )


def rendered_event_manifest(events: List[SoundFXEvent]) -> List[dict[str, Any]]:
    """What actually rendered, in the console's vocabulary.

    Without this the job result carries only URLs, so nothing downstream can tell
    whether the render honoured the plan — which is exactly the check the plan
    card exists to make possible.
    """

    manifest: List[dict[str, Any]] = []
    for event in events or []:
        manifest.append(
            {
                "id": event.event_id,
                "label": event.event_description,
                # What this effect was asked to SOUND like. A later re-render
                # reuses an effect by id, and ids are positional — so without
                # the text to compare against, an edited plan can hand the
                # sound from one moment to a different one under the same id.
                "prompt": event.sound_prompt or "",
                "start_s": round(float(event.effective_start), 3),
                "planned_start_s": round(float(event.start_time), 3),
                "rendered": bool(event.active_audio_path),
                # Kept so a later re-render can reuse this effect instead of
                # paying for it again. Server-side only; the console never
                # needs it and the egress scrub drops local paths.
                "audio_path": event.active_audio_path or "",
                "origin": event.origin,
                "timing_authority": event.timing_authority,
            }
        )
    return manifest


__all__ = [
    "DEFAULT_EVENT_DURATION_S",
    "PlannedSfxInput",
    "PlannedSfxOutput",
    "PlannedSfxRun",
    "plan_rows_to_events",
    "rendered_event_manifest",
]
