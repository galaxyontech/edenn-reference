import os
import unittest

from EdennCode.Deployment import provider_vocabulary
from EdennCode.Deployment.api_common import (
    edenn_error_to_http_exception,
    filename_from_url,
    parse_lyrics_timestamps_json,
    parse_url_list_json,
    sanitize_filename,
)
from EdennCode.Deployment.output_naming import contains_provider_token
from EdennCode.exceptions import EdennApiError, EdennProviderTimeoutError


# An invented upstream: the vendor vocabulary is deployment configuration, and a
# test file is a reader-visible surface that must not name a real provider.
_VENDOR = "Acme"
_PROVIDER_NAME = "acmesound"
_VOCABULARY = {
    # ``acme`` covers ``acmesound`` through the matcher's ``\w*`` tail.
    "PROVIDER_SCRUB_TOKENS": "acme",
    "MUSIC_PROVIDER_NAMES": _PROVIDER_NAME,
}


class ApiCommonTests(unittest.TestCase):
    def _configure_provider_vocabulary(self) -> None:
        """Install the invented vocabulary for the duration of one test.

        Both halves are load-bearing: without ``MUSIC_PROVIDER_NAMES`` the
        timeout triages as a generic AI failure rather than a music one, and
        without the scrub tokens ``contains_provider_token`` has nothing to
        match, so a leak check would pass on text that still named the vendor.
        The vocabulary is process-global and memoised, hence the cleanup.
        """
        prior = {key: os.environ.get(key) for key in _VOCABULARY}
        os.environ.update(_VOCABULARY)
        provider_vocabulary.reset_cache()

        def _restore() -> None:
            for key, value in prior.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            provider_vocabulary.reset_cache()

        self.addCleanup(_restore)

    def test_sanitize_filename_strips_path_segments_and_disallowed_characters(self) -> None:
        sanitized = sanitize_filename("../../mix<>?:track.mp3")
        self.assertEqual(sanitized, "mixtrack.mp3")

    def test_filename_from_url_rejects_non_http_schemes(self) -> None:
        with self.assertRaises(EdennApiError) as exc:
            filename_from_url("file:///tmp/input.mp4")

        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn("http or https", exc.exception.public_message)

    def test_parse_lyrics_timestamps_json_returns_word_ts_models(self) -> None:
        words = parse_lyrics_timestamps_json(
            '[{"text":"hello","startS":0.1,"endS":0.9,"i":2}]'
        )

        self.assertEqual(len(words), 1)
        self.assertEqual(words[0].text, "hello")
        self.assertAlmostEqual(words[0].startS, 0.1)
        self.assertAlmostEqual(words[0].endS, 0.9)
        self.assertEqual(words[0].i, 2)

    def test_parse_lyrics_timestamps_json_rejects_invalid_items(self) -> None:
        with self.assertRaises(EdennApiError) as exc:
            parse_lyrics_timestamps_json('[{"text":"bad","startS":"oops","endS":1.0}]')

        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn("invalid", exc.exception.public_message.lower())

    def test_parse_url_list_json_rejects_invalid_entries(self) -> None:
        with self.assertRaises(EdennApiError) as exc:
            parse_url_list_json(
                '["https://example.test/a.png", "ftp://example.test/b.png"]',
                field_name="image_urls_json",
            )

        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn("http or https", exc.exception.public_message.lower())

    def test_parse_url_list_json_returns_trimmed_urls(self) -> None:
        urls = parse_url_list_json(
            '[" https://example.test/a.png ", "https://example.test/b.png"]',
            field_name="image_urls_json",
        )

        self.assertEqual(
            urls,
            ["https://example.test/a.png", "https://example.test/b.png"],
        )

    def test_provider_error_http_detail_uses_public_catalog_message(self) -> None:
        self._configure_provider_vocabulary()
        exc = EdennProviderTimeoutError(
            f"{_VENDOR} task timed out while polling",
            provider_name=_PROVIDER_NAME,
            operation=f"{_PROVIDER_NAME}_poll",
            retryable=True,
        )

        http_exc = edenn_error_to_http_exception(exc)
        detail = http_exc.detail

        self.assertEqual(http_exc.status_code, 504)
        self.assertEqual(detail["error_code"], 30200)
        self.assertFalse(contains_provider_token(detail["message"]))
        self.assertEqual(detail["message"], "The request took too long to complete. Please try again.")


if __name__ == "__main__":
    unittest.main()
