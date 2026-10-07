from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional

import numpy as np

from EdennCode.ModelFactory.VideoSFXModelFactory.VideoConditionedGen.base import (
    VideoConditionedSfxProvider,
)
from EdennCode.Util.MediaUtils.sfx_timeline import SfxTimelineClip, render_sfx_timeline_wav
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.Stages.SoundEffectGenerationStage.sound_effect_generation_stage import (
    SoundEffectGenerationStage,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.VideoSoundEffectDataModel.datamodel import (
    ROUTE_VIDEO_NATIVE,
    SfxProject,
    SfxSuggestion,
    SoundFXEvent,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.proposers import (
    propose_from_engine_track,
    propose_stylistic,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.sfx_project_editor import SfxProjectEditor
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.tests._synthetic_media import (
    make_click_wav,
    make_flash_video,
    read_wav,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.tests.test_sfx_project_editor import (
    _RecordingProvider,
    _make_project,
)


class _FakeVideoProvider(VideoConditionedSfxProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls: List[dict] = []

    async def generate_for_video(
        self,
        *,
        video_url: str,
        output_stem: str,
        output_dir: Path,
        start_offset_s: float = 0.0,
        duration_s: Optional[float] = None,
        prompt: Optional[str] = None,
        num_samples: int = 1,
        sample_rate: int = 44100,
    ) -> list[Path]:
        self.calls.append(
            {"video_url": video_url, "start": start_offset_s, "duration": duration_s, "prompt": prompt}
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        return [
            make_click_wav(
                output_dir / f"{output_stem}_v{i}.wav",
                duration_s=max(0.3, min(duration_s or 1.0, 2.0)),
                click_duration_s=0.2,
                sample_rate=sample_rate,
            )
            for i in range(num_samples)
        ]


class VideoNativeRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _editor(self):
        project, text_provider = _make_project(self.tmp)
        project.video_url = "https://example.test/video.mp4"
        video_provider = _FakeVideoProvider()
        editor = SfxProjectEditor(
            project,
            generation_stage=SoundEffectGenerationStage(
                sound_effect_generation_model=text_provider,
                video_conditioned_model=video_provider,
            ),
        )
        return editor, text_provider, video_provider

    def test_regenerate_event_via_video_native_route(self) -> None:
        editor, text_provider, video_provider = self._editor()
        event = asyncio.run(
            editor.regenerate_event("event_001", route=ROUTE_VIDEO_NATIVE, num_variants=1)
        )
        self.assertEqual(len(video_provider.calls), 1)
        self.assertEqual(text_provider.calls, [])  # routed away from text
        call = video_provider.calls[0]
        self.assertEqual(call["video_url"], "https://example.test/video.mp4")
        self.assertAlmostEqual(call["start"], event.effective_start)
        # Provenance: newest variant is tagged with its route.
        self.assertEqual(event.variants[-1].route, ROUTE_VIDEO_NATIVE)

    def test_video_native_bed_generation(self) -> None:
        editor, _, video_provider = self._editor()
        asyncio.run(
            editor.set_ambience(prompt="score the footage", route=ROUTE_VIDEO_NATIVE, regenerate=True)
        )
        ambience = editor.project.ambience
        self.assertEqual(len(video_provider.calls), 1)
        self.assertFalse(ambience.loop)  # native bed spans the video, no tiling
        self.assertTrue(Path(ambience.active_audio_path).exists())

    def test_bed_falls_back_to_text_without_engine(self) -> None:
        project, text_provider = _make_project(self.tmp)
        stage = SoundEffectGenerationStage(
            sound_effect_generation_model=text_provider, video_conditioned_model=None
        )
        from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.VideoSoundEffectDataModel.datamodel import (
            AmbienceBed,
        )

        ambience = AmbienceBed(prompt="room tone", route=ROUTE_VIDEO_NATIVE)
        result = asyncio.run(
            stage.generate_ambience(
                ambience=ambience,
                output_directory=self.tmp / "assets",
                duration_seconds=10.0,
                video_url="",
            )
        )
        self.assertEqual(result.route, "text")  # downgraded, not failed
        self.assertTrue(result.active_audio_path)


class DuckRenderingTests(unittest.TestCase):
    def test_bed_is_attenuated_inside_duck_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bed = make_click_wav(tmp / "bed.wav", duration_s=2.0, click_duration_s=2.0, amplitude=0.5)
            out = render_sfx_timeline_wav(
                [
                    SfxTimelineClip(
                        audio_path=bed,
                        start_s=0.0,
                        fade_in_s=0.0,
                        fade_out_s=0.0,
                        duck_windows=[(0.8, 1.2)],
                        duck_db=-12.0,
                    )
                ],
                tmp / "timeline.wav",
                total_duration_s=2.0,
                sample_rate=44_100,
            )
            samples, rate, _ = read_wav(out)
            mono = samples[:, 0]
            outside = np.abs(mono[int(0.3 * rate): int(0.6 * rate)]).max()
            inside = np.abs(mono[int(0.95 * rate): int(1.05 * rate)]).max()
            self.assertLess(inside, outside * 0.35)  # ~-12dB ≈ 0.25x


class ProposerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _project_with_cuts(self) -> SfxProject:
        # Three hard cuts via flashes? Cuts need content changes — flash video
        # gives detectable luminance cuts at flash boundaries.
        video = make_flash_video(
            self.tmp / "cuts.mp4", duration_s=6.0, flash_times_s=[2.0, 4.0], flash_duration_s=1.0
        )
        return SfxProject(
            project_dir=str(self.tmp / "project"),
            video_path=str(video),
            video_duration=6.0,
        )

    def test_stylistic_proposer_creates_pending_suggestions(self) -> None:
        project = self._project_with_cuts()
        suggestions = propose_stylistic(project, style="vlog")
        self.assertTrue(suggestions, "expected cut suggestions on synthetic cuts")
        for s in suggestions:
            self.assertEqual(s.status, "pending")
            self.assertEqual(s.origin, "stylistic")
            self.assertEqual(s.timing_authority, "cut_snap")
            self.assertTrue(0 <= s.start_time <= 6.0)
        self.assertEqual(len(project.suggestions), len(suggestions))

    def test_clean_style_proposes_nothing(self) -> None:
        project = self._project_with_cuts()
        self.assertEqual(propose_stylistic(project, style="clean"), [])

    def test_engine_track_mining_skips_covered_moments(self) -> None:
        project = SfxProject(
            project_dir=str(self.tmp / "p"),
            video_path="unused.mp4",
            video_duration=10.0,
            events=[
                SoundFXEvent(
                    event_id="event_001",
                    start_time=2.0,
                    end_time=2.5,
                    event_description="covered",
                    sound_event_local_path="x.wav",
                )
            ],
        )
        suggestions = propose_from_engine_track(project, onset_times=[2.1, 5.0, 5.2, 9.0])
        starts = [s.start_time for s in suggestions]
        self.assertNotIn(2.1, starts)  # covered by event_001
        self.assertIn(5.0, starts)
        self.assertNotIn(5.2, starts)  # within tolerance of accepted 5.0
        self.assertIn(9.0, starts)

    def test_accept_and_reject_suggestion_ops(self) -> None:
        project, provider = _make_project(self.tmp)
        project.suggestions.append(
            SfxSuggestion(
                suggestion_id="suggestion_001",
                origin="narrative",
                start_time=0.5,
                end_time=1.2,
                description="distant hawk",
                sound_prompt="distant hawk cry",
                rationale="wide vista implies open air",
            )
        )
        project.suggestions.append(
            SfxSuggestion(
                suggestion_id="suggestion_002",
                origin="stylistic",
                start_time=2.0,
                end_time=2.5,
                description="whoosh",
                sound_prompt="airy whoosh",
            )
        )
        editor = SfxProjectEditor(
            project,
            generation_stage=SoundEffectGenerationStage(
                sound_effect_generation_model=provider, video_conditioned_model=None
            ),
        )
        event = asyncio.run(editor.accept_suggestion("suggestion_001"))
        self.assertEqual(event.origin, "narrative")
        self.assertEqual(project.suggestion_by_id("suggestion_001").status, "accepted")
        self.assertEqual(provider.calls[-1]["prompt"], "distant hawk cry")

        editor.reject_suggestion("suggestion_002")
        self.assertEqual(project.suggestion_by_id("suggestion_002").status, "rejected")
        with self.assertRaises(ValueError):
            asyncio.run(editor.accept_suggestion("suggestion_002"))

        # Round-trip: suggestions persist with status.
        project.save()
        reloaded = SfxProject.load(Path(project.project_dir))
        self.assertEqual(len(reloaded.suggestions), 2)
        self.assertEqual(reloaded.suggestion_by_id("suggestion_001").status, "accepted")


if __name__ == "__main__":
    unittest.main()
