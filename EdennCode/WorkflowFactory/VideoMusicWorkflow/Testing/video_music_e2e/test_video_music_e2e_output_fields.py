import asyncio
import contextlib
import math
import struct
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from EdennCode.WorkflowFactory.VideoMusicWorkflow.PipelineDataModel.pipeline_datamodel import (
    Language,
    VideoCategory,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata.video_metadata import VideoMetadata
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicGenerationStage.music_generation_stage import (
    MusicGenerationStage,
    MusicGenerationStageOutput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.word_ts import WordTS
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicPromptOrchestrationStage.music_prompt_orchestration_stage import (
    MusicPromptOrchestrationStage,
    MusicPromptOrchestrationStageOutput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.datamodel import SceneUnderstanding
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.scene_segmentation_stage import (
    SceneSegmentationStage,
    SceneSegmentationStageOutput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.UserIntentUnderstandingStage.user_prompt_preprocessor import (
    UserPromptPreprocessorAgent,
    UserPromptPreprocessorResult,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoAudioRemixStage.video_audio_remix_stage import (
    VideoAudioRemixStage,
    VideoAudioRemixStageInput,
    VideoAudioRemixStageOutput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicGenerationStage.music_generation_stage import (
    MusicGenerationStage,
    MusicGenerationStageOutput,
    MusicGenertionModelEnum,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow import (
    VideoMusicWorkflowE2E,
    VideoMusicWorkflowE2EInput,
    _normalize_token_usage,
    _sum_token_usage,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoPreprocessStage.media_preprocess_stage import (
    PreprocessStage,
    PreprocessStageOutput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoUnderstandingStage.video_understanding_page import (
    VideoUnderstandingStage,
    VideoUnderstandingStageOutput,
)
from EdennCode.exceptions import EdennValidationError
from EdennCode.TestSuites.helpers.paths import SMOKE_VIDEO_PATH


def _write_sine_wav(path: Path, duration: float = 0.5, sample_rate: int = 16000) -> None:
    frames = int(duration * sample_rate)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        for i in range(frames):
            val = int(32767 * 0.1 * math.sin(2 *
                      math.pi * 440 * (i / sample_rate)))
            wav.writeframes(struct.pack("<h", val))


def _fake_video_metadata(tmp_dir: Path) -> VideoMetadata:
    workdir = tmp_dir / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    return VideoMetadata(
        path=SMOKE_VIDEO_PATH,
        duration=3.2,
        size_bytes=SMOKE_VIDEO_PATH.stat().st_size,
        width=360,
        height=640,
        fps=30.0,
        video_codec="h264",
        video_bit_rate=350_000,
        has_audio=True,
        audio_codec="aac",
        audio_channels=2,
        audio_sample_rate=48_000,
        audio_bit_rate=48_000,
        temp_folder=str(workdir),
    )


class VideoMusicE2EReturnFieldsTests(unittest.TestCase):
    def test_video_music_e2e_returns_chinese_user_facing_metadata_for_chinese_lyrics_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            thumbnail_path = tmp_dir / "thumb.jpg"
            thumbnail_path.write_bytes(b"thumbnail")

            scene_messages = [
                SceneUnderstanding(
                    scene_index=0,
                    start_timestamp=0.0,
                    end_timestamp=video_metadata.duration,
                    visual_summary="city lights and holiday decorations",
                    key_actions="camera pans across crowd",
                    mood="festive",
                )
            ]

            preprocess_output = PreprocessStageOutput(
                video_metadata=video_metadata)
            scene_output = SceneSegmentationStageOutput(
                scene_understanding_messages=scene_messages,
                token_usage={"prompt_tokens": 2,
                             "completion_tokens": 3, "total_tokens": 5},
                thumbnail_path=thumbnail_path,
            )
            understanding_output = VideoUnderstandingStageOutput(
                video_descriptions={
                    "video_title": "海滨假日之旅",
                    "video_description": "一则充满节日氛围的海边酒店短片，展现餐饮与抽奖活动的热闹场景。",
                    "summary": "一支带有浓厚节庆气氛的酒店推广短片。",
                    "overall_mood": "温暖欢庆",
                    "core_message": "加入节日活动，赢取酒店入住体验。",
                    "has_explicit_call_to_action": True,
                },
                video_title="海滨假日之旅",
                video_description="一则充满节日氛围的海边酒店短片，展现餐饮与抽奖活动的热闹场景。",
                token_usage={"prompt_tokens": 4,
                             "completion_tokens": 6, "total_tokens": 10},
            )
            prompt_output = MusicPromptOrchestrationStageOutput(
                music_generation_prompt={
                    "global_music_prompt": "节日氛围，女声普通话，吐字清晰。",
                    "global_mood": "warm, festive",
                    "tempo_bpm": 108,
                    "instruments": ["piano", "bells", "drums"],
                },
                token_usage={"prompt_tokens": 1,
                             "completion_tokens": 1, "total_tokens": 2},
                downstream_generation_model_spec="edenn_enhanced",
            )

            generated_audio = tmp_dir / "generated.wav"
            _write_sine_wav(generated_audio)
            lyrics_timestamps = [
                WordTS(text="line one", startS=0.0, endS=1.2, i=0)]
            word_level_lyrics_timestamps = [
                WordTS(text="line", startS=0.0, endS=0.5, i=0),
                WordTS(text="one", startS=0.5, endS=1.2, i=1),
            ]
            music_output = MusicGenerationStageOutput(
                music_path=generated_audio,
                lyrics_timestamps=lyrics_timestamps,
                word_level_lyrics_timestamps=word_level_lyrics_timestamps,
                primary_full_lyrics="line one\nline two",
                primary_full_lyrics_timestamps=lyrics_timestamps,
                primary_full_word_level_lyrics_timestamps=word_level_lyrics_timestamps,
                secondary_full_lyrics="line one\nline two alt",
                secondary_full_lyrics_timestamps=lyrics_timestamps,
                secondary_full_word_level_lyrics_timestamps=word_level_lyrics_timestamps,
                matching_used_track="primary",
            )

            remixed_paths = []

            async def fake_remix_run(stage_input: VideoAudioRemixStageInput):
                out_path = Path(
                    stage_input.video_metadata.temp_folder) / "remix.mp4"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(b"remixed")
                remixed_paths.append(out_path)
                return VideoAudioRemixStageOutput(remixed_video_path=out_path.name)

            preprocessor_output = UserPromptPreprocessorResult(
                was_transformed=False,
                detected_include_vocals=True,
                transformed_prompt="cleaned user prompt",
                detected_references=[],
                detected_language=Language.EN,
                detected_category=VideoCategory.DEFAULT,
                detected_vocal_gender="female",
                detected_vocal_language=Language.CN,
                reasoning="ok",
                tokens_used=7,
                prompt_tokens=3,
                completion_tokens=4,
            )

            preprocess_mock = AsyncMock(return_value=preprocess_output)
            segmentation_mock = AsyncMock(return_value=scene_output)
            understanding_mock = AsyncMock(return_value=understanding_output)
            prompt_mock = AsyncMock(return_value=prompt_output)
            music_mock = AsyncMock(return_value=music_output)
            remix_mock = AsyncMock(side_effect=fake_remix_run)
            preprocessor_mock = AsyncMock(return_value=preprocessor_output)

            with patch(
                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.build_azure_client",
                return_value=MagicMock(),
            ), patch(
                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.build_music_provider",
                return_value=MagicMock(),
            ), patch(
                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.ProviderCApi",
                return_value=MagicMock(),
            ), patch(
                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.build_edenn_enhanced_music_provider",
                return_value=MagicMock(),
            ), patch.object(
                UserPromptPreprocessorAgent, "preprocess", new=preprocessor_mock
            ), patch.object(
                PreprocessStage, "run", new=preprocess_mock
            ), patch.object(
                SceneSegmentationStage, "run", new=segmentation_mock
            ), patch.object(
                VideoUnderstandingStage, "run", new=understanding_mock
            ), patch.object(
                MusicPromptOrchestrationStage, "run", new=prompt_mock
            ), patch.object(
                MusicGenerationStage, "run", new=music_mock
            ), patch.object(
                VideoAudioRemixStage, "run", new=remix_mock
            ):
                workflow = VideoMusicWorkflowE2E()
                workflow_input = VideoMusicWorkflowE2EInput(
                    video_path=str(video_metadata.path),
                    include_vocals=True,
                    vocal_gender="female",
                    user_prompt="make it bright",
                    music_model_spec="edenn_enhanced",
                    audio_output_format="pcm_44100",
                )
                output = asyncio.run(workflow.generate(workflow_input))

            self.assertEqual(output.video_metadata, video_metadata)
            self.assertEqual(output.scenes, scene_messages)
            self.assertEqual(output.video_summary,
                             understanding_output.video_descriptions)
            self.assertEqual(output.music_prompt,
                             prompt_output.music_generation_prompt)
            self.assertEqual(output.generated_music_path, generated_audio)
            self.assertEqual(
                output.remixed_video_path,
                Path(video_metadata.temp_folder) / "remix.mp4",
            )
            self.assertEqual(output.video_title, "海滨假日之旅")
            self.assertEqual(
                output.video_description,
                "一则充满节日氛围的海边酒店短片，展现餐饮与抽奖活动的热闹场景。",
            )
            self.assertTrue(output.include_vocals)
            self.assertEqual(output.vocal_gender, "female")
            self.assertEqual(output.lyrics_timestamps, lyrics_timestamps)
            self.assertEqual(
                output.word_level_lyrics_timestamps,
                word_level_lyrics_timestamps,
            )
            self.assertEqual(output.primary_full_lyrics, "line one\nline two")
            self.assertEqual(
                output.primary_full_lyrics_timestamps, lyrics_timestamps)
            self.assertEqual(
                output.primary_full_word_level_lyrics_timestamps,
                word_level_lyrics_timestamps,
            )
            self.assertEqual(output.secondary_full_lyrics,
                             "line one\nline two alt")
            self.assertEqual(
                output.secondary_full_lyrics_timestamps, lyrics_timestamps)
            self.assertEqual(
                output.secondary_full_word_level_lyrics_timestamps,
                word_level_lyrics_timestamps,
            )
            self.assertEqual(output.matching_used_track, "primary")
            self.assertEqual(output.thumbnail_path, thumbnail_path)
            self.assertEqual(
                output.token_usage,
                {
                    "prompt_tokens": 10,
                    "completion_tokens": 14,
                    "total_tokens": 24,
                },
            )
            self.assertEqual(
                output.token_usage_breakdown,
                {
                    "user_prompt_preprocessor": {
                        "prompt_tokens": 3,
                        "completion_tokens": 4,
                        "total_tokens": 7,
                    },
                    "scene_understanding": {
                        "prompt_tokens": 2,
                        "completion_tokens": 3,
                        "total_tokens": 5,
                    },
                    "video_summary": {
                        "prompt_tokens": 4,
                        "completion_tokens": 6,
                        "total_tokens": 10,
                    },
                    "music_prompt_orchestration": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )
            self.assertEqual(output.UsedMusicModelSpecs, "edenn_enhanced")
            self.assertEqual(output.user_requested_language, Language.CN)

            # type: ignore
            understanding_input = understanding_mock.await_args.args[0]
            self.assertEqual(
                understanding_input.preferred_language, Language.CN)

            segmentation_input = segmentation_mock.await_args.args[0]
            self.assertEqual(
                segmentation_input.preferred_language, Language.EN)

            prompt_input = prompt_mock.await_args.args[0]
            self.assertEqual(prompt_input.language, Language.CN)
            self.assertEqual(prompt_input.user_prompt, "cleaned user prompt")
            self.assertEqual(prompt_input.modelspec, "edenn_enhanced")

            music_input = music_mock.await_args.args[0]
            self.assertEqual(music_input.prompt_metadata,
                             prompt_output.music_generation_prompt)
            self.assertEqual(
                music_input.music_generation_model, "edenn_enhanced")
            self.assertEqual(music_input.lyrics_language, Language.CN)
            self.assertEqual(music_input.audio_output_format, "pcm_44100")

            remix_input = remix_mock.await_args.args[0]
            self.assertEqual(remix_input.video_metadata, video_metadata)
            self.assertEqual(remix_input.music_path, generated_audio)
            self.assertTrue(remixed_paths and remixed_paths[0].exists())


# ---------------------------------------------------------------------------
# Shared helpers for new test classes
# ---------------------------------------------------------------------------

_PROVIDER_PATCH_TARGETS = [
    "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.build_azure_client",
    "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.build_music_provider",
    "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.ProviderCApi",
    "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.build_edenn_enhanced_music_provider",
]


def _default_stage_outputs(tmp_dir: Path, video_metadata):
    thumbnail_path = tmp_dir / "thumb.jpg"
    thumbnail_path.write_bytes(b"thumbnail")
    scene_messages = [
        SceneUnderstanding(
            scene_index=0,
            start_timestamp=0.0,
            end_timestamp=video_metadata.duration,
            visual_summary="city lights",
            key_actions="camera pans",
            mood="festive",
        )
    ]
    preprocess_output = PreprocessStageOutput(video_metadata=video_metadata)
    scene_output = SceneSegmentationStageOutput(
        scene_understanding_messages=scene_messages,
        token_usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        thumbnail_path=thumbnail_path,
    )
    understanding_output = VideoUnderstandingStageOutput(
        video_descriptions={"video_title": "Test", "video_description": "Desc"},
        video_title="Test",
        video_description="Desc",
        token_usage={"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10},
    )
    prompt_output = MusicPromptOrchestrationStageOutput(
        music_generation_prompt={"global_music_prompt": "upbeat pop"},
        token_usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        downstream_generation_model_spec="edenn_basic",
    )
    return preprocess_output, scene_output, understanding_output, prompt_output


def _run_workflow(
    workflow_input: VideoMusicWorkflowE2EInput,
    preprocessor_output: UserPromptPreprocessorResult,
    music_output: MusicGenerationStageOutput,
    tmp_dir: Path,
    *,
    preprocess_output=None,
    scene_output=None,
    understanding_output=None,
    prompt_output=None,
):
    video_metadata = _fake_video_metadata(tmp_dir)
    defaults = _default_stage_outputs(tmp_dir, video_metadata)
    preprocess_output = preprocess_output or defaults[0]
    scene_output = scene_output or defaults[1]
    understanding_output = understanding_output or defaults[2]
    prompt_output = prompt_output or defaults[3]

    async def fake_remix_run(stage_input: VideoAudioRemixStageInput):
        out_path = Path(stage_input.video_metadata.temp_folder) / "remix.mp4"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"remixed")
        return VideoAudioRemixStageOutput(remixed_video_path=out_path.name)

    with contextlib.ExitStack() as stack:
        for target in _PROVIDER_PATCH_TARGETS:
            stack.enter_context(patch(target, return_value=MagicMock()))
        preprocessor_mock = AsyncMock(return_value=preprocessor_output)
        preprocess_mock = AsyncMock(return_value=preprocess_output)
        segmentation_mock = AsyncMock(return_value=scene_output)
        understanding_mock = AsyncMock(return_value=understanding_output)
        prompt_mock = AsyncMock(return_value=prompt_output)
        music_mock = AsyncMock(return_value=music_output)
        remix_mock = AsyncMock(side_effect=fake_remix_run)
        stack.enter_context(patch.object(UserPromptPreprocessorAgent, "preprocess", new=preprocessor_mock))
        stack.enter_context(patch.object(PreprocessStage, "run", new=preprocess_mock))
        stack.enter_context(patch.object(SceneSegmentationStage, "run", new=segmentation_mock))
        stack.enter_context(patch.object(VideoUnderstandingStage, "run", new=understanding_mock))
        stack.enter_context(patch.object(MusicPromptOrchestrationStage, "run", new=prompt_mock))
        stack.enter_context(patch.object(MusicGenerationStage, "run", new=music_mock))
        stack.enter_context(patch.object(VideoAudioRemixStage, "run", new=remix_mock))
        workflow = VideoMusicWorkflowE2E()
        output = asyncio.run(workflow.generate(workflow_input))

    return output, preprocessor_mock, preprocess_mock, segmentation_mock, understanding_mock, prompt_mock, music_mock, remix_mock


def _minimal_music_output(tmp_dir: Path) -> MusicGenerationStageOutput:
    audio = tmp_dir / "generated.wav"
    _write_sine_wav(audio)
    return MusicGenerationStageOutput(music_path=audio)


# ---------------------------------------------------------------------------
# Static helper unit tests
# ---------------------------------------------------------------------------

class VideoMusicWorkflowStaticHelperTests(unittest.TestCase):

    def test_normalize_modelspec_known_values(self):
        self.assertEqual(VideoMusicWorkflowE2E._normalize_modelspec("edenn_basic"), MusicGenertionModelEnum.EDENN_BASIC)
        self.assertEqual(VideoMusicWorkflowE2E._normalize_modelspec("edenn_enhanced"), MusicGenertionModelEnum.EDENN_ENHANCED)
        self.assertEqual(VideoMusicWorkflowE2E._normalize_modelspec("edenn_studio"), MusicGenertionModelEnum.EDENN_STUDIO)

    def test_normalize_modelspec_case_and_whitespace_insensitive(self):
        self.assertEqual(VideoMusicWorkflowE2E._normalize_modelspec("EDENN_BASIC"), MusicGenertionModelEnum.EDENN_BASIC)
        self.assertEqual(VideoMusicWorkflowE2E._normalize_modelspec("  Edenn_Enhanced  "), MusicGenertionModelEnum.EDENN_ENHANCED)
        self.assertEqual(VideoMusicWorkflowE2E._normalize_modelspec("EDENN_STUDIO"), MusicGenertionModelEnum.EDENN_STUDIO)

    def test_normalize_modelspec_unknown_falls_back_to_basic(self):
        self.assertEqual(VideoMusicWorkflowE2E._normalize_modelspec(None), MusicGenertionModelEnum.EDENN_BASIC)
        self.assertEqual(VideoMusicWorkflowE2E._normalize_modelspec(""), MusicGenertionModelEnum.EDENN_BASIC)
        self.assertEqual(VideoMusicWorkflowE2E._normalize_modelspec("garbage_model"), MusicGenertionModelEnum.EDENN_BASIC)

    def test_is_chinese_language_all_accepted_aliases(self):
        for alias in ["CN", "ZH", "ZH_CN", "CHINESE", "CHINESE_MAINLAND", "MANDARIN"]:
            with self.subTest(alias=alias):
                self.assertTrue(VideoMusicWorkflowE2E._is_chinese_language(alias))

    def test_is_chinese_language_case_and_hyphen_insensitive(self):
        self.assertTrue(VideoMusicWorkflowE2E._is_chinese_language("cn"))
        self.assertTrue(VideoMusicWorkflowE2E._is_chinese_language("Zh"))
        self.assertTrue(VideoMusicWorkflowE2E._is_chinese_language("zh-CN"))
        self.assertTrue(VideoMusicWorkflowE2E._is_chinese_language("ZH-CN"))

    def test_is_chinese_language_returns_false_for_non_chinese(self):
        for lang in ["EN", "ENGLISH_US", "FR", "JAPANESE", None, ""]:
            with self.subTest(lang=lang):
                self.assertFalse(VideoMusicWorkflowE2E._is_chinese_language(lang))

    def test_external_modelspec_name_all_known_values(self):
        self.assertEqual(VideoMusicWorkflowE2E._external_modelspec_name("edenn_basic"), "edenn_basic")
        self.assertEqual(VideoMusicWorkflowE2E._external_modelspec_name("edenn_enhanced"), "edenn_enhanced")
        self.assertEqual(VideoMusicWorkflowE2E._external_modelspec_name("edenn_studio"), "edenn_studio")

    def test_external_modelspec_name_unknown_falls_back_to_basic(self):
        self.assertEqual(VideoMusicWorkflowE2E._external_modelspec_name("unknown_spec"), "edenn_basic")
        self.assertEqual(VideoMusicWorkflowE2E._external_modelspec_name(""), "edenn_basic")

    def test_merge_verbose_preprocessor_results_prefers_lyrics_vocal_language(self):
        style_result = UserPromptPreprocessorResult(
            was_transformed=True,
            detected_include_vocals=True,
            transformed_prompt="cleaned Chinese female vocal style",
            detected_references=["Singer"],
            detected_language=Language.CN,
            detected_category=VideoCategory.DEFAULT,
            detected_vocal_gender="female",
            detected_vocal_language=Language.CN,
            reasoning="style ok",
            tokens_used=3,
            prompt_tokens=2,
            completion_tokens=1,
        )
        lyrics_result = UserPromptPreprocessorResult(
            was_transformed=False,
            detected_include_vocals=True,
            transformed_prompt="keep the phrase bright coast",
            detected_references=[],
            detected_language=Language.EN,
            detected_category=VideoCategory.DEFAULT,
            detected_vocal_gender="unknown",
            detected_vocal_language=Language.EN,
            reasoning="lyrics ok",
            tokens_used=5,
            prompt_tokens=3,
            completion_tokens=2,
        )

        merged = VideoMusicWorkflowE2E._merge_verbose_preprocessor_results(
            style_result=style_result,
            lyrics_result=lyrics_result,
            sanitized_style_prompt=style_result.transformed_prompt,
            sanitized_lyrics_prompt=lyrics_result.transformed_prompt,
        )

        self.assertTrue(merged.was_transformed)
        self.assertTrue(merged.detected_include_vocals)
        self.assertEqual(merged.detected_references, ["Singer"])
        self.assertEqual(merged.detected_language, Language.CN)
        self.assertEqual(merged.detected_vocal_language, Language.EN)
        self.assertEqual(merged.detected_vocal_gender, "female")
        self.assertEqual(merged.tokens_used, 8)
        self.assertIn("Music style prompt:", merged.transformed_prompt)
        self.assertIn("Lyrics prompt:", merged.transformed_prompt)


# ---------------------------------------------------------------------------
# Token utility unit tests
# ---------------------------------------------------------------------------

class TokenUsageUtilityTests(unittest.TestCase):

    def test_normalize_token_usage_with_none_returns_zeros(self):
        self.assertEqual(
            _normalize_token_usage(None),
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )

    def test_normalize_token_usage_with_empty_dict_returns_zeros(self):
        self.assertEqual(
            _normalize_token_usage({}),
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )

    def test_normalize_token_usage_with_none_values_in_dict_treats_as_zero(self):
        result = _normalize_token_usage({"prompt_tokens": None, "completion_tokens": 2, "total_tokens": None})
        self.assertEqual(result, {"prompt_tokens": 0, "completion_tokens": 2, "total_tokens": 0})

    def test_normalize_token_usage_with_valid_dict(self):
        result = _normalize_token_usage({"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7})
        self.assertEqual(result, {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7})

    def test_sum_token_usage_accumulates_correctly(self):
        a = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
        b = {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9}
        self.assertEqual(
            _sum_token_usage(a, b),
            {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        )

    def test_sum_token_usage_with_none_inputs(self):
        self.assertEqual(
            _sum_token_usage(None, None),
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )

    def test_sum_token_usage_mixed_none_and_valid(self):
        a = {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
        self.assertEqual(
            _sum_token_usage(a, None),
            {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        )


# ---------------------------------------------------------------------------
# E2E scenario tests
# ---------------------------------------------------------------------------

class VideoMusicE2EScenarioTests(unittest.TestCase):

    def _make_preprocessor_output(
        self,
        *,
        was_transformed: bool = False,
        detected_include_vocals: bool = True,
        transformed_prompt: str = "cleaned prompt",
        detected_references=None,
        detected_language: str = Language.EN,
        detected_category: str = VideoCategory.DEFAULT,
        detected_vocal_gender: str = "female",
        detected_vocal_language: str = Language.EN,
        reasoning: str = "ok",
        tokens_used: int = 3,
        prompt_tokens: int = 2,
        completion_tokens: int = 1,
    ) -> UserPromptPreprocessorResult:
        return UserPromptPreprocessorResult(
            was_transformed=was_transformed,
            detected_include_vocals=detected_include_vocals,
            transformed_prompt=transformed_prompt,
            detected_references=detected_references if detected_references is not None else [],
            detected_language=detected_language,
            detected_category=detected_category,
            detected_vocal_gender=detected_vocal_gender,
            detected_vocal_language=detected_vocal_language,
            reasoning=reasoning,
            tokens_used=tokens_used,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    def test_chinese_basic_model_is_upgraded_to_edenn_enhanced(self):
        """edenn_basic + Chinese vocals → UsedMusicModelSpecs upgraded to edenn_enhanced."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            preprocessor_output = self._make_preprocessor_output(
                detected_include_vocals=True,
                detected_vocal_language=Language.CN,
            )
            music_output = _minimal_music_output(tmp_dir)
            workflow_input = VideoMusicWorkflowE2EInput(
                video_path=str(video_metadata.path),
                user_prompt="中文歌曲",
                music_model_spec="edenn_basic",
            )
            output, *_, music_mock, _ = _run_workflow(workflow_input, preprocessor_output, music_output, tmp_dir)

        self.assertEqual(output.UsedMusicModelSpecs, "edenn_enhanced")
        music_input = music_mock.await_args.args[0]  # type: ignore[union-attr]
        self.assertEqual(music_input.music_generation_model, MusicGenertionModelEnum.EDENN_ENHANCED)

    def test_chinese_enhanced_model_is_not_downgraded(self):
        """edenn_enhanced + Chinese vocals → model stays edenn_enhanced (no double-upgrade)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            preprocessor_output = self._make_preprocessor_output(
                detected_include_vocals=True,
                detected_vocal_language=Language.CN,
            )
            music_output = _minimal_music_output(tmp_dir)
            workflow_input = VideoMusicWorkflowE2EInput(
                video_path=str(video_metadata.path),
                user_prompt="中文歌曲",
                music_model_spec="edenn_enhanced",
            )
            output, *_ = _run_workflow(workflow_input, preprocessor_output, music_output, tmp_dir)

        self.assertEqual(output.UsedMusicModelSpecs, "edenn_enhanced")

    def test_bgm_path_preserves_requested_model_and_uses_detected_language(self):
        """No vocals preserve requested model; scene analysis stays EN; understanding uses detected_language."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            preprocessor_output = self._make_preprocessor_output(
                detected_include_vocals=False,
                detected_language=Language.CN,
                detected_vocal_language="",
            )
            music_output = _minimal_music_output(tmp_dir)
            workflow_input = VideoMusicWorkflowE2EInput(
                video_path=str(video_metadata.path),
                user_prompt="背景音乐",
                music_model_spec="edenn_studio",
            )
            output, _, _, segmentation_mock, understanding_mock, prompt_mock, music_mock, _ = _run_workflow(
                workflow_input, preprocessor_output, music_output, tmp_dir
            )

        self.assertEqual(output.UsedMusicModelSpecs, "edenn_studio")
        self.assertFalse(output.include_vocals)

        segmentation_input = segmentation_mock.await_args.args[0]  # type: ignore[union-attr]
        self.assertEqual(segmentation_input.preferred_language, Language.EN)

        understanding_input = understanding_mock.await_args.args[0]  # type: ignore[union-attr]
        self.assertEqual(understanding_input.preferred_language, Language.CN)

        prompt_input = prompt_mock.await_args.args[0]  # type: ignore[union-attr]
        self.assertEqual(prompt_input.language, Language.CN)
        self.assertEqual(prompt_input.modelspec, MusicGenertionModelEnum.EDENN_STUDIO)
        self.assertFalse(prompt_input.provider_c_custom_mode)

        music_input = music_mock.await_args.args[0]  # type: ignore[union-attr]
        self.assertEqual(music_input.music_generation_model, MusicGenertionModelEnum.EDENN_STUDIO)
        self.assertFalse(music_input.provider_c_custom_mode)

    def test_verbose_instruction_uses_split_prompts_for_vocal_routing(self):
        """Verbose vocal requests use style prompt for context and pass split fields to prompt orchestration."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            preprocessor_output = self._make_preprocessor_output(
                detected_include_vocals=True,
                transformed_prompt="cleaned female vocal style",
                detected_vocal_language=Language.EN,
            )
            prompt_output = MusicPromptOrchestrationStageOutput(
                music_generation_prompt={
                    "style_prompt": "video-guided female vocal pop",
                    "lyrics_prompt": "generated lyric direction",
                },
                token_usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                downstream_generation_model_spec="edenn_studio",
            )
            music_output = _minimal_music_output(tmp_dir)
            workflow_input = VideoMusicWorkflowE2EInput(
                video_path=str(video_metadata.path),
                user_prompt="",
                verbose_instruction=True,
                music_style_prompt="female vocal pop",
                lyrics_prompt=None,
                music_model_spec="edenn_studio",
            )
            (
                _output,
                preprocessor_mock,
                _preprocess_mock,
                _segmentation_mock,
                _understanding_mock,
                prompt_mock,
                music_mock,
                _remix_mock,
            ) = _run_workflow(
                workflow_input,
                preprocessor_output,
                music_output,
                tmp_dir,
                prompt_output=prompt_output,
            )

        preprocessor_mock.assert_awaited_once_with("female vocal pop")
        prompt_input = prompt_mock.await_args.args[0]  # type: ignore[union-attr]
        self.assertTrue(prompt_input.verbose_instruction)
        self.assertEqual(prompt_input.music_style_prompt, "cleaned female vocal style")
        self.assertEqual(prompt_input.lyrics_prompt, "")
        self.assertEqual(prompt_input.modelspec, "edenn_studio")

        music_input = music_mock.await_args.args[0]  # type: ignore[union-attr]
        self.assertEqual(music_input.prompt_metadata, prompt_output.music_generation_prompt)
        self.assertEqual(music_input.music_generation_model, MusicGenertionModelEnum.EDENN_STUDIO)

    def test_verbose_instruction_instrumental_style_preserves_requested_model(self):
        """Verbose instrumental requests keep the requested provider tier and no-lyrics routing."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            preprocessor_output = self._make_preprocessor_output(
                detected_include_vocals=False,
                transformed_prompt="cleaned instrumental style",
                detected_language=Language.EN,
                detected_vocal_language="",
            )
            music_output = _minimal_music_output(tmp_dir)
            workflow_input = VideoMusicWorkflowE2EInput(
                video_path=str(video_metadata.path),
                user_prompt="",
                verbose_instruction=True,
                music_style_prompt="instrumental electronic underscore, no lyrics",
                lyrics_prompt=None,
                music_model_spec="edenn_enhanced",
            )
            output, _, _, _, _, prompt_mock, music_mock, _ = _run_workflow(
                workflow_input,
                preprocessor_output,
                music_output,
                tmp_dir,
            )

        self.assertEqual(output.UsedMusicModelSpecs, "edenn_enhanced")
        self.assertFalse(output.include_vocals)
        prompt_input = prompt_mock.await_args.args[0]  # type: ignore[union-attr]
        self.assertEqual(prompt_input.modelspec, MusicGenertionModelEnum.EDENN_ENHANCED)
        self.assertTrue(prompt_input.verbose_instruction)
        self.assertFalse(prompt_input.provider_c_custom_mode)
        music_input = music_mock.await_args.args[0]  # type: ignore[union-attr]
        self.assertEqual(music_input.music_generation_model, MusicGenertionModelEnum.EDENN_ENHANCED)

    def test_verbose_instruction_rejects_user_prompt_in_workflow(self):
        """Direct workflow callers get the same strict contract as the API."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            workflow_input = VideoMusicWorkflowE2EInput(
                video_path=str(video_metadata.path),
                user_prompt="legacy prompt",
                verbose_instruction=True,
                music_style_prompt="female vocal pop",
                music_model_spec="edenn_enhanced",
            )
            with self.assertRaises(EdennValidationError):
                _run_workflow(
                    workflow_input,
                    self._make_preprocessor_output(),
                    _minimal_music_output(tmp_dir),
                    tmp_dir,
                )

    def test_lyrics_only_verbose_runs_with_empty_style_slot(self):
        """A lyric direction alone is a complete request: no style required,
        the dual path runs and derives style from the video analysis."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            # The style-pass preprocessor sees an empty style slot and returns
            # an empty sanitized style; the lyric pass sanitizes the direction.
            preprocessor_output = self._make_preprocessor_output(
                detected_include_vocals=False,
                transformed_prompt="",
                detected_vocal_language=Language.EN,
            )
            prompt_output = MusicPromptOrchestrationStageOutput(
                music_generation_prompt={
                    "style_prompt": "video-derived warm folk",
                    "lyrics_prompt": "generated lyric direction",
                },
                token_usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                downstream_generation_model_spec="edenn_enhanced",
            )
            music_output = _minimal_music_output(tmp_dir)
            workflow_input = VideoMusicWorkflowE2EInput(
                video_path=str(video_metadata.path),
                user_prompt="",
                verbose_instruction=True,
                music_style_prompt=None,
                lyrics_prompt="themes of home",
                music_model_spec="edenn_enhanced",
            )
            lyrics_sanitize_mock = AsyncMock(
                return_value=self._make_preprocessor_output(
                    detected_include_vocals=True,
                    transformed_prompt="sanitized themes of home",
                    detected_vocal_language=Language.EN,
                    detected_vocal_gender="male",
                )
            )
            with patch.object(
                UserPromptPreprocessorAgent, "preprocess_lyrics_prompt",
                new=lyrics_sanitize_mock,
            ):
                output, style_pass_mock, _, _, _, prompt_mock, _, _ = _run_workflow(
                    workflow_input,
                    preprocessor_output,
                    music_output,
                    tmp_dir,
                    prompt_output=prompt_output,
                )

        lyrics_sanitize_mock.assert_awaited_once_with("themes of home")
        # Lyric-only requests skip the style pass entirely — a stub result
        # would mask the lyric pass's detections with dataclass defaults.
        style_pass_mock.assert_not_awaited()
        # A lyric direction forces vocals on even though nothing else asked.
        self.assertTrue(output.include_vocals)
        prompt_input = prompt_mock.await_args.args[0]  # type: ignore[union-attr]
        self.assertTrue(prompt_input.verbose_instruction)
        self.assertEqual(prompt_input.music_style_prompt, "")
        self.assertEqual(prompt_input.lyrics_prompt, "sanitized themes of home")
        # The gender stated in the lyric direction wins — not the "female"
        # dataclass default a stub style result would have injected.
        self.assertEqual(prompt_input.vocal_gender, "male")

    def test_lyrics_direction_on_basic_rejected_in_workflow(self):
        """Defense in depth: a lyric-directed job that reaches the workflow on
        edenn_basic fails loudly with the self-repairing message, never by
        silently dropping the paid-for direction."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            workflow_input = VideoMusicWorkflowE2EInput(
                video_path=str(video_metadata.path),
                user_prompt="",
                verbose_instruction=True,
                lyrics_prompt="themes of home",
                music_model_spec="edenn_basic",
            )
            with self.assertRaises(EdennValidationError) as caught:
                _run_workflow(
                    workflow_input,
                    self._make_preprocessor_output(),
                    _minimal_music_output(tmp_dir),
                    tmp_dir,
                )
        self.assertIn("edenn_enhanced", caught.exception.public_message)
        self.assertIn("edenn_studio", caught.exception.public_message)

    def test_fully_empty_verbose_request_rejected_in_workflow(self):
        """Neither style nor lyric direction: still malformed."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            workflow_input = VideoMusicWorkflowE2EInput(
                video_path=str(video_metadata.path),
                user_prompt="",
                verbose_instruction=True,
                music_style_prompt=None,
                lyrics_prompt=None,
                music_model_spec="edenn_enhanced",
            )
            with self.assertRaises(EdennValidationError):
                _run_workflow(
                    workflow_input,
                    self._make_preprocessor_output(),
                    _minimal_music_output(tmp_dir),
                    tmp_dir,
                )

    def test_vocal_clone_without_vocals_raises_validation_error(self):
        """vocal_id provided but preprocessor resolves include_vocals=False → EdennValidationError."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            preprocessor_output = self._make_preprocessor_output(detected_include_vocals=False)
            music_output = _minimal_music_output(tmp_dir)
            workflow_input = VideoMusicWorkflowE2EInput(
                video_path=str(video_metadata.path),
                user_prompt="some prompt",
                music_model_spec="edenn_basic",
                vocal_id="clone_voice_999",
            )
            with self.assertRaises(EdennValidationError):
                _run_workflow(workflow_input, preprocessor_output, music_output, tmp_dir)

    def test_vocal_sample_path_without_vocals_raises_validation_error(self):
        """vocal_sample_path provided but preprocessor resolves include_vocals=False → EdennValidationError."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            preprocessor_output = self._make_preprocessor_output(detected_include_vocals=False)
            music_output = _minimal_music_output(tmp_dir)
            sample_path = tmp_dir / "voice_sample.wav"
            _write_sine_wav(sample_path)
            workflow_input = VideoMusicWorkflowE2EInput(
                video_path=str(video_metadata.path),
                user_prompt="some prompt",
                music_model_spec="edenn_basic",
                vocal_sample_path=sample_path,
            )
            with self.assertRaises(EdennValidationError):
                _run_workflow(workflow_input, preprocessor_output, music_output, tmp_dir)

    def test_vocal_id_used_is_forwarded_from_music_stage(self):
        """vocal_id_used set on music output flows through to the workflow output."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            preprocessor_output = self._make_preprocessor_output(detected_include_vocals=True)
            audio = tmp_dir / "gen.wav"
            _write_sine_wav(audio)
            music_output = MusicGenerationStageOutput(music_path=audio, vocal_id_used="cloned_voice_42")
            workflow_input = VideoMusicWorkflowE2EInput(
                video_path=str(video_metadata.path),
                user_prompt="pop song",
                music_model_spec="edenn_enhanced",
            )
            output, *_ = _run_workflow(workflow_input, preprocessor_output, music_output, tmp_dir)

        self.assertEqual(output.vocal_id_used, "cloned_voice_42")

    def test_job_timestamps_are_set_and_monotonic(self):
        """job_received_timestamp and job_finished_timestamp are set and received <= finished."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            preprocessor_output = self._make_preprocessor_output()
            music_output = _minimal_music_output(tmp_dir)
            workflow_input = VideoMusicWorkflowE2EInput(
                video_path=str(video_metadata.path),
                user_prompt="upbeat",
                music_model_spec="edenn_basic",
            )
            before = int(time.time())
            output, *_ = _run_workflow(workflow_input, preprocessor_output, music_output, tmp_dir)
            after = int(time.time())

        self.assertIsNotNone(output.job_received_timestamp)
        self.assertIsNotNone(output.job_finished_timestamp)
        self.assertGreaterEqual(output.job_received_timestamp, before)
        self.assertLessEqual(output.job_finished_timestamp, after)
        self.assertLessEqual(output.job_received_timestamp, output.job_finished_timestamp)

    def test_music_prompt_in_chinese_mirrors_music_prompt(self):
        """music_prompt_in_chinese is set to the same value as music_prompt (pin current behaviour)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            preprocessor_output = self._make_preprocessor_output()
            music_output = _minimal_music_output(tmp_dir)
            workflow_input = VideoMusicWorkflowE2EInput(
                video_path=str(video_metadata.path),
                user_prompt="pop",
                music_model_spec="edenn_basic",
            )
            output, *_ = _run_workflow(workflow_input, preprocessor_output, music_output, tmp_dir)

        self.assertEqual(output.music_prompt_in_chinese, output.music_prompt)

    def test_token_aggregation_with_zero_stage_usage(self):
        """Stages returning empty/None token_usage don't crash and contribute zeros to the total."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            preprocessor_output = self._make_preprocessor_output(
                tokens_used=0, prompt_tokens=0, completion_tokens=0
            )
            defaults = _default_stage_outputs(tmp_dir, video_metadata)
            preprocess_output, scene_output, understanding_output, _ = defaults
            # SceneSegmentationStageOutput accepts None for token_usage
            scene_output_empty = SceneSegmentationStageOutput(
                scene_understanding_messages=scene_output.scene_understanding_messages,
                token_usage=None,
                thumbnail_path=scene_output.thumbnail_path,
            )
            # VideoUnderstandingStageOutput requires a dict; use empty to exercise _normalize_token_usage({})
            understanding_output_empty = VideoUnderstandingStageOutput(
                video_descriptions=understanding_output.video_descriptions,
                video_title=understanding_output.video_title,
                video_description=understanding_output.video_description,
                token_usage={},
            )
            # MusicPromptOrchestrationStageOutput requires a dict; use empty dict
            prompt_output_empty = MusicPromptOrchestrationStageOutput(
                music_generation_prompt={"global_music_prompt": "pop"},
                token_usage={},
                downstream_generation_model_spec="edenn_basic",
            )
            music_output = _minimal_music_output(tmp_dir)
            workflow_input = VideoMusicWorkflowE2EInput(
                video_path=str(video_metadata.path),
                user_prompt="pop",
                music_model_spec="edenn_basic",
            )
            output, *_ = _run_workflow(
                workflow_input, preprocessor_output, music_output, tmp_dir,
                preprocess_output=preprocess_output,
                scene_output=scene_output_empty,
                understanding_output=understanding_output_empty,
                prompt_output=prompt_output_empty,
            )

        self.assertEqual(output.token_usage, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        assert output.token_usage_breakdown is not None
        for stage_key in ("scene_understanding", "video_summary", "music_prompt_orchestration"):
            self.assertEqual(
                output.token_usage_breakdown[stage_key],
                {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            )

    def test_preserve_original_audio_and_music_volume_reach_remix_stage(self):
        """preserve_original_audio and music_volume from input are forwarded to the remix stage."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            preprocessor_output = self._make_preprocessor_output()
            music_output = _minimal_music_output(tmp_dir)
            workflow_input = VideoMusicWorkflowE2EInput(
                video_path=str(video_metadata.path),
                user_prompt="mellow",
                music_model_spec="edenn_basic",
                preserve_original_audio=True,
                music_volume=0.6,
            )
            _, *_, remix_mock = _run_workflow(workflow_input, preprocessor_output, music_output, tmp_dir)

        remix_input = remix_mock.await_args.args[0]  # type: ignore[union-attr]
        self.assertTrue(remix_input.preserve_original_audio)
        self.assertAlmostEqual(remix_input.music_volume, 0.6)

    def test_annotation_dispatcher_is_forwarded_to_each_pipeline_stage(self):
        """When stage runs are mocked, verify dispatcher propagation and workflow-owned events."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            preprocessor_output = self._make_preprocessor_output()
            music_output = _minimal_music_output(tmp_dir)
            dispatcher = MagicMock()
            workflow_input = VideoMusicWorkflowE2EInput(
                video_path=str(video_metadata.path),
                user_prompt="pop",
                music_model_spec="edenn_basic",
                annotation_dispatcher=dispatcher,
            )
            (
                _,
                _preprocessor_mock,
                preprocess_mock,
                segmentation_mock,
                understanding_mock,
                prompt_mock,
                music_mock,
                remix_mock,
            ) = _run_workflow(workflow_input, preprocessor_output, music_output, tmp_dir)

        for stage_mock in (
            preprocess_mock,
            segmentation_mock,
            understanding_mock,
            prompt_mock,
            music_mock,
            remix_mock,
        ):
            stage_call_input = stage_mock.await_args.args[0]
            self.assertIs(stage_call_input.annotation_dispatcher, dispatcher)

        self.assertEqual(
            [call.args[0].event_type for call in dispatcher.emit.call_args_list],
            ["request_context", "music_prompt", "remix_completion"],
        )


if __name__ == "__main__":
    unittest.main()
