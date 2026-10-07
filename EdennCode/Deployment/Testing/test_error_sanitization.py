"""Regression tests: upstream provider/model names must never reach a client.

These guard the leak reported in the agentic-audio console — a raw provider
error ("[provider_timeout_error] Timed out waiting for Acme task ...") surfacing
in the UI — and the systemic version of it across the async-v2 status/events API.
``contains_provider_token`` is the shared oracle for "does this text name a vendor".

The oracle and the scrubber both read the vocabulary a deployment configured, so
with nothing configured they match nothing and every assertion below would pass
without proving anything. Each test therefore installs an invented vocabulary —
a test file is a reader-visible surface too, and must not name a real upstream —
and restores the environment afterwards.
"""

import os
import unittest

from EdennCode.Deployment import provider_vocabulary
from EdennCode.Deployment.error_codes import (
    public_error_payload,
    redact_client_keys,
    redact_error_blob,
    scrub_provider_names,
)
from EdennCode.Deployment.output_naming import contains_provider_token
from EdennCode.exceptions import EdennProviderTimeoutError


# The invented upstream these tests hide, and the routing key its errors carry.
_VENDOR = "Acme"
_PROVIDER_NAME = "acmesound"

# The shape of the error from the bug report, with the invented vendor in the
# place the real one occupied.
_REPORTED = f"Timed out waiting for {_VENDOR} task c16124ce678f648e768ad39edadb6772"

# The configured vocabulary. Every collision the shipped one has to survive is
# represented:
#   acme    — plain token; a version, underscore or CamelCase tail is part of
#             the name and must be swallowed with it
#   nimbus  — plain token that also lives inside an ordinary word ("cumulonimbus")
#   orbit   — strict token: also the stem of an ordinary English word
#   lumen   — model family: only a leak once a version digit follows
# ``acme`` covers ``acmesound`` through its ``\w*`` tail, so a leaked routing key
# is caught by the same oracle as a leaked mention in prose.
_SYNTHETIC_VOCABULARY = {
    "PROVIDER_SCRUB_TOKENS": "acme,nimbus",
    "PROVIDER_SCRUB_TOKENS_STRICT": "orbit",
    "PROVIDER_SCRUB_MODEL_PREFIXES": "lumen",
    # Which upstreams are music generation is configuration too; triage needs it
    # to route to a 30xxx code instead of the 20xxx (AI analysis) range.
    "MUSIC_PROVIDER_NAMES": _PROVIDER_NAME,
}


class _ConfiguredVocabularyTestCase(unittest.TestCase):
    """Installs the invented vocabulary for the duration of one test.

    The vocabulary is process-global and memoised, so a test that left its own
    behind would quietly rewrite the matching rules for every test after it.
    """

    def setUp(self) -> None:
        self._prior_env = {key: os.environ.get(key) for key in _SYNTHETIC_VOCABULARY}
        os.environ.update(_SYNTHETIC_VOCABULARY)
        provider_vocabulary.reset_cache()

    def tearDown(self) -> None:
        for key, value in self._prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        provider_vocabulary.reset_cache()


class PublicErrorPayloadTests(_ConfiguredVocabularyTestCase):
    def test_provider_timeout_is_generic_and_token_free(self) -> None:
        exc = EdennProviderTimeoutError(
            _REPORTED, provider_name=_PROVIDER_NAME, retryable=True
        )
        payload = public_error_payload(exc, retryable=True)
        self.assertEqual(payload["error_code"], 30200)
        self.assertTrue(payload["retryable"])
        self.assertFalse(contains_provider_token(payload["message"]))
        self.assertNotIn(_VENDOR, str(payload))
        self.assertNotIn(
            "c16124ce678f648e768ad39edadb6772", str(payload)
        )  # no task id either

    def test_plain_exception_becomes_generic_internal_error(self) -> None:
        payload = public_error_payload(RuntimeError(f"{_VENDOR} request blew up"))
        self.assertEqual(payload["error_code"], 90001)
        self.assertFalse(contains_provider_token(payload["message"]))
        self.assertNotIn(_VENDOR, str(payload))


class ScrubProviderNamesTests(_ConfiguredVocabularyTestCase):
    def test_scrubs_known_vendor_and_model_tokens(self) -> None:
        # Free text is written by upstream services and by a language model, so
        # a name arrives in every surface form: bare, glued to a version digit,
        # underscored, camel-cased, or as a dated model id. All of them leak.
        for raw in [
            _REPORTED,
            "Nimbus request failed with HTTP 429",
            "acme music generation exceeded 120s",
            "acme_v2 / AcmeSoundAI / acme4 / nimbus / orbit / lumen-4o",
            "the lumen-2 deployment in the primary region is down",
        ]:
            scrubbed = scrub_provider_names(raw)
            for token in ("acme", "nimbus", "orbit", "lumen-2", "lumen-4o"):
                self.assertNotIn(
                    token,
                    scrubbed.lower(),
                    msg=f"token survived scrubbing: {scrubbed!r}",
                )
            # The shared oracle agrees nothing vendor-shaped is left behind.
            self.assertFalse(
                contains_provider_token(scrubbed),
                msg=f"token survived scrubbing: {scrubbed!r}",
            )

    def test_does_not_touch_legit_words_or_non_strings(self) -> None:
        # Over-redaction is not free — this text reaches a user — and a guard
        # that mangles ordinary prose gets switched off. "audio"/"studio" name
        # our own pipeline, not a vendor, and must be preserved.
        self.assertEqual(
            scrub_provider_names("the agentic audio studio process"),
            "the agentic audio studio process",
        )
        # A token glued to a preceding letter is part of a longer ordinary word,
        # never a mention of the vendor.
        self.assertEqual(
            scrub_provider_names("cumulonimbus over the studio"),
            "cumulonimbus over the studio",
        )
        # A strict token stops at the next letter, so it cannot eat the ordinary
        # English word it happens to be the stem of.
        self.assertEqual(
            scrub_provider_names("orbital mechanics"), "orbital mechanics"
        )
        # A model family is only a leak once a version digit follows it; prose
        # that merely names the family survives.
        self.assertEqual(
            scrub_provider_names("a lumen-based summary"), "a lumen-based summary"
        )
        self.assertIsNone(scrub_provider_names(None))
        self.assertEqual(scrub_provider_names(7), 7)


