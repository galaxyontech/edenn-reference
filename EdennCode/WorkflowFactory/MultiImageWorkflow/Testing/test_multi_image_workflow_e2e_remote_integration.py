import asyncio
import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from EdennCode.Util.MediaUtils.ffmpeg_utils import get_video_duration
from EdennCode.WorkflowFactory.MultiImageWorkflow import (
    MultiImageGenerationE2EStage,
    MultiImageWorkflowStageInput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.PipelineDataModel.pipeline_datamodel import (
    Language,
    VideoCategory,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.UserIntentUnderstandingStage.user_prompt_preprocessor import (
    UserPromptPreprocessorResult,
)
from EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.BeatAlignmentStage.beat_alignment_stage import (
    BeatAlignmentStage,
    BeatAlignmentStageInput,
)
from EdennCode.TestSuites.helpers.integration import (
    handle_remote_failure,
    require_any_remote_env,
    require_remote_env,
    run_with_remote_rate_limit_retry,
)
from EdennCode.TestSuites.helpers.paths import MULTI_IMAGE_DESIGN_IMAGES_DIR


EXAMPLE_MULTI_IMAGE_DIR = MULTI_IMAGE_DESIGN_IMAGES_DIR


class _FakeAzureClient:
    async def complete_messages(self, _messages, json_schema=None):
        return (
            {
                "video_title": "City Pulse Frames",
                "video_description": "A bright two-frame reveal that rises into an energetic payoff.",
                "image_order": [1, 2],
                "storyline_summary": "A two-frame lifestyle reveal with a clear rhythmic lift.",
                "overall_mood": "uplifting",
                "target_bpm": 120,
                "primary_instruments": ["drums", "bass", "synth"],
                "music_prompt_summary": "Punchy upbeat pop with a strong pulse and bright lift.",
                "image_beats": [
                    {
                        "image_index": 1,
                        "label": "Hook",
                        "role": "setup",
                        "emotion": "curious",
                        "description": "Open on the establishing frame.",
                        "transition_hint": "Cut on the downbeat.",
                    },
                    {
                        "image_index": 2,
                        "label": "Lift",
                        "role": "payoff",
                        "emotion": "excited",
                        "description": "Land on the reveal frame.",
                        "transition_hint": "Hold through the phrase ending.",
                    },
                ],
                "music_sections": [
                    {
                        "section_id": "intro",
                        "label": "Intro",
                        "image_indices": [1],
                        "objective": "Establish the beat immediately.",
                        "energy_start": 0.35,
                        "energy_end": 0.65,
                        "instrumentation_focus": ["drums", "bass"],
                        "lyric_lines": ["Step into the light"],
                    },
                    {
                        "section_id": "lift",
                        "label": "Lift",
                        "image_indices": [2],
                        "objective": "Push to a brighter payoff.",
                        "energy_start": 0.65,
                        "energy_end": 0.8,
                        "instrumentation_focus": ["synth", "claps"],
                        "lyric_lines": ["Hold the moment high"],
                    },
                ],
            },
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


def _require_provider_env(modelspec: str) -> None:
    if modelspec == "edenn_basic":
        require_remote_env("PROVIDER_A_API_KEY")
        return
    if modelspec == "edenn_enhanced":
        require_any_remote_env(
            "EDENN_ENHANCED_PROVIDER_B_API_KEY",
            "PROVIDER_B_API_KEY",
            "PROVIDER_B_API_KEY_1",
            "PROVIDER_B_API_KEY_2",
        )
        return
    if modelspec == "edenn_studio":
        require_remote_env("PROVIDER_C_API_KEY")
        return
    raise AssertionError(f"Unexpected modelspec: {modelspec}")


def _copy_example_images(destination: Path, limit: int = 2) -> list[Path]:
    source_images = sorted(path for path in EXAMPLE_MULTI_IMAGE_DIR.iterdir() if path.is_file())
    if len(source_images) < limit:
        raise RuntimeError("Multi-image example folder does not contain enough images.")

    copied_paths: list[Path] = []
    destination.mkdir(parents=True, exist_ok=True)
    for idx, source in enumerate(source_images[:limit], start=1):
        copied = destination / f"frame_{idx}{source.suffix.lower()}"
        shutil.copy2(source, copied)
        copied_paths.append(copied)
    return copied_paths


async def _recompute_beat_alignment(result, *, image_count: int, fallback_duration: float):
    section_counts = [
        max(1, len(section.get("image_indices") or []))
        for section in result.planning_metadata.get("music_sections") or []
    ]
    boundaries = [
        0.0,
        *[
            float(timing.actual_end_s if timing.actual_end_s is not None else timing.expected_end_s)
            for timing in result.section_timeline
        ],
    ]
    if not section_counts or len(boundaries) != len(section_counts) + 1:
        raise RuntimeError("Workflow did not return section metadata needed for beat alignment.")

    stage = BeatAlignmentStage()
    return await stage.run(
        BeatAlignmentStageInput(
            music_path=result.music_path,
            image_count=image_count,
            fallback_duration=fallback_duration,
            section_boundaries_s=boundaries,
            section_image_counts=section_counts,
        )
    )


@pytest.mark.remote_integration
@pytest.mark.parametrize(
    ("modelspec", "include_vocals"),
    [
        ("edenn_basic", False),
        ("edenn_basic", True),
        ("edenn_enhanced", False),
        ("edenn_enhanced", True),
        ("edenn_studio", False),
        ("edenn_studio", True),
    ],
    ids=[
        "edenn_basic_instrumental",
        "edenn_basic_vocals",
        "edenn_enhanced_instrumental",
        "edenn_enhanced_vocals",
        "edenn_studio_instrumental",
        "edenn_studio_vocals",
    ],
)
def test_multi_image_workflow_remote_branch_matrix_honors_beat_alignment(
    modelspec: str,
    include_vocals: bool,
) -> None:
    _require_provider_env(modelspec)
    preprocess_result = UserPromptPreprocessorResult(
        was_transformed=False,
        detected_include_vocals=include_vocals,
        transformed_prompt=(
            "Create an uplifting female vocal anthem in English."
            if include_vocals
            else "Create an uplifting instrumental electronic track."
        ),
        detected_references=[],
        detected_language=Language.EN,
        detected_category=VideoCategory.DEFAULT,
        detected_vocal_gender="female" if include_vocals else "unknown",
        detected_vocal_language="EN" if include_vocals else "",
        reasoning="Remote integration test fixture.",
    )

    def _run() -> None:
        with tempfile.TemporaryDirectory(prefix="multi-image-remote-e2e-") as tmp:
            tmp_dir = Path(tmp)
            images_dir = tmp_dir / "images"
            _copy_example_images(images_dir)
            output_path = tmp_dir / f"{modelspec}_{'vocals' if include_vocals else 'instrumental'}.mp4"

            try:
                with patch(
                    "EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.MultiImageE2EGenerationStage.multi_image_generation_e2e_stage.build_azure_client",
                    return_value=_FakeAzureClient(),
                ), patch(
                    "EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.MultiImageE2EGenerationStage.multi_image_generation_e2e_stage.UserPromptPreprocessorAgent.preprocess",
                    new=AsyncMock(return_value=preprocess_result),
                ):
                    stage = MultiImageGenerationE2EStage(
                        per_image_duration=3.0,
                        align_to_beats=True,
                    )
                    result = asyncio.run(
                        stage.run(
                            MultiImageWorkflowStageInput(
                                folder_path=images_dir,
                                output_path=output_path,
                                user_prompt=preprocess_result.transformed_prompt,
                                include_vocals=include_vocals,
                                vocal_gender=None,
                                align_to_beats=True,
                                lyrics_language=None,
                                modelspec=modelspec,
                            )
                        )
                    )

                beat_output = asyncio.run(
                    _recompute_beat_alignment(
                        result,
                        image_count=2,
                        fallback_duration=3.0,
                    )
                )
            except Exception as exc:
                handle_remote_failure(exc)
                raise

            assert result.final_video_path.exists()
            assert result.silent_video_path.exists()
            assert result.music_path.exists()
            assert result.full_track_paths
            assert result.full_track_paths[0].exists()
            assert result.used_modelspec == modelspec
            assert result.section_timeline
            assert result.video_title == "City Pulse Frames"
            assert result.video_description
            assert result.user_requested_language == Language.EN
            assert len(beat_output.durations) == 2
            if not beat_output.beats_detected:
                assert beat_output.durations == [3.0, 3.0]
            assert abs(sum(beat_output.durations) - get_video_duration(result.silent_video_path)) <= 0.1
            assert abs(get_video_duration(result.final_video_path) - get_video_duration(result.silent_video_path)) <= 0.1
            if include_vocals:
                assert result.lyrics_timestamps
                assert result.vocal_gender == "female"
                assert result.lyrics_language == "EN"
            else:
                assert isinstance(result.lyrics_timestamps, list)

    run_with_remote_rate_limit_retry(
        _run,
        operation_name=(
            "test_multi_image_workflow_remote_branch_matrix_honors_beat_alignment"
            f"[{modelspec}_{'vocals' if include_vocals else 'instrumental'}]"
        ),
    )
