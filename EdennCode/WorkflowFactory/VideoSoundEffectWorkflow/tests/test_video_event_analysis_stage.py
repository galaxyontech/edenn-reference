import unittest

from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.Stages.VideoEventAnalysisStage.video_event_analysis_stage import (
    VideoEventAnalysisStage,
)


class VideoEventAnalysisStageTests(unittest.TestCase):
    def test_normalize_events_clamps_and_supports_description_alias(self) -> None:
        raw_events = [
            {
                "event_id": "evt_1",
                "event_description": "camera whoosh",
                "event_type": "transition",
                "sound_prompt": "airy whoosh",
                "start_timestamp": 0.0,
                "end_timestamp": 1.2,
                "confidence": 0.9,
            },
            {
                "event_id": "evt_2",
                "event_description": "inverted range gets repaired",
                "start_timestamp": 5.0,
                "end_timestamp": 4.0,
                "confidence": 0.8,
            },
            {
                "event_id": "evt_3",
                "event_descriptions": "impact hit",
                "start_timestamp": 8.9,
                "end_timestamp": 12.0,
                "confidence": 0.7,
            },
            {
                "event_id": "evt_4",
                "event_description": "fully out of range gets dropped",
                "start_timestamp": 11.0,
                "end_timestamp": 12.0,
                "confidence": 0.9,
            },
        ]
        events = VideoEventAnalysisStage._normalize_events(raw_events, duration=10.0)
        self.assertEqual(len(events), 3)

        # Stable renumbered ids, sorted by start time.
        self.assertEqual([e.event_id for e in events], ["event_001", "event_002", "event_003"])

        self.assertEqual(events[0].start_time, 0.0)
        self.assertAlmostEqual(events[0].end_time, 1.2, places=3)
        self.assertEqual(events[0].event_type, "TRANSITION")
        self.assertEqual(events[0].sound_prompt, "airy whoosh")

        # Inverted range repaired to a short forward window.
        self.assertAlmostEqual(events[1].start_time, 5.0, places=3)
        self.assertGreater(events[1].end_time, events[1].start_time)

        # Description alias honored; end clamped to the video duration.
        self.assertEqual(events[2].event_description, "impact hit")
        self.assertAlmostEqual(events[2].end_time, 10.0, places=3)


if __name__ == "__main__":
    unittest.main()
