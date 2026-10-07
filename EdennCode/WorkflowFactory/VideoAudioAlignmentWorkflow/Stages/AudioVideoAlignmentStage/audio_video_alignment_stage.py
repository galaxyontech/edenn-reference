from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from EdennCode.Util.MediaUtils.pipeline_util import hash_str
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata.video_metadata import (
    VideoMetadata,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.audio_window_renderer import (
    AudioWindowRenderer,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.data_models import (
    WindowScore,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.word_ts import (
    WordTS,
)

from .audio_video_window_matcher import AudioVideoWindowMatcher


@dataclass
class RankedAlignmentSegment:
    rank: int
    music_start_s: float
    music_end_s: float
    score: float
    details: Dict[str, Any]
    rendered_audio_path: Path
    aligned_lyrics: List[WordTS] = field(default_factory=list)


@dataclass
class AudioVideoAlignmentStageInput:
    video_metadata: VideoMetadata
    local_audio_path: Path
    lyrics_timestamps: List[WordTS] = field(default_factory=list)
    top_k: int = 3


@dataclass
class AudioVideoAlignmentStageOutput:
    best_segment: RankedAlignmentSegment
    segments: List[RankedAlignmentSegment]


class AudioVideoAlignmentStage:
    def __init__(self) -> None:
        self.matcher = AudioVideoWindowMatcher()
        self.renderer = AudioWindowRenderer()

    @staticmethod
    def _offset_word_ts(
        words: List[WordTS],
        offset_s: float,
        duration_s: float,
    ) -> List[WordTS]:
        aligned: List[WordTS] = []
        for word in words:
            start = float(getattr(word, "startS", 0.0)) - offset_s
            end = float(getattr(word, "endS", 0.0)) - offset_s
            if end <= 0:
                continue
            start = max(0.0, start)
            if start >= duration_s:
                continue
            end = min(duration_s, end)
            aligned.append(
                WordTS(
                    text=getattr(word, "text", ""),
                    startS=start,
                    endS=end,
                    i=getattr(word, "i", None),
                )
            )
        return aligned

    def _render_segment_audio(
        self,
        *,
        local_audio_path: Path,
        video_metadata: VideoMetadata,
        window: WindowScore,
        rank: int,
    ) -> Path:
        output_path = (
            Path(video_metadata.temp_folder)
            / f"alignment_rank_{rank}_{hash_str()}.wav"
        )
        self.renderer.render_audio(
            music_path=str(local_audio_path),
            music_start_s=window.music_start_s,
            duration_s=video_metadata.duration,
            out_path=str(output_path),
        )
        return output_path

    async def run(
        self,
        stage_input: AudioVideoAlignmentStageInput,
    ) -> AudioVideoAlignmentStageOutput:
        _, top_windows = self.matcher.match(
            video_path=str(stage_input.video_metadata.path),
            music_path=str(stage_input.local_audio_path),
            word_ts=stage_input.lyrics_timestamps,
            topk=max(1, min(int(stage_input.top_k), 5)),
        )

        ranked_segments: List[RankedAlignmentSegment] = []
        for index, window in enumerate(top_windows, start=1):
            rendered_audio_path = self._render_segment_audio(
                local_audio_path=stage_input.local_audio_path,
                video_metadata=stage_input.video_metadata,
                window=window,
                rank=index,
            )
            aligned_lyrics = []
            if stage_input.lyrics_timestamps:
                aligned_lyrics = self._offset_word_ts(
                    stage_input.lyrics_timestamps,
                    window.music_start_s,
                    stage_input.video_metadata.duration,
                )
            ranked_segments.append(
                RankedAlignmentSegment(
                    rank=index,
                    music_start_s=window.music_start_s,
                    music_end_s=window.music_end_s,
                    score=window.score,
                    details=dict(window.details),
                    rendered_audio_path=rendered_audio_path,
                    aligned_lyrics=aligned_lyrics,
                )
            )

        return AudioVideoAlignmentStageOutput(
            best_segment=ranked_segments[0],
            segments=ranked_segments,
        )
