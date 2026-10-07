import os
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from EdennCode.exceptions import EdennConfigurationError
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.cloud_music_gen_util import (
    build_edenn_enhanced_music_provider,
)
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_music import (
    ProviderBMusicProvider,
)
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_payload import (
    extract_timestamped_lyrics_for_choice,
    extract_timestamped_words,
)
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_pg_lock import (
    NullKeyLock,
)
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_key_health import (
    InMemoryProviderBKeyHealthStore,
)


class EnhancedMusicProviderRoutingTests(unittest.TestCase):
    """Routing tests run with the PG advisory lock disabled so the builder
    doesn't try to reach a real Postgres. The PG-enabled path is covered by
    test_enabled_pg_lock_initialization_failure_fails_closed below and by the
    dedicated PgKeyLock unit/integration tests.
    """

    def test_edenn_enhanced_uses_env_provider_when_numbered_keys_set(self) -> None:
        # Numbered keys -> ProviderBMusicProvider() called without explicit api_key
        # so _ProviderBKeyPool.from_env() applies provider-level key precedence.
        with patch.dict(
            os.environ,
            {
                "PROVIDER_B_API_KEY_1": "key-1",
                "PROVIDER_B_API_KEY_2": "key-2",
                "EDENN_ENHANCED_PROVIDER_B_API_KEY": "enhanced-key",
                "PROVIDER_B_API_KEY": "default-key",
                "PROVIDER_B_PG_LOCK_ENABLED": "false",
            },
            clear=True,
        ), patch(
            "EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.cloud_music_gen_util.ProviderBMusicProvider",
            return_value=MagicMock(),
        ) as provider_cls:
            build_edenn_enhanced_music_provider()

        provider_cls.assert_called_once()
        assert provider_cls.call_args.args == ()
        assert "api_key" not in provider_cls.call_args.kwargs
        assert isinstance(provider_cls.call_args.kwargs["key_lock"], NullKeyLock)
        assert isinstance(
            provider_cls.call_args.kwargs["key_health_store"],
            InMemoryProviderBKeyHealthStore,
        )

    def test_edenn_enhanced_uses_env_provider_when_default_key_set(self) -> None:
        # PROVIDER_B_API_KEY alone -> env-provider path, no explicit key passed.
        with patch.dict(
            os.environ,
            {"PROVIDER_B_API_KEY": "default-key", "PROVIDER_B_PG_LOCK_ENABLED": "false"},
            clear=True,
        ), patch(
            "EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.cloud_music_gen_util.ProviderBMusicProvider",
            return_value=MagicMock(),
        ) as provider_cls:
            build_edenn_enhanced_music_provider()

        provider_cls.assert_called_once()
        assert "api_key" not in provider_cls.call_args.kwargs
        assert isinstance(provider_cls.call_args.kwargs["key_lock"], NullKeyLock)
        assert isinstance(
            provider_cls.call_args.kwargs["key_health_store"],
            InMemoryProviderBKeyHealthStore,
        )

    def test_edenn_enhanced_uses_env_provider_when_only_high_numbered_key_set(self) -> None:
        with patch.dict(
            os.environ,
            {"PROVIDER_B_API_KEY_9": "key-9", "PROVIDER_B_PG_LOCK_ENABLED": "false"},
            clear=True,
        ), patch(
            "EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.cloud_music_gen_util.ProviderBMusicProvider",
            return_value=MagicMock(),
        ) as provider_cls:
            build_edenn_enhanced_music_provider()

        provider_cls.assert_called_once()
        assert "api_key" not in provider_cls.call_args.kwargs
        assert isinstance(provider_cls.call_args.kwargs["key_lock"], NullKeyLock)
        assert isinstance(
            provider_cls.call_args.kwargs["key_health_store"],
            InMemoryProviderBKeyHealthStore,
        )

    def test_edenn_enhanced_uses_dedicated_key_when_only_enhanced_key_set(self) -> None:
        # Only EDENN_ENHANCED_PROVIDER_B_API_KEY set (no pool keys) → passed explicitly.
        with patch.dict(
            os.environ,
            {
                "EDENN_ENHANCED_PROVIDER_B_API_KEY": "enhanced-key",
                "PROVIDER_B_PG_LOCK_ENABLED": "false",
            },
            clear=True,
        ), patch(
            "EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.cloud_music_gen_util.ProviderBMusicProvider",
            return_value=MagicMock(),
        ) as provider_cls:
            build_edenn_enhanced_music_provider()

        provider_cls.assert_called_once()
        assert provider_cls.call_args.kwargs["api_key"] == "enhanced-key"
        assert isinstance(provider_cls.call_args.kwargs["key_lock"], NullKeyLock)
        assert isinstance(
            provider_cls.call_args.kwargs["key_health_store"],
            InMemoryProviderBKeyHealthStore,
        )

    def test_edenn_enhanced_requires_some_api_key(self) -> None:
        with patch.dict(
            os.environ, {"PROVIDER_B_PG_LOCK_ENABLED": "false"}, clear=True,
        ):
            with self.assertRaises(EdennConfigurationError) as exc:
                build_edenn_enhanced_music_provider()

        self.assertIn("PROVIDER_B_API_KEY_1..N", str(exc.exception))
        self.assertIn("EDENN_ENHANCED_PROVIDER_B_API_KEY", str(exc.exception))
        self.assertIn("PROVIDER_B_API_KEY", str(exc.exception))

    def test_disabled_pg_lock_uses_null_key_lock(self) -> None:
        with patch.dict(
            os.environ,
            {"PROVIDER_B_API_KEY_1": "key-1", "PROVIDER_B_PG_LOCK_ENABLED": "false"},
            clear=True,
        ), patch(
            "EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.cloud_music_gen_util.ProviderBMusicProvider",
            return_value=MagicMock(),
        ) as provider_cls, patch(
            "EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.cloud_music_gen_util.PgKeyLock.from_env",
        ) as pg_from_env:
            build_edenn_enhanced_music_provider()

        # When disabled, PgKeyLock.from_env must not be called at all.
        pg_from_env.assert_not_called()
        provider_cls.assert_called_once()
        assert isinstance(provider_cls.call_args.kwargs["key_lock"], NullKeyLock)
        assert isinstance(
            provider_cls.call_args.kwargs["key_health_store"],
            InMemoryProviderBKeyHealthStore,
        )

    def test_enabled_pg_lock_initialization_failure_fails_closed(self) -> None:
        # Default PROVIDER_B_PG_LOCK_ENABLED is true; if PgKeyLock.from_env raises,
        # the builder must surface EdennConfigurationError, not silently
        # construct with NullKeyLock.
        with patch.dict(
            os.environ,
            {"PROVIDER_B_API_KEY_1": "key-1"},
            clear=True,
        ), patch(
            "EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.cloud_music_gen_util.PgKeyLock.from_env",
            side_effect=RuntimeError("pg down"),
        ), patch(
            "EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.cloud_music_gen_util.ProviderBMusicProvider",
            return_value=MagicMock(),
        ) as provider_cls:
            with self.assertRaises(EdennConfigurationError) as exc:
                build_edenn_enhanced_music_provider()

        # The provider must NOT be constructed when PG init fails.
        provider_cls.assert_not_called()
        self.assertIn("PROVIDER_B_PG_LOCK_ENABLED", str(exc.exception))

    def test_enabled_pg_lock_passes_through_when_init_succeeds(self) -> None:
        # Default PROVIDER_B_PG_LOCK_ENABLED is true; when PgKeyLock.from_env
        # succeeds, the builder must pass that lock to the provider.
        fake_lock = MagicMock(name="PgKeyLock")
        fake_health = MagicMock(name="PgProviderBKeyHealthStore")
        with patch.dict(
            os.environ,
            {"PROVIDER_B_API_KEY_1": "key-1"},
            clear=True,
        ), patch(
            "EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.cloud_music_gen_util.PgKeyLock.from_env",
            return_value=fake_lock,
        ), patch(
            "EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.cloud_music_gen_util.PgProviderBKeyHealthStore.from_env",
            return_value=fake_health,
        ), patch(
            "EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.cloud_music_gen_util.ProviderBMusicProvider",
            return_value=MagicMock(),
        ) as provider_cls:
            build_edenn_enhanced_music_provider()

        provider_cls.assert_called_once()
        assert provider_cls.call_args.kwargs["key_lock"] is fake_lock
        assert provider_cls.call_args.kwargs["key_health_store"] is fake_health


