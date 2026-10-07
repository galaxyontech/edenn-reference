import unittest

from EdennCode.ModelFactory.PromptFactory.video_to_sound_effect_prompt import (
    VideoToSoundEffectPrompt,
)
from EdennCode.ModelFactory.PromptFactory.video_to_sound_effect_schema import (
    VideoToSoundEffectSchemas,
)


class VideoToSoundEffectPromptTests(unittest.TestCase):
    def test_prompt_and_schema_contract(self) -> None:
        payload = VideoToSoundEffectPrompt.build_input(
            video_url="https://example.com/video.mp4",
            duration=8.5,
            user_prompt="Add soft whooshes on transitions",
        )
        self.assertEqual(payload[1]["content"][-1]["input"]["url"], "https://example.com/video.mp4")

        schema = VideoToSoundEffectSchemas.event_response()
        self.assertEqual(schema["schema"]["required"], ["events"])
        item_required = schema["schema"]["properties"]["events"]["items"]["required"]
        self.assertIn("event_description", item_required)
        self.assertNotIn("event_descriptions", item_required)


if __name__ == "__main__":
    unittest.main()
