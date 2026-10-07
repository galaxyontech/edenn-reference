import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, List, Dict, Optional

from numpy.random import f

from EdennCode.ModelFactory.LanguageModelFactory import AzureMultimodalClient
from EdennCode.ModelFactory.PromptFactory.prompts import PromptBuilder, ResponseSchemas
from EdennCode.WorkflowFactory.VideoMusicWorkflow.PipelineDataModel.pipeline_datamodel import Language, VideoCategory
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicGenerationStage.music_generation_stage import \
    MusicGenertionModelEnum
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.scene_segmentation_stage import SceneUnderstanding
from EdennCode.Annotation.core.annotation_dispatcher import (
    AnnotationDispatcher,
    safe_emit_annotation,
)
from EdennCode.Annotation.events.music_prompt_event import MusicPromptEvent
from EdennCode.exceptions import EdennProviderError, EdennValidationError

logger = logging.getLogger("Music Prompt Orchestration")


def _require_generated_prompt_field(
    prompt_output: Dict[str, Any],
    field_name: str,
    *,
    modelspec: str,
    request_context: str,
) -> str:
    value = prompt_output.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise EdennProviderError(
            f"Prompt orchestration LLM did not return {field_name} for {request_context}.",
            provider_name="prompt_llm",
            operation="music_prompt_orchestration",
            context={"modelspec": modelspec, "field_name": field_name},
        )
    return value.strip()


@dataclass
class MusicPromptOrchestrationStageInput:
    list_of_scene: List[SceneUnderstanding]
    include_vocals: bool
    vocal_gender: str
    user_prompt: str
    language: str
    provider_c_custom_mode: bool = True
    modelspec: str = MusicGenertionModelEnum.EDENN_BASIC
    video_summary: Optional[Dict[str, Any]] = None
    verbose_instruction: bool = False
    music_style_prompt: Optional[str] = None
    lyrics_prompt: Optional[str] = None
    job_id: str = ""
    annotation_dispatcher: Optional[AnnotationDispatcher] = None


@dataclass
class MusicPromptOrchestrationStageOutput:
    music_generation_prompt: Dict[str, str]
    token_usage: Dict[str, int]
    downstream_generation_model_spec: str


