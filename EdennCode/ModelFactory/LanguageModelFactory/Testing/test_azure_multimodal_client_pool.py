import unittest

from EdennCode.ModelFactory.LanguageModelFactory.azure_based_model_model_gateway import (
    AzureMultimodalClientPool,
)


class _FakeAzureClient:
    def __init__(self, label: str, *, side_effect=None) -> None:
        self.label = label
        self.azure_model = f"{label}-model"
        self.display_name = f"{label}:model@example.test"
        self.side_effect = side_effect
        self.calls = 0

    async def complete_messages(self, *args, **kwargs):
        self.calls += 1
        if self.side_effect is not None:
            raise self.side_effect
        return (
            {"label": self.label},
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


class AzureMultimodalClientPoolTests(unittest.IsolatedAsyncioTestCase):
    def test_default_selection_always_prefers_primary(self) -> None:
        primary = _FakeAzureClient("primary")
        west_us = _FakeAzureClient("west_us")
        pool = AzureMultimodalClientPool([primary, west_us])

        with pool.select_for_job(job_id="job-a") as selected_a:
            self.assertIs(selected_a, primary)
        with pool.select_for_job(job_id="job-b") as selected_b:
            self.assertIs(selected_b, primary)

    def test_preferred_label_can_force_fallback_endpoint_for_testing(self) -> None:
        primary = _FakeAzureClient("primary")
        west_us = _FakeAzureClient("west_us")
        pool = AzureMultimodalClientPool([primary, west_us])

        with pool.select_for_job(job_id="job-a", preferred_label="west_us") as selected:
            self.assertIs(selected, west_us)

    async def test_fallback_endpoint_is_used_only_after_primary_is_disabled(self) -> None:
        primary = _FakeAzureClient(
            "primary",
            side_effect=RuntimeError("unsupported parameter: response_format"),
        )
        west_us = _FakeAzureClient("west_us")
        pool = AzureMultimodalClientPool([primary, west_us])

        payload, usage = await pool.complete_messages([], json_schema={})

        self.assertEqual(payload, {"label": "west_us"})
        self.assertEqual(usage["total_tokens"], 2)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(west_us.calls, 1)
        self.assertIn("primary", pool.disabled_reasons())

        with pool.select_for_job(job_id="job-after-disable") as selected:
            self.assertIs(selected, west_us)


if __name__ == "__main__":
    unittest.main()
