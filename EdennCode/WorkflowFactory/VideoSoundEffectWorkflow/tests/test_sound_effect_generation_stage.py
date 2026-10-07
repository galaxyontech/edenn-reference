from __future__ import annotations

import tempfile
import unittest
import wave
import asyncio
from pathlib import Path

from EdennCode.ModelFactory.VideoSFXModelFactory.CloudSoundEffectGen.base import (
    SoundEffectProvider,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.Stages.SoundEffectGenerationStage.sound_effect_generation_stage import (
    SoundEffectGenerationStage,
    SoundEffectGenerationStageInput,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.VideoSoundEffectDataModel.datamodel import (
    SoundFXEvent,
)


class _FakeSoundEffectProvider(SoundEffectProvider):
    async def generate(
        self,
        *,
        prompt: str,
        duration_seconds: float,
        output_stem: str,
        output_dir: Path,
        sample_rate: int = 44100,
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        wav_path = output_dir / f"{output_stem}.wav"
        nframes = max(1, int(duration_seconds * sample_rate))
        silence = b"\x00\x00" * nframes
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(silence)
        return wav_path


class SoundEffectGenerationStageTests(unittest.TestCase):
    def test_generates_paths_for_each_event(self) -> None:
        provider = _FakeSoundEffectProvider()
        stage = SoundEffectGenerationStage(sound_effect_generation_model=provider)
        with tempfile.TemporaryDirectory() as tmp:
            output = asyncio.run(
                stage.run(
                    SoundEffectGenerationStageInput(
                        list_of_generation_packages=[
                            SoundFXEvent(
                                event_id="evt_1",
                                start_time=0.3,
                                end_time=1.0,
                                event_description="short whoosh",
                                sound_event_local_path="",
                            ),
                            SoundFXEvent(
                                event_id="evt_2",
                                start_time=2.5,
                                end_time=3.3,
                                event_description="impact hit",
                                sound_event_local_path="",
                            ),
                        ],
                        output_directory=Path(tmp),
                    )
                )
            )
            self.assertEqual(len(output.generated_sound_events), 2)
            for event in output.generated_sound_events:
                self.assertTrue(event.sound_event_local_path.endswith(".wav"))
                self.assertTrue(Path(event.sound_event_local_path).exists())


if __name__ == "__main__":
    unittest.main()
