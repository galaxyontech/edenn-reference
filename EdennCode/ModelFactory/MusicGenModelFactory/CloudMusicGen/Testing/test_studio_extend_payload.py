"""What the studio extend request actually asks for.

Both modes are documented by the provider, and getting the mode wrong is not a
validation error — it is a paid task that fails minutes later, so the payload is
worth pinning here rather than discovering in a customer's deliverable.
"""

import unittest
from unittest.mock import AsyncMock, patch

from EdennCode.exceptions import EdennProviderResponseError
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_c import (
    PROVIDER_C_TERMINAL_FAILURE_STATUSES,
    ProviderCApi,
)


class StudioExtendPayloadTests(unittest.IsolatedAsyncioTestCase):
    def _api(self) -> ProviderCApi:
        with patch.dict("os.environ", {"PROVIDER_C_API_KEY": "test-key"}, clear=False):
            return ProviderCApi()

    async def _captured_payload(self, **kwargs) -> dict:
        api = self._api()
        with patch.object(api, "_json_request", new=AsyncMock(return_value={"taskId": "t1"})) as req:
            await api.extend(audio_id="aid", callback_url="https://cb.example", **kwargs)
        return req.await_args.args[2]

    async def test_default_mode_sends_only_the_source_reference(self):
        payload = await self._captured_payload()
        self.assertFalse(payload["defaultParamFlag"])
        self.assertEqual(payload["audioId"], "aid")
        self.assertNotIn("style", payload)
        self.assertNotIn("continueAt", payload)

    async def test_instrumental_is_stated_not_inherited(self):
        """The provider defaults instrumental to false and does not inherit it,
        so an instrumental extension has to say so or it comes back singing."""

        payload = await self._captured_payload(instrumental=True)
        self.assertIs(payload["instrumental"], True)

    async def test_custom_parameters_switch_the_mode_and_travel_together(self):
        payload = await self._captured_payload(
            instrumental=True, style="marimba", title="Extended", continue_at=90.0,
        )
        self.assertTrue(payload["defaultParamFlag"])
        self.assertEqual(payload["style"], "marimba")
        self.assertEqual(payload["title"], "Extended")
        self.assertEqual(payload["continueAt"], 90.0)
        # Forbidden alongside instrumental=true, so we must never add them here.
        self.assertNotIn("prompt", payload)
        self.assertNotIn("vocalGender", payload)

    async def test_partial_custom_parameters_are_refused_before_spending(self):
        api = self._api()
        with patch.object(api, "_json_request", new=AsyncMock()) as req:
            with self.assertRaises(EdennProviderResponseError) as caught:
                await api.extend(audio_id="aid", style="marimba")
            req.assert_not_awaited()
        self.assertIn("continue_at", str(caught.exception))
        self.assertIn("title", str(caught.exception))

    def test_terminal_statuses_match_the_providers_enum(self):
        """"FAILED" is not one of them — matching it was why every failed task
        ran the poll budget out and reported a timeout instead of its cause."""

        self.assertEqual(
            PROVIDER_C_TERMINAL_FAILURE_STATUSES,
            frozenset({"CREATE_TASK_FAILED", "GENERATE_AUDIO_FAILED",
                       "CALLBACK_EXCEPTION", "SENSITIVE_WORD_ERROR"}),
        )
        self.assertNotIn("FAILED", PROVIDER_C_TERMINAL_FAILURE_STATUSES)


if __name__ == "__main__":
    unittest.main()
