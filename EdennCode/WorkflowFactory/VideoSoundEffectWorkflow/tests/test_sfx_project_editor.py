from __future__ import annotations

import asyncio
import tempfile
import unittest
import wave
from pathlib import Path
from typing import List

from EdennCode.ModelFactory.VideoSFXModelFactory.CloudSoundEffectGen.base import (
    SoundEffectProvider,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.Stages.SoundEffectGenerationStage.sound_effect_generation_stage import (
    SoundEffectGenerationStage,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.VideoSoundEffectDataModel.datamodel import (
    SfxVariant,
    AmbienceBed,
    MixSettings,
    SfxProject,
    SoundFXEvent,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.sfx_project_editor import SfxProjectEditor
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.tests._synthetic_media import (
    make_click_wav,
    make_flash_video,
)


class _RecordingProvider(SoundEffectProvider):
    """Writes a short tone and records every prompt it was asked to render."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: List[dict] = []

    async def generate(
        self,
        *,
        prompt: str,
        duration_seconds: float,
        output_stem: str,
        output_dir: Path,
        sample_rate: int = 44100,
        loop: bool = False,
    ) -> Path:
        self.calls.append({"prompt": prompt, "duration": duration_seconds, "loop": loop})
        return make_click_wav(
            output_dir / f"{output_stem}.wav",
            duration_s=max(0.2, min(duration_seconds, 2.0)),
            click_duration_s=0.1,
            sample_rate=sample_rate,
        )


def _make_project(tmp: Path) -> tuple[SfxProject, _RecordingProvider]:
    video = make_flash_video(tmp / "video.mp4", duration_s=3.0, flash_times_s=[1.5])
    provider = _RecordingProvider()
    clip = make_click_wav(tmp / "assets" / "seed.wav", duration_s=0.5, click_duration_s=0.1)
    event = SoundFXEvent(
        event_id="event_001",
        start_time=1.4,
        end_time=2.0,
        event_description="white flash impact",
        sound_event_local_path=str(clip),
        confidence=0.9,
        event_type="IMPACT",
        variants=[SfxVariant(path=str(clip))],
        selected_variant=0,
    )
    project = SfxProject(
        project_dir=str(tmp / "project"),
        video_path=str(video),
        video_duration=3.0,
        events=[event],
        mix=MixSettings(preserve_original_audio=False),
    )
    return project, provider


class SfxProjectEditorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _editor(self) -> tuple[SfxProjectEditor, _RecordingProvider]:
        project, provider = _make_project(self.tmp)
        editor = SfxProjectEditor(
            project,
            generation_stage=SoundEffectGenerationStage(sound_effect_generation_model=provider),
        )
        return editor, provider

    def test_regenerate_event_adds_and_selects_new_variant(self) -> None:
        editor, provider = self._editor()
        event = asyncio.run(
            editor.regenerate_event("event_001", prompt="heavier metallic impact", num_variants=2)
        )
        self.assertEqual(len(event.variant_paths), 3)  # seed + 2 new
        self.assertEqual(event.selected_variant, 2)
        self.assertEqual(event.sound_prompt, "heavier metallic impact")
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(provider.calls[0]["prompt"], "heavier metallic impact")

    def test_select_variant_switches_active_audio(self) -> None:
        editor, _ = self._editor()
        asyncio.run(editor.regenerate_event("event_001", num_variants=1))
        event = editor.select_variant("event_001", 0)
        self.assertEqual(event.active_audio_path, event.variant_paths[0])

    def test_retime_and_mute(self) -> None:
        editor, _ = self._editor()
        event = editor.retime_event("event_001", start_time=0.5, end_time=1.2)
        self.assertAlmostEqual(event.start_time, 0.5)
        self.assertAlmostEqual(event.end_time, 1.2)
        self.assertIsNone(event.refined_start_time)
        editor.set_event_muted("event_001", True)
        self.assertTrue(editor.project.event_by_id("event_001").muted)

    def test_add_and_remove_event(self) -> None:
        editor, provider = self._editor()
        event = asyncio.run(
            editor.add_event(
                start_time=0.2,
                end_time=0.8,
                description="whoosh at open",
                sound_prompt="fast airy whoosh",
            )
        )
        self.assertEqual(event.source, "user")
        self.assertTrue(event.variant_paths)
        self.assertEqual(provider.calls[-1]["prompt"], "fast airy whoosh")
        editor.remove_event(event.event_id)
        with self.assertRaises(KeyError):
            editor.project.event_by_id(event.event_id)

    def test_render_produces_new_revision_and_persists(self) -> None:
        editor, _ = self._editor()
        result = editor.render()
        self.assertTrue(result.final_video_path.exists())
        self.assertTrue(result.mixed_audio_path.exists())
        self.assertEqual(editor.project.revision, 1)

        reloaded = SfxProject.load(Path(editor.project.project_dir))
        self.assertEqual(reloaded.revision, 1)
        self.assertEqual(reloaded.final_video_path, str(result.final_video_path))

        result2 = editor.render()
        self.assertEqual(editor.project.revision, 2)
        self.assertNotEqual(str(result.final_video_path), str(result2.final_video_path))

    def test_ambience_toggle_without_regeneration(self) -> None:
        editor, provider = self._editor()
        bed_clip = make_click_wav(self.tmp / "assets" / "bed.wav", duration_s=1.0, click_duration_s=1.0)
        editor.project.ambience = AmbienceBed(
            prompt="forest birds", audio_path=str(bed_clip), enabled=True
        )
        asyncio.run(editor.set_ambience(enabled=False, gain_db=-12))
        self.assertFalse(editor.project.ambience.enabled)
        self.assertAlmostEqual(editor.project.ambience.gain_db, -12.0)
        self.assertEqual(provider.calls, [])  # toggling must not regenerate

    def test_apply_ops_drives_same_edits(self) -> None:
        editor, _ = self._editor()
        asyncio.run(
            editor.apply_ops(
                [
                    {"op": "set_event_gain", "event_id": "event_001", "gain_db": -6},
                    {"op": "retime_event", "event_id": "event_001", "start_time": 1.0},
                    {"op": "regenerate_event", "event_id": "event_001", "num_variants": 1},
                    {"op": "set_mix", "sfx_master_gain_db": 3.0},
                ]
            )
        )
        event = editor.project.event_by_id("event_001")
        self.assertAlmostEqual(event.gain_db, -6.0)
        self.assertAlmostEqual(event.start_time, 1.0)
        self.assertAlmostEqual(editor.project.mix.sfx_master_gain_db, 3.0)

    def test_unknown_op_raises(self) -> None:
        editor, _ = self._editor()
        with self.assertRaises(ValueError):
            asyncio.run(editor.apply_ops([{"op": "definitely_not_an_op"}]))


class SfxProjectRoundTripTests(unittest.TestCase):
    def test_project_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            project, _ = _make_project(tmp)
            project.ambience = AmbienceBed(prompt="room tone", gain_db=-9.0)
            saved = project.save()
            self.assertTrue(saved.exists())

            reloaded = SfxProject.load(saved)
            self.assertEqual(reloaded.video_path, project.video_path)
            self.assertEqual(len(reloaded.events), 1)
            event = reloaded.events[0]
            self.assertEqual(event.event_id, "event_001")
            self.assertEqual(event.event_type, "IMPACT")
            self.assertEqual(event.variant_paths, project.events[0].variant_paths)
            self.assertEqual(reloaded.ambience.prompt, "room tone")
            self.assertFalse(reloaded.mix.preserve_original_audio)

    def test_v1_event_payload_still_loads(self) -> None:
        payload = {
            "event_id": "event_1",
            "start_time": 0.5,
            "end_time": 1.0,
            "event_description": "old-style event",
            "sound_event_local_path": "",
            "confidence": 0.5,
        }
        event = SoundFXEvent.from_dict(payload)
        self.assertEqual(event.event_id, "event_1")
        self.assertEqual(event.variant_paths, [])
        self.assertEqual(event.active_audio_path, "")
        self.assertAlmostEqual(event.effective_start, 0.5)


if __name__ == "__main__":
    unittest.main()
