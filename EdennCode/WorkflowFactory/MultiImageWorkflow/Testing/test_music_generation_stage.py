import tempfile
import unittest
from pathlib import Path

from EdennCode.MusicGenerationCore.models import (
    MusicGenerationResult,
    MusicModelSpec,
    MusicSection,
    MusicVariant,
    NarrativeCue,
    ProviderJobRef,
    SectionPlan,
    SectionTiming,
    TimestampedWord,
)
from EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.MusicGenerationStage.music_generation_stage import (
    MultiImageMusicGenerationStage,
    MultiImageMusicGenerationStageInput,
)


class _FakeMusicGenerationService:
    def __init__(self, result: MusicGenerationResult) -> None:
        self.result = result
        self.last_request = None

    async def generate(self, request):
        self.last_request = request
        return self.result


class MultiImageMusicGenerationStageTests(unittest.IsolatedAsyncioTestCase):
    async def test_stage_builds_core_request_and_returns_primary_output(self) -> None:
        plan = SectionPlan(
            summary="A compact visual story",
            total_duration_s=6.0,
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
                    target_duration_s=6.0,
                    objective="Set up",
                    energy_start=0.3,
                    energy_end=0.7,
                    image_indices=[1],
                    cue_ids=["image_1"],
                )
            ],
            music_prompt_summary="Bright synth pop",
        )

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            audio_path = output_dir / "primary.wav"
            audio_path.write_bytes(b"RIFF")
            result = MusicGenerationResult(
                used_modelspec=MusicModelSpec.EDENN_STUDIO,
                primary=MusicVariant(
                    variant_id="primary",
                    audio_path=audio_path,
                    duration_s=6.0,
                    lyrics_timestamps=[TimestampedWord(text="hi", startS=0.0, endS=1.0, i=0)],
                    section_timeline=[
                        SectionTiming(
                            section_id="intro",
                            expected_start_s=0.0,
                            expected_end_s=6.0,
                            actual_start_s=0.0,
                            actual_end_s=6.0,
                            confidence=1.0,
                        )
                    ],
                ),
                alternates=[
                    MusicVariant(
                        variant_id="alt2",
                        audio_path=output_dir / "alt.wav",
                        duration_s=6.1,
                    )
                ],
                job_ref=ProviderJobRef(provider="provider_c", task_id="task_1", audio_id="audio_1"),
                prompt_manifest={"style_prompt": "Bright synth pop"},
                prompt_summary="Bright synth pop",
                vocal_id_used="vocal_456",
            )
            (output_dir / "alt.wav").write_bytes(b"RIFF")
            service = _FakeMusicGenerationService(result)
            stage = MultiImageMusicGenerationStage(music_generation_service=service)

            output = await stage.run(
                MultiImageMusicGenerationStageInput(
                    plan_metadata={"music_prompt": "Bright synth pop"},
                    section_plan=plan,
                    total_duration=6.0,
                    output_dir=output_dir,
                    include_vocals=True,
                    vocal_gender="female",
                    lyrics_language="EN",
                    modelspec="provider_c",
                    vocal_id="vocal_456",
                    water_mark=True,
                )
            )

        self.assertEqual(output.music_path, audio_path)
        self.assertEqual(output.used_modelspec, "edenn_studio")
        self.assertEqual(output.prompt, "Bright synth pop")
        self.assertEqual(len(output.section_timeline), 1)
        self.assertEqual(output.full_track_paths, [audio_path, output_dir / "alt.wav"])
        self.assertEqual(output.vocal_id_used, "vocal_456")
        self.assertEqual(service.last_request.modelspec.value, "edenn_studio")
        self.assertEqual(service.last_request.options.lyrics_language, "EN")
        self.assertEqual(service.last_request.options.vocal_id, "vocal_456")
        self.assertTrue(service.last_request.options.water_mark)
