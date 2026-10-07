from __future__ import annotations

import asyncio
import logging
import mimetypes
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from EdennCode.ModelFactory.LanguageModelFactory import AzureMultimodalClient
from EdennCode.ModelFactory.PromptFactory.prompts import PromptBuilder, ResponseSchemas
from EdennCode.Util.blob_paths import safe_blob_path_component
from EdennCode.WorkflowFactory.VideoMusicWorkflow.PipelineDataModel.pipeline_datamodel import Language
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata.video_metadata import (
    VideoMetadata,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.datamodel import (
    SceneUnderstanding,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.scene_segmentation_stage import (
    SceneSegmentationStage,
    SceneSegmentationStageInput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoPreprocessStage.media_preprocess_stage import (
    InputMediaAssetTyps,
    PreprocessStage,
    PreprocessStageInput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoUnderstandingStage.video_understanding_page import (
    VideoUnderstandingStage,
    VideoUnderstandingStageInput,
)
from EdennCode.exceptions import EdennConfigurationError, EdennStorageError


logger = logging.getLogger("Audio Creative Edit Visual Conditioning")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@dataclass
class VisualConditioningStageInput:
    video_path: Optional[Path] = None
    image_paths: List[Path] = field(default_factory=list)
    preferred_language: str = Language.EN


@dataclass
class VisualConditioningStageOutput:
    input_type: str
    summary: str = ""
    overall_mood: str = ""
    visual_style: str = ""
    creative_direction: str = ""
    key_elements: List[str] = field(default_factory=list)
    scenes: List[SceneUnderstanding] = field(default_factory=list)
    video_metadata: Optional[VideoMetadata] = None
    thumbnail_path: Optional[Path] = None
    raw_summary: Dict[str, Any] = field(default_factory=dict)
    token_usage: Dict[str, int] = field(default_factory=dict)


class VisualConditioningStage:
    def __init__(
        self,
        *,
        llm_client: AzureMultimodalClient,
        storage_service: Any = None,
        llm_image_container: Optional[str] = None,
        llm_image_sas_ttl_minutes: int = 5,
        llm_image_cleanup_delay_seconds: float = 2.0,
    ) -> None:
        self.llm_client = llm_client
        self.storage_service = storage_service
        self.llm_image_container = (llm_image_container or "").strip()
        self.llm_image_sas_ttl_minutes = max(1, int(llm_image_sas_ttl_minutes))
        self.llm_image_cleanup_delay_seconds = max(0.0, float(llm_image_cleanup_delay_seconds))
        self.video_preprocess_stage = PreprocessStage()
        self.scene_segmentation_stage = SceneSegmentationStage(
            llm_model_client=llm_client,
            storage_service=storage_service,
            llm_image_container=self.llm_image_container,
            llm_image_sas_ttl_minutes=self.llm_image_sas_ttl_minutes,
            llm_image_cleanup_delay_seconds=self.llm_image_cleanup_delay_seconds,
        )
        self.video_understanding_stage = VideoUnderstandingStage(llm_client=llm_client)

    async def run(self, stage_input: VisualConditioningStageInput) -> VisualConditioningStageOutput:
        logger.info("Visual conditioning stage start")
        if stage_input.video_path:
            logger.info("Visual conditioning input resolved to video path: %s", stage_input.video_path)
            return await self._run_video(stage_input.video_path, stage_input.preferred_language)
        if stage_input.image_paths:
            logger.info(
                "Visual conditioning input resolved to %s image(s).",
                len(stage_input.image_paths),
            )
            return await self._run_images(stage_input.image_paths)
        logger.info("No visual conditioning inputs provided. Proceeding without visual analysis.")
        return VisualConditioningStageOutput(input_type="none")

    async def _run_video(
        self,
        video_path: Path,
        preferred_language: str,
    ) -> VisualConditioningStageOutput:
        logger.info(
            "================ 1.1 Visual Video Preprocess and Metadata Extraction ================"
        )
        preprocess_start_time = time.time()
        preprocess_output = await self.video_preprocess_stage.run(
            PreprocessStageInput(
                asset_path=str(video_path),
                asset_type=InputMediaAssetTyps.VIDEO,
            )
        )
        logger.info(
            "Visual video preprocess stage took %.2fs",
            time.time() - preprocess_start_time,
        )
        video_metadata = preprocess_output.video_metadata
        logger.info(
            "================ 1.2 Visual Scene Segmentation Stage ================"
        )
        segmentation_start_time = time.time()
        segmentation_output = await self.scene_segmentation_stage.run(
            SceneSegmentationStageInput(
                video_path=video_metadata.path,
                duration=video_metadata.duration,
                fps=video_metadata.fps,
            )
        )
        logger.info(
            "Visual scene segmentation stage took %.2fs",
            time.time() - segmentation_start_time,
        )
        logger.info(
            "Detected %s visual scenes.",
            len(segmentation_output.scene_understanding_messages),
        )
        logger.info(
            "================ 1.3 Visual Video Understanding Stage ================"
        )
        understanding_start_time = time.time()
        understanding_output = await self.video_understanding_stage.run(
            VideoUnderstandingStageInput(
                list_of_scene=segmentation_output.scene_understanding_messages,
                preferred_language=preferred_language,
            )
        )
        logger.info(
            "Visual video understanding stage took %.2fs",
            time.time() - understanding_start_time,
        )
        raw_summary = (
            understanding_output.video_descriptions
            if isinstance(understanding_output.video_descriptions, dict)
            else {"summary": str(understanding_output.video_descriptions or "")}
        )
        logger.info("Generated visual description: %s", raw_summary)
        return VisualConditioningStageOutput(
            input_type="video",
            summary=(raw_summary.get("summary") or "").strip(),
            overall_mood=(raw_summary.get("overall_mood") or "").strip(),
            visual_style=(raw_summary.get("core_message") or "").strip(),
            creative_direction=(raw_summary.get("video_description") or "").strip(),
            key_elements=[],
            scenes=list(segmentation_output.scene_understanding_messages),
            video_metadata=video_metadata,
            thumbnail_path=segmentation_output.thumbnail_path,
            raw_summary=raw_summary,
            token_usage=_sum_token_usage(
                segmentation_output.token_usage,
                understanding_output.token_usage,
            ),
        )

    async def _run_images(self, image_paths: List[Path]) -> VisualConditioningStageOutput:
        logger.info(
            "================ 1.1 Visual Image Batch Analysis Stage ================"
        )
        self._ensure_storage()
        normalized_paths: List[Path] = []
        normalize_start_time = time.time()
        for image_path in image_paths:
            resolved = Path(image_path).expanduser().resolve()
            if not resolved.exists():
                raise FileNotFoundError(resolved)
            normalized_paths.append(resolved)
        logger.info(
            "Resolved %s image(s) for visual conditioning in %.2fs",
            len(normalized_paths),
            time.time() - normalize_start_time,
        )

        uploaded_blobs: List[Any] = []
        try:
            upload_start_time = time.time()
            for idx, image_path in enumerate(normalized_paths):
                mime_type, _ = mimetypes.guess_type(str(image_path))
                safe_image_name = safe_blob_path_component(
                    image_path.stem,
                    fallback_prefix="image",
                )
                uploaded_blob = await asyncio.to_thread(
                    self.storage_service.upload_temporary_bytes,
                    container=self.llm_image_container,
                    blob_name=(
                        f"llm-inputs/audio-creative-edit/{safe_image_name}_"
                        f"{idx:03d}_{uuid4().hex}{image_path.suffix or '.png'}"
                    ),
                    data=image_path.read_bytes(),
                    content_type=mime_type or "image/png",
                    ttl_minutes=self.llm_image_sas_ttl_minutes,
                )
                if uploaded_blob is None:
                    raise EdennStorageError(
                        "Failed to upload temporary image for creative edit visual analysis.",
                        component="audio_creative_edit",
                        operation="upload_temporary_image",
                        context={"image_path": str(image_path)},
                    )
                uploaded_blobs.append(uploaded_blob)
            logger.info(
                "Uploaded %s temporary visual image(s) in %.2fs",
                len(uploaded_blobs),
                time.time() - upload_start_time,
            )

            analysis_start_time = time.time()
            messages = PromptBuilder.build_image_batch_visual_analysis_messages(
                [blob.sas_url for blob in uploaded_blobs]
            )
            output = await self.llm_client.complete_messages(
                messages,
                json_schema=ResponseSchemas.image_batch_visual_analysis(),
            )
            content, usage = output
            logger.info(
                "Image batch visual analysis stage took %.2fs",
                time.time() - analysis_start_time,
            )
            key_elements = content.get("key_elements") or []
            if not isinstance(key_elements, list):
                key_elements = []
            logger.info("Generated visual description: %s", content)
            return VisualConditioningStageOutput(
                input_type="images",
                summary=(content.get("summary") or "").strip(),
                overall_mood=(content.get("overall_mood") or "").strip(),
                visual_style=(content.get("visual_style") or "").strip(),
                creative_direction=(content.get("creative_direction") or "").strip(),
                key_elements=[str(item) for item in key_elements if item is not None],
                thumbnail_path=normalized_paths[0],
                raw_summary=dict(content),
                token_usage=_normalize_usage(usage),
            )
        finally:
            await self._cleanup_temporary_blobs(uploaded_blobs)

    def _ensure_storage(self) -> None:
        if self.storage_service is None or not getattr(self.storage_service, "enabled", False):
            raise EdennConfigurationError(
                "Image-based creative edit conditioning requires Azure Blob Storage for temporary LLM image URLs.",
                public_message="Image-based visual conditioning is unavailable right now.",
                component="audio_creative_edit",
                operation="image_visual_conditioning",
            )
        if not self.llm_image_container:
            raise EdennConfigurationError(
                "AZURE_STORAGE_LLM_IMAGE_CONTAINER must be configured for image conditioning.",
                component="audio_creative_edit",
                operation="image_visual_conditioning",
            )

    async def _cleanup_temporary_blobs(self, temporary_blobs: List[Any]) -> None:
        if not temporary_blobs or self.storage_service is None:
            return
        if self.llm_image_cleanup_delay_seconds > 0:
            await asyncio.sleep(self.llm_image_cleanup_delay_seconds)
        logger.info("Cleaning up %s temporary visual blob(s).", len(temporary_blobs))
        for blob in temporary_blobs:
            try:
                await asyncio.to_thread(
                    self.storage_service.delete_blob,
                    container=blob.container,
                    blob_name=blob.blob_name,
                )
            except Exception:
                continue


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


def _sum_token_usage(*usages: Optional[Dict[str, Any]]) -> Dict[str, int]:
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
