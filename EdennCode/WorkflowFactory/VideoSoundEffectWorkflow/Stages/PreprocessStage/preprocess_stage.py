from dataclasses import dataclass
from pathlib import Path

import logging

from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata import VideoMetadata
from EdennCode.env import load_env

load_env()
logger = logging.getLogger(__name__)



@dataclass
class PreprocessStageInput:
    video_path: Path


@dataclass
class PreprocessStageOutput:
    metadata: VideoMetadata


class PreprocessStage:
    """
    Preprocess stage for video-to-sound-effect generation.
    """

    async def run(self, stage_input: PreprocessStageInput) -> PreprocessStageOutput:
        metadata = VideoMetadata.from_file(stage_input.video_path)
        logger.info("Preprocess metadata: %s", metadata)
        return PreprocessStageOutput(metadata=metadata)
