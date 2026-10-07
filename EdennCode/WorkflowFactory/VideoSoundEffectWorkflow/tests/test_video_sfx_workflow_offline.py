from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Dict, Optional

from EdennCode.ModelFactory.VideoSFXModelFactory.CloudSoundEffectGen.base import (
    SoundEffectProvider,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.Stages.SoundEffectGenerationStage.sound_effect_generation_stage import (
    SoundEffectGenerationStage,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.Stages.VideoEventAnalysisStage.video_event_analysis_stage import (
    VideoEventAnalysisStage,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.VideoSoundEffectDataModel.datamodel import (
    SfxProject,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.video_sound_effect_workflow import (
    VideoSfxWorkflowOptions,
    VideoSoundEffectWorkflowE2E,
    VideoSoundEffectWorkflowE2EInput,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.tests._synthetic_media import (
    make_click_wav,
    make_flash_video,
)


class _StubAnalysisProvider:
    """Stands in for the multimodal video-analysis provider — no network."""

    def __init__(self, payload: Dict) -> None:
        self.payload = payload
        self.v2_calls = 0

    async def analyze_v2(
        self, *, uploaded_url: str, duration: float, user_prompt: Optional[str] = None
    ) -> Dict:
        self.v2_calls += 1
        return self.payload

    async def analyze_events(self, **kwargs) -> Dict:
        return {"events": self.payload.get("events", [])}


class _FakeSfxProvider(SoundEffectProvider):
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
        return make_click_wav(
            output_dir / f"{output_stem}.wav",
            duration_s=max(0.2, min(duration_seconds, 2.0)),
            click_duration_s=0.1,
            sample_rate=sample_rate,
        )


class VideoSfxWorkflowOfflineE2ETests(unittest.TestCase):
    """Full workflow run with stubbed model calls on synthetic media."""

    def test_end_to_end_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            video = make_flash_video(
                tmp / "video.mp4",
                duration_s=4.0,
                flash_times_s=[1.5, 3.0],
                with_silent_audio=True,
            )
            analysis_payload = {
                "scene_summary": "Synthetic flash test video.",
                "ambience_description": "soft room tone, airy hum",
                "events": [
                    {
                        "event_id": "x",
                        "event_type": "IMPACT",
                        "event_description": "first white flash",
                        "sound_prompt": "sharp bright impact hit",
                        "start_timestamp": 1.2,  # deliberately ~0.3s early
                        "end_timestamp": 1.9,
                        "confidence": 0.9,
                    },
                    {
                        "event_id": "y",
                        "event_type": "IMPACT",
                        "event_description": "second white flash",
                        "sound_prompt": "deep boom impact",
                        "start_timestamp": 2.9,
                        "end_timestamp": 3.4,
                        "confidence": 0.8,
                    },
                    {
                        "event_id": "z",
                        "event_type": "IMPACT",
                        "event_description": "out of range event",
                        "sound_prompt": "should be clamped",
                        "start_timestamp": 9.0,
                        "end_timestamp": 10.0,
                        "confidence": 0.9,
                    },
                ],
            }
            workflow = VideoSoundEffectWorkflowE2E(
                video_event_analysis_stage=VideoEventAnalysisStage(
                    provider=_StubAnalysisProvider(analysis_payload)
                ),
                sound_effect_generation_stage=SoundEffectGenerationStage(
                    sound_effect_generation_model=_FakeSfxProvider(),
                    video_conditioned_model=None,  # offline: text bed fallback path
                ),
            )
            output = asyncio.run(
                workflow.execute(
                    VideoSoundEffectWorkflowE2EInput(
                        video_path=str(video),
                        uploaded_public_facing_url="https://example.invalid/video.mp4",
                        user_prompt=None,  # avoids the prompt-understanding LLM call
                        run_dir=str(tmp / "run"),
                        options=VideoSfxWorkflowOptions(num_variants=2),
                    )
                )
            )

            # Outputs exist and are H.264-muxed mp4s.
            self.assertTrue(output.final_video_path.exists())
            self.assertTrue(output.mixed_audio_path.exists())
            self.assertTrue(Path(output.project.sfx_only_video_path).exists())

            # Out-of-range event was dropped, valid ones kept and generated.
            self.assertEqual(len(output.generated_sound_events), 2)
            for event in output.generated_sound_events:
                self.assertEqual(len(event.variant_paths), 2)
                self.assertTrue(Path(event.active_audio_path).exists())

            # Timing refinement snapped the early first event toward the flash.
            first = output.generated_sound_events[0]
            self.assertIsNotNone(first.refined_start_time)
            self.assertAlmostEqual(first.refined_start_time, 1.5, delta=0.2)

            # Ambience bed generated from the analysis description.
            self.assertIsNotNone(output.project.ambience)
            self.assertTrue(Path(output.project.ambience.active_audio_path).exists())

            # Project persisted, loadable, and consistent.
            project_file = Path(output.project.project_dir) / "sfx_project.json"
            self.assertTrue(project_file.exists())
            reloaded = SfxProject.load(project_file)
            self.assertEqual(len(reloaded.events), 2)
            self.assertEqual(reloaded.revision, 1)
            payload = json.loads(project_file.read_text())
            self.assertEqual(payload["schema_version"], 3)


if __name__ == "__main__":
    unittest.main()
