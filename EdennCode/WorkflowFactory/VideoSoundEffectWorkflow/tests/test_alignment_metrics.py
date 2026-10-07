from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.VideoSoundEffectDataModel.datamodel import (
    SfxVariant,
    MixSettings,
    SfxProject,
    SoundFXEvent,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.eval.alignment_metrics import (
    LabeledEvent,
    audio_onset_time,
    measure_event_detection,
    measure_project_alignment,
    temporal_iou,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.tests._synthetic_media import (
    make_click_wav,
    make_flash_video,
)


class AudioOnsetTests(unittest.TestCase):
    def test_onset_found_at_click_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clip = make_click_wav(
                Path(tmp) / "delayed.wav", duration_s=1.0, click_at_s=0.3, click_duration_s=0.1
            )
            onset = audio_onset_time(clip)
            self.assertIsNotNone(onset)
            self.assertAlmostEqual(onset, 0.3, delta=0.03)

    def test_silence_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clip = make_click_wav(
                Path(tmp) / "silent.wav", duration_s=0.5, click_duration_s=0.0, amplitude=0.0
            )
            self.assertIsNone(audio_onset_time(clip))


class ProjectAlignmentTests(unittest.TestCase):
    def test_perfectly_placed_event_scores_near_zero_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            video = make_flash_video(tmp / "video.mp4", duration_s=3.0, flash_times_s=[1.5])
            clip = make_click_wav(tmp / "clip.wav", duration_s=0.4, click_duration_s=0.1)
            event = SoundFXEvent(
                event_id="event_001",
                start_time=1.5,
                end_time=2.0,
                event_description="flash impact",
                sound_event_local_path=str(clip),
                event_type="IMPACT",
                variants=[SfxVariant(path=str(clip))],
                selected_variant=0,
            )
            project = SfxProject(
                project_dir=str(tmp / "project"),
                video_path=str(video),
                video_duration=3.0,
                events=[event],
                mix=MixSettings(),
            )
            report = measure_project_alignment(project)
            self.assertEqual(report.measured_events, 1)
            self.assertLessEqual(report.mean_abs_error_s, 0.15)
            self.assertGreaterEqual(report.within_tolerance["250ms"], 1.0)

    def test_misplaced_event_scores_large_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            video = make_flash_video(tmp / "video.mp4", duration_s=4.0, flash_times_s=[1.5])
            clip = make_click_wav(tmp / "clip.wav", duration_s=0.4, click_duration_s=0.1)
            event = SoundFXEvent(
                event_id="event_001",
                start_time=2.1,  # 600ms late
                end_time=2.6,
                event_description="flash impact",
                sound_event_local_path=str(clip),
                event_type="IMPACT",
            )
            project = SfxProject(
                project_dir=str(tmp / "project"),
                video_path=str(video),
                video_duration=4.0,
                events=[event],
            )
            report = measure_project_alignment(project)
            self.assertEqual(report.measured_events, 1)
            self.assertGreaterEqual(report.mean_abs_error_s, 0.4)
            self.assertEqual(report.within_tolerance["100ms"], 0.0)


class DetectionMetricTests(unittest.TestCase):
    def test_temporal_iou(self) -> None:
        self.assertAlmostEqual(temporal_iou(0, 1, 0, 1), 1.0)
        self.assertAlmostEqual(temporal_iou(0, 1, 0.5, 1.5), 1.0 / 3.0, places=4)
        self.assertAlmostEqual(temporal_iou(0, 1, 2, 3), 0.0)

    def _event(self, event_id: str, start: float, end: float) -> SoundFXEvent:
        return SoundFXEvent(
            event_id=event_id,
            start_time=start,
            end_time=end,
            event_description="",
            sound_event_local_path="",
        )

    def test_greedy_matching_and_f1(self) -> None:
        predicted = [self._event("a", 1.0, 2.0), self._event("b", 5.0, 6.0), self._event("c", 8.0, 8.5)]
        labels = [
            LabeledEvent(start_time=1.1, end_time=2.1),
            LabeledEvent(start_time=5.2, end_time=6.2),
        ]
        report = measure_event_detection(predicted, labels)
        self.assertEqual(len(report.matches), 2)
        stats = report.per_threshold["iou_0.5"]
        self.assertEqual(stats["true_positives"], 2)
        self.assertAlmostEqual(stats["recall"], 1.0)
        self.assertAlmostEqual(stats["precision"], 2 / 3, places=4)
        self.assertLessEqual(report.mean_onset_error_s, 0.21)

    def test_one_to_one_matching(self) -> None:
        # Two predictions overlapping one label: only one may match.
        predicted = [self._event("a", 1.0, 2.0), self._event("b", 1.05, 2.05)]
        labels = [LabeledEvent(start_time=1.0, end_time=2.0)]
        report = measure_event_detection(predicted, labels)
        self.assertEqual(len(report.matches), 1)
        self.assertEqual(report.matches[0]["pred_event_id"], "a")


if __name__ == "__main__":
    unittest.main()
