import unittest

from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_a_compose import (
    ProviderALyrics,
)


class ProviderAComposePayloadTests(unittest.TestCase):
    def test_build_payload_uses_composition_plan_mode_without_prompt_fields(self) -> None:
        payload = ProviderALyrics._build_payload(
            prompt="ignored",
            music_length_ms=30000,
            with_timestamps=True,
            extra={
                "composition_plan": {"sections": []},
                "respect_sections_durations": False,
                "song_metadata": {"title": "ignored"},
            },
        )

        self.assertNotIn("prompt", payload)
        self.assertNotIn("music_length_ms", payload)
        self.assertNotIn("song_metadata", payload)
        self.assertEqual(payload["composition_plan"], {"sections": []})
        self.assertFalse(payload["respect_sections_durations"])
        self.assertTrue(payload["with_timestamps"])

    def test_build_payload_uses_prompt_mode_without_composition_plan(self) -> None:
        payload = ProviderALyrics._build_payload(
            prompt="bright pop",
            music_length_ms=30000,
            with_timestamps=False,
            extra=None,
        )

        self.assertEqual(payload["prompt"], "bright pop")
        self.assertEqual(payload["music_length_ms"], 30000)
        self.assertFalse(payload["with_timestamps"])
