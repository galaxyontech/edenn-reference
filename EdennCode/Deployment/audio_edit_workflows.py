from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from EdennCode.WorkflowFactory.AudioCreativeEditWorkflow import (
    AudioCreativeEditWorkflow,
    AudioCreativeEditWorkflowInput,
    AudioCreativeEditWorkflowOutput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.word_ts import (
    WordTS,
)

VALID_CREATIVE_EDIT_MODEL_SPECS = {"edenn_enhanced", "edenn_studio"}
LEGACY_MODEL_MAP = {
    "provider_c": "edenn_studio",
}


@dataclass
class AudioCreativeEditResult:
    source_audio_path: Path
    edited_audio_path: Path
    secondary_edited_audio_path: Optional[Path]
    visual_analysis: Any
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


class AudioCreativeEditOrchestrator:
    def __init__(
        self,
        *,
        storage: Any = None,
        llm_image_container: Optional[str] = None,
        llm_image_sas_ttl_minutes: int = 5,
        llm_image_cleanup_delay_seconds: float = 2.0,
    ) -> None:
        self.workflow = AudioCreativeEditWorkflow(
            storage_service=storage,
            llm_image_container=llm_image_container,
            llm_image_sas_ttl_minutes=llm_image_sas_ttl_minutes,
            llm_image_cleanup_delay_seconds=llm_image_cleanup_delay_seconds,
        )

    @staticmethod
    def _normalize_modelspec(value: Optional[str]) -> str:
        normalized = (value or "").strip().lower()
        mapped = LEGACY_MODEL_MAP.get(normalized, normalized)
        if mapped in VALID_CREATIVE_EDIT_MODEL_SPECS:
            return mapped
        return "edenn_enhanced"

    async def run(
        self,
        *,
        source_audio_path: Path,
        user_prompt: str,
        modelspec: str,
        source_audio_provider_url: Optional[str] = None,
        video_path: Optional[Path] = None,
        image_paths: Optional[List[Path]] = None,
        vocal_id: Optional[str] = None,
        vocal_sample_path: Optional[Path] = None,
        provider_c_custom_mode: bool = False,
        provider_c_style_weight: Optional[float] = None,
        provider_c_audio_weight: Optional[float] = None,
        provider_c_weirdness_constraint: Optional[float] = None,
    ) -> AudioCreativeEditResult:
        workflow_output: AudioCreativeEditWorkflowOutput = await self.workflow.run(
            AudioCreativeEditWorkflowInput(
                source_audio_path=source_audio_path,
                user_prompt=user_prompt,
                modelspec=self._normalize_modelspec(modelspec),
                source_audio_provider_url=source_audio_provider_url,
                video_path=video_path,
                image_paths=list(image_paths or []),
                vocal_id=vocal_id,
                vocal_sample_path=vocal_sample_path,
                provider_c_custom_mode=provider_c_custom_mode,
                provider_c_style_weight=provider_c_style_weight,
                provider_c_audio_weight=provider_c_audio_weight,
                provider_c_weirdness_constraint=provider_c_weirdness_constraint,
            )
        )
        return AudioCreativeEditResult(
            source_audio_path=workflow_output.source_audio_path,
            edited_audio_path=workflow_output.edited_audio_path,
            secondary_edited_audio_path=workflow_output.secondary_edited_audio_path,
            visual_analysis=workflow_output.visual_analysis,
            creative_edit_prompt=workflow_output.creative_edit_prompt,
            lyrics_timestamps=workflow_output.lyrics_timestamps,
            include_vocals=workflow_output.include_vocals,
            vocal_gender=workflow_output.vocal_gender,
            user_requested_language=workflow_output.user_requested_language,
            token_usage=workflow_output.token_usage,
            token_usage_breakdown=workflow_output.token_usage_breakdown,
            used_music_model_spec=workflow_output.used_music_model_spec,
            job_received_timestamp=workflow_output.job_received_timestamp,
            job_finished_timestamp=workflow_output.job_finished_timestamp,
            vocal_id_used=workflow_output.vocal_id_used,
        )


__all__ = ["AudioCreativeEditOrchestrator", "AudioCreativeEditResult"]
