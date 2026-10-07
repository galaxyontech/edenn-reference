import asyncio
import httpx
import math
import os
import struct
import shutil
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from EdennCode.ModelFactory.LanguageModelFactory.gateway_clients import (
    GatewayBadRequestError as BadRequestError,
)

from EdennCode.Deployment.storage import AzureBlobStorageService, TemporaryBlobUpload
from EdennCode.exceptions import (
    EdennContentPolicyViolationError,
    EdennConfigurationError,
    EdennProviderError,
    EdennProviderImageFetchTimeoutError,
    EdennProviderResponseError,
)
from EdennCode.ModelFactory.LanguageModelFactory.azure_based_model_model_gateway import AzureMultimodalClient
from EdennCode.ModelFactory.PromptFactory.prompts import ResponseSchemas
from EdennCode.ModelFactory.PromptFactory.prompts import Prompt
from EdennCode.WorkflowFactory.VideoMusicWorkflow.PipelineDataModel.pipeline_datamodel import (
    Language,
    VideoCategory,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata.video_metadata import VideoMetadata
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicGenerationStage.music_generation_stage import (
    provider_appends_track_tail,
    FullTrackLyricsData,
    MusicGenerationStage,
    MusicGenerationStageOutput,
    MusicGenerationStageInput,
    MusicGenertionModelEnum,
    to_eleven_ms,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.music_matching_stage import (
    MusicMatchingStageOutput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicPromptOrchestrationStage.music_prompt_orchestration_stage import (
    MusicPromptOrchestrationStage,
    MusicPromptOrchestrationStageInput,
    MusicPromptOrchestrationStageOutput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.word_ts import WordTS
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_music import (
    ProviderBTimestampedLyrics,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.datamodel import SceneUnderstanding
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.scene_segmentation_stage import (
    SceneSegmentationStage,
    SceneSegmentationStageOutput,
    extract_frame_jpeg_bytes,
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
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow import (
    VideoMusicWorkflowE2E,
    VideoMusicWorkflowE2EInput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoPreprocessStage.media_preprocess_stage import (
    InputMediaAssetTyps,
    PreprocessStage,
    PreprocessStageOutput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoUnderstandingStage.video_understanding_page import (
    VideoUnderstandingStage,
    VideoUnderstandingStageOutput,
)
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_c import (
    GenerationResult,
    ProviderCTrack,
)
from EdennCode.TestSuites.helpers.paths import SMOKE_VIDEO_PATH


def _write_sine_wav(path: Path, duration: float = 0.5, sample_rate: int = 16000) -> None:
    """Write a tiny sine wave so downstream stages see a real audio file."""
    frames = int(duration * sample_rate)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        for i in range(frames):
            val = int(32767 * 0.1 * math.sin(2 * math.pi * 440 * (i / sample_rate)))
            wav.writeframes(struct.pack("<h", val))


def _fake_video_metadata(tmp_dir: Path) -> VideoMetadata:
    workdir = tmp_dir / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    return VideoMetadata(
        path=SMOKE_VIDEO_PATH,
        duration=2.5,
        size_bytes=SMOKE_VIDEO_PATH.stat().st_size,
        width=320,
        height=240,
        fps=24.0,
        video_codec="h264",
        video_bit_rate=200_000,
        has_audio=False,
        audio_codec=None,
        audio_channels=None,
        audio_sample_rate=None,
        audio_bit_rate=None,
        temp_folder=str(workdir),
    )


class _FakeContentPolicyError(Exception):
    def __init__(self) -> None:
        self.body = {
            "message": "Your input image may contain content that is not allowed by our content safety system.",
            "type": "invalid_request_error",
            "param": None,
            "code": "content_policy_violation",
        }
        super().__init__(str(self.body))


def _scene_prompt_builder():
    return MagicMock(
        build_scene_understanding_messages=MagicMock(
            side_effect=lambda **kwargs: [
                {
                    "role": "system",
                    "content": [{"type": "text", "text": "system"}],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "prompt"},
                        *(
                            [{"type": "image_url", "image_url": {"url": kwargs["first_frame_url"]}}]
                            if kwargs.get("first_frame_url") else []
                        ),
                        *(
                            [{"type": "image_url", "image_url": {"url": kwargs["last_frame_url"]}}]
                            if kwargs.get("last_frame_url") else []
                        ),
                    ],
                },
            ]
        )
    )


def _image_urls_from_prompt(prompt):
    return [
        part["image_url"]["url"]
        for part in prompt[1]["content"]
        if part["type"] == "image_url"
    ]


def _make_model_gateway_bad_request_error(body, *, message: str | None = None) -> BadRequestError:
    request = httpx.Request(
        "POST",
        "https://example.model_gateway.azure.com/model_gateway/deployments/chat-test/chat/completions",
    )
    response_kwargs = {"request": request}
    if isinstance(body, (dict, list)):
        response_kwargs["json"] = body
    else:
        response_kwargs["text"] = str(body)
    response = httpx.Response(400, **response_kwargs)
    return BadRequestError(
        message or f"Error code: 400 - {body}",
        response=response,
        body=body,
    )


class VideoMusicWorkflowE2ETests(unittest.TestCase):
    def test_workflow_init_defers_model_specific_music_provider_init(self) -> None:
        with patch(
            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.build_azure_client",
            return_value=MagicMock(),
        ), patch(
            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.build_music_provider",
            side_effect=AssertionError("ProviderA should be lazy"),
        ) as build_basic, patch(
            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.ProviderCApi",
            side_effect=AssertionError("ProviderC should be lazy"),
        ) as build_studio, patch(
            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.build_edenn_enhanced_music_provider",
            side_effect=EdennConfigurationError(
                "PG unavailable",
                component="provider_b",
                operation="initialize",
            ),
        ) as build_enhanced:
            workflow = VideoMusicWorkflowE2E()

        build_basic.assert_not_called()
        build_studio.assert_not_called()
        build_enhanced.assert_not_called()
        self.assertIsNone(workflow.provider_a_music_provider)
        self.assertIsNone(workflow.provider_c_music_provider)
        self.assertIsNone(workflow.provider_b_music_provider)

    def test_azure_client_maps_image_download_timeout_bad_request(self) -> None:
        client = AzureMultimodalClient(
            azure_endpoint="https://example.model_gateway.azure.com",
            azure_api_version="2024-12-01-preview",
            azure_model="chat-test",
            api_key="test-key",
        )
        exc = _make_model_gateway_bad_request_error(
            {
                "error": {
                    "message": (
                        "Timed out while downloading image from "
                        "https://example.blob.core.windows.net/container/frame.jpg?sig=REDACTED."
                    ),
                    "type": "invalid_request_error",
                    "param": None,
                    "code": None,
                }
            }
        )

        mapped = client._map_bad_request_error(
            exc,
            operation="chat.completions.create",
        )

        self.assertIsInstance(mapped, EdennProviderImageFetchTimeoutError)
        self.assertEqual(
            mapped.failed_image_url,
            "https://example.blob.core.windows.net/container/frame.jpg?sig=REDACTED",
        )
        self.assertTrue(mapped.retryable)

    def test_azure_client_maps_image_download_timeout_from_message_only(self) -> None:
        client = AzureMultimodalClient(
            azure_endpoint="https://example.model_gateway.azure.com",
            azure_api_version="2024-12-01-preview",
            azure_model="chat-test",
            api_key="test-key",
        )
        body_text = (
            "{'error': {'message': 'Timed out while downloading image from "
            "https://example.blob.core.windows.net/container/frame.jpg?sig=REDACTED.', "
            "'type': 'invalid_request_error', 'param': None, 'code': None}}"
        )
        exc = _make_model_gateway_bad_request_error(
            body_text,
            message=f"Error code: 400 - {body_text}",
        )

        mapped = client._map_bad_request_error(
            exc,
            operation="chat.completions.create",
        )

        self.assertIsInstance(mapped, EdennProviderImageFetchTimeoutError)
        self.assertEqual(
            mapped.failed_image_url,
            "https://example.blob.core.windows.net/container/frame.jpg?sig=REDACTED",
        )

    def test_azure_client_maps_image_download_failure_bad_request(self) -> None:
        client = AzureMultimodalClient(
            azure_endpoint="https://example.model_gateway.azure.com",
            azure_api_version="2024-12-01-preview",
            azure_model="chat-test",
            api_key="test-key",
        )
        exc = _make_model_gateway_bad_request_error(
            {
                "error": {
                    "message": (
                        "Unable to download image from "
                        "https://example.blob.core.windows.net/container/frame.jpg?sig=REDACTED."
                    ),
                    "type": "invalid_request_error",
                    "param": None,
                    "code": None,
                }
            }
        )

        mapped = client._map_bad_request_error(
            exc,
            operation="chat.completions.create",
        )

        self.assertIsInstance(mapped, EdennProviderImageFetchTimeoutError)
        self.assertEqual(
            mapped.failed_image_url,
            "https://example.blob.core.windows.net/container/frame.jpg?sig=REDACTED",
        )
        self.assertTrue(mapped.retryable)

    def test_azure_client_maps_generic_image_fetch_failure_bad_request(self) -> None:
        client = AzureMultimodalClient(
            azure_endpoint="https://example.model_gateway.azure.com",
            azure_api_version="2024-12-01-preview",
            azure_model="chat-test",
            api_key="test-key",
        )
        exc = _make_model_gateway_bad_request_error(
            {
                "error": {
                    "message": (
                        "Failed to fetch input image from "
                        "https://example.blob.core.windows.net/container/frame.jpg?sig=REDACTED;"
                    ),
                    "type": "invalid_request_error",
                    "param": None,
                    "code": None,
                }
            }
        )

        mapped = client._map_bad_request_error(
            exc,
            operation="chat.completions.create",
        )

        self.assertIsInstance(mapped, EdennProviderImageFetchTimeoutError)
        self.assertEqual(
            mapped.failed_image_url,
            "https://example.blob.core.windows.net/container/frame.jpg?sig=REDACTED",
        )
        self.assertTrue(mapped.retryable)

    def test_azure_client_maps_content_filter_bad_request_innererror(self) -> None:
        client = AzureMultimodalClient(
            azure_endpoint="https://example.model_gateway.azure.com",
            azure_api_version="2024-12-01-preview",
            azure_model="chat-test",
            api_key="test-key",
        )
        exc = _make_model_gateway_bad_request_error(
            {
                "error": {
                    "message": "The response was filtered due to the prompt triggering the model gateway's content management policy.",
                    "type": None,
                    "param": "prompt",
                    "code": "content_filter",
                    "status": 400,
                    "innererror": {
                        "code": "ResponsibleAIPolicyViolation",
                        "content_filter_result": {
                            "sexual": {"filtered": True, "severity": "high"},
                            "violence": {"filtered": False, "severity": "safe"},
                        },
                    },
                }
            }
        )

        mapped = client._map_bad_request_error(
            exc,
            operation="chat.completions.create",
        )

        self.assertIsInstance(mapped, EdennContentPolicyViolationError)
        self.assertEqual(mapped.provider_name, "model_gateway")
        self.assertEqual(mapped.policy_code, "ResponsibleAIPolicyViolation")
        self.assertEqual(mapped.provider_error_code, "content_filter")
        self.assertEqual(mapped.param, "prompt")
        self.assertEqual(
            mapped.filter_results["sexual"],
            {"filtered": True, "severity": "high"},
        )

    def test_force_provider_b_only_for_chinese_lyrics(self) -> None:
        self.assertTrue(
            VideoMusicWorkflowE2E._should_force_provider_b_for_chinese_lyrics(
                Language.CN,
                True,
                "edenn_basic",
            )
        )
        self.assertTrue(
            VideoMusicWorkflowE2E._should_force_provider_b_for_chinese_lyrics(
                "zh",
                True,
                "edenn_basic",
            )
        )
        self.assertFalse(
            VideoMusicWorkflowE2E._should_force_provider_b_for_chinese_lyrics(
                Language.CN,
                True,
                "edenn_studio",
            )
        )
        self.assertFalse(
            VideoMusicWorkflowE2E._should_force_provider_b_for_chinese_lyrics(
                Language.CN,
                True,
                "edenn_enhanced",
            )
        )
        self.assertFalse(
            VideoMusicWorkflowE2E._should_force_provider_b_for_chinese_lyrics(
                Language.CN,
                False,
                "edenn_basic",
            )
        )
        self.assertFalse(
            VideoMusicWorkflowE2E._should_force_provider_b_for_chinese_lyrics(
                Language.EN,
                True,
                "edenn_basic",
            )
        )

    def test_generate_wires_stage_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            thumbnail_path = tmp_dir / "thumb.jpg"
            thumbnail_path.write_bytes(b"thumb")

            preprocess_output = PreprocessStageOutput(video_metadata=video_metadata)
            scene_output = SceneSegmentationStageOutput(
                scene_understanding_messages=[
                    SceneUnderstanding(
                        scene_index=0,
                        start_timestamp=0.0,
                        end_timestamp=video_metadata.duration,
                        visual_summary="intro scene",
                        key_actions="walks in",
                        mood="calm",
                    )
                ],
                token_usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                thumbnail_path=thumbnail_path,
            )
            understanding_output = VideoUnderstandingStageOutput(
                video_descriptions="a person walks inside a room",
                video_title="shareable title",
                video_description="shareable description",
                token_usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )
            prompt_output = MusicPromptOrchestrationStageOutput(
                music_generation_prompt={
                    "global_music_prompt": "uplifting pop",
                    "global_mood": "uplifting",
                    "tempo_bpm": 110,
                    "instruments": ["guitar"],
                },
                token_usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                downstream_generation_model_spec="edenn_basic",
            )
            preprocessor_output = UserPromptPreprocessorResult(
                was_transformed=False,
                detected_include_vocals=True,
                transformed_prompt="make it bright",
                detected_references=[],
                detected_language=Language.EN,
                detected_category=VideoCategory.DEFAULT,
                detected_vocal_gender="female",
                detected_vocal_language=Language.EN,
                reasoning="ok",
                tokens_used=3,
                prompt_tokens=2,
                completion_tokens=1,
            )

            generated_audio = tmp_dir / "music.wav"
            remixed_paths = []

            async def fake_music_run(stage_input):
                if not generated_audio.exists():
                    _write_sine_wav(generated_audio)
                return MusicGenerationStageOutput(
                    music_path=generated_audio,
                    complete_music_path=generated_audio,
                    secondary_complete_music_path=None,
                    lyrics_timestamps=[],
                )

            async def fake_remix_run(stage_input: VideoAudioRemixStageInput):
                out_path = Path(stage_input.video_metadata.temp_folder) / "remix.mp4"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(b"remixed")
                remixed_paths.append(out_path)
                return VideoAudioRemixStageOutput(remixed_video_path=out_path.name)

            preprocess_mock = AsyncMock(return_value=preprocess_output)
            segmentation_mock = AsyncMock(return_value=scene_output)
            understanding_mock = AsyncMock(return_value=understanding_output)
            prompt_mock = AsyncMock(return_value=prompt_output)
            music_mock = AsyncMock(side_effect=fake_music_run)
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
            ), patch.object(PreprocessStage, "run", new=preprocess_mock), patch.object(
                SceneSegmentationStage, "run", new=segmentation_mock
            ), patch.object(VideoUnderstandingStage, "run", new=understanding_mock), patch.object(
                MusicPromptOrchestrationStage, "run", new=prompt_mock
            ), patch.object(
                MusicGenerationStage, "run", new=music_mock
            ), patch.object(VideoAudioRemixStage, "run", new=remix_mock):
                workflow = VideoMusicWorkflowE2E()
                workflow_input = VideoMusicWorkflowE2EInput(
                    video_path=str(video_metadata.path),
                    include_vocals=True,
                    vocal_gender="female",
                    user_prompt="make it bright",
                    music_model_spec="edenn_basic",
                )
                output = asyncio.run(workflow.generate(workflow_input))

            preprocess_mock.assert_awaited_once()
            segmentation_mock.assert_awaited_once()
            understanding_mock.assert_awaited_once()
            prompt_mock.assert_awaited_once()
            music_mock.assert_awaited_once()
            remix_mock.assert_awaited_once()

            preprocess_input = preprocess_mock.await_args.args[0]
            self.assertEqual(preprocess_input.asset_path, str(video_metadata.path))
            self.assertEqual(preprocess_input.asset_type, InputMediaAssetTyps.VIDEO)

            segmentation_input = segmentation_mock.await_args.args[0]
            self.assertEqual(segmentation_input.video_path, video_metadata.path)
            self.assertEqual(segmentation_input.duration, video_metadata.duration)
            self.assertEqual(segmentation_input.fps, video_metadata.fps)
            self.assertEqual(segmentation_input.preferred_language, Language.EN)

            understanding_input = understanding_mock.await_args.args[0]
            self.assertEqual(understanding_input.list_of_scene, scene_output.scene_understanding_messages)
            self.assertEqual(understanding_input.preferred_language, Language.EN)

            prompt_input = prompt_mock.await_args.args[0]
            self.assertEqual(prompt_input.list_of_scene, scene_output.scene_understanding_messages)
            self.assertEqual(prompt_input.language, Language.EN)

            music_input = music_mock.await_args.args[0]
            self.assertEqual(music_input.prompt_metadata, prompt_output.music_generation_prompt)
            self.assertEqual(music_input.video_metadata, video_metadata)
            self.assertTrue(music_input.include_vocals)
            self.assertEqual(music_input.vocal_gender, "female")

            remix_input = remix_mock.await_args.args[0]
            self.assertEqual(remix_input.video_metadata, video_metadata)
            self.assertEqual(remix_input.music_path, generated_audio)
            self.assertFalse(remix_input.preserve_original_audio)
            self.assertEqual(remix_input.music_volume, 1.0)

            self.assertTrue(generated_audio.exists())
            self.assertTrue(remixed_paths and remixed_paths[0].exists())
            self.assertEqual(output.complete_generated_music_path, generated_audio)
            self.assertIsNone(output.secondary_complete_generated_music_path)
            self.assertEqual(
                output.token_usage,
                {
                    "prompt_tokens": 5,
                    "completion_tokens": 4,
                    "total_tokens": 9,
                },
            )
            self.assertEqual(
                output.token_usage_breakdown,
                {
                    "user_prompt_preprocessor": {
                        "prompt_tokens": 2,
                        "completion_tokens": 1,
                        "total_tokens": 3,
                    },
                    "scene_understanding": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                    "video_summary": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                    "music_prompt_orchestration": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )

    def test_to_eleven_ms_clamps_duration(self) -> None:
        self.assertEqual(to_eleven_ms(0.5), 10_000)
        self.assertEqual(to_eleven_ms(42.0), 42_000)
        self.assertEqual(to_eleven_ms(400.0), 300_000)

    def test_provider_c_custom_lyrics_schema_caps_lyrics_prompt_at_190_chars(self) -> None:
        schema = ResponseSchemas.provider_c_custom_lyrics()
        self.assertEqual(
            schema["schema"]["properties"]["lyrics_prompt"]["maxLength"],
            190,
        )
        self.assertIn(
            "Extremely short",
            schema["schema"]["properties"]["lyrics_prompt"]["description"],
        )

    def test_provider_c_custom_lyrics_prompt_requests_extremely_short_lyrics_instruction(self) -> None:
        prompt = Prompt().format_provider_c_custom_lyrics(
            scene_lines="Scene 0 (0.00s-3.00s): city lights",
            include_vocals=True,
            vocal_gender="female",
            user_prompt="modern pop",
            language=Language.EN,
        )

        self.assertIn("Must be very short and paced to the video.", prompt)
        self.assertIn("under 190 characters", prompt)

    def test_verbose_lyrics_preprocessor_preserves_original_language_guidance(self) -> None:
        class _FakeLlmClient:
            def __init__(self) -> None:
                self.messages = None

            async def complete_messages(self, *, messages, json_schema, max_tokens):
                self.messages = messages
                return (
                    {
                        "was_transformed": False,
                        "transformed_prompt": "El coro debe decir: brilla mi corazon",
                        "detected_references": [],
                        "detected_language": "SPANISH",
                        "detected_vocal_language": "SPANISH",
                        "detected_category": VideoCategory.DEFAULT,
                        "detected_vocal_gender": "unknown",
                        "detected_include_vocals": True,
                        "reasoning": "Spanish lyrics guidance.",
                    },
                    {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                )

        fake_client = _FakeLlmClient()
        agent = UserPromptPreprocessorAgent(llm_client=fake_client)
        result = asyncio.run(
            agent.preprocess_lyrics_prompt("El coro debe decir: brilla mi corazon")
        )

        self.assertEqual(result.transformed_prompt, "El coro debe decir: brilla mi corazon")
        self.assertTrue(result.detected_include_vocals)
        self.assertEqual(result.detected_vocal_language, "SPANISH")
        assert fake_client.messages is not None
        self.assertIn("Do not translate lyrics guidance", fake_client.messages[0]["content"])

    def test_verbose_prompt_orchestration_uses_generated_lyrics_prompt(self) -> None:
        class _FakeLlmClient:
            def __init__(self) -> None:
                self.calls = []

            async def complete_messages(self, messages, json_schema):
                self.calls.append((messages, json_schema))
                return (
                    {
                        "style_prompt": "video-guided cinematic female vocal pop",
                        "lyrics_prompt": "model-generated lyric prompt",
                    },
                    {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                )

        fake_client = _FakeLlmClient()
        stage = MusicPromptOrchestrationStage(llm_client=fake_client)
        output = asyncio.run(
            stage.run(
                MusicPromptOrchestrationStageInput(
                    list_of_scene=[
                        SceneUnderstanding(
                            scene_index=0,
                            start_timestamp=0.0,
                            end_timestamp=3.0,
                            visual_summary="coastal holiday launch",
                            key_actions="guests cheer near the shore",
                            mood="warm and celebratory",
                        )
                    ],
                    include_vocals=True,
                    vocal_gender="female",
                    user_prompt="cleaned style",
                    language=Language.EN,
                    modelspec=MusicGenertionModelEnum.EDENN_STUDIO,
                    video_summary={"overall_mood": "festive", "core_message": "holiday offer"},
                    verbose_instruction=True,
                    music_style_prompt="cleaned style",
                    lyrics_prompt="keep the words bright coast in the chorus",
                )
            )
        )

        self.assertEqual(
            output.music_generation_prompt,
            {
                "style_prompt": "video-guided cinematic female vocal pop",
                "lyrics_prompt": "model-generated lyric prompt",
            },
        )
        prompt_text = fake_client.calls[0][0][0]["content"][0]["text"]
        self.assertIn("keep the words bright coast in the chorus", prompt_text)
        self.assertIn("overall_mood: festive", prompt_text)
        self.assertIn("music mood=warm and celebratory", prompt_text)
        self.assertNotIn("coastal holiday launch", prompt_text)
        self.assertNotIn("guests cheer near the shore", prompt_text)

    def test_verbose_prompt_orchestration_generates_lyrics_prompt_when_missing(self) -> None:
        class _FakeLlmClient:
            async def complete_messages(self, messages, json_schema):
                return (
                    {
                        "style_prompt": "video-guided mandopop",
                        "lyrics_prompt": "Mandarin lyrics about a warm coastal celebration.",
                    },
                    {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                )

        stage = MusicPromptOrchestrationStage(llm_client=_FakeLlmClient())
        output = asyncio.run(
            stage.run(
                MusicPromptOrchestrationStageInput(
                    list_of_scene=[
                        SceneUnderstanding(
                            scene_index=0,
                            start_timestamp=0.0,
                            end_timestamp=3.0,
                            visual_summary="seaside celebration",
                            key_actions="people toast together",
                            mood="joyful",
                        )
                    ],
                    include_vocals=True,
                    vocal_gender="female",
                    user_prompt="cleaned style",
                    language=Language.CN,
                    modelspec=MusicGenertionModelEnum.EDENN_ENHANCED,
                    verbose_instruction=True,
                    music_style_prompt="cleaned style",
                    lyrics_prompt="",
                )
            )
        )

        self.assertEqual(output.music_generation_prompt["style_prompt"], "video-guided mandopop")
        self.assertEqual(
            output.music_generation_prompt["lyrics_prompt"],
            "Mandarin lyrics about a warm coastal celebration.",
        )

    def test_enhanced_instrumental_prompt_orchestration_outputs_empty_lyrics_prompt(self) -> None:
        class _FakeLlmClient:
            def __init__(self) -> None:
                self.messages = None

            async def complete_messages(self, messages, json_schema):
                self.messages = messages
                return (
                    {
                        "style_prompt": "video-guided cinematic instrumental score",
                        "lyrics_prompt": "lyrics should be discarded",
                    },
                    {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                )

        fake_client = _FakeLlmClient()
        stage = MusicPromptOrchestrationStage(llm_client=fake_client)
        output = asyncio.run(
            stage.run(
                MusicPromptOrchestrationStageInput(
                    list_of_scene=[
                        SceneUnderstanding(
                            scene_index=0,
                            start_timestamp=0.0,
                            end_timestamp=3.0,
                            visual_summary="quiet coastal product scene",
                            key_actions="camera glides across the product",
                            mood="calm",
                        )
                    ],
                    include_vocals=False,
                    vocal_gender="",
                    user_prompt="cleaned instrumental style",
                    language=Language.EN,
                    modelspec=MusicGenertionModelEnum.EDENN_ENHANCED,
                )
            )
        )

        self.assertEqual(
            output.music_generation_prompt,
            {
                "style_prompt": "video-guided cinematic instrumental score",
                "lyrics_prompt": "",
            },
        )
        assert fake_client.messages is not None
        prompt_text = fake_client.messages[0]["content"][0]["text"]
        self.assertIn("instrumental/no lyrics", prompt_text)

    def test_studio_instrumental_prompt_orchestration_uses_simple_prompt_schema(self) -> None:
        class _FakeLlmClient:
            def __init__(self) -> None:
                self.schemas = []

            async def complete_messages(self, messages, json_schema):
                self.schemas.append(json_schema["name"])
                return (
                    {"prompt": "cinematic instrumental score, no vocals"},
                    {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                )

        fake_client = _FakeLlmClient()
        stage = MusicPromptOrchestrationStage(llm_client=fake_client)
        output = asyncio.run(
            stage.run(
                MusicPromptOrchestrationStageInput(
                    list_of_scene=[
                        SceneUnderstanding(
                            scene_index=0,
                            start_timestamp=0.0,
                            end_timestamp=3.0,
                            visual_summary="quiet coastal product scene",
                            key_actions="camera glides across the product",
                            mood="calm",
                        )
                    ],
                    include_vocals=False,
                    vocal_gender="",
                    user_prompt="cleaned instrumental style",
                    language=Language.EN,
                    modelspec=MusicGenertionModelEnum.EDENN_STUDIO,
                    verbose_instruction=True,
                    music_style_prompt="caller instrumental style",
                    lyrics_prompt="",
                )
            )
        )

        self.assertEqual(fake_client.schemas, ["provider_c_simple_prompt"])
        self.assertEqual(
            output.music_generation_prompt,
            {"prompt": "cinematic instrumental score, no vocals"},
        )

    def test_verbose_prompt_orchestration_requires_generated_lyrics_prompt_for_vocals(self) -> None:
        class _FakeLlmClient:
            async def complete_messages(self, messages, json_schema):
                return (
                    {"style_prompt": "video-guided pop", "lyrics_prompt": ""},
                    {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                )

        stage = MusicPromptOrchestrationStage(llm_client=_FakeLlmClient())
        with self.assertRaises(EdennProviderError):
            asyncio.run(
                stage.run(
                    MusicPromptOrchestrationStageInput(
                        list_of_scene=[
                            SceneUnderstanding(
                                scene_index=0,
                                start_timestamp=0.0,
                                end_timestamp=3.0,
                                visual_summary="seaside celebration",
                                key_actions="people toast together",
                                mood="joyful",
                            )
                        ],
                        include_vocals=True,
                        vocal_gender="female",
                        user_prompt="cleaned style",
                        language=Language.EN,
                        modelspec=MusicGenertionModelEnum.EDENN_STUDIO,
                        verbose_instruction=True,
                        music_style_prompt="cleaned style",
                        lyrics_prompt="guide the chorus words",
                    )
                )
            )

    def test_verbose_prompt_orchestration_requires_generated_style_prompt(self) -> None:
        class _FakeLlmClient:
            async def complete_messages(self, messages, json_schema):
                return (
                    {"style_prompt": "", "lyrics_prompt": "lyrics about the coast"},
                    {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                )

        stage = MusicPromptOrchestrationStage(llm_client=_FakeLlmClient())
        with self.assertRaises(EdennProviderError):
            asyncio.run(
                stage.run(
                    MusicPromptOrchestrationStageInput(
                        list_of_scene=[
                            SceneUnderstanding(
                                scene_index=0,
                                start_timestamp=0.0,
                                end_timestamp=3.0,
                                visual_summary="seaside celebration",
                                key_actions="people toast together",
                                mood="joyful",
                            )
                        ],
                        include_vocals=True,
                        vocal_gender="female",
                        user_prompt="cleaned style",
                        language=Language.EN,
                        modelspec=MusicGenertionModelEnum.EDENN_STUDIO,
                        verbose_instruction=True,
                        music_style_prompt="cleaned style",
                        lyrics_prompt="guide the chorus words",
                    )
                )
            )

    def test_provider_c_music_generation_custom_mode_uses_lyrics_prompt_without_suffix(self) -> None:
        class _FakeProviderCProvider:
            def __init__(self, audio_path: Path) -> None:
                self.audio_path = audio_path
                self.lyrics_prompts = []
                self.generate_params = []

            async def generate_lyrics(self, *, prompt: str, **_kwargs):
                self.lyrics_prompts.append(prompt)
                return "generated lyrics"

            async def generate_and_poll_tracks(self, gen_params, **_kwargs):
                self.generate_params.append(gen_params)
                track = ProviderCTrack(audio_id="orig_track", audio_url="https://example/orig.wav")
                return "task_orig", GenerationResult(task_id="task_orig", status="SUCCESS", tracks=[track]), "base"

            async def download(self, _track: ProviderCTrack, dest_path: Path) -> Path:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                dest_path.write_bytes(self.audio_path.read_bytes())
                return dest_path

            async def wait_for_timestamped_lyrics(self, *_args, **_kwargs):
                return [WordTS(text="generated", startS=0.0, endS=1.0, i=0)]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            generated_audio = tmp_dir / "generated.wav"
            _write_sine_wav(generated_audio, duration=12.0)

            fake_provider_c = _FakeProviderCProvider(generated_audio)
            stage = MusicGenerationStage(
                provider_a_music_provider=MagicMock(),
                provider_c_music_provider=fake_provider_c,
                provider_b_music_provider=MagicMock(),
            )

            final_path, secondary_path, primary_track_lyrics, secondary_track_lyrics = asyncio.run(
                stage.provider_c_music_generation_workflow(
                    {
                        "lyrics_prompt": "base lyric prompt",
                        "style_prompt": "cinematic female vocal anthem",
                    },
                    include_vocals=True,
                    vocal_gender="female",
                    video_metadata=video_metadata,
                    provider_c_custom_mode=True,
                )
            )

            self.assertEqual(fake_provider_c.lyrics_prompts, ["base lyric prompt"])
            self.assertEqual(fake_provider_c.generate_params[0].prompt, "generated lyrics")
            self.assertEqual(fake_provider_c.generate_params[0].style, "cinematic female vocal anthem")
            # Studio full tracks are tail-trimmed, so the delivered name may be
            # "primary.mp3" or "primary_trimmed_tail6s.mp3".
            self.assertTrue(final_path.name.startswith("primary"), final_path.name)
            self.assertIsNone(secondary_path)
            self.assertIsNone(secondary_track_lyrics)
            self.assertEqual(
                [word.text for word in primary_track_lyrics.word_level_lyrics_timestamps],
                ["generated"],
            )
            self.assertEqual(primary_track_lyrics.full_lyrics, "generated lyrics")

    def test_provider_c_music_generation_instrumental_uses_simple_mode_and_skips_lyrics(self) -> None:
        class _FakeProviderCProvider:
            def __init__(self, audio_path: Path) -> None:
                self.audio_path = audio_path
                self.generate_params = []

            async def generate_lyrics(self, **_kwargs):
                raise AssertionError("Instrumental ProviderC generation must not request lyrics")

            async def generate_and_poll_tracks(self, gen_params, **_kwargs):
                self.generate_params.append(gen_params)
                track = ProviderCTrack(audio_id="instrumental_track", audio_url="https://example/instrumental.wav")
                return "task_inst", GenerationResult(task_id="task_inst", status="SUCCESS", tracks=[track]), "base"

            async def download(self, _track: ProviderCTrack, dest_path: Path) -> Path:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                dest_path.write_bytes(self.audio_path.read_bytes())
                return dest_path

            async def wait_for_timestamped_lyrics(self, *_args, **_kwargs):
                raise AssertionError("Instrumental ProviderC generation must not poll timestamped lyrics")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            generated_audio = tmp_dir / "generated.wav"
            _write_sine_wav(generated_audio, duration=12.0)

            fake_provider_c = _FakeProviderCProvider(generated_audio)
            stage = MusicGenerationStage(
                provider_a_music_provider=MagicMock(),
                provider_c_music_provider=fake_provider_c,
                provider_b_music_provider=MagicMock(),
            )

            final_path, secondary_path, primary_track_lyrics, secondary_track_lyrics = asyncio.run(
                stage.provider_c_music_generation_workflow(
                    {"prompt": "cinematic instrumental bed"},
                    include_vocals=False,
                    vocal_gender="",
                    video_metadata=video_metadata,
                    provider_c_custom_mode=True,
                )
            )

            params = fake_provider_c.generate_params[0]
            self.assertFalse(params.custom_mode)
            self.assertTrue(params.instrumental)
            self.assertEqual(params.prompt, "cinematic instrumental bed")
            # Studio full tracks are tail-trimmed, so the delivered name may be
            # "primary.mp3" or "primary_trimmed_tail6s.mp3".
            self.assertTrue(final_path.name.startswith("primary"), final_path.name)
            self.assertIsNone(secondary_path)
            self.assertIsNone(secondary_track_lyrics)
            self.assertIsNone(primary_track_lyrics.full_lyrics)
            self.assertEqual(primary_track_lyrics.lyrics_timestamps, [])
            self.assertEqual(primary_track_lyrics.word_level_lyrics_timestamps, [])

    def test_provider_c_music_generation_extends_when_track_is_shorter_than_video(self) -> None:
        class _FakeProviderCProvider:
            def __init__(self, short_path: Path, extended_path: Path) -> None:
                self.short_path = short_path
                self.extended_path = extended_path
                self.extend_calls = []
                self.timestamp_calls = []

            async def generate_and_poll_tracks(self, *_args, **_kwargs):
                track = ProviderCTrack(audio_id="orig_track", audio_url="https://example/orig.wav")
                return "task_orig", GenerationResult(task_id="task_orig", status="SUCCESS", tracks=[track]), "base"

            async def download(self, track: ProviderCTrack, dest_path: Path) -> Path:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                source = self.short_path if track.audio_id == "orig_track" else self.extended_path
                dest_path.write_bytes(source.read_bytes())
                return dest_path

            async def extend_and_poll_track(self, audio_id: str, **_kwargs):
                self.extend_calls.append(audio_id)
                return "task_extend", ProviderCTrack(audio_id="extended_track", audio_url="https://example/extended.wav"), "base"

            async def wait_for_timestamped_lyrics(self, task_id: str, audio_id: str, **_kwargs):
                self.timestamp_calls.append((task_id, audio_id))
                return [WordTS(text="extended", startS=0.0, endS=1.0, i=0)]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            video_metadata.duration = 10.0
            short_audio = tmp_dir / "short.wav"
            extended_audio = tmp_dir / "extended.wav"
            _write_sine_wav(short_audio, duration=4.0)
            # The loop reads the CHOPPED length, so the raw extension must reach
            # video + 6s for one round to satisfy it (16s -> 10s chopped).
            _write_sine_wav(extended_audio, duration=16.0)

            fake_provider_c = _FakeProviderCProvider(short_audio, extended_audio)
            stage = MusicGenerationStage(
                provider_a_music_provider=MagicMock(),
                provider_c_music_provider=fake_provider_c,
                provider_b_music_provider=MagicMock(),
            )

            final_path, secondary_path, primary_track_lyrics, secondary_track_lyrics = asyncio.run(
                stage.provider_c_music_generation_workflow(
                    {"prompt": "upbeat pop"},
                    include_vocals=True,
                    vocal_gender="female",
                    video_metadata=video_metadata,
                    provider_c_custom_mode=False,
                )
            )

            self.assertIsNone(secondary_path)
            # The studio provider ends its tracks musically, so nothing is
            # chopped here: the extended track is delivered whole. Chopping it
            # "for parity with enhanced" only ever deleted real music.
            self.assertEqual(final_path.name, "primary.mp3")
            self.assertAlmostEqual(
                MusicGenerationStage._duration_seconds(final_path), 16.0, delta=0.3
            )
            self.assertEqual(fake_provider_c.extend_calls, ["orig_track"])
            self.assertEqual(fake_provider_c.timestamp_calls, [("task_extend", "extended_track")])
            self.assertIsNone(secondary_track_lyrics)
            self.assertEqual(
                [word.text for word in primary_track_lyrics.word_level_lyrics_timestamps],
                ["extended"],
            )
            self.assertIsNone(primary_track_lyrics.full_lyrics)
            self.assertEqual(primary_track_lyrics.generation_api_call_count, 2)

    def test_edenn_enhanced_instrumental_run_uses_provider_b_instrumental_and_matching(self) -> None:
        class _FakeProviderBProvider:
            def __init__(self, audio_path: Path) -> None:
                self.audio_path = audio_path
                self.generate_calls = []
                self.download_calls = []

            def begin_warning_collection(self):
                return object()

            def finish_warning_collection(self, _token):
                return None

            async def generate_instrumental_task(self, **kwargs):
                self.generate_calls.append(kwargs)
                return SimpleNamespace(task_id="task_inst")

            async def wait_instrumental_task(self, task_id: str, **_kwargs):
                return SimpleNamespace(
                    task_id=task_id,
                    raw={"audio_url": "https://example/instrumental.wav"},
                )

            async def download_audio(self, url: str, path: Path):
                self.download_calls.append((url, path))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self.audio_path.read_bytes())
                return path

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            video_metadata.duration = 2.0
            primary_audio = tmp_dir / "primary.wav"
            matched_audio = tmp_dir / "matched.wav"
            _write_sine_wav(primary_audio, duration=3.0)
            _write_sine_wav(matched_audio, duration=2.0)

            fake_provider_b = _FakeProviderBProvider(primary_audio)
            stage = MusicGenerationStage(
                provider_a_music_provider=MagicMock(),
                provider_c_music_provider=MagicMock(),
                provider_b_music_provider=fake_provider_b,
            )
            matching_mock = AsyncMock(return_value=MusicMatchingStageOutput(
                reranked_music_outputs_path=matched_audio,
                music_start_s=0.25,
                aligned_lyrics=[],
                used_track="primary",
                alignment_score=0.42,
                alignment_details={"shape_score": 0.8},
            ))

            with patch.object(stage.music_reranker_stage, "run", new=matching_mock):
                output = asyncio.run(
                    stage.run(
                        MusicGenerationStageInput(
                            prompt_metadata={"style_prompt": "cinematic instrumental score"},
                            video_metadata=video_metadata,
                            include_vocals=False,
                            vocal_gender="",
                            music_generation_model=MusicGenertionModelEnum.EDENN_ENHANCED,
                        )
                    )
                )

            self.assertEqual(fake_provider_b.generate_calls[0]["prompt"], "cinematic instrumental score")
            self.assertEqual(fake_provider_b.generate_calls[0]["model"], "provider_b-9")
            matching_input = matching_mock.await_args.args[0]
            self.assertFalse(matching_input.require_lyrics)
            self.assertEqual(output.music_path, matched_audio)
            self.assertEqual(output.complete_music_path.name, "instrumental.mp3")
            self.assertEqual(output.matching_used_track, "primary")
            self.assertEqual(output.lyrics_timestamps, [])
            self.assertEqual(output.word_level_lyrics_timestamps, [])

    def test_provider_b_music_generation_extends_when_track_is_shorter_than_video(self) -> None:
        class _FakeProviderBProvider:
            def __init__(self, short_path: Path, extended_path: Path) -> None:
                self.short_path = short_path
                self.extended_path = extended_path
                self.extend_calls = []

            def begin_warning_collection(self) -> object:
                return object()

            def finish_warning_collection(self, _token: object) -> None:
                return None

            async def generate_with_variants_detailed(self, **_kwargs):
                self.short_path.with_suffix(".lyrics.txt").write_text("base lyrics", encoding="utf-8")
                return (
                    self.short_path,
                    None,
                    ProviderBTimestampedLyrics(
                        word_level=[WordTS(text="orig", startS=0.0, endS=1.0, i=0)],
                        line_level=[WordTS(text="orig line", startS=0.0, endS=1.0, i=0)],
                    ),
                    None,
                )

            async def extend_song_from_audio_detailed(
                self,
                *,
                audio_path: Path,
                prompt: str,
                lyrics: str,
                output_path: Path,
                **kwargs,
            ):
                self.extend_calls.append(
                    (
                        audio_path.name,
                        prompt,
                        lyrics,
                        output_path.name,
                        kwargs.get("extend_type"),
                        kwargs.get("extend_at_ms"),
                    )
                )
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(self.extended_path.read_bytes())
                return output_path, ProviderBTimestampedLyrics(
                    word_level=[WordTS(text="extended", startS=0.0, endS=2.0, i=0)],
                    line_level=[WordTS(text="extended line", startS=0.0, endS=2.0, i=0)],
                ), "extended lyrics"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            video_metadata.duration = 10.0
            short_audio = tmp_dir / "provider_b_short.wav"
            extended_audio = tmp_dir / "provider_b_extended.wav"
            _write_sine_wav(short_audio, duration=4.0)
            # Raw extension reaches video + 6s so the chopped result covers the
            # video and the loop stops after one round.
            _write_sine_wav(extended_audio, duration=16.0)

            fake_provider_b = _FakeProviderBProvider(short_audio, extended_audio)
            stage = MusicGenerationStage(
                provider_a_music_provider=MagicMock(),
                provider_c_music_provider=MagicMock(),
                provider_b_music_provider=fake_provider_b,
            )

            (
                final_path,
                secondary_path,
                primary_track_lyrics,
                secondary_track_lyrics,
                vocal_id_used,
            ) = asyncio.run(
                stage.provider_b_music_generation_workflow(
                    prompt="cinematic pop",
                    lyrics_prompt="base lyric prompt",
                    video_metadata=video_metadata,
                )
            )

            self.assertIsNone(secondary_path)
            # Delivered = the chopped extension, still covering the video.
            self.assertEqual(final_path.name, "provider_b_short_extend1_trimmed_tail6s.wav")
            self.assertAlmostEqual(
                MusicGenerationStage._duration_seconds(final_path), 10.0, delta=0.3
            )
            self.assertEqual(
                fake_provider_b.extend_calls,
                [
                    (
                        "provider_b_short.wav",
                        "cinematic pop",
                        "base lyrics",
                        "provider_b_short_extend1.wav",
                        "tail",
                        # 4s seed is under the provider's 8s extend-at floor, so
                        # the extension point is left to the provider.
                        None,
                    )
                ],
            )
            self.assertEqual(
                [word.text for word in primary_track_lyrics.word_level_lyrics_timestamps],
                ["extended"],
            )
            self.assertEqual(
                [word.text for word in primary_track_lyrics.lyrics_timestamps],
                ["extended line"],
            )
            self.assertEqual(primary_track_lyrics.full_lyrics, "extended lyrics")
            self.assertIsNone(secondary_track_lyrics)
            self.assertIsNone(vocal_id_used)
            self.assertEqual(primary_track_lyrics.generation_api_call_count, 2)

    def test_provider_b_music_generation_uses_final_extension_lyrics_and_timestamps(self) -> None:
        class _FakeProviderBProvider:
            def __init__(self, short_path: Path) -> None:
                self.short_path = short_path
                self.extend_calls = []

            async def generate_with_variants_detailed(self, **_kwargs):
                self.short_path.with_suffix(".lyrics.txt").write_text("base lyrics", encoding="utf-8")
                return (
                    self.short_path,
                    None,
                    ProviderBTimestampedLyrics(
                        word_level=[WordTS(text="base", startS=0.0, endS=1.0, i=0)],
                        line_level=[WordTS(text="base line", startS=0.0, endS=1.0, i=0)],
                    ),
                    None,
                )

            async def extend_song_from_audio_detailed(
                self,
                *,
                audio_path: Path,
                prompt: str,
                lyrics: str,
                output_path: Path,
                **kwargs,
            ):
                round_idx = len(self.extend_calls)
                self.extend_calls.append(
                    (
                        audio_path.name,
                        prompt,
                        lyrics,
                        output_path.name,
                        kwargs.get("extend_type"),
                        kwargs.get("extend_at_ms"),
                    )
                )
                if round_idx == 0:
                    # 12s raw -> 6s chopped: still short of the 10s video, so a
                    # second round runs.
                    _write_sine_wav(output_path, duration=12.0)
                    return output_path, ProviderBTimestampedLyrics(
                        word_level=[WordTS(text="mid", startS=0.0, endS=5.8, i=0)],
                        line_level=[WordTS(text="mid line", startS=0.0, endS=5.8, i=0)],
                    ), "mid lyrics"
                # 16s raw -> 10s chopped: covers the video, loop exits.
                _write_sine_wav(output_path, duration=16.0)
                return output_path, ProviderBTimestampedLyrics(
                    word_level=[WordTS(text="final", startS=0.0, endS=9.8, i=0)],
                    line_level=[WordTS(text="final line", startS=0.0, endS=9.8, i=0)],
                ), "final lyrics"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            video_metadata.duration = 10.0
            short_audio = tmp_dir / "provider_b_short.wav"
            _write_sine_wav(short_audio, duration=4.0)

            fake_provider_b = _FakeProviderBProvider(short_audio)
            stage = MusicGenerationStage(
                provider_a_music_provider=MagicMock(),
                provider_c_music_provider=MagicMock(),
                provider_b_music_provider=fake_provider_b,
            )

            (
                final_path,
                _secondary_path,
                primary_track_lyrics,
                _secondary_track_lyrics,
                _vocal_id_used,
            ) = asyncio.run(
                stage.provider_b_music_generation_workflow(
                    prompt="cinematic pop",
                    lyrics_prompt="base lyric prompt",
                    video_metadata=video_metadata,
                )
            )

            # Every round re-seeds from the CHOPPED previous file, so the tag
            # is never at the extension seam, and the delivered final is the
            # chopped last round still covering the video.
            self.assertEqual(
                final_path.name,
                "provider_b_short_extend1_trimmed_tail6s_extend2_trimmed_tail6s.wav",
            )
            self.assertAlmostEqual(
                MusicGenerationStage._duration_seconds(final_path), 10.0, delta=0.3
            )
            self.assertEqual(
                fake_provider_b.extend_calls,
                [
                    (
                        "provider_b_short.wav",
                        "cinematic pop",
                        "base lyrics",
                        "provider_b_short_extend1.wav",
                        "tail",
                        None,
                    ),
                    (
                        "provider_b_short_extend1_trimmed_tail6s.wav",
                        "cinematic pop",
                        "mid lyrics",
                        "provider_b_short_extend1_trimmed_tail6s_extend2.wav",
                        "tail",
                        None,
                    ),
                ],
            )
            self.assertEqual(primary_track_lyrics.full_lyrics, "final lyrics")
            self.assertEqual(
                [word.text for word in primary_track_lyrics.word_level_lyrics_timestamps],
                ["final"],
            )
            self.assertEqual(
                [word.text for word in primary_track_lyrics.lyrics_timestamps],
                ["final line"],
            )

    def test_provider_b_music_generation_fails_when_extensions_still_do_not_cover_video(self) -> None:
        class _FakeProviderBProvider:
            def __init__(self, short_path: Path) -> None:
                self.short_path = short_path
                self.extend_calls = []

            async def generate_with_variants_detailed(self, **_kwargs):
                self.short_path.with_suffix(".lyrics.txt").write_text("base lyrics", encoding="utf-8")
                return (
                    self.short_path,
                    None,
                    ProviderBTimestampedLyrics(
                        word_level=[WordTS(text="base", startS=0.0, endS=1.0, i=0)],
                        line_level=[WordTS(text="base line", startS=0.0, endS=1.0, i=0)],
                    ),
                    None,
                )

            async def extend_song_from_audio_detailed(self, *, output_path: Path, **kwargs):
                self.extend_calls.append(kwargs)
                _write_sine_wav(output_path, duration=5.0)
                return output_path, ProviderBTimestampedLyrics(
                    word_level=[WordTS(text="short", startS=0.0, endS=4.8, i=0)],
                    line_level=[WordTS(text="short line", startS=0.0, endS=4.8, i=0)],
                ), "short lyrics"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            video_metadata.duration = 10.0
            short_audio = tmp_dir / "provider_b_short.wav"
            _write_sine_wav(short_audio, duration=4.0)

            fake_provider_b = _FakeProviderBProvider(short_audio)
            stage = MusicGenerationStage(
                provider_a_music_provider=MagicMock(),
                provider_c_music_provider=MagicMock(),
                provider_b_music_provider=fake_provider_b,
            )
            stage.max_extension_rounds = 2

            with self.assertRaises(EdennProviderResponseError) as ctx:
                asyncio.run(
                    stage.provider_b_music_generation_workflow(
                        prompt="cinematic pop",
                        lyrics_prompt="base lyric prompt",
                        video_metadata=video_metadata,
                    )
                )

            self.assertEqual(ctx.exception.provider_name, "provider_b")
            self.assertEqual(
                ctx.exception.operation,
                "extend_provider_b_track_to_video_duration",
            )
            self.assertEqual(ctx.exception.context["attempted_extension_rounds"], 2)
            self.assertEqual(len(fake_provider_b.extend_calls), 2)

    def test_provider_b_music_generation_workflow_clones_and_forwards_vocal_id(self) -> None:
        class _FakeProviderBProvider:
            def __init__(self) -> None:
                self.clone_calls = []
                self.generate_calls = []

            def begin_warning_collection(self) -> object:
                return object()

            def finish_warning_collection(self, _token: object) -> None:
                return None

            async def clone_vocal(self, audio_path: Path) -> str:
                self.clone_calls.append(audio_path)
                return "vocal_654"

            async def generate_with_variants_detailed(self, **kwargs):
                self.generate_calls.append(kwargs)
                short_audio.with_suffix(".lyrics.txt").write_text("base lyrics", encoding="utf-8")
                return (
                    short_audio,
                    None,
                    ProviderBTimestampedLyrics(
                        word_level=[WordTS(text="orig", startS=0.0, endS=1.0, i=0)],
                        line_level=[WordTS(text="orig line", startS=0.0, endS=1.0, i=0)],
                    ),
                    None,
                )

            async def extend_song_from_audio_detailed(self, **_kwargs):
                return short_audio, ProviderBTimestampedLyrics(
                    word_level=[WordTS(text="orig", startS=0.0, endS=1.0, i=0)],
                    line_level=[WordTS(text="orig line", startS=0.0, endS=1.0, i=0)],
                ), "base lyrics"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            video_metadata.duration = 1.0
            short_audio = tmp_dir / "provider_b_short.wav"
            vocal_sample = tmp_dir / "voice.m4a"
            _write_sine_wav(short_audio, duration=3.0)
            vocal_sample.write_bytes(b"fake")

            fake_provider_b = _FakeProviderBProvider()
            stage = MusicGenerationStage(
                provider_a_music_provider=MagicMock(),
                provider_c_music_provider=MagicMock(),
                provider_b_music_provider=fake_provider_b,
            )

            result = asyncio.run(
                stage.provider_b_music_generation_workflow(
                    prompt="cinematic pop",
                    lyrics_prompt="base lyric prompt",
                    video_metadata=video_metadata,
                    vocal_sample_path=vocal_sample,
                )
            )

            self.assertEqual(fake_provider_b.clone_calls, [vocal_sample])
            self.assertEqual(fake_provider_b.generate_calls[0]["vocal_id"], "vocal_654")
            self.assertEqual(result[4], "vocal_654")

    def test_edenn_enhanced_run_preserves_line_level_output_and_exposes_word_level_output(self) -> None:
        class _FakeProviderBProvider:
            def begin_warning_collection(self) -> object:
                return object()

            def finish_warning_collection(self, _token: object) -> None:
                return None

            async def generate_with_variants_detailed(self, **_kwargs):
                audio_path = Path(_kwargs["output_path"]) if _kwargs.get("output_path") else short_audio
                short_audio.with_suffix(".lyrics.txt").write_text("base lyrics", encoding="utf-8")
                return (
                    short_audio,
                    None,
                    ProviderBTimestampedLyrics(
                        word_level=[
                            WordTS(text="Move", startS=0.6, endS=0.9, i=0),
                            WordTS(text="with", startS=0.9, endS=1.2, i=1),
                            WordTS(text="me", startS=1.2, endS=1.4, i=2),
                            WordTS(text="tonight", startS=1.4, endS=1.8, i=3),
                        ],
                        line_level=[
                            WordTS(text="Move with me tonight", startS=0.6, endS=1.8, i=0),
                        ],
                    ),
                    None,
                )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            video_metadata.duration = 2.0
            short_audio = tmp_dir / "provider_b_short.wav"
            _write_sine_wav(short_audio, duration=3.0)

            matched_audio = tmp_dir / "matched.wav"
            _write_sine_wav(matched_audio, duration=2.0)

            fake_provider_b = _FakeProviderBProvider()
            stage = MusicGenerationStage(
                provider_a_music_provider=MagicMock(),
                provider_c_music_provider=MagicMock(),
                provider_b_music_provider=fake_provider_b,
            )

            captured_rerank_input = None

            async def _fake_rerank_run(stage_input):
                nonlocal captured_rerank_input
                captured_rerank_input = stage_input
                return MusicMatchingStageOutput(
                    reranked_music_outputs_path=matched_audio,
                    music_start_s=1.0,
                    aligned_lyrics=[WordTS(text="Move with me tonight", startS=0.0, endS=0.8, i=0)],
                    used_track="primary",
                )

            with patch.object(
                stage.music_reranker_stage,
                "run",
                new=AsyncMock(side_effect=_fake_rerank_run),
            ):
                output = asyncio.run(
                    stage.run(
                        MusicGenerationStageInput(
                            prompt_metadata={
                                "style_prompt": "cinematic pop",
                                "lyrics_prompt": "Move with me tonight",
                            },
                            video_metadata=video_metadata,
                            include_vocals=True,
                            vocal_gender="female",
                            music_generation_model=MusicGenertionModelEnum.EDENN_ENHANCED,
                        )
                    )
                )

            self.assertEqual(output.music_path, matched_audio)
            self.assertIsNotNone(captured_rerank_input)
            self.assertEqual(
                [word.text for word in captured_rerank_input.timestamp_lyrics],
                ["Move", "with", "me", "tonight"],
            )
            self.assertEqual(
                [(word.text, word.startS, word.endS) for word in output.lyrics_timestamps],
                [("with me tonight", 0.0, 800.0)],
            )
            self.assertEqual(
                [
                    (word.text, round(word.startS, 3), round(word.endS, 3))
                    for word in output.word_level_lyrics_timestamps
                ],
                [("with", 0.0, 200.0), ("me", 200.0, 400.0), ("tonight", 400.0, 800.0)],
            )
            self.assertEqual(output.primary_full_lyrics, "base lyrics")
            self.assertIsNone(output.secondary_full_lyrics)
            self.assertEqual(
                [(word.text, word.startS, word.endS) for word in output.primary_full_lyrics_timestamps],
                [("Move with me tonight", 600.0, 1800.0)],
            )
            self.assertEqual(
                [
                    (word.text, word.startS, word.endS)
                    for word in output.primary_full_word_level_lyrics_timestamps
                ],
                [
                    ("Move", 600.0, 900.0),
                    ("with", 900.0, 1200.0),
                    ("me", 1200.0, 1400.0),
                    ("tonight", 1400.0, 1800.0),
                ],
            )
            self.assertEqual(output.secondary_full_lyrics_timestamps, [])
            self.assertEqual(output.secondary_full_word_level_lyrics_timestamps, [])
            self.assertEqual(output.matching_used_track, "primary")

    def test_edenn_enhanced_run_trims_full_track_before_matching_and_clips_full_timestamps(self) -> None:
        class _FakeProviderBProvider:
            def begin_warning_collection(self) -> object:
                return object()

            def finish_warning_collection(self, _token: object) -> None:
                return None

            async def generate_with_variants_detailed(self, **_kwargs):
                full_audio.with_suffix(".lyrics.txt").write_text("base lyrics", encoding="utf-8")
                return (
                    full_audio,
                    None,
                    ProviderBTimestampedLyrics(
                        word_level=[
                            WordTS(text="keep", startS=12.0, endS=13.0, i=0),
                            WordTS(text="clip", startS=13.5, endS=16.0, i=1),
                            WordTS(text="drop", startS=15.0, endS=16.5, i=2),
                        ],
                        line_level=[
                            WordTS(text="keep clip drop", startS=12.0, endS=16.5, i=0),
                        ],
                    ),
                    None,
                )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            video_metadata.duration = 5.0
            full_audio = tmp_dir / "provider_b_full.wav"
            matched_audio = tmp_dir / "matched.wav"
            _write_sine_wav(full_audio, duration=20.0)
            _write_sine_wav(matched_audio, duration=5.0)

            fake_provider_b = _FakeProviderBProvider()
            stage = MusicGenerationStage(
                provider_a_music_provider=MagicMock(),
                provider_c_music_provider=MagicMock(),
                provider_b_music_provider=fake_provider_b,
            )

            captured_rerank_input = None

            async def _fake_rerank_run(stage_input):
                nonlocal captured_rerank_input
                captured_rerank_input = stage_input
                return MusicMatchingStageOutput(
                    reranked_music_outputs_path=matched_audio,
                    music_start_s=0.0,
                    aligned_lyrics=[],
                    used_track="primary",
                )

            with patch.object(
                stage.music_reranker_stage,
                "run",
                new=AsyncMock(side_effect=_fake_rerank_run),
            ):
                output = asyncio.run(
                    stage.run(
                        MusicGenerationStageInput(
                            prompt_metadata={
                                "style_prompt": "cinematic pop",
                                "lyrics_prompt": "Move with me tonight",
                            },
                            video_metadata=video_metadata,
                            include_vocals=True,
                            vocal_gender="female",
                            music_generation_model=MusicGenertionModelEnum.EDENN_ENHANCED,
                        )
                    )
                )

            self.assertIsNotNone(captured_rerank_input)
            self.assertIn("_trimmed_tail6s", captured_rerank_input.local_music_path.stem)
            self.assertEqual(output.complete_music_path, captured_rerank_input.local_music_path)
            self.assertAlmostEqual(
                MusicGenerationStage._duration_seconds(output.complete_music_path),
                14.0,
                delta=0.35,
            )
            self.assertEqual(
                [
                    (word.text, word.startS, word.endS)
                    for word in captured_rerank_input.timestamp_lyrics
                ],
                [
                    ("keep", 12.0, 13.0),
                    ("clip", 13.5, 14.0),
                ],
            )
            self.assertEqual(
                [
                    (word.text, word.startS, word.endS)
                    for word in output.primary_full_word_level_lyrics_timestamps
                ],
                [
                    ("keep", 12000.0, 13000.0),
                    ("clip", 13500.0, 14000.0),
                ],
            )
            self.assertEqual(
                [
                    (word.text, word.startS, word.endS)
                    for word in output.primary_full_lyrics_timestamps
                ],
                [("keep clip drop", 12000.0, 14000.0)],
            )

    def test_edenn_enhanced_instrumental_run_trims_full_track_before_returning_complete_path(self) -> None:
        class _FakeProviderBProvider:
            def __init__(self, audio_path: Path) -> None:
                self.audio_path = audio_path

            def begin_warning_collection(self):
                return object()

            def finish_warning_collection(self, _token):
                return None

            async def generate_instrumental_task(self, **_kwargs):
                return SimpleNamespace(task_id="task_inst")

            async def wait_instrumental_task(self, task_id: str, **_kwargs):
                return SimpleNamespace(
                    task_id=task_id,
                    raw={"audio_url": "https://example/instrumental.wav"},
                )

            async def download_audio(self, _url: str, path: Path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self.audio_path.read_bytes())
                return path

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            video_metadata.duration = 5.0
            primary_audio = tmp_dir / "primary.wav"
            matched_audio = tmp_dir / "matched.wav"
            _write_sine_wav(primary_audio, duration=20.0)
            _write_sine_wav(matched_audio, duration=5.0)

            stage = MusicGenerationStage(
                provider_a_music_provider=MagicMock(),
                provider_c_music_provider=MagicMock(),
                provider_b_music_provider=_FakeProviderBProvider(primary_audio),
            )

            with patch.object(
                stage.music_reranker_stage,
                "run",
                new=AsyncMock(return_value=MusicMatchingStageOutput(
                    reranked_music_outputs_path=matched_audio,
                    music_start_s=0.0,
                    aligned_lyrics=[],
                    used_track="primary",
                )),
            ):
                output = asyncio.run(
                    stage.run(
                        MusicGenerationStageInput(
                            prompt_metadata={"style_prompt": "cinematic instrumental score"},
                            video_metadata=video_metadata,
                            include_vocals=False,
                            vocal_gender="",
                            music_generation_model=MusicGenertionModelEnum.EDENN_ENHANCED,
                        )
                    )
                )

            self.assertIsNotNone(output.complete_music_path)
            self.assertIn("_trimmed_tail6s", output.complete_music_path.stem)
            self.assertAlmostEqual(
                MusicGenerationStage._duration_seconds(output.complete_music_path),
                14.0,
                delta=0.35,
            )

    def test_provider_b_music_generation_workflow_requests_single_variant(self) -> None:
        class _FakeProviderBProvider:
            def __init__(self) -> None:
                self.generate_calls: list[dict] = []

            def begin_warning_collection(self) -> object:
                return object()

            def finish_warning_collection(self, _token: object) -> None:
                return None

            async def generate_with_variants_detailed(self, **kwargs):
                self.generate_calls.append(kwargs)
                audio_path.with_suffix(".lyrics.txt").write_text("lyrics", encoding="utf-8")
                return (
                    audio_path,
                    None,
                    ProviderBTimestampedLyrics(
                        word_level=[WordTS(text="hello", startS=0.0, endS=1.0, i=0)],
                        line_level=[WordTS(text="hello", startS=0.0, endS=1.0, i=0)],
                    ),
                    None,
                )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            video_metadata.duration = 2.0
            audio_path = tmp_dir / "provider_b.wav"
            _write_sine_wav(audio_path, duration=3.0)

            fake_provider_b = _FakeProviderBProvider()
            stage = MusicGenerationStage(
                provider_a_music_provider=MagicMock(),
                provider_c_music_provider=MagicMock(),
                provider_b_music_provider=fake_provider_b,
            )

            asyncio.run(
                stage.provider_b_music_generation_workflow(
                    prompt="chill pop",
                    lyrics_prompt="some lyrics",
                    video_metadata=video_metadata,
                )
            )

            self.assertEqual(len(fake_provider_b.generate_calls), 1)
            self.assertEqual(fake_provider_b.generate_calls[0]["n"], 1)

    def test_edenn_studio_run_sets_secondary_fields_none_and_matching_primary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            video_metadata.duration = 2.0
            primary_audio = tmp_dir / "primary.wav"
            matched_audio = tmp_dir / "matched.wav"
            _write_sine_wav(primary_audio, duration=3.0)
            _write_sine_wav(matched_audio, duration=2.0)

            stage = MusicGenerationStage(
                provider_a_music_provider=MagicMock(),
                provider_c_music_provider=MagicMock(),
                provider_b_music_provider=MagicMock(),
            )

            async def _fake_provider_c_workflow(*_args, **_kwargs):
                return (
                    primary_audio,
                    None,
                    FullTrackLyricsData(
                        full_lyrics="test lyrics",
                        lyrics_timestamps=[WordTS(text="test", startS=0.0, endS=1.0, i=0)],
                        word_level_lyrics_timestamps=[WordTS(text="test", startS=0.0, endS=1.0, i=0)],
                    ),
                    None,
                )

            with patch.object(stage, "provider_c_music_generation_workflow", side_effect=_fake_provider_c_workflow), \
                 patch.object(
                     stage.music_reranker_stage,
                     "run",
                     new=AsyncMock(return_value=MusicMatchingStageOutput(
                         reranked_music_outputs_path=matched_audio,
                         music_start_s=0.0,
                         aligned_lyrics=[WordTS(text="test", startS=0.0, endS=1.0, i=0)],
                         used_track="primary",
                     )),
                 ):
                output = asyncio.run(
                    stage.run(
                        MusicGenerationStageInput(
                            prompt_metadata={"prompt": "upbeat pop"},
                            video_metadata=video_metadata,
                            include_vocals=True,
                            vocal_gender="female",
                            music_generation_model=MusicGenertionModelEnum.EDENN_STUDIO,
                        )
                    )
                )

            self.assertEqual(output.matching_used_track, "primary")
            self.assertIsNone(output.secondary_complete_music_path)
            self.assertIsNone(output.secondary_full_lyrics)
            self.assertEqual(output.secondary_full_lyrics_timestamps, [])
            self.assertEqual(output.secondary_full_word_level_lyrics_timestamps, [])

    def test_watermark_applies_to_complete_track_after_matching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            video_metadata.duration = 2.0
            primary_audio = tmp_dir / "primary.wav"
            matched_audio = tmp_dir / "matched.wav"
            watermarked_audio = tmp_dir / "primary_watermarked.wav"
            _write_sine_wav(primary_audio, duration=3.0)
            _write_sine_wav(matched_audio, duration=2.0)
            _write_sine_wav(watermarked_audio, duration=3.4)

            stage = MusicGenerationStage(
                provider_a_music_provider=MagicMock(),
                provider_c_music_provider=MagicMock(),
                provider_b_music_provider=MagicMock(),
            )

            async def _fake_provider_c_workflow(*_args, **_kwargs):
                return (
                    primary_audio,
                    None,
                    FullTrackLyricsData(
                        full_lyrics=None,
                        lyrics_timestamps=[],
                        word_level_lyrics_timestamps=[],
                    ),
                    None,
                )

            with patch.object(stage, "provider_c_music_generation_workflow", side_effect=_fake_provider_c_workflow), \
                 patch.object(
                     stage.music_reranker_stage,
                     "run",
                     new=AsyncMock(return_value=MusicMatchingStageOutput(
                         reranked_music_outputs_path=matched_audio,
                         music_start_s=0.0,
                         aligned_lyrics=[],
                         used_track="primary",
                     )),
                 ), patch(
                     "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicGenerationStage.music_generation_stage.append_voice_watermark_to_audio",
                     return_value=watermarked_audio,
                 ) as watermark:
                output = asyncio.run(
                    stage.run(
                        MusicGenerationStageInput(
                            prompt_metadata={"prompt": "upbeat pop"},
                            video_metadata=video_metadata,
                            include_vocals=False,
                            vocal_gender="",
                            music_generation_model=MusicGenertionModelEnum.EDENN_STUDIO,
                            water_mark=True,
                        )
                    )
                )

            self.assertEqual(output.music_path, matched_audio)
            self.assertEqual(output.complete_music_path, watermarked_audio)
            watermark.assert_called_once_with(primary_audio)

    def _run_short_full_track_stage(self, tmp_dir: Path, *, water_mark: bool):
        """Drive the stage with a full track shorter than the video.

        That is the shape that let job_1e3ed627's provider tail reach the
        customer: the generation-time trim stands down to keep the track long
        enough for the video, so the delivered full track kept the tag.
        """

        video_metadata = _fake_video_metadata(tmp_dir)
        video_metadata.duration = 60.0
        primary_audio = tmp_dir / "instrumental.wav"
        matched_audio = tmp_dir / "matched.wav"
        _write_sine_wav(primary_audio, duration=20.0)
        _write_sine_wav(matched_audio, duration=20.0)

        stage = MusicGenerationStage(
            provider_a_music_provider=MagicMock(),
            provider_c_music_provider=MagicMock(),
            provider_b_music_provider=MagicMock(),
        )

        # The ENHANCED provider is the one that closes tracks with its own tag,
        # so it is the one whose short track must still lose its tail.
        async def _fake_provider_b_workflow(*_args, **_kwargs):
            return (
                primary_audio,
                None,
                FullTrackLyricsData(
                    full_lyrics="la la",
                    lyrics_timestamps=[WordTS(text="la", startS=2.0, endS=3.0, i=0)],
                    word_level_lyrics_timestamps=[
                        WordTS(text="la", startS=2.0, endS=3.0, i=0),
                        WordTS(text="tag", startS=16.0, endS=17.0, i=1),
                    ],
                ),
                None,
                None,
            )

        aligned_inputs: list[Path] = []

        async def _fake_matching_run(matching_input):
            aligned_inputs.append(Path(matching_input.local_music_path))
            return MusicMatchingStageOutput(
                reranked_music_outputs_path=matched_audio,
                music_start_s=0.0,
                aligned_lyrics=[],
                used_track="primary",
            )

        with patch.object(stage, "provider_b_music_generation_workflow", side_effect=_fake_provider_b_workflow), \
             patch.object(stage.music_reranker_stage, "run", new=_fake_matching_run):
            output = asyncio.run(
                stage.run(
                    MusicGenerationStageInput(
                        prompt_metadata={"prompt": "upbeat pop"},
                        video_metadata=video_metadata,
                        include_vocals=True,
                        vocal_gender="f",
                        music_generation_model=MusicGenertionModelEnum.EDENN_ENHANCED,
                        water_mark=water_mark,
                    )
                )
            )
        return stage, output, primary_audio, matched_audio, aligned_inputs

    def test_instrumental_generation_asks_for_the_longer_model(self) -> None:
        """The model version is the only lever on instrumental length.

        Measured on one prompt, n=1: provider_b-8 returned 62s and ignored an
        explicit five-minute request; provider_b-9 returned 148s from the same
        words. Four production jobs shipped with 23-51% of the video unscored
        because the short model was pinned here, so the choice is pinned by a
        test and overridable from configuration.
        """

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            video_metadata.duration = 140.0
            generated = tmp_dir / "instrumental.wav"
            _write_sine_wav(generated, duration=150.0)

            calls = {}

            class _FakeTask:
                task_id = "task-1"

            async def _generate_instrumental_task(*, prompt, model, n):
                calls["model"] = model
                calls["n"] = n
                return _FakeTask()

            async def _wait_instrumental_task(task_id, **_kw):
                return SimpleNamespace(task_id=task_id, raw={"choices": [{"url": "https://x/a.wav"}]})

            async def _download_audio(url, dest):
                shutil.copyfile(generated, dest)
                return Path(dest)

            provider = MagicMock()
            provider.generate_instrumental_task = _generate_instrumental_task
            provider.wait_instrumental_task = _wait_instrumental_task
            provider.download_audio = _download_audio
            del provider._run_with_cycle_failover

            stage = MusicGenerationStage(
                provider_a_music_provider=MagicMock(),
                provider_c_music_provider=MagicMock(),
                provider_b_music_provider=provider,
            )
            with patch(
                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicGenerationStage."
                "music_generation_stage.extract_audio_url",
                return_value="https://x/a.wav",
            ):
                asyncio.run(
                    stage.provider_b_instrumental_generation_workflow(
                        "playful marimba", video_metadata=video_metadata,
                    )
                )

            self.assertEqual(calls["model"], "provider_b-9")
            self.assertEqual(calls["n"], 1)

    def test_only_a_tagging_provider_loses_its_tail(self) -> None:
        """The chop follows the provider, not the tier's price.

        Verified against production audio 2026-08-23: enhanced tracks close with
        a 1.95s tag after a 3-4s silence gap; studio and basic tracks fade out
        musically with no isolated trailing segment anywhere in the file. So the
        chop must fire for enhanced and stand down for the others — a chop on an
        untagged provider is six seconds of the customer's music, and prod had
        already lost that on 39 studio jobs before this was measured.
        """

        stage = MusicGenerationStage(
            provider_a_music_provider=MagicMock(),
            provider_c_music_provider=MagicMock(),
            provider_b_music_provider=MagicMock(),
        )
        self.assertTrue(provider_appends_track_tail(MusicGenertionModelEnum.EDENN_ENHANCED))
        self.assertFalse(provider_appends_track_tail(MusicGenertionModelEnum.EDENN_STUDIO))
        self.assertFalse(provider_appends_track_tail(MusicGenertionModelEnum.EDENN_BASIC))
        # An unknown tier keeps its audio: a missed chop is a tag we will hear
        # about, a wrong chop is music we silently deleted.
        self.assertFalse(provider_appends_track_tail("edenn_something_new"))
        self.assertFalse(provider_appends_track_tail(None))

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            for spec, expect_cut in (
                (MusicGenertionModelEnum.EDENN_ENHANCED, True),
                (MusicGenertionModelEnum.EDENN_STUDIO, False),
                (MusicGenertionModelEnum.EDENN_BASIC, False),
            ):
                track = tmp_dir / f"{spec}_track.wav"
                _write_sine_wav(track, duration=20.0)

                aligned, aligned_dur = stage.clean_track_for_alignment(track, model_spec=spec)
                with patch(
                    "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicGenerationStage."
                    "music_generation_stage.append_voice_watermark_to_audio",
                    side_effect=lambda path: path,
                ) as watermark:
                    delivered, delivered_dur = stage.prepare_full_track_for_delivery(
                        track, model_spec=spec, water_mark=True
                    )

                with self.subTest(spec=spec):
                    if expect_cut:
                        self.assertNotEqual(aligned, track)
                        self.assertAlmostEqual(aligned_dur, 14.0, delta=0.3)
                        self.assertAlmostEqual(delivered_dur, 14.0, delta=0.3)
                        self.assertIn("_trimmed_tail6s", delivered.stem)
                    else:
                        self.assertEqual(aligned, track)
                        self.assertIsNone(aligned_dur)
                        self.assertIsNone(delivered_dur)
                        self.assertNotIn("_trimmed_tail", delivered.stem)
                        self.assertAlmostEqual(
                            stage._duration_seconds(delivered), 20.0, delta=0.3
                        )
                    # Every tier still gets our watermark on its full track.
                    watermark.assert_called_once()

    def test_short_full_track_loses_provider_tail_before_the_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            watermarked_audio = tmp_dir / "delivered_watermarked.wav"
            _write_sine_wav(watermarked_audio, duration=0.5)
            with patch(
                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicGenerationStage."
                "music_generation_stage.append_voice_watermark_to_audio",
                return_value=watermarked_audio,
            ) as watermark:
                stage, output, primary_audio, matched_audio, aligned_inputs = (
                    self._run_short_full_track_stage(tmp_dir, water_mark=True)
                )

            self.assertEqual(output.complete_music_path, watermarked_audio)
            watermark.assert_called_once()
            trimmed_path = watermark.call_args.args[0]
            self.assertNotEqual(trimmed_path, primary_audio)
            self.assertIn("_trimmed_tail6s", trimmed_path.stem)
            self.assertAlmostEqual(stage._duration_seconds(trimmed_path), 14.0, delta=0.3)

            # Alignment saw the clean track, so the window it picked — and the
            # audio the video plays — cannot contain the provider's tail.
            self.assertEqual(output.music_path, matched_audio)
            self.assertEqual(len(aligned_inputs), 1)
            aligned_input = aligned_inputs[0]
            self.assertNotEqual(aligned_input, primary_audio)
            self.assertIn("_trimmed_tail6s", aligned_input.stem)
            self.assertAlmostEqual(
                stage._duration_seconds(aligned_input), 14.0, delta=0.3
            )

            # Full-track timings follow the audio that ships — a word at 16s has
            # no track left to land on.
            word_starts_ms = [
                word.startS for word in output.primary_full_word_level_lyrics_timestamps
            ]
            self.assertEqual(word_starts_ms, [2000.0])

    def test_extended_track_is_not_mistaken_for_an_already_trimmed_one(self) -> None:
        """A pre-extension trim leaves its marker mid-stem, not at the end.

        ProviderB names an extension `{stem}_extend{n}`, so `song_trimmed_tail6s`
        becomes `song_trimmed_tail6s_extend1` — carrying the marker while its
        actual tail is whatever the provider just appended. Treating that as
        already-trimmed would concatenate our watermark onto the vendor tag.
        """

        stage = MusicGenerationStage(
            provider_a_music_provider=MagicMock(),
            provider_c_music_provider=MagicMock(),
            provider_b_music_provider=MagicMock(),
        )
        self.assertTrue(stage._is_tail_trimmed(Path("song_trimmed_tail6s.mp3")))
        self.assertFalse(stage._is_tail_trimmed(Path("song_trimmed_tail6s_extend1.mp3")))
        self.assertTrue(
            stage._is_tail_trimmed(Path("song_trimmed_tail6s_extend1_trimmed_tail6s.mp3"))
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            extended = tmp_dir / "song_trimmed_tail6s_extend1.wav"
            _write_sine_wav(extended, duration=20.0)
            with patch(
                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicGenerationStage."
                "music_generation_stage.append_voice_watermark_to_audio",
                side_effect=lambda path: path,
            ) as watermark:
                delivered, trimmed_s = stage.prepare_full_track_for_delivery(
                    extended,
                    model_spec=MusicGenertionModelEnum.EDENN_ENHANCED,
                    water_mark=True,
                )

            self.assertNotEqual(delivered, extended)
            self.assertTrue(delivered.stem.endswith("_trimmed_tail6s"))
            self.assertAlmostEqual(trimmed_s, 14.0, delta=0.3)
            watermark.assert_called_once_with(delivered)

    def test_provider_tail_is_cut_even_when_the_watermark_is_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            with patch(
                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicGenerationStage."
                "music_generation_stage.append_voice_watermark_to_audio",
            ) as watermark:
                stage, output, primary_audio, _, _ = self._run_short_full_track_stage(
                    tmp_dir, water_mark=False
                )

            watermark.assert_not_called()
            self.assertNotEqual(output.complete_music_path, primary_audio)
            self.assertIn("_trimmed_tail6s", output.complete_music_path.stem)
            self.assertAlmostEqual(
                stage._duration_seconds(output.complete_music_path), 14.0, delta=0.3
            )

    def test_edenn_basic_run_leaves_matching_used_track_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            video_metadata.duration = 2.0
            audio_path = tmp_dir / "basic.wav"
            _write_sine_wav(audio_path, duration=3.0)

            fake_eleven = MagicMock()
            fake_eleven.generate = AsyncMock(return_value=(
                audio_path,
                [WordTS(text="word", startS=0.0, endS=500.0, i=0)],
            ))
            stage = MusicGenerationStage(
                provider_a_music_provider=fake_eleven,
                provider_c_music_provider=MagicMock(),
                provider_b_music_provider=MagicMock(),
            )

            output = asyncio.run(
                stage.run(
                    MusicGenerationStageInput(
                        prompt_metadata={"prompt": "calm instrumental"},
                        video_metadata=video_metadata,
                        include_vocals=False,
                        vocal_gender="",
                        music_generation_model=MusicGenertionModelEnum.EDENN_BASIC,
                    )
                )
            )

            self.assertIsNone(output.matching_used_track)
            self.assertIsNone(output.secondary_complete_music_path)
            self.assertIsNone(output.secondary_full_lyrics)
            self.assertEqual(output.secondary_full_lyrics_timestamps, [])
            self.assertEqual(output.secondary_full_word_level_lyrics_timestamps, [])


class TemporaryBlobPathTests(unittest.TestCase):
    def test_scene_frame_upload_uses_ascii_safe_blob_name(self) -> None:
        storage = MagicMock()
        storage.upload_temporary_bytes.return_value = TemporaryBlobUpload(
            container="user-uploads",
            blob_name="llm-inputs/video-123456789abc/scene_000_last_deadbeef.jpg",
            sas_url="https://example.com/scene.jpg",
        )
        stage = SceneSegmentationStage(
            llm_model_client=MagicMock(),
            storage_service=storage,
            llm_image_container="user-uploads",
            llm_image_cleanup_delay_seconds=0,
        )

        uploaded = asyncio.run(
            stage._upload_scene_frame(
                video_path=Path("/tmp/海边日落晚霞映照.mp4"),
                scene_idx=0,
                frame_label="last",
                frame_bytes=b"jpeg-bytes",
            )
        )

        blob_name = storage.upload_temporary_bytes.call_args.kwargs["blob_name"]
        self.assertEqual(uploaded.sas_url, "https://example.com/scene.jpg")
        self.assertRegex(
            blob_name,
            r"^llm-inputs/video-[0-9a-f]{12}/scene_000_last_[0-9a-f]{32}\.jpg$",
        )
        self.assertNotIn("海边", blob_name)

    def test_generate_sas_url_percent_encodes_blob_path(self) -> None:
        service = object.__new__(AzureBlobStorageService)
        service.settings = SimpleNamespace(
            storage_account_url="https://primary.storage.example.invalid",
            storage_account_name="primarystorage",
            sas_ttl_minutes=5,
        )
        service.enabled = True
        service._client = None
        service._sas_key = "secret-key"

        with patch(
            "EdennCode.Deployment.storage.generate_blob_sas",
            return_value="REDACTED_SAS",
        ):
            url = service.generate_sas_url(
                container="user-uploads",
                blob_name="llm-inputs/海边日落晚霞映照/scene_000_last_deadbeef.jpg",
            )

        self.assertIsNotNone(url)
        self.assertIn(
            "llm-inputs/%E6%B5%B7%E8%BE%B9%E6%97%A5%E8%90%BD%E6%99%9A%E9%9C%9E%E6%98%A0%E7%85%A7/",
            url,
        )
        self.assertNotIn("海边日落晚霞映照", url)
        self.assertTrue(url.endswith("?REDACTED_SAS"))


class SceneSegmentationWindowTests(unittest.TestCase):
    def test_default_scene_llm_concurrency_is_ten(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            stage = SceneSegmentationStage(
                llm_model_client=MagicMock(),
                llm_image_cleanup_delay_seconds=0,
            )

        self.assertEqual(stage.max_concurrent_llm_calls, 10)

    def test_scene_detection_sweeps_threshold_before_downsampling(self) -> None:
        stage = SceneSegmentationStage(
            llm_model_client=MagicMock(),
            scene_threshold=0.3,
            max_scenes=8,
            threshold_sweep_steps=4,
            llm_image_cleanup_delay_seconds=0,
        )
        observed_thresholds = []

        def _fake_detect_scene_cuts(
            _video_path,
            scene_threshold,
            *,
            detector,
            pyscene_method,
            pyscene_adaptive_threshold,
            pyscene_content_threshold,
            min_scene_len_s,
        ):
            observed_thresholds.append(pyscene_adaptive_threshold)
            if scene_threshold < 0.6:
                return [0.0] + [float(second) for second in range(1, 35)]
            return [0.0, 5.0, 10.0, 15.0, 20.0, 25.0]

        with patch(
            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.scene_segmentation_stage.detect_scene_cuts",
            side_effect=_fake_detect_scene_cuts,
        ):
            windows = stage._get_scene_windows(
                SMOKE_VIDEO_PATH,
                duration=40.0,
                threshold=stage.scene_threshold,
                min_length=stage.min_scene_length,
                max_scenes=stage.max_scenes,
                detector="pyscenedetect",
                method="adaptive",
                threshold_sweep_steps=stage.threshold_sweep_steps,
            )

        self.assertEqual([round(value, 3) for value in observed_thresholds], [0.3, 0.6])
        self.assertEqual(
            windows,
            [
                (0.0, 5.0),
                (5.0, 10.0),
                (10.0, 15.0),
                (15.0, 20.0),
                (20.0, 25.0),
                (25.0, 40.0),
            ],
        )


class SceneFrameFallbackTests(unittest.TestCase):
    def test_extract_frame_retries_floor_second_timestamp(self) -> None:
        class _FakeBuffer:
            @staticmethod
            def tobytes() -> bytes:
                return b"jpg-bytes"

        class _FakeCapture:
            def __init__(self) -> None:
                self.last_pos_msec = 0.0
                self.pos_msec_calls = []

            def isOpened(self) -> bool:
                return True

            def get(self, prop: int) -> float:
                if prop == 5:  # cv2.CAP_PROP_FPS
                    return 30.0
                if prop == 7:  # cv2.CAP_PROP_FRAME_COUNT
                    return 600.0
                return 0.0

            def set(self, prop: int, value: float) -> bool:
                if prop == 0:  # cv2.CAP_PROP_POS_MSEC
                    self.last_pos_msec = value
                    self.pos_msec_calls.append(value)
                return True

            def read(self):
                if abs(self.last_pos_msec - 18_000.0) < 0.1:
                    return True, MagicMock()
                return False, None

            def release(self) -> None:
                return None

        fake_capture = _FakeCapture()
        with patch(
            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.scene_segmentation_stage.cv2.VideoCapture",
            return_value=fake_capture,
        ), patch(
            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.scene_segmentation_stage.cv2.imencode",
            return_value=(True, _FakeBuffer()),
        ):
            output = extract_frame_jpeg_bytes(
                SMOKE_VIDEO_PATH,
                18.535,
                duration_hint=20.0,
            )

        self.assertEqual(output, b"jpg-bytes")
        self.assertIn(18_535.0, fake_capture.pos_msec_calls)
        self.assertIn(18_000.0, fake_capture.pos_msec_calls)

    def test_analyze_single_scene_uses_one_frame_when_other_fails(self) -> None:
        stage = SceneSegmentationStage(
            llm_model_client=MagicMock(),
            llm_image_cleanup_delay_seconds=0,
        )
        stage.llm_model_client.complete_messages = AsyncMock(
            return_value=(
                {
                    "visual_summary": "beach crowd",
                    "key_actions": "walks and waves",
                    "mood": "joyful",
                },
                {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            )
        )
        stage._upload_scene_frame = AsyncMock(
            return_value=SimpleNamespace(
                sas_url="https://example.com/scene.jpg",
                container="user-uploads",
                blob_name="scene.jpg",
            )
        )
        stage._cleanup_temporary_blobs = AsyncMock()

        with patch(
            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.scene_segmentation_stage.extract_frame_jpeg_bytes",
            side_effect=[b"first-frame", RuntimeError("Failed to read frame at 18.535s")],
        ):
            result = asyncio.run(
                stage._analyze_single_scene(
                    prompt_builder=MagicMock(
                        build_scene_understanding_messages=MagicMock(
                            side_effect=lambda **kwargs: [{
                                "role": "system",
                                "content": [{"type": "text", "text": "system"}],
                            }, {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "prompt"},
                                    *(
                                        [{"type": "image_url", "image_url": {"url": kwargs["first_frame_url"]}}]
                                        if kwargs.get("first_frame_url") else []
                                    ),
                                    *(
                                        [{"type": "image_url", "image_url": {"url": kwargs["last_frame_url"]}}]
                                        if kwargs.get("last_frame_url") else []
                                    ),
                                ],
                            }]
                        )
                    ),
                    video_path=SMOKE_VIDEO_PATH,
                    scene_idx=0,
                    start=18.535,
                    end=19.200,
                    duration=30.0,
                    fps=30.0,
                    preferred_language=Language.EN,
                )
            )

        self.assertIsNotNone(result)
        stage._upload_scene_frame.assert_not_awaited()
        prompt = stage.llm_model_client.complete_messages.await_args.args[0]
        image_parts = [part for part in prompt[1]["content"] if part["type"] == "image_url"]
        self.assertEqual(len(image_parts), 1)
        self.assertTrue(image_parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_analyze_single_scene_retries_neighboring_frames_on_content_policy_error(self) -> None:
        stage = SceneSegmentationStage(
            llm_model_client=MagicMock(),
            llm_image_cleanup_delay_seconds=0,
            llm_image_transport="blob",
        )
        stage.llm_model_client.complete_messages = AsyncMock(
            side_effect=[
                _FakeContentPolicyError(),
                (
                    {
                        "visual_summary": "city street",
                        "key_actions": "people pass by",
                        "mood": "calm",
                    },
                    {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                ),
            ]
        )
        stage._upload_scene_frame = AsyncMock(
            return_value=SimpleNamespace(
                sas_url="https://example.com/scene.jpg",
                container="user-uploads",
                blob_name="scene.jpg",
            )
        )
        stage._cleanup_temporary_blobs = AsyncMock()

        extracted_timestamps = []

        def _fake_extract_frame(_video_path, timestamp, *, duration_hint=None):
            extracted_timestamps.append(timestamp)
            return f"frame-{timestamp:.3f}".encode("ascii")

        with patch(
            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.scene_segmentation_stage.extract_frame_jpeg_bytes",
            side_effect=_fake_extract_frame,
        ):
            result = asyncio.run(
                stage._analyze_single_scene(
                    prompt_builder=MagicMock(
                        build_scene_understanding_messages=MagicMock(
                            return_value=[
                                {
                                    "role": "system",
                                    "content": [{"type": "text", "text": "system"}],
                                },
                                {
                                    "role": "user",
                                    "content": [{"type": "text", "text": "prompt"}],
                                },
                            ]
                        )
                    ),
                    video_path=SMOKE_VIDEO_PATH,
                    scene_idx=0,
                    start=18.535,
                    end=19.200,
                    duration=30.0,
                    fps=30.0,
                    preferred_language=Language.EN,
                )
            )

        retry_step = stage._content_policy_retry_step_s(30.0)
        self.assertIsNotNone(result)
        self.assertEqual(stage.llm_model_client.complete_messages.await_count, 2)
        self.assertEqual(
            [round(ts, 3) for ts in extracted_timestamps],
            [18.535, 19.2, round(18.535 + retry_step, 3), 19.2],
        )

    def test_analyze_single_scene_retries_failed_blob_frame_inline(self) -> None:
        stage = SceneSegmentationStage(
            llm_model_client=MagicMock(),
            llm_image_cleanup_delay_seconds=0,
            llm_image_transport="blob",
        )
        stage.llm_model_client.complete_messages = AsyncMock(
            side_effect=[
                EdennProviderImageFetchTimeoutError(
                    "timed out",
                    provider_name="model_gateway",
                    failed_image_url="https://example.com/last.jpg?sig=REDACTED",
                ),
                (
                    {
                        "visual_summary": "city street",
                        "key_actions": "people pass by",
                        "mood": "calm",
                    },
                    {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                ),
            ]
        )
        stage._upload_scene_frame = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    sas_url="https://example.com/first.jpg?sig=first",
                    container="user-uploads",
                    blob_name="first.jpg",
                ),
                SimpleNamespace(
                    sas_url="https://example.com/last.jpg?sig=last",
                    container="user-uploads",
                    blob_name="last.jpg",
                ),
            ]
        )
        stage._cleanup_temporary_blobs = AsyncMock()

        with patch(
            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.scene_segmentation_stage.extract_frame_jpeg_bytes",
            side_effect=[b"first-frame", b"last-frame"],
        ):
            result = asyncio.run(
                stage._analyze_single_scene(
                    prompt_builder=_scene_prompt_builder(),
                    video_path=SMOKE_VIDEO_PATH,
                    scene_idx=0,
                    start=18.535,
                    end=19.200,
                    duration=30.0,
                    fps=30.0,
                    preferred_language=Language.EN,
                )
            )

        self.assertIsNotNone(result)
        self.assertEqual(stage.llm_model_client.complete_messages.await_count, 2)
        first_urls = _image_urls_from_prompt(
            stage.llm_model_client.complete_messages.await_args_list[0].args[0]
        )
        second_urls = _image_urls_from_prompt(
            stage.llm_model_client.complete_messages.await_args_list[1].args[0]
        )
        self.assertEqual(first_urls, [
            "https://example.com/first.jpg?sig=first",
            "https://example.com/last.jpg?sig=last",
        ])
        self.assertEqual(second_urls[0], "https://example.com/first.jpg?sig=first")
        self.assertTrue(second_urls[1].startswith("data:image/jpeg;base64,"))

    def test_analyze_single_scene_drops_failed_frame_after_inline_retry_failure(self) -> None:
        stage = SceneSegmentationStage(
            llm_model_client=MagicMock(),
            llm_image_cleanup_delay_seconds=0,
            llm_image_transport="blob",
        )
        stage.llm_model_client.complete_messages = AsyncMock(
            side_effect=[
                EdennProviderImageFetchTimeoutError(
                    "timed out",
                    provider_name="model_gateway",
                    failed_image_url="https://example.com/last.jpg?sig=REDACTED",
                ),
                RuntimeError("inline image was rejected"),
                (
                    {
                        "visual_summary": "city street",
                        "key_actions": "people pass by",
                        "mood": "calm",
                    },
                    {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                ),
            ]
        )
        stage._upload_scene_frame = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    sas_url="https://example.com/first.jpg?sig=first",
                    container="user-uploads",
                    blob_name="first.jpg",
                ),
                SimpleNamespace(
                    sas_url="https://example.com/last.jpg?sig=last",
                    container="user-uploads",
                    blob_name="last.jpg",
                ),
            ]
        )
        stage._cleanup_temporary_blobs = AsyncMock()

        with patch(
            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.scene_segmentation_stage.extract_frame_jpeg_bytes",
            side_effect=[b"first-frame", b"last-frame"],
        ):
            result = asyncio.run(
                stage._analyze_single_scene(
                    prompt_builder=_scene_prompt_builder(),
                    video_path=SMOKE_VIDEO_PATH,
                    scene_idx=0,
                    start=18.535,
                    end=19.200,
                    duration=30.0,
                    fps=30.0,
                    preferred_language=Language.EN,
                )
            )

        self.assertIsNotNone(result)
        self.assertEqual(stage.llm_model_client.complete_messages.await_count, 3)
        second_urls = _image_urls_from_prompt(
            stage.llm_model_client.complete_messages.await_args_list[1].args[0]
        )
        third_urls = _image_urls_from_prompt(
            stage.llm_model_client.complete_messages.await_args_list[2].args[0]
        )
        self.assertEqual(second_urls[0], "https://example.com/first.jpg?sig=first")
        self.assertTrue(second_urls[1].startswith("data:image/jpeg;base64,"))
        self.assertEqual(third_urls, ["https://example.com/first.jpg?sig=first"])

    def test_analyze_single_scene_retries_raw_bad_request_blob_frame_inline(self) -> None:
        stage = SceneSegmentationStage(
            llm_model_client=MagicMock(azure_model="chat-test"),
            llm_image_cleanup_delay_seconds=0,
            llm_image_transport="blob",
        )
        stage.llm_model_client.complete_messages = AsyncMock(
            side_effect=[
                _make_model_gateway_bad_request_error(
                    {
                        "error": {
                            "message": (
                                "Timed out while downloading image from "
                                "https://example.com/last.jpg?sig=last."
                            ),
                            "type": "invalid_request_error",
                            "param": None,
                            "code": None,
                        }
                    }
                ),
                (
                    {
                        "visual_summary": "city street",
                        "key_actions": "people pass by",
                        "mood": "calm",
                    },
                    {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                ),
            ]
        )
        stage._upload_scene_frame = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    sas_url="https://example.com/first.jpg?sig=first",
                    container="user-uploads",
                    blob_name="first.jpg",
                ),
                SimpleNamespace(
                    sas_url="https://example.com/last.jpg?sig=last",
                    container="user-uploads",
                    blob_name="last.jpg",
                ),
            ]
        )
        stage._cleanup_temporary_blobs = AsyncMock()

        with patch(
            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.scene_segmentation_stage.extract_frame_jpeg_bytes",
            side_effect=[b"first-frame", b"last-frame"],
        ):
            result = asyncio.run(
                stage._analyze_single_scene(
                    prompt_builder=_scene_prompt_builder(),
                    video_path=SMOKE_VIDEO_PATH,
                    scene_idx=0,
                    start=18.535,
                    end=19.200,
                    duration=30.0,
                    fps=30.0,
                    preferred_language=Language.EN,
                )
            )

        self.assertIsNotNone(result)
        self.assertEqual(stage.llm_model_client.complete_messages.await_count, 2)
        second_urls = _image_urls_from_prompt(
            stage.llm_model_client.complete_messages.await_args_list[1].args[0]
        )
        self.assertEqual(second_urls[0], "https://example.com/first.jpg?sig=first")
        self.assertTrue(second_urls[1].startswith("data:image/jpeg;base64,"))

    def test_analyze_single_scene_raises_when_image_fetch_fallback_is_exhausted(self) -> None:
        stage = SceneSegmentationStage(
            llm_model_client=MagicMock(),
            llm_image_cleanup_delay_seconds=0,
            llm_image_transport="blob",
        )
        stage.llm_model_client.complete_messages = AsyncMock(
            side_effect=[
                EdennProviderImageFetchTimeoutError(
                    "timed out",
                    provider_name="model_gateway",
                    failed_image_url="https://example.com/last.jpg?sig=REDACTED",
                ),
                RuntimeError("inline image was rejected"),
                EdennProviderImageFetchTimeoutError(
                    "timed out",
                    provider_name="model_gateway",
                    failed_image_url="https://example.com/first.jpg?sig=REDACTED",
                ),
                RuntimeError("inline image was rejected again"),
            ]
        )
        stage._upload_scene_frame = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    sas_url="https://example.com/first.jpg?sig=first",
                    container="user-uploads",
                    blob_name="first.jpg",
                ),
                SimpleNamespace(
                    sas_url="https://example.com/last.jpg?sig=last",
                    container="user-uploads",
                    blob_name="last.jpg",
                ),
            ]
        )
        stage._cleanup_temporary_blobs = AsyncMock()

        with patch(
            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.scene_segmentation_stage.extract_frame_jpeg_bytes",
            side_effect=[b"first-frame", b"last-frame"],
        ):
            with self.assertRaises(EdennProviderImageFetchTimeoutError) as exc_info:
                asyncio.run(
                    stage._analyze_single_scene(
                        prompt_builder=_scene_prompt_builder(),
                        video_path=SMOKE_VIDEO_PATH,
                        scene_idx=0,
                        start=18.535,
                        end=19.200,
                        duration=30.0,
                        fps=30.0,
                        preferred_language=Language.EN,
                    )
                )

        self.assertEqual(exc_info.exception.default_error_code, "provider_image_fetch_timeout")
        self.assertEqual(stage.llm_model_client.complete_messages.await_count, 4)

    def test_analyze_single_scene_stops_after_three_content_policy_retries(self) -> None:
        stage = SceneSegmentationStage(
            llm_model_client=MagicMock(),
            llm_image_cleanup_delay_seconds=0,
        )
        stage.llm_model_client.complete_messages = AsyncMock(
            side_effect=_FakeContentPolicyError()
        )
        stage._upload_scene_frame = AsyncMock(
            return_value=SimpleNamespace(
                sas_url="https://example.com/scene.jpg",
                container="user-uploads",
                blob_name="scene.jpg",
            )
        )
        stage._cleanup_temporary_blobs = AsyncMock()

        extracted_timestamps = []

        def _fake_extract_frame(_video_path, timestamp, *, duration_hint=None):
            extracted_timestamps.append(timestamp)
            return f"frame-{timestamp:.3f}".encode("ascii")

        expected_attempts = stage._build_content_policy_retry_attempts(
            start_timestamp=18.535,
            end_timestamp=19.200,
            duration=30.0,
            fps=30.0,
        )

        with patch(
            "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.scene_segmentation_stage.extract_frame_jpeg_bytes",
            side_effect=_fake_extract_frame,
        ), self.assertRaises(_FakeContentPolicyError):
            asyncio.run(
                stage._analyze_single_scene(
                    prompt_builder=MagicMock(
                        build_scene_understanding_messages=MagicMock(
                            return_value=[
                                {
                                    "role": "system",
                                    "content": [{"type": "text", "text": "system"}],
                                },
                                {
                                    "role": "user",
                                    "content": [{"type": "text", "text": "prompt"}],
                                },
                            ]
                        )
                    ),
                    video_path=SMOKE_VIDEO_PATH,
                    scene_idx=0,
                    start=18.535,
                    end=19.200,
                    duration=30.0,
                    fps=30.0,
                    preferred_language=Language.EN,
                )
            )

        expected_timestamps = []
        for first_timestamp, last_timestamp in expected_attempts:
            expected_timestamps.extend([round(first_timestamp, 3), round(last_timestamp, 3)])

        self.assertEqual(stage.llm_model_client.complete_messages.await_count, len(expected_attempts))
        self.assertEqual(
            [round(ts, 3) for ts in extracted_timestamps],
            expected_timestamps,
        )

    def test_analyze_scenes_skips_scene_when_both_frames_fail(self) -> None:
        stage = SceneSegmentationStage(
            llm_model_client=MagicMock(),
            llm_image_cleanup_delay_seconds=0,
        )
        stage._ensure_llm_image_storage = MagicMock()
        stage._analyze_single_scene = AsyncMock(
            side_effect=[
                None,
                SimpleNamespace(
                    start_timestamp=2.0,
                    end_timestamp=4.0,
                    content={
                        "visual_summary": "festival lights",
                        "key_actions": "people dance",
                        "mood": "energetic",
                    },
                    usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                    thumbnail_b64="thumb-b64",
                ),
            ]
        )

        output = asyncio.run(
            stage._analyze_scenes(
                SMOKE_VIDEO_PATH,
                [(0.0, 2.0), (2.0, 4.0)],
                duration=4.0,
                fps=30.0,
            )
        )

        self.assertEqual(len(output["scenes"]), 1)
        self.assertEqual(output["scenes"][0].scene_index, 1)
        self.assertEqual(output["scenes"][0].start_timestamp, 2.0)
        self.assertEqual(output["usage"]["total_tokens"], 5)
        self.assertEqual(output["thumbnail_b64"], "thumb-b64")

    def test_default_inline_transport_does_not_require_blob_storage(self) -> None:
        stage = SceneSegmentationStage(
            llm_model_client=MagicMock(),
            storage_service=None,
            llm_image_container="",
            llm_image_cleanup_delay_seconds=0,
        )
        stage._ensure_llm_image_storage = MagicMock()
        stage._analyze_single_scene = AsyncMock(
            return_value=SimpleNamespace(
                start_timestamp=0.0,
                end_timestamp=2.0,
                content={
                    "visual_summary": "festival lights",
                    "key_actions": "people dance",
                    "mood": "energetic",
                },
                usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                thumbnail_b64="thumb-b64",
            )
        )

        output = asyncio.run(
            stage._analyze_scenes(
                SMOKE_VIDEO_PATH,
                [(0.0, 2.0)],
                duration=2.0,
                fps=30.0,
            )
        )

        stage._ensure_llm_image_storage.assert_not_called()
        self.assertEqual(len(output["scenes"]), 1)
        self.assertEqual(output["usage"]["total_tokens"], 5)

    def test_blob_transport_requires_blob_storage(self) -> None:
        stage = SceneSegmentationStage(
            llm_model_client=MagicMock(),
            storage_service=None,
            llm_image_container="",
            llm_image_cleanup_delay_seconds=0,
            llm_image_transport="blob",
        )

        with self.assertRaises(EdennConfigurationError):
            asyncio.run(
                stage._analyze_scenes(
                    SMOKE_VIDEO_PATH,
                    [(0.0, 2.0)],
                    duration=2.0,
                    fps=30.0,
                )
            )

    def test_analyze_scenes_skips_failed_scene_when_other_scenes_succeed(self) -> None:
        stage = SceneSegmentationStage(
            llm_model_client=MagicMock(),
            llm_image_cleanup_delay_seconds=0,
        )
        stage._ensure_llm_image_storage = MagicMock()
        stage._analyze_single_scene = AsyncMock(
            side_effect=[
                EdennProviderImageFetchTimeoutError(
                    "timed out",
                    provider_name="model_gateway",
                    failed_image_url="https://example.com/scene-0.jpg?sig=REDACTED",
                ),
                SimpleNamespace(
                    start_timestamp=2.0,
                    end_timestamp=4.0,
                    content={
                        "visual_summary": "festival lights",
                        "key_actions": "people dance",
                        "mood": "energetic",
                    },
                    usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                    thumbnail_b64="thumb-b64",
                ),
            ]
        )

        output = asyncio.run(
            stage._analyze_scenes(
                SMOKE_VIDEO_PATH,
                [(0.0, 2.0), (2.0, 4.0)],
                duration=4.0,
                fps=30.0,
            )
        )

        self.assertEqual(len(output["scenes"]), 1)
        self.assertEqual(output["scenes"][0].scene_index, 1)
        self.assertEqual(output["usage"]["total_tokens"], 5)
        self.assertEqual(output["thumbnail_b64"], "thumb-b64")

    def test_analyze_scenes_raises_when_all_scenes_fail(self) -> None:
        stage = SceneSegmentationStage(
            llm_model_client=MagicMock(),
            llm_image_cleanup_delay_seconds=0,
        )
        stage._ensure_llm_image_storage = MagicMock()
        stage._analyze_single_scene = AsyncMock(
            side_effect=[
                EdennProviderImageFetchTimeoutError(
                    "timed out",
                    provider_name="model_gateway",
                    failed_image_url="https://example.com/scene-0.jpg?sig=REDACTED",
                ),
                EdennProviderImageFetchTimeoutError(
                    "timed out again",
                    provider_name="model_gateway",
                    failed_image_url="https://example.com/scene-1.jpg?sig=REDACTED",
                ),
            ]
        )

        with self.assertRaises(EdennProviderImageFetchTimeoutError):
            asyncio.run(
                stage._analyze_scenes(
                    SMOKE_VIDEO_PATH,
                    [(0.0, 2.0), (2.0, 4.0)],
                    duration=4.0,
                    fps=30.0,
                )
            )


if __name__ == "__main__":
    unittest.main()
