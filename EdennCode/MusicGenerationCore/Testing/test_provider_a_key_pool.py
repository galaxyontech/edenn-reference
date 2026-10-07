import os
import unittest
from unittest.mock import MagicMock, patch

from EdennCode.exceptions import EdennConfigurationError
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.cloud_music_gen_util import (
    build_music_provider,
)
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_a_compose import (
    ProviderALyrics,
)


class ProviderAKeyPoolTests(unittest.TestCase):
    def test_build_music_provider_passes_primary_and_numbered_keys(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PROVIDER_A_API_KEY": "primary-key",
                "PROVIDER_A_API_KEY_2": "second-key",
                "PROVIDER_A_API_KEY_9": "ninth-key",
                "PROVIDER_A_TIMEOUT": "17",
            },
            clear=True,
        ), patch(
            "EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.cloud_music_gen_util.ProviderALyrics",
            return_value=MagicMock(),
        ) as provider_cls:
            build_music_provider()

        provider_cls.assert_called_once()
        self.assertEqual(
            provider_cls.call_args.kwargs["api_keys"],
            [
                ("PROVIDER_A_API_KEY", "primary-key"),
                ("PROVIDER_A_API_KEY_2", "second-key"),
                ("PROVIDER_A_API_KEY_9", "ninth-key"),
            ],
        )
        self.assertEqual(provider_cls.call_args.kwargs["timeout"], 17)

    def test_build_music_provider_requires_some_key(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(EdennConfigurationError) as exc:
                build_music_provider()

        self.assertIn("PROVIDER_A_API_KEY", str(exc.exception))
        self.assertIn("PROVIDER_A_API_KEY_1..N", str(exc.exception))

    def test_provider_a_client_rotates_keys_round_robin(self) -> None:
        provider = ProviderALyrics(
            api_keys=[
                ("PROVIDER_A_API_KEY", "primary-key"),
                ("PROVIDER_A_API_KEY_2", "second-key"),
            ],
        )

        selected = [provider._select_api_key().label for _ in range(4)]

        self.assertEqual(
            selected,
            [
                "PROVIDER_A_API_KEY",
                "PROVIDER_A_API_KEY_2",
                "PROVIDER_A_API_KEY",
                "PROVIDER_A_API_KEY_2",
            ],
        )


if __name__ == "__main__":
    unittest.main()