class MusicPromptOrchestrationStage:
    def __init__(self, llm_client: AzureMultimodalClient):
        self.llm_client = llm_client

    async def run(self, stage_input: MusicPromptOrchestrationStageInput) -> MusicPromptOrchestrationStageOutput:
        _stage_start = time.time()
        call_list = []
        # Create Video Understanding Response Streams
        prompt_builder = PromptBuilder()
        """
        Create Music Prompts based on 
        1. Vocals
        2. List of scene
        3. Gender of vocals 
        4. Specific user prompts 
        """

        logger.info(
            f"Generation Config"
            f"{stage_input.language} "
            f"{stage_input.user_prompt}"
            f"{stage_input.include_vocals}"
            f"{stage_input.vocal_gender}"
            f"{stage_input.modelspec}"
            f"{stage_input.verbose_instruction}"
            f"{stage_input.music_style_prompt}"
            f"{stage_input.lyrics_prompt}"
        )

        if (
            stage_input.modelspec == MusicGenertionModelEnum.EDENN_STUDIO
            and not stage_input.include_vocals
        ):
            if (
                stage_input.verbose_instruction
                and (
                    not isinstance(stage_input.music_style_prompt, str)
                    or not stage_input.music_style_prompt.strip()
                )
            ):
                raise EdennValidationError(
                    "music_style_prompt is required for verbose music prompt orchestration.",
                    public_message="music_style_prompt is required when verbose_instruction=true.",
                    component="music_prompt_orchestration",
                    operation="build_verbose_prompt",
                )
            simple_user_prompt = (
                stage_input.music_style_prompt
                if stage_input.verbose_instruction and stage_input.music_style_prompt
                else stage_input.user_prompt
            )
            music_prompt_builder_input = prompt_builder.build_provider_c_simple_prompt_messages(
                stage_input.list_of_scene,
                include_vocals=False,
                vocal_gender=stage_input.vocal_gender,
                user_prompt=simple_user_prompt,
                language=stage_input.language,
            )
            call_list.append(self.llm_client.complete_messages(
                music_prompt_builder_input, json_schema=ResponseSchemas.provider_c_simple_prompt()))
        elif (
            stage_input.verbose_instruction
            and stage_input.modelspec
            in {
                MusicGenertionModelEnum.EDENN_ENHANCED,
                MusicGenertionModelEnum.EDENN_STUDIO,
            }
        ):
            _is_verbose_provider_c = stage_input.modelspec == MusicGenertionModelEnum.EDENN_STUDIO
            _has_style = bool(
                isinstance(stage_input.music_style_prompt, str)
                and stage_input.music_style_prompt.strip()
            )
            _has_lyrics = bool(
                isinstance(stage_input.lyrics_prompt, str)
                and stage_input.lyrics_prompt.strip()
            )
            # A lyric-only request is valid: the dual template derives the
            # style entirely from the video analysis when the style slot is
            # empty. Only a request with neither field is malformed.
            if not _has_style and not _has_lyrics:
                raise EdennValidationError(
                    "music_style_prompt or lyrics_prompt is required for verbose music prompt orchestration.",
                    public_message=(
                        "Provide music direction: music_style_prompt or "
                        "lyrics_prompt is required for an explicit-direction request."
                    ),
                    component="music_prompt_orchestration",
                    operation="build_verbose_prompt",
                )
            # Neutral tier labels: naming the upstream product in the prompt both
            # leaks the vendor and pattern-matches "write prompts for another AI
            # system", which the content filter flags as a jailbreak.
            music_prompt_builder_input = prompt_builder.build_verbose_dual_prompt_messages(
                stage_input.list_of_scene,
                provider_label="Studio" if _is_verbose_provider_c else "Enhanced",
                include_vocals=stage_input.include_vocals,
                vocal_gender=stage_input.vocal_gender,
                music_style_prompt=stage_input.music_style_prompt,
                lyrics_prompt=stage_input.lyrics_prompt,
                language=stage_input.language,
                video_summary=stage_input.video_summary,
            )
            # ProviderC enforces 190-char lyrics_prompt / 980-char style_prompt limits;
            # use its schema so the LLM respects them rather than the limit-free ProviderB schema.
            _verbose_schema = (
                ResponseSchemas.provider_c_custom_lyrics()
                if _is_verbose_provider_c
                else ResponseSchemas.provider_b_dual_prompt()
            )
            call_list.append(self.llm_client.complete_messages(
                music_prompt_builder_input, json_schema=_verbose_schema))
        elif stage_input.modelspec == MusicGenertionModelEnum.EDENN_STUDIO:
            if stage_input.provider_c_custom_mode and stage_input.include_vocals:
                music_prompt_builder_input = prompt_builder.build_provider_c_custom_lyrics_messages(
                    stage_input.list_of_scene,
                    include_vocals=stage_input.include_vocals,
                    vocal_gender=stage_input.vocal_gender,
                    user_prompt=stage_input.user_prompt,
                    language=stage_input.language,
                )
                call_list.append(self.llm_client.complete_messages(
                    music_prompt_builder_input, json_schema=ResponseSchemas.provider_c_custom_lyrics()))
            else:
                music_prompt_builder_input = prompt_builder.build_provider_c_simple_prompt_messages(
                    stage_input.list_of_scene,
                    include_vocals=stage_input.include_vocals,
                    vocal_gender=stage_input.vocal_gender,
                    user_prompt=stage_input.user_prompt,
                    language=stage_input.language,
                )
                call_list.append(self.llm_client.complete_messages(
                    music_prompt_builder_input, json_schema=ResponseSchemas.provider_c_simple_prompt()))
        elif stage_input.modelspec == MusicGenertionModelEnum.EDENN_ENHANCED:
            music_prompt_builder_input = prompt_builder.build_provider_b_music_prompt_messages(
                stage_input.list_of_scene,
                include_vocals=stage_input.include_vocals,
                vocal_gender=stage_input.vocal_gender,
                user_prompt=stage_input.user_prompt,
                language=stage_input.language,
            )
            call_list.append(self.llm_client.complete_messages(
                music_prompt_builder_input, json_schema=ResponseSchemas.provider_b_dual_prompt()))
        else:
            music_prompt_builder_input = prompt_builder.build_provider_a_music_prompt_messages(
                stage_input.list_of_scene,
                include_vocals=stage_input.include_vocals,
                vocal_gender=stage_input.vocal_gender,
                user_prompt=stage_input.user_prompt,
                language=stage_input.language,
            )

            call_list.append(self.llm_client.complete_messages(
                music_prompt_builder_input, json_schema=ResponseSchemas.music_alignment()))

        logger.info(
            f"Music Prompt Generation: {music_prompt_builder_input} with Model type {stage_input.modelspec}")

        outputs = await asyncio.gather(*call_list)
        music_prompt_output, music_prompt_usage = outputs[0]

        # If dual-prompt provider, keep both keys
        if (
            stage_input.verbose_instruction
            and stage_input.modelspec
            in {
                MusicGenertionModelEnum.EDENN_ENHANCED,
                MusicGenertionModelEnum.EDENN_STUDIO,
            }
            and not (
                stage_input.modelspec == MusicGenertionModelEnum.EDENN_STUDIO
                and not stage_input.include_vocals
            )
        ):
            style_prompt = _require_generated_prompt_field(
                music_prompt_output,
                "style_prompt",
                modelspec=stage_input.modelspec,
                request_context="a verbose request",
            )
            if stage_input.include_vocals:
                # The caller's lyrics_prompt guides the prompt LLM upstream.
                # The provider receives only the generated, video-adapted prompt.
                lyrics_prompt = _require_generated_prompt_field(
                    music_prompt_output,
                    "lyrics_prompt",
                    modelspec=stage_input.modelspec,
                    request_context="a verbose vocal request",
                )
            else:
                # Intentional provider contract for instrumental/no-lyrics requests.
                lyrics_prompt = ""

            music_prompt_output = {
                "style_prompt": style_prompt,
                "lyrics_prompt": lyrics_prompt,
            }
        elif stage_input.modelspec == MusicGenertionModelEnum.EDENN_ENHANCED:
            style_prompt = _require_generated_prompt_field(
                music_prompt_output,
                "style_prompt",
                modelspec=stage_input.modelspec,
                request_context="an edenn_enhanced request",
            )
            if stage_input.include_vocals:
                lyrics_prompt = _require_generated_prompt_field(
                    music_prompt_output,
                    "lyrics_prompt",
                    modelspec=stage_input.modelspec,
                    request_context="an edenn_enhanced vocal request",
                )
            else:
                lyrics_prompt = ""
            music_prompt_output = {
                "style_prompt": style_prompt,
                "lyrics_prompt": lyrics_prompt,
            }

        logger.info(f"Music Generation Prompt : {music_prompt_output}")

        total_usage = {
            "prompt_tokens": music_prompt_usage.get("prompt_tokens", 0),
            "completion_tokens": music_prompt_usage.get("completion_tokens", 0),
            "total_tokens": music_prompt_usage.get("total_tokens", 0),
        }

        output = MusicPromptOrchestrationStageOutput(
            music_generation_prompt=music_prompt_output,
            token_usage=total_usage,
            downstream_generation_model_spec=stage_input.modelspec
        )
        safe_emit_annotation(
            stage_input.annotation_dispatcher,
            lambda: MusicPromptEvent(
                job_id=stage_input.job_id,
                model_spec=stage_input.modelspec,
                style_prompt=output.music_generation_prompt.get(
                    "style_prompt"),
                lyrics_prompt=output.music_generation_prompt.get(
                    "lyrics_prompt"),
                combined_prompt=output.music_generation_prompt.get("prompt"),
                include_vocals=stage_input.include_vocals,
                vocal_gender=stage_input.vocal_gender,
                generation_language=stage_input.language,
                stage_latency_s=time.time() - _stage_start,
                token_usage=output.token_usage,
                prompt_dict=dict(output.music_generation_prompt),
            ),
        )
        return output
