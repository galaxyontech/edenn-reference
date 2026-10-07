"""The response guardrail: no upstream brand/model token reaches a client, and no
legitimate content is corrupted in the process.

The vocabulary is deployment configuration rather than source (``provider_vocabulary``
explains why), so these tests install a SYNTHETIC one — invented names that exist
nowhere but in this file — and prove the matching *rules* against it. The rules are
what the guardrail guarantees; the names are interchangeable, and writing a real
upstream name here would be the very leak the guardrail exists to prevent.
"""
import os
import unittest
from unittest import mock

from EdennCode.Deployment import provider_vocabulary
from EdennCode.Deployment.api_video_generation import (
    AudioMetadataBlock,
    VideoJobResponse,
    VideoMetadataBlock,
)
from EdennCode.Deployment.response_guardrail import (
    guard_response_dict,
    guard_response_model,
)

# The invented vocabulary. Every entry earns its place by encoding one matching
# rule the guardrail has to get right:
#   acmesound  plain   — the flagship brand, seen in every surface form a model
#                        or an upstream service writes it in
#   zephyrmix  plain   — a glued compound whose spaced form ("zephyr mix") and
#                        whose bare first word ("zephyr") are ordinary prose
#   rift       plain   — a short token that hides inside ordinary words
#                        ("adrift", "driftwood") and must never match there
#   nimbus     strict  — also the stem of an ordinary word, so it must not eat
#                        "nimbuses"
#   lumen      strict  — likewise, "lumens"
#   orbit      model   — a family name that is ordinary prose until a version
#                        digit follows it
_TOKENS = "acmesound,zephyrmix,rift"
_STRICT = "nimbus,lumen"
_MODELS = "orbit"

_ENV_KEYS = (
    "PROVIDER_SCRUB_TOKENS",
    "PROVIDER_SCRUB_TOKENS_STRICT",
    "PROVIDER_SCRUB_MODEL_PREFIXES",
)


class _SyntheticVocabulary(unittest.TestCase):
    """Installs the synthetic vocabulary for the duration of one test.

    The vocabulary is memoised, so the environment and the cache have to be put
    back together — otherwise the next test inherits this one's configuration and
    a guardrail regression hides behind a stale pattern.
    """

    def setUp(self) -> None:
        self._env = mock.patch.dict(
            os.environ,
            {
                "PROVIDER_SCRUB_TOKENS": _TOKENS,
                "PROVIDER_SCRUB_TOKENS_STRICT": _STRICT,
                "PROVIDER_SCRUB_MODEL_PREFIXES": _MODELS,
            },
        )
        self._env.start()
        provider_vocabulary.reset_cache()

    def tearDown(self) -> None:
        self._env.stop()
        provider_vocabulary.reset_cache()


class GuardrailScrubTests(_SyntheticVocabulary):
    def test_scrubs_brand_tokens_from_free_text(self) -> None:
        for text in (
            "An AcmeSound anthem",
            "generated with Zephyrmix",
            "zephyrmix vocal take",  # identifier form (spaced "zephyr mix" is prose)
            "made on acmesound-o1",
            "prompted through orbit-4o",
            "an AcmeSoundAI-assisted mix",
            "rift / zephyrmix / nimbus blend",
            # brands the adversarial pass found slipping through (now covered)
            "Generated with zephyrmix music-2.5",
            "rendered by acmesoundstudio",
            "made with orbit4o",
            "authored on orbit4",
            # separator / digit-glued surface forms
            "trained on zephyrmix_v4",
            "acmesound_rift transcript",
            "an acmesound4 remix",
            "nimbus_v2 vocals",
            # fullwidth Unicode (Japan deployment)
            "Ａｃｍｅｓｏｕｎｄ generated it",  # ｎｏｒｍａｌｉｓｅｓ to acmesound
        ):
            out, leaks = guard_response_dict({"t": text})
            with self.subTest(text=text):
                self.assertTrue(leaks, text)
                self.assertIn("the generation service", out["t"], text)

    def test_leaves_legitimate_content_untouched(self) -> None:
        # Ambiguous words (azure=colour, capybara=animal), our own brand, and
        # ordinary media prose that collides with a brand alternative. None may be
        # rewritten or flagged.
        for text in (
            "an azure blue sky at dusk",
            "a capybara wading through a pond",
            "edenn_basic",
            "edenn-perceptron-1.1",
            "a boat adrift on the bay",  # 'rift' glued to a letter must not match
            "driftwood stacked along the shore",  # same rule, same token
            "a zephyr breeze through the pines",  # bare first word is not a brand
            "a zephyr mix of warm strings",  # 'zephyrmix' must not eat the spaced form
            "nimbuses gathering over the ridge",  # strict token, must not eat the plural
            "lumens of stage light",  # likewise, an ordinary word's plural
            "an orbit-based summary of the scene",  # family name with no version digit
        ):
            out, leaks = guard_response_dict({"t": text})
            with self.subTest(text=text):
                self.assertEqual(leaks, [], text)
                self.assertEqual(out["t"], text)

    def test_url_is_flagged_but_never_rewritten(self) -> None:
        for url in (
            "https://acct.blob.core.windows.net/audio/acmesound/track.mp3?sig=abc",
            # underscore-joined blob name — the flag path must catch it too
            "https://cdn.example.com/acmesound_track_final.mp3",
        ):
            out, leaks = guard_response_dict({"audio_url": url})
            with self.subTest(url=url):
                self.assertEqual(out["audio_url"], url)  # download must still work
                self.assertEqual(len(leaks), 1)
                self.assertIn("URL", leaks[0])

    def test_neutralized_media_url_is_not_falsely_flagged(self) -> None:
        # The blob names the pipeline actually produces must not trip the URL flag.
        for url in (
            "https://acct.blob.core.windows.net/audio/jobs/j/audio/complete_audio.mp3?sig=x",
            "https://acct.blob.core.windows.net/audio/jobs/j/audio/matched_audio.mp3?sig=x",
            "https://acct.blob.core.windows.net/voicestorage/jobs/j/video/slideshow.mp4?sig=x",
        ):
            _, leaks = guard_response_dict({"u": url})
            with self.subTest(url=url):
                self.assertEqual(leaks, [], url)

    def test_flags_a_brand_token_in_a_field_name(self) -> None:
        out, leaks = guard_response_dict({"acmesound_id": "x"})
        self.assertIn("acmesound_id", out)  # key kept — shape must not break
        self.assertTrue(any("field name" in leak for leak in leaks))

    def test_walks_nested_dicts_and_lists(self) -> None:
        payload = {
            "video_metadata": {
                "scenes": [
                    {"visual_summary": "a calm shore"},
                    {"visual_summary": "closing on an AcmeSound logo"},
                ]
            }
        }
        out, leaks = guard_response_dict(payload)
        self.assertEqual(len(leaks), 1)
        self.assertIn("scenes[1].visual_summary", leaks[0])
        self.assertNotIn("AcmeSound", out["video_metadata"]["scenes"][1]["visual_summary"])


