import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from EdennCode.Deployment.api_common import ApiContext
from EdennCode.Deployment.storage import TemporaryBlobUpload
from EdennCode.exceptions import EdennConfigurationError
from EdennCode.Deployment.api_audio_creative_edit import create_audio_creative_edit_router
from EdennCode.Util.MediaUtils import resolve_ffmpeg_binary
from EdennCode.WorkflowFactory.AudioCreativeEditWorkflow import (
    AudioCreativeEditWorkflow,
    AudioCreativeEditWorkflowInput,
)
from EdennCode.WorkflowFactory.AudioCreativeEditWorkflow.Stages.AudioCreativeGenerationStage.audio_creative_generation_stage import (
    AudioCreativeGenerationStage,
    AudioCreativeGenerationStageInput,
    AudioCreativeGenerationStageOutput,
)
from EdennCode.WorkflowFactory.AudioCreativeEditWorkflow.Stages.CreativeEditPromptStage.creative_edit_prompt_stage import (
    CreativeEditPromptStageOutput,
)
from EdennCode.WorkflowFactory.AudioCreativeEditWorkflow.Stages.VisualConditioningStage.visual_conditioning_stage import (
    VisualConditioningStage,
    VisualConditioningStageInput,
    VisualConditioningStageOutput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.word_ts import (
    WordTS,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.UserIntentUnderstandingStage.user_prompt_preprocessor import (
    UserPromptPreprocessorResult,
)


def _write_tone(path: Path, *, duration_s: float = 20.0) -> Path:
    """Write a real track — the delivery chop refuses to pass a file it cannot probe."""

    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            resolve_ffmpeg_binary(), "-y", "-v", "error", "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={duration_s}",
            "-c:a", "libmp3lame", "-b:a", "192k", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


class AudioCreativeEditWorkflowTests(unittest.TestCase):
    def test_workflow_init_defers_model_specific_music_provider_init(self) -> None:
        with patch(
            "EdennCode.WorkflowFactory.AudioCreativeEditWorkflow.audio_creative_edit_workflow.build_azure_client",
            return_value=MagicMock(),
        ), patch(
            "EdennCode.WorkflowFactory.AudioCreativeEditWorkflow.audio_creative_edit_workflow.ProviderCApi",
            side_effect=AssertionError("ProviderC should be lazy"),
        ) as build_studio, patch(
            "EdennCode.WorkflowFactory.AudioCreativeEditWorkflow.audio_creative_edit_workflow.build_edenn_enhanced_music_provider",
            side_effect=EdennConfigurationError(
                "PG unavailable",
                component="provider_b",
                operation="initialize",
            ),
        ) as build_enhanced:
            workflow = AudioCreativeEditWorkflow()

        build_studio.assert_not_called()
        build_enhanced.assert_not_called()
        self.assertIsNone(workflow.provider_c_music_provider)
        self.assertIsNone(workflow.provider_b_music_provider)

    def test_workflow_wires_outputs_and_token_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            source_audio = tmp_dir / "source.wav"
            source_audio.write_bytes(b"audio")
            edited_audio = tmp_dir / "edited.wav"
            _write_tone(edited_audio)

            with patch(
                "EdennCode.WorkflowFactory.AudioCreativeEditWorkflow.audio_creative_edit_workflow.build_azure_client",
                return_value=MagicMock(),
            ), patch(
                "EdennCode.WorkflowFactory.AudioCreativeEditWorkflow.audio_creative_edit_workflow.ProviderCApi",
                return_value=MagicMock(),
            ), patch(
                "EdennCode.WorkflowFactory.AudioCreativeEditWorkflow.audio_creative_edit_workflow.build_edenn_enhanced_music_provider",
                return_value=MagicMock(),
            ):
                workflow = AudioCreativeEditWorkflow()

            workflow.user_prompt_preprocessor.preprocess = AsyncMock(
                return_value=UserPromptPreprocessorResult(
                    was_transformed=False,
                    detected_include_vocals=True,
                    transformed_prompt="edit the chorus and brighten the energy",
                    detected_references=[],
                    detected_language="ENGLISH_US",
                    detected_category="VIDEO",
                    detected_vocal_gender="female",
                    detected_vocal_language="ENGLISH_US",
                    reasoning="ok",
                    tokens_used=11,
                    prompt_tokens=7,
                    completion_tokens=4,
                )
            )
            workflow.visual_conditioning_stage.run = AsyncMock(
                return_value=VisualConditioningStageOutput(
                    input_type="none",
                    token_usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                )
            )
            workflow.creative_edit_prompt_stage.run = AsyncMock(
                return_value=CreativeEditPromptStageOutput(
                    prompt_payload={
                        "title": "Bright Cut",
                        "edit_intent_summary": "Push the source into a brighter chorus-forward shape.",
                        "visual_style_summary": "Driven by user instruction only.",
                        "edit_prompt": "Transform the source audio into a brighter, cleaner, chorus-forward pop edit.",
                        "style_prompt": "bright modern pop, tighter drums, glossy synths, sharper lift into hook",
                        "lyrics_prompt": "hopeful chorus about momentum and confidence",
                    },
                    token_usage={"prompt_tokens": 31, "completion_tokens": 12, "total_tokens": 43},
                )
            )
            workflow.audio_generation_stage.run = AsyncMock(
                return_value=AudioCreativeGenerationStageOutput(
                    edited_audio_path=edited_audio,
                    secondary_edited_audio_path=None,
                    lyrics_timestamps=[WordTS(text="hello", startS=0.1, endS=0.7, i=0)],
                    used_modelspec="edenn_enhanced",
                    vocal_id_used="vocal_abc",
                )
            )

            output = asyncio.run(
                workflow.run(
                    AudioCreativeEditWorkflowInput(
                        source_audio_path=source_audio,
                        user_prompt="please brighten this audio and give it a hopeful hook",
                        modelspec="edenn_enhanced",
                        provider_c_custom_mode=True,
                        provider_c_style_weight=0.65,
                        provider_c_audio_weight=0.75,
                        provider_c_weirdness_constraint=0.2,
                    )
                )
            )

            self.assertEqual(output.source_audio_path, source_audio)
            # The generation stage is stubbed here, so this asserts wiring only —
            # the tail chop is covered where the real stage runs.
            self.assertEqual(output.edited_audio_path, edited_audio)
            self.assertTrue(output.include_vocals)
            self.assertEqual(output.vocal_gender, "female")
            self.assertEqual(output.user_requested_language, "ENGLISH_US")
            self.assertEqual(output.creative_edit_prompt["title"], "Bright Cut")
            self.assertEqual(output.token_usage["total_tokens"], 54)
            self.assertEqual(output.token_usage_breakdown["creative_edit_prompt"]["total_tokens"], 43)
            self.assertEqual(output.vocal_id_used, "vocal_abc")
            stage_input = workflow.audio_generation_stage.run.await_args.args[0]
            self.assertTrue(stage_input.provider_c_custom_mode)
            self.assertEqual(stage_input.provider_c_style_weight, 0.65)
            self.assertEqual(stage_input.provider_c_audio_weight, 0.75)
            self.assertEqual(stage_input.provider_c_weirdness_constraint, 0.2)

    def test_generation_stage_uses_provider_b_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            source_audio = tmp_dir / "source.wav"
            source_audio.write_bytes(b"audio")
            melody_audio = tmp_dir / "melody.m4a"
            melody_audio.write_bytes(b"melody")
            edited_audio = tmp_dir / "edited.mp3"
            _write_tone(edited_audio)

            fake_provider_b = MagicMock()
            fake_provider_b.generate_with_melody_variants = AsyncMock(
                return_value=(edited_audio, None, [WordTS(text="line", startS=0.0, endS=1.0, i=0)])
            )
            fake_provider_b.clone_vocal = AsyncMock(return_value="vocal_123")
            stage = AudioCreativeGenerationStage(
                provider_c_music_provider=MagicMock(),
                provider_b_music_provider=fake_provider_b,
            )

            with patch.object(stage, "_prepare_provider_b_melody_audio", return_value=melody_audio):
                output = asyncio.run(
                    stage.run(
                        AudioCreativeGenerationStageInput(
                            source_audio_path=source_audio,
                            source_audio_provider_url=None,
                            prompt_payload={
                                "edit_prompt": "bright pop edit",
                                "style_prompt": "bright pop edit",
                                "lyrics_prompt": "uplifting lyrics",
                            },
                            include_vocals=True,
                            vocal_gender="female",
                            modelspec="edenn_enhanced",
                            workdir=tmp_dir,
                            vocal_sample_path=tmp_dir / "voice.m4a",
                        )
                    )
                )

            # Delivered takes lose the provider's trailing tag before they leave the stage.
            self.assertEqual(
                output.edited_audio_path,
                edited_audio.with_name(f"{edited_audio.stem}_trimmed_tail6s.mp3"),
            )
            self.assertEqual(output.used_modelspec, "edenn_enhanced")
            fake_provider_b.generate_with_melody_variants.assert_awaited_once()
            fake_provider_b.clone_vocal.assert_awaited_once_with(tmp_dir / "voice.m4a")
            _, kwargs = fake_provider_b.generate_with_melody_variants.await_args
            self.assertEqual(kwargs["prompt"], "bright pop edit")
            self.assertEqual(kwargs["lyrics_prompt"], "uplifting lyrics")
            self.assertEqual(kwargs["melody_audio_path"], melody_audio)
            self.assertEqual(kwargs["vocal_id"], "vocal_123")
            self.assertEqual(output.vocal_id_used, "vocal_123")

    def test_generation_stage_uses_provider_b_prompt_for_instrumental_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            source_audio = tmp_dir / "source.wav"
            source_audio.write_bytes(b"audio")
            melody_audio = tmp_dir / "melody.m4a"
            melody_audio.write_bytes(b"melody")
            edited_audio = tmp_dir / "edited.mp3"
            _write_tone(edited_audio)
            secondary_audio = tmp_dir / "edited_alt.mp3"
            _write_tone(secondary_audio, duration_s=18.0)

            fake_provider_b = MagicMock()
            fake_provider_b.generate_instrumental_with_melody_variants = AsyncMock(
                return_value=(edited_audio, secondary_audio)
            )
            stage = AudioCreativeGenerationStage(
                provider_c_music_provider=MagicMock(),
                provider_b_music_provider=fake_provider_b,
            )

            with patch.object(stage, "_prepare_provider_b_melody_audio", return_value=melody_audio):
                output = asyncio.run(
                    stage.run(
                        AudioCreativeGenerationStageInput(
                            source_audio_path=source_audio,
                            source_audio_provider_url=None,
                            prompt_payload={
                                "edit_prompt": "warm desert-drive instrumental",
                                "style_prompt": "warm desert-drive instrumental",
                                "lyrics_prompt": "",
                            },
                            include_vocals=False,
                            vocal_gender="female",
                            modelspec="edenn_enhanced",
                            workdir=tmp_dir,
                        )
                    )
                )

            # Delivered takes lose the provider's trailing tag before they leave the stage.
            self.assertEqual(
                output.edited_audio_path,
                edited_audio.with_name(f"{edited_audio.stem}_trimmed_tail6s.mp3"),
            )
            self.assertEqual(
                output.secondary_edited_audio_path,
                secondary_audio.with_name(f"{secondary_audio.stem}_trimmed_tail6s.mp3"),
            )
            self.assertEqual(output.used_modelspec, "edenn_enhanced")
            fake_provider_b.generate_instrumental_with_melody_variants.assert_awaited_once()
            _, kwargs = fake_provider_b.generate_instrumental_with_melody_variants.await_args
            self.assertEqual(kwargs["prompt"], "warm desert-drive instrumental")
            self.assertEqual(kwargs["melody_audio_path"], melody_audio)

    def test_generation_stage_uses_provider_c_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            source_audio = tmp_dir / "source.wav"
            source_audio.write_bytes(b"audio")

            provider_c_provider = MagicMock()
            provider_c_provider.upload_cover_and_poll_tracks = AsyncMock(
                return_value=(
                    "task_1",
                    MagicMock(
                        tracks=[
                            MagicMock(audio_id="track_1", audio_url="https://example.com/track.wav"),
                        ]
                    ),
                    "https://api.provider-c.example.invalid/api/v1",
                )
            )
            provider_c_provider.download = AsyncMock(
                side_effect=lambda _track, path: _write_tone(Path(path))
            )
            provider_c_provider.wait_for_timestamped_lyrics = AsyncMock(return_value=[])

            stage = AudioCreativeGenerationStage(
                provider_c_music_provider=provider_c_provider,
                provider_b_music_provider=MagicMock(),
            )
            output = asyncio.run(
                stage.run(
                    AudioCreativeGenerationStageInput(
                        source_audio_path=source_audio,
                        source_audio_provider_url="https://storage.example.com/source.wav",
                        prompt_payload={"edit_prompt": "cinematic cover edit"},
                        include_vocals=False,
                        vocal_gender="female",
                        modelspec="edenn_studio",
                        workdir=tmp_dir,
                    )
                )
            )

            self.assertEqual(
                output.edited_audio_path, tmp_dir / "primary_trimmed_tail6s.mp3"
            )
            self.assertEqual(output.used_modelspec, "edenn_studio")
            provider_c_provider.upload_cover_and_poll_tracks.assert_awaited_once()

    def test_generation_stage_uses_provider_c_custom_mode_with_weights(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            source_audio = tmp_dir / "source.wav"
            source_audio.write_bytes(b"audio")

            provider_c_provider = MagicMock()
            provider_c_provider.upload_cover_and_poll_tracks = AsyncMock(
                return_value=(
                    "task_1",
                    MagicMock(
                        tracks=[
                            MagicMock(audio_id="track_1", audio_url="https://example.com/track.wav"),
                        ]
                    ),
                    "https://api.provider-c.example.invalid/api/v1",
                )
            )
            provider_c_provider.download = AsyncMock(
                side_effect=lambda _track, path: _write_tone(Path(path))
            )
            provider_c_provider.wait_for_timestamped_lyrics = AsyncMock(return_value=[])

            stage = AudioCreativeGenerationStage(
                provider_c_music_provider=provider_c_provider,
                provider_b_music_provider=MagicMock(),
            )
            output = asyncio.run(
                stage.run(
                    AudioCreativeGenerationStageInput(
                        source_audio_path=source_audio,
                        source_audio_provider_url="https://storage.example.com/source.wav",
                        prompt_payload={
                            "title": "Open Highway",
                            "edit_prompt": "cinematic road-song edit",
                            "style_prompt": "cinematic americana with wider guitars",
                            "lyrics_prompt": "These are the exact lyrics to sing.",
                        },
                        include_vocals=True,
                        vocal_gender="female",
                        modelspec="edenn_studio",
                        workdir=tmp_dir,
                        provider_c_custom_mode=True,
                        provider_c_style_weight=0.65,
                        provider_c_audio_weight=0.75,
                        provider_c_weirdness_constraint=0.2,
                    )
                )
            )

            self.assertEqual(
                output.edited_audio_path, tmp_dir / "primary_trimmed_tail6s.mp3"
            )
            provider_c_provider.upload_cover_and_poll_tracks.assert_awaited_once()
            params = provider_c_provider.upload_cover_and_poll_tracks.await_args.args[0]
            self.assertTrue(params.custom_mode)
            self.assertFalse(params.instrumental)
            self.assertEqual(params.title, "Open Highway")
            self.assertEqual(params.style, "cinematic americana with wider guitars")
            self.assertEqual(params.prompt, "These are the exact lyrics to sing.")
            self.assertEqual(params.style_weight, 0.65)
            self.assertEqual(params.audio_weight, 0.75)
            self.assertEqual(params.weirdness_constraint, 0.2)
            self.assertEqual(params.extra, {"vocalGender": "f"})

    def test_audio_creative_edit_router_registers_route(self) -> None:
        context = ApiContext(
            settings=MagicMock(),
            storage=MagicMock(),
            workflow=MagicMock(),
            alignment_workflow=MagicMock(),
            audio_creative_edit_workflow=MagicMock(),
            logger=MagicMock(),
        )
        router = create_audio_creative_edit_router(context)
        paths = sorted(route.path for route in router.routes)
        self.assertIn("/api/v1/jobs/audio-creative-edit", paths)


class VisualConditioningStageTests(unittest.TestCase):
    def test_image_upload_uses_ascii_safe_blob_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            image_path = tmp_dir / "海边日落.png"
            image_path.write_bytes(b"image-bytes")

            storage = MagicMock()
            storage.enabled = True
            storage.upload_temporary_bytes.return_value = TemporaryBlobUpload(
                container="user-uploads",
                blob_name="llm-inputs/audio-creative-edit/image-123456789abc_000_deadbeef.png",
                sas_url="https://example.com/blob.png",
            )

            llm_client = MagicMock()
            llm_client.complete_messages = AsyncMock(
                return_value=(
                    {
                        "summary": "sunset beach shot",
                        "overall_mood": "calm",
                        "visual_style": "warm",
                        "creative_direction": "gentle tropical",
                        "key_elements": ["beach"],
                    },
                    {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
                )
            )
            stage = VisualConditioningStage(
                llm_client=llm_client,
                storage_service=storage,
                llm_image_container="user-uploads",
                llm_image_cleanup_delay_seconds=0,
            )

            output = asyncio.run(
                stage.run(
                    VisualConditioningStageInput(
                        image_paths=[image_path],
                    )
                )
            )

            blob_name = storage.upload_temporary_bytes.call_args.kwargs["blob_name"]
            self.assertEqual(output.input_type, "images")
            self.assertRegex(
                blob_name,
                r"^llm-inputs/audio-creative-edit/image-[0-9a-f]{12}_000_[0-9a-f]{32}\.png$",
            )
            self.assertNotIn("海边", blob_name)


if __name__ == "__main__":
    unittest.main()