class RedactErrorBlobTests(_ConfiguredVocabularyTestCase):
    def test_legacy_to_dict_blob_is_fully_sanitized(self) -> None:
        exc = EdennProviderTimeoutError(_REPORTED, provider_name=_PROVIDER_NAME)
        blob = exc.to_dict()  # legacy persisted shape: raw message + provider_name
        self.assertTrue(contains_provider_token(str(blob)))  # precondition

        clean = redact_error_blob(blob)
        self.assertNotIn("provider_name", clean)
        self.assertNotIn("component", clean)
        self.assertFalse(contains_provider_token(str(clean)))
        # public_message is surfaced as the message.
        self.assertEqual(
            clean["message"], "The request took too long to complete. Please try again."
        )


class RedactClientKeysTests(_ConfiguredVocabularyTestCase):
    def test_drops_vendor_identity_keys_but_preserves_urls(self) -> None:
        url = "https://acct.blob.core.windows.net/a.mp3?sig=azureSIG"
        payload = {
            "candidates": [
                {
                    "candidate_id": "c1",
                    "provider": _PROVIDER_NAME,
                    "provider_audio_id": "a1",
                    "provider_task_id": "t1",
                    "audio_url": url,
                }
            ]
        }
        clean = redact_client_keys(payload)
        candidate = clean["candidates"][0]
        self.assertNotIn("provider", candidate)
        self.assertNotIn("provider_audio_id", candidate)
        self.assertNotIn("provider_task_id", candidate)
        # Drop-only: the URL (whose signature happens to carry vendor-looking
        # text) is left intact.
        self.assertEqual(candidate["audio_url"], url)


class AsyncV2StatusAndEventsSanitizationTests(_ConfiguredVocabularyTestCase):
    """The status and events endpoints must scrub even legacy persisted rows."""

    def _legacy_error(self) -> dict:
        exc = EdennProviderTimeoutError(_REPORTED, provider_name=_PROVIDER_NAME)
        return exc.to_dict()

    def test_public_status_view_scrubs_job_error(self) -> None:
        from EdennCode.Deployment.async_pipeline_v2.api import _public_status_view

        view = {"job_id": "job_1", "status": "failed", "error": self._legacy_error()}
        public = _public_status_view(view)
        self.assertFalse(contains_provider_token(str(public["error"])))
        self.assertNotIn("provider_name", public["error"])

    def test_event_response_scrubs_message_and_payload_error(self) -> None:
        from EdennCode.Deployment.async_pipeline_v2.api import _event_response

        class _Event:
            event_id = "e1"
            job_id = "job_1"
            event_type = "stage.failed"
            stage_name = "video_music_monolith"
            message = f"Timed out waiting for {_VENDOR} task xyz"
            payload_json = {"task_id": "t", "error": {"message": _REPORTED}}
            created_at = None

        out = _event_response(_Event())
        self.assertFalse(contains_provider_token(out["message"]))
        self.assertFalse(contains_provider_token(str(out["payload"])))


class AgenticSnapshotSerializerTests(_ConfiguredVocabularyTestCase):
    def test_snapshot_and_event_models_strip_provider_identity(self) -> None:
        from EdennCode.EdennAgent.AgenticAudio.models import (
            AgenticAudioSessionSnapshot,
            AgenticAudioWebSocketEvent,
        )

        candidates = [
            {
                "candidate_id": "c1",
                "provider": _PROVIDER_NAME,
                "provider_audio_id": "a1",
                "provider_task_id": "t1",
                "audio_url": "https://cdn.test/a.mp3",
            }
        ]
        snap = AgenticAudioSessionSnapshot(
            session_id="s1",
            source_video_artifact_id="v1",
            status="active",
            phase="generating",
            state={"candidates": candidates},
            tool_calls=[
                {
                    "tool_call_id": "tc1",
                    "tool_name": "generate_candidates",
                    "status": "error",
                    "output": {"candidates": candidates},
                    "error": {
                        "message": _REPORTED,
                        "provider_name": _PROVIDER_NAME,
                    },
                }
            ],
        )
        dumped = snap.model_dump(mode="json")
        self.assertFalse(contains_provider_token(str(dumped)))
        self.assertNotIn("provider", dumped["state"]["candidates"][0])

        event = AgenticAudioWebSocketEvent(
            event_type="candidate.cards",
            session_id="s1",
            payload={"candidates": candidates},
        )
        self.assertFalse(
            contains_provider_token(str(event.model_dump(mode="json")))
        )


if __name__ == "__main__":
    unittest.main()
