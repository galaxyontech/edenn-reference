from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import moviepy.audio.fx as afx
from moviepy.audio.AudioClip import CompositeAudioClip
from moviepy.audio.io.AudioFileClip import AudioFileClip
from moviepy.video.io.VideoFileClip import VideoFileClip

from EdennCode.Util.MediaUtils.pipeline_util import hash_str
from EdennCode.WorkflowFactory.VideoVoiceOverWorkflow.Stages.PreprocessStage.preprocess_stage import VoiceOverAssetMetadata

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class VideoMergeStageInput:
    video_metadata: VoiceOverAssetMetadata
    audio_path: Path


@dataclass(slots=True)
class VideoMergeStageOutput:
    final_video_path: Path


class VideoMergeStage:
    """
    Merge synthesized VO with the base video audio.
    """

    def run(self, stage_input: VideoMergeStageInput) -> VideoMergeStageOutput:
        output_dir = stage_input.video_metadata.output_temporary_folder

        # Expect your metadata to contain the base video path
        video_path: Path = Path(stage_input.video_metadata.video_asset_path)  # adjust if your field name differs
        audio_path: Path = stage_input.audio_path

        if not video_path.exists():
            raise FileNotFoundError(f"Video not found: {video_path}")
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio not found: {audio_path}")

        merge_stage_hash = hash_str()
        output_path = output_dir / f"final_video_{merge_stage_hash}.mp4"

        video_clip = None
        vo_clip = None
        final_clip = None

        try:
            video_clip = VideoFileClip(str(video_path))

            # Load VO audio
            vo_clip = AudioFileClip(str(audio_path))

            # If VO is longer than video, trim it
            if vo_clip.duration and video_clip.duration and vo_clip.duration > video_clip.duration:
                vo_clip = vo_clip.subclipped(0, video_clip.duration)

            # Mix with original audio (duck original). If no original audio, just use VO.
            if video_clip.audio is not None:
                base_audio = video_clip.audio.with_effects([afx.MultiplyVolume(0.25)])  # duck original audio
                vo_audio = vo_clip.with_effects([afx.MultiplyVolume(1.0)])
                mixed_audio = CompositeAudioClip([base_audio, vo_audio])
            else:
                mixed_audio = vo_clip

            final_clip = video_clip.set_audio(mixed_audio)

            # Write out
            final_clip.write_videofile(
                str(output_path),
                codec="libx264",
                audio_codec="aac",
                temp_audiofile=str(output_dir / f"temp_audio_{merge_stage_hash}.m4a"),
                remove_temp=True,
                threads=4,
                logger=None,  # set to "bar" if you want a progress bar
            )

            return VideoMergeStageOutput(final_video_path=output_path)

        finally:
            # Close clips to release file handles
            try:
                if final_clip is not None:
                    final_clip.close()
            except Exception:
                pass
            try:
                if vo_clip is not None:
                    vo_clip.close()
            except Exception:
                pass
            try:
                if video_clip is not None:
                    video_clip.close()
            except Exception:
                pass