class GuardrailUnconfiguredVocabularyTests(unittest.TestCase):
    """A deployment that has named no vendor: the token pass has nothing to match.

    ``configured_pattern()`` is ``None`` here, and the guardrail reads that as
    "match nothing" — never as "match everything", which would rewrite every
    response the product ships. The structural half of the contract is unchanged:
    the response shape survives and a URL is still returned byte-for-byte. And the
    key check and the URL check are still on the path, keyed off configuration
    alone — naming the vendor in the environment is the whole difference between a
    silent payload and a flagged one, so the flags cannot be lost by a deployment
    that simply forgot to configure the list.
    """

    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, {key: "" for key in _ENV_KEYS})
        self._env.start()
        provider_vocabulary.reset_cache()

    def tearDown(self) -> None:
        self._env.stop()
        provider_vocabulary.reset_cache()

    def test_free_text_is_untouched_when_no_vendor_is_named(self) -> None:
        text = "An AcmeSound anthem"
        out, leaks = guard_response_dict({"t": text})
        self.assertEqual(out["t"], text)
        self.assertEqual(leaks, [])

    def test_key_and_url_flags_are_armed_by_configuration(self) -> None:
        payload = {
            "acmesound_id": "x",
            "audio_url": "https://acct.blob.core.windows.net/a/acmesound/t.mp3?sig=abc",
        }

        out, leaks = guard_response_dict(payload)
        self.assertIn("acmesound_id", out)  # shape survives with or without a list
        self.assertEqual(out["audio_url"], payload["audio_url"])
        self.assertEqual(leaks, [])

        # Name the vendor and the same payload is flagged twice — once for the
        # field name, once for the signed URL — and the URL is still not rewritten.
        with mock.patch.dict(os.environ, {"PROVIDER_SCRUB_TOKENS": _TOKENS}):
            provider_vocabulary.reset_cache()
            out, leaks = guard_response_dict(payload)
        provider_vocabulary.reset_cache()

        self.assertTrue(any("field name" in leak for leak in leaks), leaks)
        self.assertTrue(any("URL" in leak for leak in leaks), leaks)
        self.assertEqual(out["audio_url"], payload["audio_url"])


class GuardrailModelTests(_SyntheticVocabulary):
    def _response(self, **audio) -> VideoJobResponse:
        return VideoJobResponse(
            job_id="j",
            video_metadata=VideoMetadataBlock(video_summary={}),
            audio_metadata=AudioMetadataBlock(**audio),
        )

    def test_clean_response_returned_unchanged(self) -> None:
        response = self._response(music_title="Blush Snowfall")
        self.assertIs(guard_response_model(response), response)

    def test_leaking_response_is_corrected(self) -> None:
        response = self._response(
            music_title="AcmeSound Nights",
            music_description="bright pop, rendered on zephyrmix",
        )
        guarded = guard_response_model(response)
        self.assertIsNot(guarded, response)
        dumped = guarded.model_dump(mode="json")["audio_metadata"]
        self.assertNotIn("AcmeSound", dumped["music_title"])
        self.assertNotIn("zephyrmix", dumped["music_description"])

    def test_correction_preserves_urls(self) -> None:
        url = "https://acct.blob.core.windows.net/audio/track.mp3?sig=xyz"
        response = self._response(music_title="AcmeSound Nights", audio_url=url)
        guarded = guard_response_model(response)
        self.assertEqual(guarded.audio_metadata.audio_url, url)


if __name__ == "__main__":
    unittest.main()
