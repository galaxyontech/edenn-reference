from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata.video_metadata import (
    VideoMetadata,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.word_ts import (
    WordTS,
)

from .Stages.AudioVideoAlignmentStage import (
    AudioVideoAlignmentStage,
    AudioVideoAlignmentStageInput,
    AudioVideoAlignmentStageOutput,
    RankedAlignmentSegment,
)


@dataclass
class VideoAudioAlignmentWorkflowInput:
    video_path: str
    audio_path: str
    lyrics_timestamps: List[WordTS] = field(default_factory=list)
    top_k: int = 3


@dataclass
class VideoAudioAlignmentWorkflowOutput:
    video_metadata: VideoMetadata
    best_segment: RankedAlignmentSegment
    segments: List[RankedAlignmentSegment]
    lyrics_provided: bool = False


class VideoAudioAlignmentWorkflow:
    def __init__(self) -> None:
        self.alignment_stage = AudioVideoAlignmentStage()

    async def run(
        self,
        workflow_input: VideoAudioAlignmentWorkflowInput,
    ) -> VideoAudioAlignmentWorkflowOutput:
        video_metadata = VideoMetadata.from_file(Path(workflow_input.video_path))
        stage_output: AudioVideoAlignmentStageOutput = await self.alignment_stage.run(
            AudioVideoAlignmentStageInput(
                video_metadata=video_metadata,
                local_audio_path=Path(workflow_input.audio_path),
                lyrics_timestamps=list(workflow_input.lyrics_timestamps),
                top_k=workflow_input.top_k,
            )
        )
        return VideoAudioAlignmentWorkflowOutput(
            video_metadata=video_metadata,
            best_segment=stage_output.best_segment,
            segments=stage_output.segments,
            lyrics_provided=bool(workflow_input.lyrics_timestamps),
        )
