import asyncio

import pytest

from EdennCode.WorkflowFactory.VideoMusicWorkflow.PipelineDataModel.pipeline_datamodel import (
    VideoCategory,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.UserIntentUnderstandingStage.user_prompt_preprocessor import (
    UserPromptPreprocessorAgent,
    UserPromptPreprocessingStageInput,
)
from EdennCode.TestSuites.helpers.integration import (
    handle_remote_failure,
    require_remote_env,
    run_with_remote_rate_limit_retry,
)


# Adversarial vocal-intent battery run against the REAL preprocessor + the
# deterministic correction net (via run()). Frontend tag formats and natural
# language must resolve to vocals; instrumental requests must NOT. Guards the
# 2026-07-20 regression where "[vocal_gender: female][language: english]" was
# rendered instrumental.
_VOCAL_INTENT_CASES = [
    ("[vocal_gender: female]\n[language: english]\n", True),   # the failing case
    ("[vocal_gender: male]\n[language: english]\n", True),
    ("Create bright Mandarin pop vocals for a hotel promo.", True),
    ("upbeat track with a warm female singer", True),
    ("calm instrumental background score, no vocals", False),
    ("cinematic orchestral instrumental for a car ad", False),
]


@pytest.mark.remote_integration
@pytest.mark.parametrize("prompt, expect_vocals", _VOCAL_INTENT_CASES)
def test_user_prompt_preprocessor_remote_vocal_intent(prompt: str, expect_vocals: bool) -> None:
    """Real-LLM + correction net: explicit vocal requests (incl. frontend tags)
    resolve to vocals; instrumental requests stay instrumental."""

    require_remote_env("AZURE_ENDPOINT", "AZURE_API_KEY", "AZURE_MODEL")

    def _run() -> None:
        agent = UserPromptPreprocessorAgent()
        try:
            out = asyncio.run(agent.run(UserPromptPreprocessingStageInput(
                user_prompt=prompt, verbose_instruction=False,
                music_style_prompt=None, lyrics_prompt=None,
                music_model_spec="edenn_enhanced", job_id="remote-vocal-intent",
            )))
        except Exception as exc:
            handle_remote_failure(exc)
            raise
        assert out.effective_include_vocals is expect_vocals, (
            f"prompt={prompt!r} expected include_vocals={expect_vocals} "
            f"got {out.effective_include_vocals}"
        )
        if expect_vocals:
            # A vocal job must carry a usable gender + lyrics language.
            assert (out.effective_vocal_gender or "").strip()
            assert (out.effective_language or "").strip()

    run_with_remote_rate_limit_retry(
        _run, operation_name="test_user_prompt_preprocessor_remote_vocal_intent"
    )


@pytest.mark.remote_integration
def test_user_prompt_preprocessor_remote_returns_structured_result() -> None:
    """Verify the remote prompt preprocessor returns a usable structured contract."""

    require_remote_env("AZURE_ENDPOINT", "AZURE_API_KEY", "AZURE_MODEL")

    def _run() -> None:
        agent = UserPromptPreprocessorAgent()
        try:
            result = asyncio.run(
                agent.preprocess(
                    "Create bright Mandarin pop vocals for a short hotel promotion video."
                )
            )
        except Exception as exc:
            handle_remote_failure(exc)
            raise

        if result.tokens_used <= 0:
            handle_remote_failure(
                RuntimeError(f"Remote call did not complete successfully: {result.reasoning}")
            )

        assert isinstance(result.was_transformed, bool)
        assert isinstance(result.detected_include_vocals, bool)
        assert isinstance(result.detected_references, list)
        assert result.transformed_prompt.strip()
        assert result.detected_category in {
            VideoCategory.ADVERTISEMENT,
            VideoCategory.VLOG,
            VideoCategory.CREATOR_CONTENT,
            VideoCategory.DEFAULT,
        }
        assert result.prompt_tokens >= 0
        assert result.completion_tokens >= 0

    run_with_remote_rate_limit_retry(
        _run,
        operation_name="test_user_prompt_preprocessor_remote_returns_structured_result",
    )


@pytest.mark.remote_integration
def test_lyrics_preprocessor_remote_preserves_original_language_guidance() -> None:
    """Verify remote lyric guidance preprocessing preserves required source-language phrases."""

    require_remote_env("AZURE_ENDPOINT", "AZURE_API_KEY", "AZURE_MODEL")

    def _run() -> None:
        agent = UserPromptPreprocessorAgent()
        try:
            result = asyncio.run(
                agent.preprocess_lyrics_prompt(
                    "El coro debe incluir exactamente la frase: brilla mi corazon."
                )
            )
        except Exception as exc:
            handle_remote_failure(exc)
            raise

        if result.tokens_used <= 0:
            handle_remote_failure(
                RuntimeError(f"Remote lyrics preprocessor call did not complete: {result.reasoning}")
            )

        assert result.detected_include_vocals is True
        assert result.transformed_prompt.strip()
        assert "brilla mi corazon" in result.transformed_prompt.lower()
        assert result.detected_vocal_language.strip()

    run_with_remote_rate_limit_retry(
        _run,
        operation_name="test_lyrics_preprocessor_remote_preserves_original_language_guidance",
    )
