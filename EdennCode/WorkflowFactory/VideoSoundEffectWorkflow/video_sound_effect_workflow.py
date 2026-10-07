from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.Stages.PreprocessStage.preprocess_stage import (
    PreprocessStage,
    PreprocessStageInput,
    PreprocessStageOutput,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.Stages.SoundEffectGenerationStage.sound_effect_generation_stage import (
    SoundEffectGenerationStage,
    SoundEffectGenerationStageInput,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.Stages.TimingRefinementStage.timing_refinement_stage import (
    EventTimingRefinement,
    TimingRefinementStage,
    TimingRefinementStageInput,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.Stages.UserPromptUnderstandingStage.user_prompt_understanding_stage import (
    UserPromptUnderstandingStage,
    UserPromptUnderstandingStageInput,
    UserPromptUnderstandingStageOutput,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.Stages.VideoEventAnalysisStage.video_event_analysis_stage import (
    VideoEventAnalysisStage,
    VideoEventAnalysisStageInput,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.VideoSoundEffectDataModel.datamodel import (
    AmbienceBed,
    MixSettings,
    SfxProject,
    SoundFXEvent,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.rendering import (
    RenderResult,
    render_project,
)

logger = logging.getLogger(__name__)


@dataclass
class VideoSfxWorkflowOptions:
    sample_rate: int = 44_100
    num_variants: int = 1
    enable_ambience: bool = True
    enable_timing_refinement: bool = True
    preserve_original_audio: bool = True
    sfx_master_gain_db: float = 0.0
    ambience_gain_db: float = -8.0
    min_event_confidence: float = 0.0
    render_sfx_only_debug: bool = True
    # "auto": video-native bed when an engine key is configured and the video
    # fits the engine's single-call cap; otherwise the text-loop bed.
    bed_route: str = "auto"


@dataclass
class VideoSoundEffectWorkflowE2EInput:
    video_path: str
    uploaded_public_facing_url: str
    user_prompt: Optional[str] = None
    run_dir: Optional[str] = None
    options: VideoSfxWorkflowOptions = field(default_factory=VideoSfxWorkflowOptions)


@dataclass
class VideoSoundEffectWorkflowE2EOutput:
    project: SfxProject
    video_duration: float
    user_prompt_understanding: Optional[UserPromptUnderstandingStageOutput]
    llm_events: List[SoundFXEvent]
    generated_sound_events: List[SoundFXEvent]
    timing_refinements: List[EventTimingRefinement]
    mixed_audio_path: Path
    final_video_path: Path
    latency_seconds: float


class VideoSoundEffectWorkflowE2E:
    """
    End-to-end video→SFX generation.

    Pipeline: metadata → (optional) prompt understanding → two-scan multimodal
    event spotting (events + ambience bed) → local motion-onset timing
    refinement → per-event SFX generation with variants → timeline render →
    mux. The result is persisted as an `SfxProject`, which `SfxProjectEditor`
    can then iterate on (regenerate / swap / retime / mute / re-render) without
    re-running analysis.
    """

    def __init__(
        self,
        *,
        preprocess_stage: Optional[PreprocessStage] = None,
        user_prompt_understanding_stage: Optional[UserPromptUnderstandingStage] = None,
        video_event_analysis_stage: Optional[VideoEventAnalysisStage] = None,
        timing_refinement_stage: Optional[TimingRefinementStage] = None,
        sound_effect_generation_stage: Optional[SoundEffectGenerationStage] = None,
    ) -> None:
        self.preprocess_stage = preprocess_stage or PreprocessStage()
        self._user_prompt_understanding_stage = user_prompt_understanding_stage
        self._video_event_analysis_stage = video_event_analysis_stage
        self.timing_refinement_stage = timing_refinement_stage or TimingRefinementStage()
        self._sound_effect_generation_stage = sound_effect_generation_stage

    # Network-backed stages are built lazily so offline construction (tests,
    # editor-only sessions) never requires provider credentials.
    @property
    def user_prompt_understanding_stage(self) -> UserPromptUnderstandingStage:
        if self._user_prompt_understanding_stage is None:
            self._user_prompt_understanding_stage = UserPromptUnderstandingStage()
        return self._user_prompt_understanding_stage

    @property
    def video_event_analysis_stage(self) -> VideoEventAnalysisStage:
        if self._video_event_analysis_stage is None:
            self._video_event_analysis_stage = VideoEventAnalysisStage()
        return self._video_event_analysis_stage

    @property
    def sound_effect_generation_stage(self) -> SoundEffectGenerationStage:
        if self._sound_effect_generation_stage is None:
            self._sound_effect_generation_stage = SoundEffectGenerationStage()
        return self._sound_effect_generation_stage

    async def execute(
        self, stage_input: VideoSoundEffectWorkflowE2EInput
    ) -> VideoSoundEffectWorkflowE2EOutput:
        start_ts = time.time()
        options = stage_input.options
        video_path = Path(stage_input.video_path).expanduser().resolve()
        run_dir = Path(
            stage_input.run_dir
            or Path("outputs") / "video_sfx_runs" / time.strftime("%Y%m%d_%H%M%S")
        )
        run_dir.mkdir(parents=True, exist_ok=True)

        # 1. Metadata
        preprocess_output: PreprocessStageOutput = await self.preprocess_stage.run(
            PreprocessStageInput(video_path=video_path)
        )
        video_duration = float(preprocess_output.metadata.duration)
        logger.info("Video duration %.2fs (%s)", video_duration, video_path.name)

        # 2. Prompt understanding (only when the user said something)
        prompt_understanding: Optional[UserPromptUnderstandingStageOutput] = None
        if (stage_input.user_prompt or "").strip():
            prompt_understanding = await self.user_prompt_understanding_stage.run(
                UserPromptUnderstandingStageInput(user_prompt=stage_input.user_prompt)
            )
            logger.info("Prompt understanding: %s", prompt_understanding)

        # 3. Two-scan event spotting
        analysis = await self.video_event_analysis_stage.run_v2(
            VideoEventAnalysisStageInput(
                uploaded_url=stage_input.uploaded_public_facing_url,
                video_metadata=preprocess_output.metadata,
                prompt_understanding=prompt_understanding,
                user_prompt=stage_input.user_prompt,
            )
        )
        events = [
            e for e in analysis.list_sound_events if e.confidence >= options.min_event_confidence
        ]
        logger.info(
            "Event analysis: %d events (%d kept), ambience=%r",
            len(analysis.list_sound_events),
            len(events),
            analysis.ambience_description[:60],
        )

        # 4. Local motion-onset timing refinement
        timing_refinements: List[EventTimingRefinement] = []
        if options.enable_timing_refinement and events:
            refinement_output = await self.timing_refinement_stage.run(
                TimingRefinementStageInput(
                    video_path=video_path,
                    events=events,
                    video_duration=video_duration,
                )
            )
            events = refinement_output.events
            timing_refinements = refinement_output.refinements
            snapped = sum(1 for r in timing_refinements if r.snapped)
            logger.info("Timing refinement snapped %d/%d events.", snapped, len(events))

        # 5. Per-event generation (+ ambience bed)
        generation_output = await self.sound_effect_generation_stage.run(
            SoundEffectGenerationStageInput(
                list_of_generation_packages=events,
                output_directory=run_dir / "sfx_assets",
                sample_rate=options.sample_rate,
                num_variants=options.num_variants,
            )
        )
        events = generation_output.generated_sound_events

        ambience: Optional[AmbienceBed] = None
        if options.enable_ambience and analysis.ambience_description:
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
                prompt=analysis.ambience_description,
                gain_db=options.ambience_gain_db,
                route=bed_route,
            )
            bed_duration = video_duration if bed_route == "video_native" else min(video_duration, 20.0)
            try:
                ambience = await generation_stage.generate_ambience(
                    ambience=ambience,
                    output_directory=run_dir / "sfx_assets",
                    duration_seconds=bed_duration,
                    sample_rate=options.sample_rate,
                    video_url=stage_input.uploaded_public_facing_url,
                )
            except Exception:
                # The bed is an optional layer; a provider hiccup must not orphan
                # the paid analysis + event clips. The editor can regenerate it.
                logger.exception("Ambience generation failed; continuing without a bed.")
                ambience.enabled = False

        # 6-7. Render timeline + mux (shared with the iteration editor)
        project = SfxProject(
            project_dir=str(run_dir),
            video_path=str(video_path),
            video_url=stage_input.uploaded_public_facing_url,
            video_duration=video_duration,
            user_prompt=stage_input.user_prompt or "",
            scene_summary=analysis.scene_summary,
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

        # 8. Persist the editable project state
        project.save()
        latency = time.time() - start_ts
        logger.info("Video SFX workflow finished in %.2fs -> %s", latency, project.project_file)

        return VideoSoundEffectWorkflowE2EOutput(
            project=project,
            video_duration=video_duration,
            user_prompt_understanding=prompt_understanding,
            llm_events=analysis.list_sound_events,
            generated_sound_events=events,
            timing_refinements=timing_refinements,
            mixed_audio_path=render_result.mixed_audio_path,
            final_video_path=render_result.final_video_path,
            latency_seconds=latency,
        )
