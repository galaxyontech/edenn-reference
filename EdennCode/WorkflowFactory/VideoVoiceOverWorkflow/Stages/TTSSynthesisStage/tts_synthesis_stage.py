from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List
from urllib.parse import urlparse

from datetime import datetime


from EdennCode.ModelFactory.VoiceOverModelFactory.model_gateway_base_model import AzureModelGatewayTTS4OMini
from EdennCode.Util.MediaUtils.pipeline_util import hash_str
from EdennCode.WorkflowFactory.VideoVoiceOverWorkflow.Stages.SemanticPlanningStage.semantic_planning_stage import ModelGatewayVoPlanMetadata
from EdennCode.WorkflowFactory.VideoVoiceOverWorkflow.datamodel import VoiceSegmentPlan

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TTSSynthesisStageInput:
    voice_over_plan: ModelGatewayVoPlanMetadata
    output_dir: Path


@dataclass(slots=True)
class TTSSynthesisStageOutput:
    audio_path: Path


class TTSSynthesisStage:
    """
    Call ProviderA or ModelGateway TTS for planned narration segments.
    """

    def __init__(
        self,
        model_gateway_client: AzureModelGatewayTTS4OMini,
    ) -> None:
        self._model_gateway_client = model_gateway_client

    def run(self, stage_input: TTSSynthesisStageInput) -> TTSSynthesisStageOutput:
        temp_hash = hash_str()
        voice_audio_stream_path = stage_input.output_dir / f"voiceover_audio{temp_hash}.wav" 
        audio_path:Path  = self._model_gateway_client.send_request_streaming(
            instructions=stage_input.voice_over_plan.instruction,
            speed=float(stage_input.voice_over_plan.speed),
            script=stage_input.voice_over_plan.script,
            voice=stage_input.voice_over_plan.voice,
            audio_save_path=voice_audio_stream_path,
        )
        return TTSSynthesisStageOutput(audio_path=audio_path)

