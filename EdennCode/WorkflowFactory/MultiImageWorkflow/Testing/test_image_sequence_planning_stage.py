import tempfile
import unittest
from pathlib import Path

from EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.ImageSequencePlanningStage.image_sequence_planning_stage import (
    ImageSequencePlanningStage,
    ImageSequencePlanningStageInput,
)


class _FakeLLMClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.messages = None

    async def complete_messages(self, messages, json_schema=None):
        self.messages = messages
        return self.payload


class ImageSequencePlanningStageTests(unittest.IsolatedAsyncioTestCase):
    async def test_stage_builds_section_plan_and_legacy_metadata(self) -> None:
        payload = {
            "video_title": "Glow Reveal for Launch Night Celebration Storyboard",
            "video_description": "A bright product reveal that builds into a joyful payoff.",
            "image_order": [2, 1, 3],
            "storyline_summary": "A product reveal grows into a celebratory payoff.",
            "image_beats": [
                {
                    "image_index": 1,
                    "label": "Reveal",
                    "role": "build",
                    "emotion": "anticipation",
                    "description": "The product enters frame.",
                    "transition_hint": "push in",
                },
                {
                    "image_index": 2,
                    "label": "Hook",
                    "role": "setup",
                    "emotion": "curious",
                    "description": "The customer notices the scene.",
                    "transition_hint": "quick cut",
                },
                {
                    "image_index": 3,
                    "label": "Payoff",
                    "role": "resolve",
                    "emotion": "joyful",
                    "description": "The final lifestyle payoff lands.",
                    "transition_hint": "hold",
                },
            ],
            "overall_mood": "uplifting",
            "target_bpm": 118,
            "primary_instruments": ["piano", "claps"],
            "music_sections": [
                {
                    "section_id": "intro",
                    "label": "Intro",
                    "image_indices": [2, 1],
                    "objective": "Set up and build interest",
                    "energy_start": 0.2,
                    "energy_end": 0.6,
                    "instrumentation_focus": ["piano"],
                    "lyric_lines": ["Step into the light"],
                },
                {
                    "section_id": "resolve",
                    "label": "Resolve",
                    "image_indices": [3],
                    "objective": "Land the payoff",
                    "energy_start": 0.6,
                    "energy_end": 0.8,
                    "instrumentation_focus": ["claps"],
                    "lyric_lines": ["Feel the moment rise"],
                },
            ],
            "music_prompt_summary": "Uplifting piano pop with a rising payoff.",
        }

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            images = []
            for idx in range(1, 4):
                image_path = base / f"frame_{idx}.png"
                image_path.write_bytes(b"png")
                images.append(image_path)

            stage = ImageSequencePlanningStage(llm_client=_FakeLLMClient(payload))
            result = await stage.run(
                ImageSequencePlanningStageInput(
                    image_paths=images,
                    default_image_duration_s=2.5,
                )
            )

        self.assertEqual([path.name for path in result.ordered_images], ["frame_2.png", "frame_1.png", "frame_3.png"])
        self.assertEqual(result.section_plan.total_duration_s, 7.5)
        self.assertEqual(result.plan["video_title"], "Glow Reveal for Launch Night Celebration Storyboard")
        self.assertEqual(result.plan["video_description"], "A bright product reveal that builds into a joyful payoff.")
        self.assertEqual(result.plan["music_prompt"], "Uplifting piano pop with a rising payoff.")
        self.assertEqual(result.plan["recommended_mood"], "uplifting")
        self.assertEqual(result.section_plan.section_image_counts(), [2, 1])

    async def test_fixed_image_order_overrides_planner_reordering(self) -> None:
        """image_order='fixed': the user's upload order is authoritative even
        when the planner returns a different narrative order, and the prompt
        tells the planner the order is fixed."""
        payload = {
            "video_title": "Fixed Order",
            "video_description": "d",
            "image_order": [3, 1, 2],  # planner tries to reorder — must be ignored
            "storyline_summary": "s",
            "overall_mood": "calm",
            "target_bpm": 100,
            "primary_instruments": ["piano"],
            "music_sections": [
                {
                    "section_id": "all",
                    "label": "All",
                    "image_indices": [1, 2, 3],
                    "objective": "o",
                    "energy_start": 0.3,
                    "energy_end": 0.6,
                    "instrumentation_focus": ["piano"],
                    "lyric_lines": ["line"],
                }
            ],
            "music_prompt_summary": "p",
        }
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            images = []
            for idx in range(1, 4):
                image_path = base / f"frame_{idx}.png"
                image_path.write_bytes(b"png")
                images.append(image_path)

            client = _FakeLLMClient(payload)
            stage = ImageSequencePlanningStage(llm_client=client)
            result = await stage.run(
                ImageSequencePlanningStageInput(
                    image_paths=images,
                    fixed_image_order=True,
                )
            )

        self.assertEqual(
            [path.name for path in result.ordered_images],
            ["frame_1.png", "frame_2.png", "frame_3.png"],  # upload order kept
        )
        self.assertEqual(result.plan["image_order"], [1, 2, 3])
        prompt_text = client.messages[0]["content"][0]["text"]
        self.assertIn("FIXED", prompt_text)

    async def test_stage_normalizes_video_title_to_short_form(self) -> None:
        payload = {
            "video_title": "This title is much too long for the slideshow output response contract",
            "video_description": "Short description.",
            "image_order": [1],
            "storyline_summary": "Single frame story.",
            "image_beats": [
                {
                    "image_index": 1,
                    "label": "Hook",
                    "role": "setup",
                    "emotion": "curious",
                    "description": "Single image.",
                    "transition_hint": "hold",
                }
            ],
            "overall_mood": "uplifting",
            "target_bpm": 118,
            "primary_instruments": ["piano"],
            "music_sections": [
                {
                    "section_id": "intro",
                    "label": "Intro",
                    "image_indices": [1],
                    "objective": "Set up",
                    "energy_start": 0.2,
                    "energy_end": 0.4,
                    "instrumentation_focus": ["piano"],
                    "lyric_lines": [],
                }
            ],
            "music_prompt_summary": "Simple piano mood.",
        }

        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "frame_1.png"
            image_path.write_bytes(b"png")
            stage = ImageSequencePlanningStage(llm_client=_FakeLLMClient(payload))
            result = await stage.run(ImageSequencePlanningStageInput(image_paths=[image_path]))

        self.assertEqual(result.plan["video_title"], "This title is much too long for the")
