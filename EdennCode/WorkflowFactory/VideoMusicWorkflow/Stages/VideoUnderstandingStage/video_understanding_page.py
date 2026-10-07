import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from EdennCode.ModelFactory.LanguageModelFactory import AzureMultimodalClient
from EdennCode.ModelFactory.PromptFactory.prompts import PromptBuilder, ResponseSchemas
from EdennCode.WorkflowFactory.VideoMusicWorkflow.PipelineDataModel.pipeline_datamodel import Language
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.scene_segmentation_stage import SceneUnderstanding
from EdennCode.Annotation.core.annotation_dispatcher import (
    AnnotationDispatcher,
    safe_emit_annotation,
)
from EdennCode.Annotation.events.video_understanding_event import VideoUnderstandingEvent


@dataclass
class VideoUnderstandingStageInput:
    list_of_scene: List[SceneUnderstanding]
    include_vocals: bool = False
    vocal_gender: str = "female"
    user_prompt: str = ""
    preferred_language: str = Language.EN
    job_id: str = ""
    annotation_dispatcher: Optional[AnnotationDispatcher] = None


@dataclass
class VideoUnderstandingStageOutput:
    video_descriptions: Dict[str, Any]
    video_title: str = ""
    music_title: str = ""
    video_description: str = ""
    music_generation_prompt: Optional[Dict[str, Any]] = None
    token_usage: Dict[str, int] = field(default_factory=dict)


class VideoUnderstandingStage:
    def __init__(self, llm_client: AzureMultimodalClient):
        self.llm_client = llm_client

    async def run(self, stage_input: VideoUnderstandingStageInput) -> VideoUnderstandingStageOutput:
        _stage_start = time.time()
        call_list = []
        # Create Video Understanding Response Streams
        prompt_builder = PromptBuilder()
        video_description_prompt = prompt_builder.build_video_summary_messages(
            stage_input.list_of_scene,
            language=stage_input.preferred_language,
        )
        call_list.append(self.llm_client.complete_messages(
            video_description_prompt, json_schema=ResponseSchemas.video_summary()))
        outputs = await asyncio.gather(*call_list)

        video_description_output, video_desc_usage = outputs[0]
        total_usage = {
            "prompt_tokens": video_desc_usage.get("prompt_tokens", 0),
            "completion_tokens": video_desc_usage.get("completion_tokens", 0),
            "total_tokens": video_desc_usage.get("total_tokens", 0),
        }
        title = ""
        music_title = ""
        description = ""
        if isinstance(video_description_output, dict):
            title = (video_description_output.get("video_title") or "").strip()
            music_title = (video_description_output.get("music_title") or "").strip()
            description = (
                video_description_output.get("video_description") or ""
            ).strip()

        output = VideoUnderstandingStageOutput(
            video_descriptions=video_description_output,
            video_title=title,
            music_title=music_title,
            video_description=description,
            token_usage=total_usage
        )
        _vd = output.video_descriptions if isinstance(
            output.video_descriptions, dict
        ) else {}
        safe_emit_annotation(
            stage_input.annotation_dispatcher,
            lambda: VideoUnderstandingEvent(
                job_id=stage_input.job_id,
                video_title=output.video_title,
                video_description=output.video_description,
                overall_mood=_vd.get("overall_mood", ""),
                core_message=_vd.get("core_message", ""),
                has_explicit_call_to_action=bool(_vd.get("has_explicit_call_to_action", False)),
                stage_latency_s=time.time() - _stage_start,
                token_usage=output.token_usage,
                raw_descriptions=_vd,
            ),
        )
        return output
