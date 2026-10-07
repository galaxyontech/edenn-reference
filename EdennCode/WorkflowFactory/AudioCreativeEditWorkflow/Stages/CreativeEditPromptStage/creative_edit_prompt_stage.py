from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

from EdennCode.ModelFactory.LanguageModelFactory import AzureMultimodalClient
from EdennCode.ModelFactory.PromptFactory.prompts import (
    PromptBuilder,
    ResponseSchemas,
    format_scene_lines,
)

from EdennCode.WorkflowFactory.AudioCreativeEditWorkflow.Stages.VisualConditioningStage.visual_conditioning_stage import (
    VisualConditioningStageOutput,
)


@dataclass
class CreativeEditPromptStageInput:
    visual_analysis: VisualConditioningStageOutput
    user_prompt: str
    include_vocals: bool
    vocal_gender: str
    language: str


@dataclass
class CreativeEditPromptStageOutput:
    prompt_payload: Dict[str, str] = field(default_factory=dict)
    token_usage: Dict[str, int] = field(default_factory=dict)


class CreativeEditPromptStage:
    def __init__(self, *, llm_client: AzureMultimodalClient) -> None:
        self.llm_client = llm_client

    async def run(self, stage_input: CreativeEditPromptStageInput) -> CreativeEditPromptStageOutput:
        messages = PromptBuilder.build_audio_creative_edit_messages(
            visual_context=self._build_visual_context(stage_input.visual_analysis),
            visual_input_type=stage_input.visual_analysis.input_type,
            include_vocals=stage_input.include_vocals,
            vocal_gender=stage_input.vocal_gender,
            user_prompt=stage_input.user_prompt,
            language=stage_input.language,
        )
        prompt_payload, usage = await self.llm_client.complete_messages(
            messages,
            json_schema=ResponseSchemas.audio_creative_edit_prompt(),
        )
        return CreativeEditPromptStageOutput(
            prompt_payload=dict(prompt_payload),
            token_usage=_normalize_usage(usage),
        )

    @staticmethod
    def _build_visual_context(visual_analysis: VisualConditioningStageOutput) -> str:
        lines = [
            f"Summary: {visual_analysis.summary or 'No visual summary provided.'}",
            f"Overall mood: {visual_analysis.overall_mood or 'Not specified'}",
            f"Visual style: {visual_analysis.visual_style or 'Not specified'}",
            f"Creative direction: {visual_analysis.creative_direction or 'Not specified'}",
        ]
        if visual_analysis.key_elements:
            lines.append(f"Key elements: {', '.join(visual_analysis.key_elements)}")
        if visual_analysis.scenes:
            lines.append("Scene list:")
            lines.append(format_scene_lines(visual_analysis.scenes))
        return "\n".join(lines)


def _normalize_usage(usage: Dict[str, Any] | None) -> Dict[str, int]:
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
