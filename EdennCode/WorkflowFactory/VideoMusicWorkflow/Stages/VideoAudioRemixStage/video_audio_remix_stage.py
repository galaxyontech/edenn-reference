from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import hashlib
import time

from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata import VideoMetadata
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata.datamodel import FileHandlerEnum
from EdennCode.Util.MediaUtils import overlay_music_on_video
from EdennCode.Annotation.core.annotation_dispatcher import (
    AnnotationDispatcher,
    safe_emit_annotation,
)
from EdennCode.Annotation.events.remix_completion_event import RemixCompletionEvent


@dataclass
class VideoAudioRemixStageInput:
    preserve_original_audio: bool
    music_volume: float
    music_path: Path
    video_metadata: VideoMetadata
    duck_gain_db: float = -9.0
    duck_padding_s: float = 0.25
    job_id: str = ""
    annotation_dispatcher: Optional[AnnotationDispatcher] = None
    pipeline_start_time: float = 0.0

@dataclass
class VideoAudioRemixStageOutput:
    remixed_video_path:str


class VideoAudioRemixStage:
    def __init__(self, ) -> None:
        pass

    async def run(self, stage_input: VideoAudioRemixStageInput) -> VideoAudioRemixStageOutput:
        _stage_start = time.time()
        output_dir = Path(stage_input.video_metadata.temp_folder)
        output_dir.mkdir(parents=True, exist_ok=True)
        base_name = Path(FileHandlerEnum.video_post_remix_output).stem
        suffix = Path(FileHandlerEnum.video_post_remix_output).suffix or ".mp4"
        signature_seed = f"{stage_input.video_metadata.path}-{stage_input.music_path}-{time.time()}".encode("utf-8")
        signature = hashlib.sha1(signature_seed).hexdigest()[:8]
        output_filename = f"{base_name}_{signature}{suffix}"
        output_path = output_dir / output_filename
        ducking = stage_input.video_metadata.audio_activity if stage_input.preserve_original_audio else []
        result_path = overlay_music_on_video(
            stage_input.video_metadata.path,
            stage_input.music_path,
            output_path,
            music_volume=stage_input.music_volume,
            preserve_original_audio=stage_input.preserve_original_audio,
            ducking_segments=ducking,
            ducking_gain_db=stage_input.duck_gain_db,
            ducking_padding=stage_input.duck_padding_s,
        )
        output = VideoAudioRemixStageOutput(remixed_video_path=result_path.name)
        _stage_latency_s = time.time() - _stage_start
        _total_latency_s = (
            time.time() - stage_input.pipeline_start_time
            if stage_input.pipeline_start_time > 0
            else None
        )
        safe_emit_annotation(
            stage_input.annotation_dispatcher,
            lambda: RemixCompletionEvent(
                job_id=stage_input.job_id,
                remixed_video_filename=output.remixed_video_path,
                preserve_original_audio=stage_input.preserve_original_audio,
                music_volume=stage_input.music_volume,
                duck_gain_db=stage_input.duck_gain_db,
                stage_latency_s=_stage_latency_s,
                total_pipeline_latency_s=_total_latency_s,
            ),
        )
        return output