class ProviderBTimestampParsingTests(unittest.TestCase):
    def test_extract_timestamped_words_prefers_verified_word_level_shape(self) -> None:
        payload = {
            "choices": [
                {
                    "lyrics_sections": [
                        {"section_type": "verse"},
                        {
                            "section_type": "verse",
                            "start": 0,
                            "end": 1000,
                            "lines": [
                                {
                                    "text": "Glow tonight",
                                    "start": 0,
                                    "end": 1000,
                                    "words": [
                                        {"text": "Glow", "start": 0, "end": 350},
                                        {"text": "tonight", "start": 350, "end": 1000},
                                    ],
                                }
                            ],
                        },
                        {
                            "section_type": "chorus",
                            "start": 1000,
                            "end": 2500,
                            "lines": [
                                {
                                    "text": "Hold the light",
                                    "start": 1000,
                                    "end": 2500,
                                    "words": [
                                        {"text": "Hold", "start": 1000, "end": 1450},
                                        {"text": "the", "start": 1450, "end": 1700},
                                        {"text": "light", "start": 1700, "end": 2500},
                                    ],
                                }
                            ],
                        },
                    ]
                }
            ]
        }

        words = extract_timestamped_words(payload)

        self.assertEqual(
            [word.text for word in words],
            ["Glow", "tonight", "Hold", "the", "light"],
        )
        self.assertEqual(
            [(word.startS, word.endS) for word in words],
            [(0.0, 0.35), (0.35, 1.0), (1.0, 1.45), (1.45, 1.7), (1.7, 2.5)],
        )

    def test_extract_timestamped_words_falls_back_to_line_level_when_words_missing(self) -> None:
        payload = {
            "choices": [
                {
                    "lyrics_sections": [
                        {
                            "section_type": "verse",
                            "start": 0,
                            "end": 1200,
                            "lines": [
                                {
                                    "text": "Glow tonight",
                                    "start": 0,
                                    "end": 1200,
                                }
                            ],
                        }
                    ]
                }
            ]
        }

        words = extract_timestamped_words(payload)

        self.assertEqual(len(words), 1)
        self.assertEqual(words[0].text, "Glow tonight")
        self.assertEqual((words[0].startS, words[0].endS), (0.0, 1.2))

    def test_extract_timestamped_lyrics_for_choice_reads_secondary_choice(self) -> None:
        payload = {
            "choices": [
                {
                    "lyrics_sections": [
                        {
                            "section_type": "verse",
                            "lines": [
                                {
                                    "text": "Primary glow",
                                    "start": 0,
                                    "end": 1000,
                                    "words": [
                                        {"text": "Primary", "start": 0, "end": 450},
                                        {"text": "glow", "start": 450, "end": 1000},
                                    ],
                                }
                            ],
                        }
                    ]
                },
                {
                    "lyrics_sections": [
                        {
                            "section_type": "verse",
                            "lines": [
                                {
                                    "text": "Secondary shine",
                                    "start": 0,
                                    "end": 1500,
                                    "words": [
                                        {"text": "Secondary", "start": 0, "end": 900},
                                        {"text": "shine", "start": 900, "end": 1500},
                                    ],
                                }
                            ],
                        }
                    ]
                },
            ]
        }

        secondary = extract_timestamped_lyrics_for_choice(payload, 1)

        self.assertEqual(
            [word.text for word in secondary.line_level],
            ["Secondary shine"],
        )
        self.assertEqual(
            [word.text for word in secondary.word_level],
            ["Secondary", "shine"],
        )


class ProviderBMelodyGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_clone_vocal_returns_vocal_id_from_response(self) -> None:
        provider = ProviderBMusicProvider(api_key="test-key")
        provider._credit_cache["constructor_api_key"] = (10000, time.time())
        audio_path = Path(self.id()).with_suffix(".m4a")
        audio_path.write_bytes(b"fake")
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"data": {"id": "vocal_789"}}
        response.raise_for_status.return_value = None

        client_cm = AsyncMock()
        client_cm.__aenter__.return_value = client_cm
        client_cm.__aexit__.return_value = False
        client_cm.post = AsyncMock(return_value=response)

        with patch(
            "EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_music.httpx.AsyncClient",
            return_value=client_cm,
        ):
            vocal_id = await provider.clone_vocal(audio_path)

        self.assertEqual(vocal_id, "vocal_789")
        audio_path.unlink(missing_ok=True)

    async def test_generate_with_melody_variants_omits_prompt_from_song_request(self) -> None:
        provider = object.__new__(ProviderBMusicProvider)
        provider.default_model = "auto"
        provider.generate_lyrics = AsyncMock(
            return_value={"title": "Glow", "lyrics": "[Verse]\nGlow tonight"}
        )
        provider.upload_audio_file = AsyncMock(return_value="melody_123")
        provider.generate_song_task = AsyncMock(
            return_value=MagicMock(task_id="task_123")
        )
        provider.wait_song_task = AsyncMock(
            return_value=MagicMock(
                task_id="task_123",
                status="succeeded",
                trace_id="trace_123",
                raw={
                    "audio_urls": ["https://example.com/provider_b_primary.mp3"],
                    "lyrics_sections": [
                        {
                            "lines": [
                                {
                                    "text": "Glow tonight",
                                    "start": 0,
                                    "end": 1000,
                                }
                            ]
                        }
                    ],
                },
            )
        )
        provider.download_audio = AsyncMock(side_effect=lambda _url, dest_path: dest_path)

        result = await provider._generate_with_melody_variants_in_cycle(
            prompt="bright female vocal pop cover",
            lyrics_prompt="clear uplifting lyrics",
            melody_audio_path=Path("melody.m4a"),
            vocal_id="vocal_123",
            output_path=Path("primary.mp3"),
            save_sidecar=False,
        )

        self.assertEqual(result[0], Path("primary.mp3"))
        self.assertEqual(result[1], None)
        self.assertEqual([word.text for word in result[2]], ["Glow tonight"])
        _, kwargs = provider.generate_song_task.await_args
        self.assertEqual(kwargs["prompt"], "")
        self.assertIsNone(kwargs["melody_id"])
        self.assertEqual(kwargs["vocal_id"], "vocal_123")


if __name__ == "__main__":
    unittest.main()
