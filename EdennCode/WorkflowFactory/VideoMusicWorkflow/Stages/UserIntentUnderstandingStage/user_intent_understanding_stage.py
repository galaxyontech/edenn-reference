"""
DEPRECATED: This module is deprecated and will be removed in a future version.

Use `UserPromptPreprocessorAgent` from `user_prompt_preprocessor.py` instead, which combines:
- TOS compliance (celebrity voice sanitization)
- Language detection (Chinese/English)
- Video category detection

Migration:
    # Old
    from ...user_intent_understanding_stage import UserIntentUnderstandingStage
    stage = UserIntentUnderstandingStage(classifier_client=client)
    result = await stage.run(UserIntentUnderstandingStageInputs(user_prompt=prompt))
    is_chinese = result.is_chinese
    
    # New
    from ...user_prompt_preprocessor import UserPromptPreprocessorAgent
    agent = UserPromptPreprocessorAgent(llm_client=client)
    result = await agent.preprocess(prompt)
    language = result.detected_language  # Language.CN or Language.EN
    sanitized_prompt = result.transformed_prompt
"""
import warnings
from dataclasses import dataclass
from typing import Dict

from EdennCode.ModelFactory.LanguageModelFactory import AzureMultimodalClient
from EdennCode.ModelFactory.VoiceOverModelFactory.model_gateway_base_model import load_env
from EdennCode.Util.MediaUtils.pipeline_util import build_azure_client

warnings.warn(
    "UserIntentUnderstandingStage is deprecated. Use UserPromptPreprocessorAgent instead.",
    DeprecationWarning,
    stacklevel=2,
)


@dataclass
class UserIntentUnderstandingStageInputs:
    user_prompt : str

@dataclass
class UserIntentUnderstandingStageOutputs:
    """
    DEPRECATED: Binary flag: True if the user is requesting Chinese lyrics/content; otherwise False.
    """
    is_chinese: bool = False

classifier_schema: Dict = {
    "name": "is_chinese_request",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "is_chinese": {
                "type": "boolean",
                "description": "True if the user wants Chinese/Mandarin lyrics or wrote in Chinese; False otherwise.",
            },
        },
        "required": ["is_chinese"],
        "additionalProperties": False,
    },
}


@dataclass
class UserIntentUnderstandingStage:
    """
    DEPRECATED: Use UserPromptPreprocessorAgent instead.
    
    This stage only detected Chinese vs English. The new UserPromptPreprocessorAgent
    combines TOS compliance + language detection + category detection in one LLM call.
    """
    def __init__(self, classifier_client: AzureMultimodalClient):
        warnings.warn(
            "UserIntentUnderstandingStage is deprecated. Use UserPromptPreprocessorAgent instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.classifier_client = classifier_client

    async def run(self, stage_input: UserIntentUnderstandingStageInputs) -> UserIntentUnderstandingStageOutputs:
        """
        Binary classify whether the user asks for Chinese lyrics/content.
        """
        user_prompt = (stage_input.user_prompt or "").strip()
        if not user_prompt:
            return UserIntentUnderstandingStageOutputs(is_chinese=False)

        system_instruction = (
            "Determine if the user is requesting Chinese lyrics (Mandarin / Putonghua / 中文 / 汉字 / Simplified Chinese). "
            "Return is_chinese=true if the prompt is in Chinese or asks for Chinese lyrics. "
            "Return is_chinese=false for English or any other language, or if unclear. "
            "Prefer false when ambiguous."
        )

        messages = [
            {"role": "system", "content": [{"type": "text", "text": system_instruction}]},
            {"role": "user", "content": [{"type": "text", "text": user_prompt}]},
        ]

        response, _ = await self.classifier_client.complete_messages(
            messages,
            json_schema=classifier_schema,
            max_tokens=400,
        )

        is_chinese = bool(response.get("is_chinese", False))

        return UserIntentUnderstandingStageOutputs(is_chinese=is_chinese)


if __name__ == "__main__":
    import asyncio
    load_env()
    intent_classifier = UserIntentUnderstandingStage(classifier_client=build_azure_client())
    output = asyncio.run(intent_classifier.run(stage_input=UserIntentUnderstandingStageInputs(user_prompt="")))
    print(output)

