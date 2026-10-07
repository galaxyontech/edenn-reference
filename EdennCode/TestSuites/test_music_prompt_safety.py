from EdennCode.ModelFactory.PromptFactory.prompts import (
    Prompt,
    PromptBuilder,
    format_music_safe_scene_lines,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.PipelineDataModel.pipeline_datamodel import (
    Language,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.datamodel import (
    SceneUnderstanding,
)


def _scene_with_ip_details() -> SceneUnderstanding:
    return SceneUnderstanding(
        scene_index=3,
        start_timestamp=12.0,
        end_timestamp=18.5,
        visual_summary=(
            "Tom and Jerry chase through a Disney hotel lobby near a Warner Bros sign."
        ),
        key_actions="Tom chases Jerry while Mickey artwork is visible.",
        mood="playful and frantic",
    )


def _message_text(messages: list[dict]) -> str:
    parts: list[str] = []
    for message in messages:
        for item in message.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
    return "\n".join(parts)


def test_music_safe_scene_lines_omit_visual_ip_details() -> None:
    text = format_music_safe_scene_lines([_scene_with_ip_details()])

    assert "playful and frantic" in text
    for blocked in ("Tom", "Jerry", "Disney", "Warner", "Mickey", "cat", "mouse"):
        assert blocked not in text


def test_music_prompt_builders_use_safe_scene_context() -> None:
    scenes = [_scene_with_ip_details()]
    builders = [
        PromptBuilder.build_provider_a_music_prompt_messages(
            scenes,
            include_vocals=False,
            user_prompt="instrumental background music",
            language=Language.EN,
        ),
        PromptBuilder.build_provider_b_music_prompt_messages(
            scenes,
            include_vocals=True,
            user_prompt="upbeat orchestral comedy",
            language=Language.EN,
        ),
        PromptBuilder.build_provider_c_simple_prompt_messages(
            scenes,
            include_vocals=False,
            user_prompt="upbeat orchestral comedy",
            language=Language.EN,
        ),
    ]

    safe_lines = format_music_safe_scene_lines(scenes).strip()

    for messages in builders:
        text = _message_text(messages)
        # Pin the mechanism, not the copy. This used to assert a shared
        # "Provider safety for music generation" heading, which the neutral
        # rewrite removed — that heading named a supplier and told the model
        # what NOT to do, and the negation was itself tripping the content
        # filter. What has to stay true is narrower and stronger: whatever
        # framing each provider's prompt carries, the scene context it embeds
        # is the safe formatter's output and nothing else.
        assert safe_lines in text
        assert "playful and frantic" in text
        for blocked in ("Tom", "Jerry", "Disney", "Warner", "Mickey"):
            assert blocked not in text


def test_scene_understanding_prompt_discourages_named_ip() -> None:
    prompt = Prompt().format_scene_understanding(
        scene_index=1,
        start_time=0.0,
        end_time=2.5,
        language=Language.EN,
    )

    assert "Do not identify or name copyrighted/trademarked characters" in prompt
