from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.cloud_music_gen_util import (
    build_edenn_enhanced_music_provider,
)
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_c import ProviderCApi
from EdennCode.Util.MediaUtils.pipeline_util import build_azure_client
from EdennCode.WorkflowFactory.AudioCreativeEditWorkflow.Stages.AudioCreativeGenerationStage.audio_creative_generation_stage import (
    AudioCreativeGenerationStage,
    AudioCreativeGenerationStageInput,
)
from EdennCode.WorkflowFactory.AudioCreativeEditWorkflow.Stages.CreativeEditPromptStage.creative_edit_prompt_stage import (
    CreativeEditPromptStage,
    CreativeEditPromptStageInput,
)
from EdennCode.WorkflowFactory.AudioCreativeEditWorkflow.Stages.VisualConditioningStage.visual_conditioning_stage import (
    VisualConditioningStage,
    VisualConditioningStageInput,
    VisualConditioningStageOutput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.word_ts import (
    WordTS,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.UserIntentUnderstandingStage.user_prompt_preprocessor import (
    UserPromptPreprocessorAgent,
    UserPromptPreprocessorResult,
)


logger = logging.getLogger("Audio Creative Edit Task")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@dataclass
class AudioCreativeEditWorkflowInput:
    source_audio_path: Path
    user_prompt: str
    modelspec: str
    source_audio_provider_url: Optional[str] = None
    video_path: Optional[Path] = None
    image_paths: List[Path] = field(default_factory=list)
    vocal_id: Optional[str] = None
    vocal_sample_path: Optional[Path] = None
    provider_c_custom_mode: bool = False
    provider_c_style_weight: Optional[float] = None
    provider_c_audio_weight: Optional[float] = None
    provider_c_weirdness_constraint: Optional[float] = None


@dataclass
class AudioCreativeEditWorkflowOutput:
    source_audio_path: Path
    edited_audio_path: Path
    secondary_edited_audio_path: Optional[Path]
    visual_analysis: VisualConditioningStageOutput
    creative_edit_prompt: Dict[str, Any]
    lyrics_timestamps: List[WordTS]
    include_vocals: bool
    vocal_gender: str
    user_requested_language: str
    token_usage: Dict[str, int]
    token_usage_breakdown: Dict[str, Dict[str, int]]
    used_music_model_spec: str
    job_received_timestamp: int
    job_finished_timestamp: int
    vocal_id_used: Optional[str] = None


class AudioCreativeEditWorkflow:
    def __init__(
        self,
        *,
        storage_service: Any = None,
        llm_image_container: Optional[str] = None,
        llm_image_sas_ttl_minutes: int = 5,
        llm_image_cleanup_delay_seconds: float = 2.0,
    ) -> None:
        self.azure_client = build_azure_client()
        self.user_prompt_preprocessor = UserPromptPreprocessorAgent(llm_client=self.azure_client)
        self.visual_conditioning_stage = VisualConditioningStage(
            llm_client=self.azure_client,
            storage_service=storage_service,
            llm_image_container=llm_image_container,
            llm_image_sas_ttl_minutes=llm_image_sas_ttl_minutes,
            llm_image_cleanup_delay_seconds=llm_image_cleanup_delay_seconds,
        )
        self.creative_edit_prompt_stage = CreativeEditPromptStage(llm_client=self.azure_client)
        self.provider_c_music_provider = None
        self.provider_b_music_provider = None

        def _build_provider_c_music_provider():
            if self.provider_c_music_provider is None:
                self.provider_c_music_provider = ProviderCApi()
            return self.provider_c_music_provider

        def _build_provider_b_music_provider():
            if self.provider_b_music_provider is None:
                self.provider_b_music_provider = build_edenn_enhanced_music_provider()
            return self.provider_b_music_provider

        self.audio_generation_stage = AudioCreativeGenerationStage(
            provider_c_music_provider_factory=_build_provider_c_music_provider,
            provider_b_music_provider_factory=_build_provider_b_music_provider,
        )

    async def run(
        self,
        workflow_input: AudioCreativeEditWorkflowInput,
    ) -> AudioCreativeEditWorkflowOutput:
        logger.info("Pipeline Start ")
        logger.info("Source Audio Path: %s", workflow_input.source_audio_path)
        logger.info("User Prompt: %s", workflow_input.user_prompt)
        logger.info("Requested Modelspec: %s", workflow_input.modelspec)

        job_received_timestamp = int(time.time())
        overall_start_time = time.time()

        logger.info(
            "================= 0. User Prompt Preprocessing (TOS Compliance + Intent Extraction) =============="
        )
        preprocessor_start_time = time.time()
        preprocessor_result: UserPromptPreprocessorResult = await self.user_prompt_preprocessor.preprocess(
            workflow_input.user_prompt
        )
        effective_include_vocals = bool(preprocessor_result.detected_include_vocals)
        effective_vocal_gender = preprocessor_result.detected_vocal_gender or "female"
        effective_language = (
            preprocessor_result.detected_vocal_language
            if effective_include_vocals
            else preprocessor_result.detected_language
        )
        logger.info(
            "User Intent Understanding stage took %.2fs",
            time.time() - preprocessor_start_time,
        )
        logger.info("Detected Include Vocals = %s", effective_include_vocals)
        logger.info("Downstream Generation Language = %s", effective_language)
        if effective_include_vocals:
            logger.info(
                "Detected Vocal Generation Request, Vocal Gender is = %s vocal language is = %s",
                effective_vocal_gender,
                effective_language,
            )
        else:
            logger.info(
                "Detected Instrument Generation Request, Vocal Request is  = %s",
                effective_include_vocals,
            )
        if preprocessor_result.was_transformed:
            logger.info(
                "User Prompt Inputs is transformed =%s",
                preprocessor_result.was_transformed,
            )
            logger.info("User Prompt Result: %s", preprocessor_result.transformed_prompt)

        logger.info(
            "================= 1. Visual Conditioning Stage =============="
        )
        visual_stage_start_time = time.time()
        visual_analysis = await self.visual_conditioning_stage.run(
            VisualConditioningStageInput(
                video_path=workflow_input.video_path,
                image_paths=list(workflow_input.image_paths),
                preferred_language=effective_language,
            )
        )
        logger.info(
            "Visual conditioning stage took %.2fs",
            time.time() - visual_stage_start_time,
        )
        logger.info("Resolved visual input type = %s", visual_analysis.input_type)
        if visual_analysis.scenes:
            logger.info("Detected %s visual scenes.", len(visual_analysis.scenes))
        if visual_analysis.summary:
            logger.info("Generated visual summary: %s", visual_analysis.summary)

        logger.info(
            "================= 2. Creative Edit Prompt Generation Stage =============="
        )
        creative_prompt_start_time = time.time()
        prompt_output = await self.creative_edit_prompt_stage.run(
            CreativeEditPromptStageInput(
                visual_analysis=visual_analysis,
                user_prompt=preprocessor_result.transformed_prompt,
                include_vocals=effective_include_vocals,
                vocal_gender=effective_vocal_gender,
                language=effective_language,
            )
        )
        logger.info(
            "Creative edit prompt stage took %.2fs",
            time.time() - creative_prompt_start_time,
        )
        logger.info("Creative edit prompt payload: %s", prompt_output.prompt_payload)

        logger.info(
            "================= 3. Audio Creative Generation Stage =============="
        )
        generation_stage_start_time = time.time()
        generation_output = await self.audio_generation_stage.run(
            AudioCreativeGenerationStageInput(
                source_audio_path=workflow_input.source_audio_path,
                source_audio_provider_url=workflow_input.source_audio_provider_url,
                prompt_payload=prompt_output.prompt_payload,
                include_vocals=effective_include_vocals,
                vocal_gender=effective_vocal_gender,
                modelspec=workflow_input.modelspec,
                workdir=workflow_input.source_audio_path.parent,
                vocal_id=workflow_input.vocal_id,
                vocal_sample_path=workflow_input.vocal_sample_path,
                provider_c_custom_mode=workflow_input.provider_c_custom_mode,
                provider_c_style_weight=workflow_input.provider_c_style_weight,
                provider_c_audio_weight=workflow_input.provider_c_audio_weight,
                provider_c_weirdness_constraint=workflow_input.provider_c_weirdness_constraint,
            )
        )
        logger.info(
            "Audio creative generation stage took %.2fs",
            time.time() - generation_stage_start_time,
        )
        logger.info("Used downstream music model = %s", generation_output.used_modelspec)
        logger.info("Edited audio path: %s", generation_output.edited_audio_path)
        if generation_output.secondary_edited_audio_path:
            logger.info(
                "Secondary edited audio path: %s",
                generation_output.secondary_edited_audio_path,
            )

        job_finished_timestamp = int(time.time())

        token_usage_breakdown = {
            "user_prompt_preprocessor": {
                "prompt_tokens": int(preprocessor_result.prompt_tokens or 0),
                "completion_tokens": int(preprocessor_result.completion_tokens or 0),
                "total_tokens": int(preprocessor_result.tokens_used or 0),
            },
            "visual_analysis": _normalize_usage(visual_analysis.token_usage),
            "creative_edit_prompt": _normalize_usage(prompt_output.token_usage),
        }
        total_usage = _sum_token_usage(
            token_usage_breakdown["user_prompt_preprocessor"],
            token_usage_breakdown["visual_analysis"],
            token_usage_breakdown["creative_edit_prompt"],
        )
        logger.info("Token usage breakdown: %s", token_usage_breakdown)
        logger.info("Total workflow latency: %.2fs", time.time() - overall_start_time)

        return AudioCreativeEditWorkflowOutput(
            source_audio_path=workflow_input.source_audio_path,
            edited_audio_path=generation_output.edited_audio_path,
            secondary_edited_audio_path=generation_output.secondary_edited_audio_path,
            visual_analysis=visual_analysis,
            creative_edit_prompt=prompt_output.prompt_payload,
            lyrics_timestamps=generation_output.lyrics_timestamps,
            include_vocals=effective_include_vocals,
            vocal_gender=effective_vocal_gender,
            user_requested_language=effective_language,
            token_usage=total_usage,
            token_usage_breakdown=token_usage_breakdown,
            used_music_model_spec=generation_output.used_modelspec,
            job_received_timestamp=job_received_timestamp,
            job_finished_timestamp=job_finished_timestamp,
            vocal_id_used=generation_output.vocal_id_used,
        )


def _normalize_usage(usage: Optional[Dict[str, Any]]) -> Dict[str, int]:
    if not usage:
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
    return {
        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
        "total_tokens": int(usage.get("total_tokens", 0) or 0),
    }


def _sum_token_usage(*usages: Dict[str, Any]) -> Dict[str, int]:
    total = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    for usage in usages:
        normalized = _normalize_usage(usage)
        total["prompt_tokens"] += normalized["prompt_tokens"]
        total["completion_tokens"] += normalized["completion_tokens"]
        total["total_tokens"] += normalized["total_tokens"]
    return total
