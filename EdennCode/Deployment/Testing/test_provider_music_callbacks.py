from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.api_common import ApiContext
from EdennCode.Deployment.api_provider_callbacks import create_provider_callbacks_router
from EdennCode.Deployment.provider_music_callbacks import (
    InMemoryProviderMusicCallbackStore,
    set_provider_music_callback_store_for_testing,
    wait_for_provider_c_generation_callback,
)
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_c import (
    GenerateParams,
    GenerationResult,
    ProviderCApi,
    ProviderCTrack,
)


def _callback_payload(task_id: str = "task_123") -> dict:
    return {
        "code": 200,
        "msg": "All generated successfully.",
        "data": {
            "callbackType": "complete",
            "task_id": task_id,
            "data": [
                {
                    "id": "audio_1",
                    "audio_url": "https://example.test/audio_1.mp3",
                    "title": "First",
                }
            ],
        },
    }


class ProviderMusicCallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryProviderMusicCallbackStore()
        set_provider_music_callback_store_for_testing(self.store)

    def tearDown(self) -> None:
        set_provider_music_callback_store_for_testing(None)

    def _build_app(self) -> FastAPI:
        context = ApiContext(
            settings=SimpleNamespace(),
            storage=SimpleNamespace(),
            workflow=SimpleNamespace(),
            alignment_workflow=SimpleNamespace(),
            audio_creative_edit_workflow=SimpleNamespace(),
            logger=MagicMock(),
        )
        app = FastAPI()
        app.include_router(create_provider_callbacks_router(context))
        return app

    def test_provider_c_callback_endpoint_stores_payload_for_waiter(self) -> None:
        app = self._build_app()
        with patch.dict("os.environ", {"PROVIDER_C_WEBHOOK_SECRET": "dev-secret"}):
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/provider_callbacks/provider_c?secret=dev-secret",
                    json=_callback_payload(),
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["task_id"], "task_123")

        result = asyncio.run(
            wait_for_provider_c_generation_callback(
                "task_123",
                timeout_s=0.2,
                store=self.store,
            )
        )
        self.assertEqual(result.task_id, "task_123")
        self.assertEqual(result.tracks[0].audio_id, "audio_1")
        self.assertEqual(result.tracks[0].audio_url, "https://example.test/audio_1.mp3")

    def test_provider_c_callback_endpoint_rejects_bad_secret(self) -> None:
        app = self._build_app()
        with patch.dict("os.environ", {"PROVIDER_C_WEBHOOK_SECRET": "dev-secret"}):
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/provider_callbacks/provider_c?secret=wrong",
                    json=_callback_payload(),
                )

        self.assertEqual(response.status_code, 401, response.text)
        self.assertIsNone(self.store.get("provider_c", "task_123"))

    def test_provider_b_callback_route_is_not_exposed(self) -> None:
        app = self._build_app()
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/provider_callbacks/provider_b",
                json={"task_id": "provider_b_task"},
            )

        self.assertEqual(response.status_code, 404, response.text)


class _CallbackWaitProviderCApi(ProviderCApi):
    def __init__(self) -> None:
        super().__init__(
            api_key="test-key",
            base_url="https://provider-c.test/api/v1",
            max_retries=0,
            retry_backoff_s=0.2,
        )
        self.base_candidates = ["https://provider-c.test/api/v1"]
        self.base = self.base_candidates[0]
        self.polled = False

    async def generate(self, params: GenerateParams) -> str:
        self.generated_payload = params.to_payload()
        return "task_from_generate"

    async def poll_generation(self, task_id: str, **kwargs):
        self.polled = True
        raise AssertionError("poll_generation should not run when callback resolves")


class ProviderCCallbackWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_and_wait_tracks_uses_callback_waiter(self) -> None:
        api = _CallbackWaitProviderCApi()

        async def waiter(task_id: str, timeout_s: float) -> GenerationResult:
            self.assertEqual(task_id, "task_from_generate")
            self.assertEqual(timeout_s, 12.0)
            return GenerationResult(
                task_id=task_id,
                status="SUCCESS",
                tracks=[
                    ProviderCTrack(
                        audio_id="audio_from_callback",
                        audio_url="https://example.test/audio.mp3",
                    )
                ],
            )

        task_id, result, base = await api.generate_and_wait_tracks(
            GenerateParams(
                prompt="cinematic pop",
                callback_url="https://api.example.test/api/v1/provider_callbacks/provider_c",
            ),
            timeout_s=12.0,
            callback_waiter=waiter,
        )

        self.assertEqual(task_id, "task_from_generate")
        self.assertEqual(base, "https://provider-c.test/api/v1")
        self.assertEqual(result.tracks[0].audio_id, "audio_from_callback")
        self.assertFalse(api.polled)
        self.assertEqual(
            api.generated_payload["callBackUrl"],
            "https://api.example.test/api/v1/provider_callbacks/provider_c",
        )


if __name__ == "__main__":
    unittest.main()
