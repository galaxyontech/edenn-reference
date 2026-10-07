from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from EdennCode.ModelFactory.LanguageModelFactory import AzureMultimodalClient
from EdennCode.ModelFactory.PromptFactory.video_to_sound_effect_schema import (
    VideoToSoundEffectSchemas,
)
from EdennCode.Util.MediaUtils.pipeline_util import build_azure_client


@dataclass
class UserPromptUnderstandingStageInput:
    user_prompt: Optional[str] = None


@dataclass
class UserPromptUnderstandingStageOutput:
    extract_sfx_description: str
    extract_intent_where_to_add_this_event: str


class UserPromptUnderstandingStage:
    """
    Extract structured SFX intent from user prompt text.
    """

    def __init__(self, llm_client: Optional[AzureMultimodalClient] = None) -> None:
        self.llm_client: AzureMultimodalClient = llm_client or build_azure_client()

    async def run(
        self, stage_input: UserPromptUnderstandingStageInput
    ) -> UserPromptUnderstandingStageOutput:
        prompt = (stage_input.user_prompt or "").strip()
        schema = VideoToSoundEffectSchemas.user_prompt_understanding()
        messages = self._build_messages(prompt)
        payload, _usage = await self.llm_client.complete_messages(
            messages=messages,
            json_schema=schema,
            max_tokens=400,
        )

        return UserPromptUnderstandingStageOutput(
            extract_sfx_description=str(
                payload.get("extract_sfx_description", prompt)
            ).strip(),
            extract_intent_where_to_add_this_event=str(
                payload.get("extract_intent_where_to_add_this_event", "")
            ).strip(),
        )

    @staticmethod
    def _build_messages(prompt: str) -> list[dict]:
        instruction = (
            "Extract user intent for video sound effects. "
            "Return JSON only with extract_sfx_description and "
            "extract_intent_where_to_add_this_event."
        )
        return [
            {
                "role": "system",
                "content": [{"type": "text", "text": instruction}],
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            },
        ]
