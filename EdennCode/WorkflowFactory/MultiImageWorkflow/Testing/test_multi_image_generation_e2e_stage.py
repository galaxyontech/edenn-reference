import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from EdennCode.MusicGenerationCore.models import MusicSection, NarrativeCue, SectionPlan, SectionTiming, TimestampedWord
from EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.MultiImageE2EGenerationStage.multi_image_generation_e2e_stage import (
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


class MultiImageGenerationE2EStageTests(unittest.IsolatedAsyncioTestCase):
    async def test_stage_infers_vocal_settings_from_user_prompt_before_planning_and_generation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="multi-image-e2e-") as tmp:
            tmp_dir = Path(tmp)
            source_dir = tmp_dir / "images"
            output_path = tmp_dir / "out.mp4"
            source_dir.mkdir(parents=True, exist_ok=True)
            input_image = source_dir / "frame1.png"
            input_image.write_bytes(b"png")
            processed_image = tmp_dir / "processed" / "frame1.png"
            processed_image.parent.mkdir(parents=True, exist_ok=True)
            processed_image.write_bytes(b"png")
            audio_path = tmp_dir / "audio" / "primary.wav"
            audio_path.parent.mkdir(parents=True, exist_ok=True)
            audio_path.write_bytes(b"RIFF")
            final_video = tmp_dir / "final.mp4"
            silent_video = tmp_dir / "silent.mp4"
            final_video.write_bytes(b"video")
            silent_video.write_bytes(b"silent")

            section_plan = SectionPlan(
                summary="A short reveal sequence",
                total_duration_s=3.0,
                overall_mood="bright",
                target_bpm=120.0,
                primary_instruments=["synth"],
                cues=[
                    NarrativeCue(
                        cue_id="image_1",
                        label="Frame 1",
                        role="setup",
                        target_duration_s=3.0,
                    )
                ],
                sections=[
                    MusicSection(
                        section_id="intro",
                        label="Intro",
                        target_duration_s=3.0,
                        objective="Set up the story",
                        energy_start=0.3,
                        energy_end=0.7,
                        image_indices=[1],
                        cue_ids=["image_1"],
                        lyric_lines=["点亮今晚"],
                    )
                ],
                music_prompt_summary="Bright pop lift",
            )

            preprocess_output = SimpleNamespace(
                preprocessed_images=[processed_image],
                compression_applied=True,
            )
            planning_output = SimpleNamespace(
                ordered_images=[processed_image],
                plan={
                    "video_title": "夜色开场",
                    "video_description": "一个明亮的开场镜头。",
                    "storyline_summary": "一个明亮的开场镜头。",
                    "music_sections": [{"image_indices": [1]}],
                },
                section_plan=section_plan,
            )
            music_output = SimpleNamespace(
                music_path=audio_path,
                full_track_paths=[audio_path],
                prompt="Bright pop lift",
                used_modelspec="edenn_studio",
                lyrics_timestamps=[TimestampedWord(text="点亮", startS=0.0, endS=0.5, i=0)],
                section_timeline=[
                    SectionTiming(
                        section_id="intro",
                        expected_start_s=0.0,
                        expected_end_s=3.0,
                        actual_start_s=0.0,
                        actual_end_s=3.0,
                        confidence=1.0,
                    )
                ],
            )
            preprocess_result = UserPromptPreprocessorResult(
                was_transformed=False,
                detected_include_vocals=True,
                transformed_prompt="Create a bright female vocal pop track in Chinese.",
                detected_references=[],
                detected_language=Language.CN,
                detected_category=VideoCategory.DEFAULT,
                detected_vocal_gender="female",
                detected_vocal_language="ZH",
                reasoning="Prompt requests Chinese female vocals.",
            )

            with patch(
                "EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.MultiImageE2EGenerationStage.multi_image_generation_e2e_stage.build_azure_client",
                return_value=MagicMock(),
            ):
                stage = MultiImageGenerationE2EStage(
                    per_image_duration=3.0,
                    align_to_beats=False,
                )

            stage.prompt_preprocessor.preprocess = AsyncMock(return_value=preprocess_result)
            stage.preprocess_stage.run = MagicMock(return_value=preprocess_output)
            stage.planning_stage.run = AsyncMock(return_value=planning_output)
            stage.music_stage.run = AsyncMock(return_value=music_output)
            stage.assembly_stage.run = MagicMock(
                return_value=SimpleNamespace(
                    final_video_path=final_video,
                    silent_video_path=silent_video,
                    applied_transitions=[],
                    applied_transition_duration_s=0.0,
                )
            )

            result = await stage.run(
                MultiImageWorkflowStageInput(
                    folder_path=source_dir,
                    output_path=output_path,
                    user_prompt="来一首中文女声流行歌",
                    include_vocals=False,
                    vocal_gender=None,
                    align_to_beats=False,
                    lyrics_language=None,
                    modelspec="edenn_studio",
                )
            )

        planning_input = stage.planning_stage.run.await_args.args[0]
        music_input = stage.music_stage.run.await_args.args[0]
        self.assertTrue(planning_input.include_vocals)
        self.assertEqual(planning_input.preferred_lyric_language, "ZH")
        self.assertIn("Chinese", planning_input.preferred_output_language)
        self.assertEqual(music_input.vocal_gender, "female")
        self.assertEqual(music_input.lyrics_language, "ZH")
        self.assertTrue(music_input.include_vocals)
        self.assertEqual(result.video_title, "夜色开场")
        self.assertEqual(result.video_description, "一个明亮的开场镜头。")
        self.assertTrue(result.compression_applied)
        self.assertEqual(result.user_requested_language, Language.CN)

    async def test_explicit_per_image_durations_disable_beats_and_reach_assembly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="multi-image-durations-") as tmp:
            tmp_dir = Path(tmp)
            source_dir = tmp_dir / "images"
            source_dir.mkdir(parents=True, exist_ok=True)
            (source_dir / "f1.png").write_bytes(b"png")
            images = []
            for i in range(3):
                p = tmp_dir / f"processed_{i}.png"
                p.write_bytes(b"png")
                images.append(p)
            audio_path = tmp_dir / "primary.wav"
            audio_path.write_bytes(b"RIFF")

            section_plan = SectionPlan(
                summary="s", total_duration_s=9.0, overall_mood="bright",
                target_bpm=120.0, primary_instruments=["synth"], cues=[],
                sections=[MusicSection(section_id="intro", label="Intro",
                                       target_duration_s=9.0, objective="o",
                                       energy_start=0.3, energy_end=0.7,
                                       image_indices=[1, 2, 3], lyric_lines=["la"])],
                music_prompt_summary="p",
            )
            preprocess_output = SimpleNamespace(preprocessed_images=images, compression_applied=False)
            planning_output = SimpleNamespace(
                ordered_images=images,
                plan={"video_title": "t", "video_description": "d", "storyline_summary": "s",
                      "music_sections": [{"image_indices": [1, 2, 3]}]},
                section_plan=section_plan,
            )
            # Track length == window (sum of durations) so no real audio-window
            # ffmpeg extraction runs on the placeholder wav in this unit test.
            music_output = SimpleNamespace(
                music_path=audio_path, full_track_paths=[audio_path], prompt="p",
                used_modelspec="edenn_enhanced", lyrics_timestamps=[], section_timeline=[],
                music_duration_s=9.0,
            )

            with patch(
                "EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.MultiImageE2EGenerationStage.multi_image_generation_e2e_stage.build_azure_client",
                return_value=MagicMock(),
            ):
                stage = MultiImageGenerationE2EStage(per_image_duration=3.0, align_to_beats=True)

            stage.preprocess_stage.run = MagicMock(return_value=preprocess_output)
            stage.planning_stage.run = AsyncMock(return_value=planning_output)
            stage.music_stage.run = AsyncMock(return_value=music_output)
            stage.beat_stage.run = AsyncMock()  # must NOT be called
            stage.assembly_stage.run = MagicMock(return_value=SimpleNamespace(
                final_video_path=tmp_dir / "final.mp4", silent_video_path=tmp_dir / "silent.mp4",
                applied_transitions=[], applied_transition_duration_s=0.0,
            ))
            (tmp_dir / "final.mp4").write_bytes(b"v")
            (tmp_dir / "silent.mp4").write_bytes(b"s")

            await stage.run(MultiImageWorkflowStageInput(
                folder_path=source_dir, output_path=tmp_dir / "out.mp4",
                include_vocals=True, align_to_beats=True, modelspec="edenn_enhanced",
                fixed_image_order=True, per_image_durations=[4.0, 2.0, 3.0],
            ))

            # Beat alignment is skipped when explicit durations are given.
            stage.beat_stage.run.assert_not_called()
            # The assembly stage receives the exact per-image durations.
            assembly_input = stage.assembly_stage.run.call_args.args[0]
            self.assertEqual(assembly_input.durations, [4.0, 2.0, 3.0])
            # Music generation is asked for the summed window length.
            music_input = stage.music_stage.run.await_args.args[0]
            self.assertEqual(music_input.total_duration, 9.0)


if __name__ == "__main__":
    unittest.main()
