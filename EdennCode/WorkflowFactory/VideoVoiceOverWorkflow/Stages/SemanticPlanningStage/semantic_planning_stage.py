from __future__ import annotations

import base64
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple, Union

from EdennCode.ModelFactory.LanguageModelFactory.azure_based_model_model_gateway import AzureMultimodalClient
from EdennCode.ModelFactory.PromptFactory.prompts import ResponseSchemas, VoiceOverPromptBuilder, VoiceoverSceneContext
from EdennCode.Util.MediaUtils import extract_frame, get_video_duration
from EdennCode.WorkflowFactory.VideoVoiceOverWorkflow.Stages.SceneDetectionStage.scene_detection_stage import SceneSegment
from EdennCode.WorkflowFactory.VideoVoiceOverWorkflow.datamodel import (
    VoiceOverMode,
    VoiceSegmentPlan,
)

@dataclass
class VoiceOverTTSProvider:
    PROVIDER_A:str  = "PROVIDER_A"
    MODEL_GATEWAY :str = "MODEL_GATEWAY"

@dataclass
class ModelGatewayVoPlanMetadata:
    voice:str
    speed:str
    instruction:str 
    script:str 


@dataclass(slots=True)
class SemanticPlanningStageInput:
    segments: List[SceneSegment]
    video_path: Path
    tts_provider: str  
    vo_mode: VoiceOverMode = VoiceOverMode.FULL
    language: str = "en"


@dataclass(slots=True)
class SemanticPlanningStageOutput:
    model_gateway_vo_plan: ModelGatewayVoPlanMetadata 


class SemanticPlanningStage:
    """
    Ask the planning model to craft a semantic plan for which scenes should
    feature narration.
    """

    def __init__(
        self,
        *,
        client: AzureMultimodalClient 
        
    ) -> None:
        self.client = client

    async def run(self, stage_input: SemanticPlanningStageInput) -> SemanticPlanningStageOutput:
        duration = get_video_duration(stage_input.video_path)

        provider_value = (
            stage_input.tts_provider
        )
        print(provider_value)
        print(provider_value == VoiceOverTTSProvider.MODEL_GATEWAY)
        contexts: List[VoiceoverSceneContext] = self._build_scene_contexts(
            stage_input.video_path,
            stage_input.segments,
            duration,
        )
        
        if provider_value == VoiceOverTTSProvider.MODEL_GATEWAY:
            # IF used ModelGateway 4-o TTS Downstream Provider 
            prompt = VoiceOverPromptBuilder.build_full_video_voiceover_script_messages_model_gateway(
                contexts,
                duration=duration ,
                language=stage_input.language,
                model_name=stage_input.tts_provider
            )
            schema = ResponseSchemas.voiceover_full_plan_model_gateway()
        else:
            prompt = VoiceOverPromptBuilder.build_full_video_voiceover_script_messages(
                contexts,
                duration=duration,
            )
            schema = ResponseSchemas.voiceover_full_plan()

        raw_payload = await self.client.complete_messages(
            messages=prompt,
            json_schema=schema,
        )
        if stage_input.tts_provider == VoiceOverTTSProvider.MODEL_GATEWAY:
            model_gateway_vo_plan_metadata : ModelGatewayVoPlanMetadata = self._extraction_model_gateway(raw_payload)
        else:
            model_gateway_vo_plan_metadata = None 

        return SemanticPlanningStageOutput(model_gateway_vo_plan= model_gateway_vo_plan_metadata)
    @staticmethod
    def _extraction_model_gateway(model_response_raw_payload):
        """
        Docstring for _extraction_model_gateway
        :param model_response_raw_payload: Description

        """
        print(model_response_raw_payload)
        voice = model_response_raw_payload[0]['model_gateway_tts_settings']['voice']
        speed = model_response_raw_payload[0]['model_gateway_tts_settings']['speed']
        instructions = model_response_raw_payload[0]['model_gateway_tts_settings']['instructions']
        scripts = model_response_raw_payload[0]['script']
        return ModelGatewayVoPlanMetadata(
            voice  = voice,
            speed = speed,
            instruction = instructions,
            script = scripts
        )


        
    
    def _build_scene_contexts(
        self,
        video_path: Path,
        segments: List[SceneSegment],
        duration: float,
    ) -> List[VoiceoverSceneContext]:
        contexts: List[VoiceoverSceneContext] = []

        with tempfile.TemporaryDirectory(prefix="vo_frames_") as tmp_dir:
            tmp_root = Path(tmp_dir)
            for idx, seg in enumerate(segments):
                first_path = tmp_root / f"scene_{idx}_first.jpg"
                last_path = tmp_root / f"scene_{idx}_last.jpg"
                first_ts = self._clamp(seg.start_sec, duration)
                last_ts = self._clamp(seg.end_sec - 0.05, duration)
                extract_frame(video_path, first_ts, first_path, duration=duration)
                extract_frame(video_path, max(seg.start_sec, last_ts), last_path, duration=duration)
                contexts.append(
                    VoiceoverSceneContext(
                        scene_index=idx,
                        start_sec=seg.start_sec,
                        end_sec=seg.end_sec,
                        first_frame_b64=self._encode_image(first_path),
                        last_frame_b64=self._encode_image(last_path),
                    )
                )
        return contexts

    @staticmethod
    def _encode_image(path: Path) -> str:
        return base64.b64encode(path.read_bytes()).decode("utf-8")

    @staticmethod
    def _clamp(value: float, duration: float) -> float:
        if duration <= 0:
            return max(0.0, value)
        return max(0.0, min(value, max(0.0, duration - 0.02)))
