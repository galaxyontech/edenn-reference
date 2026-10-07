import unittest
import asyncio

from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.Stages.UserPromptUnderstandingStage.user_prompt_understanding_stage import (
    UserPromptUnderstandingStage,
    UserPromptUnderstandingStageInput,
)


class _FakePromptLLM:
    async def complete_messages(self, messages, *, json_schema, max_tokens=400):
        user_text = messages[1]["content"][0]["text"]
        return (
            {
                "extract_sfx_description": user_text or "",
                "extract_intent_where_to_add_this_event": "across transitions",
            },
            {},
        )


class UserPromptUnderstandingStageTests(unittest.TestCase):
    def test_empty_prompt_returns_empty_description(self) -> None:
        stage = UserPromptUnderstandingStage(llm_client=_FakePromptLLM())
        output = asyncio.run(stage.run(UserPromptUnderstandingStageInput(user_prompt="")))
        self.assertEqual(output.extract_sfx_description, "")
        self.assertEqual(output.extract_intent_where_to_add_this_event, "across transitions")

    def test_understanding_returns_structured_output(self) -> None:
        stage = UserPromptUnderstandingStage(llm_client=_FakePromptLLM())
        output = asyncio.run(
            stage.run(
                UserPromptUnderstandingStageInput(
                    user_prompt="Add metallic whoosh for every transition."
                )
            )
        )
        self.assertIn("metallic whoosh", output.extract_sfx_description.lower())
        self.assertEqual(output.extract_intent_where_to_add_this_event, "across transitions")


if __name__ == "__main__":
    unittest.main()
