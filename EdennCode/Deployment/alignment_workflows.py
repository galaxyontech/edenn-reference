from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from EdennCode.WorkflowFactory.VideoAudioAlignmentWorkflow import (
    VideoAudioAlignmentWorkflow,
    VideoAudioAlignmentWorkflowInput,
)
from EdennCode.WorkflowFactory.VideoAudioAlignmentWorkflow.Stages.AudioVideoAlignmentStage import (
    RankedAlignmentSegment,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata.video_metadata import (
    VideoMetadata,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.word_ts import (
    WordTS,
)


@dataclass
class AudioAlignmentSegmentResult:
    rank: int
    music_start_s: float
    music_end_s: float
    score: float
    details: Dict[str, Any]
    rendered_audio_path: Path
    aligned_lyrics: List[WordTS]


@dataclass
class AudioAlignmentResult:
    video_metadata: VideoMetadata
    best_segment: AudioAlignmentSegmentResult
    segments: List[AudioAlignmentSegmentResult]
    lyrics_provided: bool


class AudioAlignmentOrchestrator:
    """
    Standalone orchestration for aligning an existing audio source to a video.
    """

    def __init__(self) -> None:
        self.workflow = VideoAudioAlignmentWorkflow()

    @staticmethod
    def _to_segment_result(segment: RankedAlignmentSegment) -> AudioAlignmentSegmentResult:
        return AudioAlignmentSegmentResult(
            rank=segment.rank,
            music_start_s=segment.music_start_s,
            music_end_s=segment.music_end_s,
            score=segment.score,
            details=dict(segment.details),
            rendered_audio_path=segment.rendered_audio_path,
            aligned_lyrics=list(segment.aligned_lyrics),
        )

    async def run(
        self,
        *,
        video_path: Path,
        audio_path: Path,
        lyrics_timestamps: List[WordTS],
        top_k: int,
    ) -> AudioAlignmentResult:
        workflow_output = await self.workflow.run(
            VideoAudioAlignmentWorkflowInput(
                video_path=str(video_path),
                audio_path=str(audio_path),
                lyrics_timestamps=list(lyrics_timestamps),
                top_k=top_k,
            )
        )
        segment_results = [
            self._to_segment_result(segment)
            for segment in workflow_output.segments
        ]
        return AudioAlignmentResult(
            video_metadata=workflow_output.video_metadata,
            best_segment=segment_results[0],
            segments=segment_results,
            lyrics_provided=workflow_output.lyrics_provided,
        )


__all__ = [
    "AudioAlignmentOrchestrator",
    "AudioAlignmentResult",
    "AudioAlignmentSegmentResult",
]
