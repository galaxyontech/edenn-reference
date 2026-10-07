from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from EdennCode.ModelFactory.VoiceOverModelFactory.model_gateway_base_model import AzureModelGatewayTTS4OMini
from EdennCode.WorkflowFactory.VideoVoiceOverWorkflow.Stages.PreprocessStage.preprocess_stage import (
    PreprocessStage,
    PreprocessStageInput,
)
from EdennCode.WorkflowFactory.VideoVoiceOverWorkflow.Stages.SceneDetectionStage.scene_detection_stage import (
    SceneDetectionStage,
    SceneDetectionStageInput,
    SceneSegment,
)
from EdennCode.WorkflowFactory.VideoVoiceOverWorkflow.Stages.SemanticPlanningStage.semantic_planning_stage import SemanticPlanningStageInput, SemanticPlanningStageOutput, SemanticPlanningStage, VoiceOverTTSProvider
from EdennCode.WorkflowFactory.VideoVoiceOverWorkflow.Stages.TTSSynthesisStage.tts_synthesis_stage import TTSSynthesisStage, TTSSynthesisStageInput, TTSSynthesisStageOutput
from EdennCode.WorkflowFactory.VideoVoiceOverWorkflow.Stages.VideoMergeStage.video_merge_stage import VideoMergeStage, VideoMergeStageInput
from EdennCode.Util.MediaUtils import get_video_duration
from EdennCode.WorkflowFactory.VideoVoiceOverWorkflow.datamodel import VoiceOverMode
from EdennCode.Util.MediaUtils.pipeline_util import build_azure_client

from logging import Logger


logger: Logger = Logger("VoiceOverWorkflow")


@dataclass
class VoiceOverWorkflowInput:
    video_path: Path
    mode: VoiceOverMode = VoiceOverMode.FULL
    use_local_outputs: bool = True
    tts_provider: str = VoiceOverTTSProvider.MODEL_GATEWAY
    language: str = "en"


class VoiceOverWorkflow:
    """
    Orchestrates all stages required to generate ad-ready voiceovers.
    """

    def __init__(
        self,
    ) -> None:
        self.model_gateway_tts_client = AzureModelGatewayTTS4OMini()
        self.azure_multi_modal_client = build_azure_client()

    async def execute(self, stage_input: VoiceOverWorkflowInput) -> Path:
        """
        Preprocess Stage -> Semantic Planning Stage -> TTS Synthesis Stage -> Video Merge Stage
        """
        preprocess_stage_input = PreprocessStageInput(video_path=stage_input.video_path)
        proprocess_stage = PreprocessStage()
        preprocess_output = proprocess_stage.run(preprocess_stage_input, is_local=stage_input.use_local_outputs)
        logger.info(
            "Preprocess Stage completed. Temporary folder at %s",
            preprocess_output.metadata.output_temporary_folder,
        )
        """
        2. Scene Detection Stage to break up the scene based a batch of adaptive algorithms. 
        """
        scene_detection_stage:SceneDetectionStage = SceneDetectionStage()
        scene_detection_input = SceneDetectionStageInput(
            video_path=preprocess_output.metadata.video_asset_path,
            scene_threshold=3.0,
            min_scene_length=1.0,
            scene_detector="auto",
            scene_detection_method="adaptive",
            max_scenes=50,
        )
        scene_detection_output = scene_detection_stage.run(scene_detection_input)


        """
        3. Planned the semantic voice over stage over the previous scene detection stage. 
         1. break down based on whether it is going to be a full voice over 
         2. or per scene voice over ( lesser quality based on current tts model ) 
        """
        
        semantic_planning_stage = SemanticPlanningStage(client=self.azure_multi_modal_client)
        semantic_planning_stage_input = SemanticPlanningStageInput(
            segments=scene_detection_output.segments,
            video_path=preprocess_output.metadata.video_asset_path,
            vo_mode=stage_input.mode,
            tts_provider=stage_input.tts_provider,
            language=stage_input.language,
        )
        semantic_planning_stage_output: SemanticPlanningStageOutput = await  semantic_planning_stage.run(
            semantic_planning_stage_input
        )
        """
        4. TTS Synthesis Stage to generate voice over audio files based on the semantic
        planning stage output.
        """

        tts_stage = TTSSynthesisStage(model_gateway_client=self.model_gateway_tts_client)
        tts_stage_input = TTSSynthesisStageInput(
            voice_over_plan=semantic_planning_stage_output.model_gateway_vo_plan,
            output_dir=preprocess_output.metadata.output_temporary_folder,
        )
        tts_output: TTSSynthesisStageOutput = tts_stage.run(tts_stage_input)

        merge_stage_input = VideoMergeStageInput(
            video_metadata=preprocess_output.metadata,
            audio_path=tts_output.audio_path,
        )
        merge_stage = VideoMergeStage()
        merge_output = merge_stage.run(merge_stage_input)

        return merge_output.final_video_path





if __name__ == "__main__":
    video_example_path = Path("/path/to/repo/EdennCode/TestSuites/assets/smoke/videos/sample_clip.mp4")
    example_voiceover_workflow = VoiceOverWorkflow()
    import asyncio
    asyncio.run( example_voiceover_workflow.execute(
        VoiceOverWorkflowInput(
            video_path=video_example_path,
            mode=VoiceOverMode.FULL,
            use_local_outputs=True,
        )
    ))
