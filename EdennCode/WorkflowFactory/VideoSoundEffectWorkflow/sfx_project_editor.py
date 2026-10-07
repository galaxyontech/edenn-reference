"""
Iteration loop for video→SFX projects.

Loads the `SfxProject` a workflow run produced and applies edits without ever
re-running video analysis: regenerate one event, swap variants, retime (with
optional motion re-snap), add/remove events, adjust gains, mute layers, then
`render()` to get a new revision of the mixed video in seconds.

Edits are also drivable as data via `apply_ops`, so a chat/agent surface can
emit the same operations a UI would.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.Stages.SoundEffectGenerationStage.sound_effect_generation_stage import (
    SoundEffectGenerationStage,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.Stages.TimingRefinementStage.timing_refinement_stage import (
    TimingRefinementStage,
    TimingRefinementStageInput,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.VideoSoundEffectDataModel.datamodel import (
    AmbienceBed,
    SfxProject,
    SoundFXEvent,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.rendering import (
    RenderResult,
    render_project,
)

logger = logging.getLogger(__name__)


class SfxProjectEditor:
    def __init__(
        self,
        project: Union[SfxProject, Path, str],
        *,
        generation_stage: Optional[SoundEffectGenerationStage] = None,
        timing_refinement_stage: Optional[TimingRefinementStage] = None,
    ) -> None:
        self.project = (
            project if isinstance(project, SfxProject) else SfxProject.load(Path(project))
        )
        self._generation_stage = generation_stage
        self.timing_refinement_stage = timing_refinement_stage or TimingRefinementStage()

    @property
    def generation_stage(self) -> SoundEffectGenerationStage:
        # Lazy: pure re-mix/retime/mute sessions never need provider credentials.
        if self._generation_stage is None:
            self._generation_stage = SoundEffectGenerationStage()
        return self._generation_stage

    @property
    def assets_dir(self) -> Path:
        return Path(self.project.project_dir) / "sfx_assets"

    # ------------------------------------------------------------------ events

    async def regenerate_event(
        self,
        event_id: str,
        *,
        prompt: Optional[str] = None,
        num_variants: int = 1,
        route: Optional[str] = None,
    ) -> SoundFXEvent:
        """
        New variants for one event; the newest variant becomes active.

        route="video_native" sends the event's window of the source video to
        the video-conditioned engine — its take lands in the same variant
        picker as the text-route takes (cross-engine A/B per event).
        """
        event = self.project.event_by_id(event_id)
        if prompt is not None and prompt.strip():
            event.sound_prompt = prompt.strip()
        use_route = route or event.route
        paths = await self.generation_stage.generate_event_variants(
            event=event,
            output_directory=self.assets_dir,
            sample_rate=self.project.mix.sample_rate,
            num_variants=num_variants,
            route=use_route,
            video_url=self.project.video_url,
        )
        for path in paths:
            event.add_variant(str(path), select=True, route=use_route, prompt=event.generation_prompt)
        return event

    def select_variant(self, event_id: str, variant_index: int) -> SoundFXEvent:
        event = self.project.event_by_id(event_id)
        event.select_variant(variant_index)
        return event

    def retime_event(
        self,
        event_id: str,
        *,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
    ) -> SoundFXEvent:
        """Move/resize an event. Manual retiming clears any motion snap."""
        event = self.project.event_by_id(event_id)
        if start_time is not None:
            event.start_time = max(0.0, min(float(start_time), self.project.video_duration))
            event.refined_start_time = None
        if end_time is not None:
            event.end_time = max(event.start_time, min(float(end_time), self.project.video_duration))
        return event

    async def resnap_event_timing(self, event_id: str) -> SoundFXEvent:
        """Re-run motion-onset snapping for a single event."""
        event = self.project.event_by_id(event_id)
        await self.timing_refinement_stage.run(
            TimingRefinementStageInput(
                video_path=Path(self.project.video_path),
                events=[event],
                video_duration=self.project.video_duration,
            )
        )
        return event

    def set_event_gain(self, event_id: str, gain_db: float) -> SoundFXEvent:
        event = self.project.event_by_id(event_id)
        event.gain_db = float(gain_db)
        return event

    def set_event_muted(self, event_id: str, muted: bool) -> SoundFXEvent:
        event = self.project.event_by_id(event_id)
        event.muted = bool(muted)
        return event

    async def add_event(
        self,
        *,
        start_time: float,
        end_time: float,
        description: str,
        sound_prompt: str = "",
        event_type: str = "",
        origin: str = "user",
        timing_authority: str = "user",
        generate: bool = True,
        num_variants: int = 1,
    ) -> SoundFXEvent:
        event = SoundFXEvent(
            event_id=self.project.next_event_id(),
            start_time=max(0.0, float(start_time)),
            end_time=min(float(end_time), self.project.video_duration),
            event_description=description,
            sound_event_local_path="",
            confidence=1.0,
            event_type=event_type.upper(),
            sound_prompt=sound_prompt,
            source="user",
            origin=origin,
            timing_authority=timing_authority,
        )
        self.project.events.append(event)
        self.project.events.sort(key=lambda e: e.start_time)
        if generate:
            await self.regenerate_event(event.event_id, num_variants=num_variants)
        return event

    # ------------------------------------------------------------- suggestions

    def propose_stylistic(self, *, style: str = "cinematic") -> List[Dict[str, Any]]:
        """Ghost transition suggestions on scene cuts (deterministic)."""
        from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.proposers import propose_stylistic

        return [s.to_dict() for s in propose_stylistic(self.project, style=style)]

    async def propose_narrative(self, *, direction: str = "") -> List[Dict[str, Any]]:
        """LLM-proposed offscreen/emotional sounds with rationale (density-capped)."""
        from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.proposers import propose_narrative

        return [s.to_dict() for s in await propose_narrative(self.project, direction=direction)]

    async def accept_suggestion(
        self, suggestion_id: str, *, generate: bool = True, num_variants: int = 1
    ) -> SoundFXEvent:
        suggestion = self.project.suggestion_by_id(suggestion_id)
        if suggestion.status != "pending":
            raise ValueError(f"suggestion {suggestion_id} is already {suggestion.status}")
        event = await self.add_event(
            start_time=suggestion.start_time,
            end_time=suggestion.end_time,
            description=suggestion.description,
            sound_prompt=suggestion.sound_prompt or suggestion.description,
            event_type=suggestion.event_type,
            origin=suggestion.origin,
            timing_authority=suggestion.timing_authority,
            generate=generate,
            num_variants=num_variants,
        )
        suggestion.status = "accepted"
        return event

    def reject_suggestion(self, suggestion_id: str) -> None:
        suggestion = self.project.suggestion_by_id(suggestion_id)
        suggestion.status = "rejected"

    def remove_event(self, event_id: str) -> None:
        event = self.project.event_by_id(event_id)
        self.project.events.remove(event)

    # ---------------------------------------------------------------- ambience

    async def set_ambience(
        self,
        *,
        prompt: Optional[str] = None,
        enabled: Optional[bool] = None,
        gain_db: Optional[float] = None,
        duck_db: Optional[float] = None,
        route: Optional[str] = None,
        regenerate: bool = False,
    ) -> Optional[AmbienceBed]:
        ambience = self.project.ambience
        if ambience is None:
            if prompt is None and route is None:
                return None
            ambience = AmbienceBed(prompt=prompt or "")
            self.project.ambience = ambience
            regenerate = True
        if prompt is not None and prompt.strip() and prompt.strip() != ambience.prompt:
            ambience.prompt = prompt.strip()
            regenerate = True
        if route is not None and route != ambience.route:
            ambience.route = route
            regenerate = True
        if enabled is not None:
            ambience.enabled = bool(enabled)
        if gain_db is not None:
            ambience.gain_db = float(gain_db)
        if duck_db is not None:
            ambience.duck_db = float(duck_db)
        if regenerate and ambience.enabled:
            duration = self.project.video_duration
            if ambience.route != "video_native":
                duration = min(duration, 20.0)  # text loops get tiled by the renderer
            await self.generation_stage.generate_ambience(
                ambience=ambience,
                output_directory=self.assets_dir,
                duration_seconds=duration,
                sample_rate=self.project.mix.sample_rate,
                video_url=self.project.video_url,
            )
        return ambience

    # --------------------------------------------------------------------- mix

    def set_mix(
        self,
        *,
        preserve_original_audio: Optional[bool] = None,
        sfx_master_gain_db: Optional[float] = None,
    ) -> None:
        if preserve_original_audio is not None:
            self.project.mix.preserve_original_audio = bool(preserve_original_audio)
        if sfx_master_gain_db is not None:
            self.project.mix.sfx_master_gain_db = float(sfx_master_gain_db)

    # ------------------------------------------------------------------ render

    def render(self, *, render_sfx_only_debug: bool = False, save: bool = True) -> RenderResult:
        result = render_project(self.project, render_sfx_only_debug=render_sfx_only_debug)
        if save:
            self.project.save()
        return result

    def save(self) -> Path:
        return self.project.save()

    # ------------------------------------------------------------ op interface

    async def apply_ops(self, ops: List[Dict[str, Any]]) -> None:
        """
        Data-driven edits, e.g.:
            {"op": "regenerate_event", "event_id": "event_003", "prompt": "...", "num_variants": 2}
            {"op": "select_variant", "event_id": "event_003", "variant_index": 1}
            {"op": "retime_event", "event_id": "event_003", "start_time": 4.2}
            {"op": "resnap_event_timing", "event_id": "event_003"}
            {"op": "set_event_gain", "event_id": "event_003", "gain_db": -3}
            {"op": "set_event_muted", "event_id": "event_003", "muted": true}
            {"op": "add_event", "start_time": 1.0, "end_time": 2.0, "description": "..."}
            {"op": "remove_event", "event_id": "event_004"}
            {"op": "set_ambience", "prompt": "...", "gain_db": -10}
            {"op": "set_mix", "sfx_master_gain_db": 2.0}
        """
        allowed = {
            "regenerate_event",
            "select_variant",
            "retime_event",
            "resnap_event_timing",
            "set_event_gain",
            "set_event_muted",
            "add_event",
            "remove_event",
            "set_ambience",
            "set_mix",
            "propose_stylistic",
            "propose_narrative",
            "accept_suggestion",
            "reject_suggestion",
        }
        for op in ops:
            payload = dict(op)
            name = payload.pop("op")
            if name not in allowed:
                raise ValueError(f"unknown edit op: {name!r}")
            result = getattr(self, name)(**payload)
            if hasattr(result, "__await__"):
                await result
