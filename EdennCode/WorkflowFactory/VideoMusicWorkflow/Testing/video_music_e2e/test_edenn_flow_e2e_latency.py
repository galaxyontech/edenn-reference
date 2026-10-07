import asyncio
import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from EdennCode.WorkflowFactory.VideoMusicWorkflow.PipelineDataModel.pipeline_datamodel import (
    Language,
    VideoCategory,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata.video_metadata import VideoMetadata
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.music_matching_stage import (
    MusicMatchingStageOutput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicPromptOrchestrationStage.music_prompt_orchestration_stage import (
    MusicPromptOrchestrationStage,
    MusicPromptOrchestrationStageOutput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.datamodel import (
    SceneUnderstanding,
)
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
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow import (
    VideoMusicWorkflowE2E,
    VideoMusicWorkflowE2EInput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoPreprocessStage.media_preprocess_stage import (
    PreprocessStage,
    PreprocessStageOutput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoUnderstandingStage.video_understanding_page import (
    VideoUnderstandingStage,
    VideoUnderstandingStageOutput,
)
from EdennCode.TestSuites.helpers.paths import PRODUCTION_VIDEO_PATH


def _write_sine_wav(path: Path, duration: float = 0.5, sample_rate: int = 16000) -> None:
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
        path=PRODUCTION_VIDEO_PATH,
        duration=3.0,
        size_bytes=PRODUCTION_VIDEO_PATH.stat().st_size,
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


class _FakeProviderDProvider:
    def __init__(self, audio_path: Path):
        self.audio_path = audio_path
        self.generate_calls = []
        self.lyrics_calls = []

    async def generate_lyrics(self, *, prompt: str, mode: str = "write_full_song", title: str = ""):
        self.lyrics_calls.append({"prompt": prompt, "mode": mode, "title": title})
        return {"song_title": "demo", "style_tags": "pop", "lyrics": "line one\nline two"}

    async def generate(self, prompt: str, *, lyrics: str, output_format: str = "url", **kwargs):
        self.generate_calls.append(
            {
                "prompt": prompt,
                "lyrics": lyrics,
                "output_format": output_format,
            }
        )
        return self.audio_path, []


@unittest.skip("edenn_enhanced/provider_d path removed; superseded by edenn_enhanced provider")
class EdennFlowE2ETests(unittest.TestCase):
    def test_edenn_enhanced_provider_d_e2e_and_latency_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_metadata = _fake_video_metadata(tmp_dir)
            thumbnail_path = tmp_dir / "thumb.jpg"
            thumbnail_path.write_bytes(b"thumb")
            provider_d_audio = tmp_dir / "provider_d.wav"
            _write_sine_wav(provider_d_audio)
            matched_audio = tmp_dir / "matched.wav"
            _write_sine_wav(matched_audio)

            preprocess_output = PreprocessStageOutput(video_metadata=video_metadata)
            scene_output = SceneSegmentationStageOutput(
                scene_understanding_messages=[
                    SceneUnderstanding(
                        scene_index=0,
                        start_timestamp=0.0,
                        end_timestamp=video_metadata.duration,
                        visual_summary="outdoor celebration",
                        key_actions="people gather and smile",
                        mood="festive",
                    )
                ],
                token_usage={"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
                thumbnail_path=thumbnail_path,
            )
            understanding_output = VideoUnderstandingStageOutput(
                video_descriptions={
                    "video_title": "Holiday Promo",
                    "video_description": "Festive hotel campaign.",
                    "summary": "Short ad summary.",
                    "overall_mood": "upbeat",
                    "core_message": "Join holiday offer.",
                    "has_explicit_call_to_action": True,
                },
                video_title="Holiday Promo",
                video_description="Festive hotel campaign.",
                token_usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )
            prompt_output = MusicPromptOrchestrationStageOutput(
                music_generation_prompt={
                    "style_prompt": "Mandopop upbeat with bells",
                    "lyrics_prompt": "holiday celebration by the sea",
                },
                token_usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                downstream_generation_model_spec="edenn_enhanced",
            )
            preprocessor_output = UserPromptPreprocessorResult(
                was_transformed=False,
                detected_include_vocals=True,
                transformed_prompt="cleaned prompt",
                detected_references=[],
                detected_language=Language.EN,
                detected_category=VideoCategory.DEFAULT,
                reasoning="ok",
                tokens_used=3,
                prompt_tokens=2,
                completion_tokens=1,
            )

            async def fake_remix_run(stage_input: VideoAudioRemixStageInput):
                out_path = Path(stage_input.video_metadata.temp_folder) / "remix.mp4"
                out_path.write_bytes(b"remix")
                return VideoAudioRemixStageOutput(remixed_video_path=out_path.name)

            preprocess_mock = AsyncMock(return_value=preprocess_output)
            segmentation_mock = AsyncMock(return_value=scene_output)
            understanding_mock = AsyncMock(return_value=understanding_output)
            prompt_mock = AsyncMock(return_value=prompt_output)
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
                VideoAudioRemixStage, "run", new=remix_mock
            ):
                workflow = VideoMusicWorkflowE2E()
                fake_provider_d = _FakeProviderDProvider(provider_d_audio)
                workflow.music_generation_stage.provider_d_music_provider = fake_provider_d
                workflow.music_generation_stage.music_reranker_stage.run = AsyncMock(
                    return_value=MusicMatchingStageOutput(reranked_music_outputs_path=matched_audio)
                )

                with self.assertLogs("Music Generation", level="INFO") as logs:
                    output = asyncio.run(
                        workflow.generate(
                            VideoMusicWorkflowE2EInput(
                                video_path=str(video_metadata.path),
                                include_vocals=True,
                                vocal_gender="female",
                                user_prompt="make it festive",
                                music_model_spec="edenn_enhanced",
                            )
                        )
                    )

            self.assertEqual(output.UsedMusicModelSpecs, "edenn_enhanced")
            self.assertEqual(output.generated_music_path, matched_audio)
            self.assertEqual(len(output.lyrics_timestamps), 2)
            self.assertEqual(fake_provider_d.lyrics_calls[0]["prompt"], "holiday celebration by the sea")
            self.assertEqual(fake_provider_d.lyrics_calls[0]["mode"], "write_full_song")
            self.assertEqual(fake_provider_d.generate_calls[0]["prompt"], "Mandopop upbeat with bells")
            self.assertEqual(fake_provider_d.generate_calls[0]["lyrics"], "line one\nline two")
            self.assertEqual(fake_provider_d.generate_calls[0]["output_format"], "url")

            combined_logs = "\n".join(logs.output)
            self.assertIn("[edenn_enhanced] Legacy generation took", combined_logs)
            self.assertIn("[edenn_enhanced] Music matching took", combined_logs)
            self.assertIn("[edenn_enhanced] Total branch latency", combined_logs)


if __name__ == "__main__":
    unittest.main()
