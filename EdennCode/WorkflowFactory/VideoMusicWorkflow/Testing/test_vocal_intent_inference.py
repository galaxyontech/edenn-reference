"""Vocal-intent inference: an explicit vocal request must never be rendered
instrumental. Regression origin (2026-07-20): video-music job
job_661c0cb88b4141299b374b70c07733b1 had prompt "[vocal_gender: female]
[language: english]" but the LLM preprocessor inferred detected_include_vocals=
false, so the pipeline produced a 100s instrumental (full_lyrics=None).

These offline tests pin the deterministic safety net (which corrects the false
inference from the prompt's explicit gender tag) and its adversarial negatives
(instrumental / no-vocals requests must NOT be forced to vocals). The real-LLM
behavior is covered by the gated remote_integration battery.
"""
import asyncio
import unittest
from unittest.mock import AsyncMock

from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.UserIntentUnderstandingStage.user_prompt_preprocessor import (
    UserPromptPreprocessorAgent,
    UserPromptPreprocessingStageInput,
    UserPromptPreprocessorResult,
    parse_prompt_language,
    parse_prompt_vocal_gender,
    prompt_declares_vocal_gender,
    resolve_effective_include_vocals,
)


class VocalIntentHelperTests(unittest.TestCase):
    # (prompt, expected prompt_declares_vocal_gender)
    POSITIVE = [
        "[vocal_gender: female]\n[language: english]\n",   # the failing case
        "[vocal_gender: male]",
        "vocal_gender=female",
        "vocal-gender : Male",
        "add a female vocal",
        "warm female voice please",
        "energetic male singer",
        "a female vocalist over the beat",
        "male lead vocal, anthemic",
    ]
    # Must NOT be read as a vocal request (adversarial negatives).
    NEGATIVE = [
        "",
        "instrumental only, no vocals",
        "calm piano background score",
        "upbeat instrumental for a product reel",
        "no singing, ambient pads",
        "make it sound like a female artist's production vibe",  # 'female' not + vocal noun
        "cinematic orchestral, no lyrics",
        "female lead dancer in the video",  # 'female lead' but 'dancer', not vocal
    ]

    def test_positive_prompts_declare_vocals(self):
        for p in self.POSITIVE:
            with self.subTest(prompt=p):
                self.assertTrue(prompt_declares_vocal_gender(p), p)

    def test_negative_prompts_do_not_declare_vocals(self):
        for p in self.NEGATIVE:
            with self.subTest(prompt=p):
                self.assertFalse(prompt_declares_vocal_gender(p), p)

    def test_resolve_corrects_false_inference_on_explicit_gender(self):
        # The exact regression: model said instrumental, prompt says female vocals.
        self.assertTrue(resolve_effective_include_vocals(
            detected_include_vocals=False,
            user_prompt="[vocal_gender: female]\n[language: english]\n",
        ))

    def test_resolve_never_forces_vocals_on_instrumental(self):
        for p in self.NEGATIVE:
            with self.subTest(prompt=p):
                self.assertFalse(resolve_effective_include_vocals(
                    detected_include_vocals=False, user_prompt=p), p)

    def test_resolve_never_turns_vocals_off(self):
        self.assertTrue(resolve_effective_include_vocals(
            detected_include_vocals=True, user_prompt="instrumental only"))

    def test_tag_fallback_parsers(self):
        self.assertEqual(parse_prompt_vocal_gender("[vocal_gender: female][language: english]"), "female")
        self.assertEqual(parse_prompt_vocal_gender("vocal_gender = MALE"), "male")
        self.assertIsNone(parse_prompt_vocal_gender("no gender here"))
        self.assertEqual(parse_prompt_language("[vocal_gender: female][language: english]"), "english")
        self.assertEqual(parse_prompt_language("[language: mandarin]"), "mandarin")
        self.assertIsNone(parse_prompt_language("no language tag"))


class VocalIntentStageTests(unittest.IsolatedAsyncioTestCase):
    """Drive the real run() with a mocked LLM to prove the correction end-to-end."""

    def _agent_returning(self, result: UserPromptPreprocessorResult) -> UserPromptPreprocessorAgent:
        agent = UserPromptPreprocessorAgent()  # no client built (preprocess is mocked)
        agent.preprocess = AsyncMock(return_value=result)
        return agent

    async def _run(self, agent, prompt):
        return await agent.run(UserPromptPreprocessingStageInput(
            user_prompt=prompt, verbose_instruction=False,
            music_style_prompt=None, lyrics_prompt=None,
            music_model_spec="edenn_enhanced", job_id="t",
        ))

    async def test_false_inference_with_gender_tag_is_corrected(self):
        # Model said instrumental AND blanked the vocal fields (its usual shape
        # when it decides no vocals). The stage must correct to vocals and
        # backfill gender/language from the prompt tags.
        result = UserPromptPreprocessorResult(
            was_transformed=False, detected_include_vocals=False,
            transformed_prompt="A whimsical instrumental score.",
            detected_vocal_gender="unknown", detected_vocal_language="",
            detected_language="ENGLISH_US", tokens_used=5,
        )
        out = await self._run(self._agent_returning(result),
                              "[vocal_gender: female]\n[language: english]\n")
        self.assertTrue(out.effective_include_vocals)
        self.assertEqual(out.effective_vocal_gender, "female")
        self.assertTrue(out.effective_language.strip())

    async def test_genuine_instrumental_stays_instrumental(self):
        # No explicit gender in the prompt; the model's default 'Female' gender
        # must NOT force vocals.
        result = UserPromptPreprocessorResult(
            was_transformed=False, detected_include_vocals=False,
            transformed_prompt="Calm piano background, no vocals.",
            detected_vocal_gender="Female",  # dataclass default — must be ignored
            detected_vocal_language="", detected_language="ENGLISH_US", tokens_used=5,
        )
        out = await self._run(self._agent_returning(result),
                              "calm piano background, no vocals")
        self.assertFalse(out.effective_include_vocals)

    async def test_model_true_is_respected_unchanged(self):
        result = UserPromptPreprocessorResult(
            was_transformed=False, detected_include_vocals=True,
            transformed_prompt="Female English pop vocals.",
            detected_vocal_gender="female", detected_vocal_language="ENGLISH_US",
            detected_language="ENGLISH_US", tokens_used=5,
        )
        out = await self._run(self._agent_returning(result),
                              "female english pop with vocals")
        self.assertTrue(out.effective_include_vocals)
        self.assertEqual(out.effective_vocal_gender, "female")


if __name__ == "__main__":
    unittest.main()
