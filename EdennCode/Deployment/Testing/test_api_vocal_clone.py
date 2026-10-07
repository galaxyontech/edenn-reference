import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.api_common import ApiContext
from EdennCode.Deployment.api_vocal_clone import create_vocal_clone_router
from EdennCode.Deployment.vocal_clone_workflows import VocalCloneResult


class _FakeStorage:
    def __init__(self) -> None:
        self.enabled = True
        self.upload_calls: list[dict[str, object]] = []

    def upload_path(
        self,
        *,
        container: str,
        path: Path,
        blob_name: str,
        content_type: str,
    ) -> str:
        self.upload_calls.append(
            {
                "container": container,
                "path": path,
                "blob_name": blob_name,
                "content_type": content_type,
            }
        )
        return blob_name

    def generate_sas_url(
        self,
        *,
        container: str,
        blob_name: str,
        require_signed: bool = False,
    ) -> str:
        signed_suffix = "?signed=1" if require_signed else ""
        return f"https://example.test/{container}/{blob_name}{signed_suffix}"


class VocalCloneApiTests(unittest.TestCase):
    def _build_app(
        self,
        tmp_dir: Path,
        workflow_run: AsyncMock,
        storage: _FakeStorage,
    ) -> FastAPI:
        settings = SimpleNamespace(
            workdir=tmp_dir / "jobs",
            upload_container="uploads",
        )
        context = ApiContext(
            settings=settings,
            storage=storage,
            workflow=MagicMock(),
            alignment_workflow=MagicMock(),
            audio_creative_edit_workflow=MagicMock(),
            logger=MagicMock(),
            vocal_clone_workflow=SimpleNamespace(run=workflow_run),
        )
        app = FastAPI()
        app.include_router(create_vocal_clone_router(context))
        return app

    def test_router_registers_route(self) -> None:
        context = ApiContext(
            settings=MagicMock(),
            storage=MagicMock(),
            workflow=MagicMock(),
            alignment_workflow=MagicMock(),
            audio_creative_edit_workflow=MagicMock(),
            logger=MagicMock(),
            vocal_clone_workflow=MagicMock(),
        )
        router = create_vocal_clone_router(context)
        paths = sorted(route.path for route in router.routes)
        self.assertIn("/api/v1/jobs/vocal-clone", paths)

    def test_api_returns_vocal_id_and_staged_sample_url(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-vocal-clone-") as tmp:
            tmp_dir = Path(tmp)
            sample_path = tmp_dir / "prepared.m4a"
            sample_path.write_bytes(b"fake")

            async def fake_workflow_run(**kwargs):
                self.assertEqual(kwargs["source_audio_path"], sample_path)
                return VocalCloneResult(
                    source_audio_path=sample_path,
                    vocal_id="vocal_987",
                )

            storage = _FakeStorage()
            app = self._build_app(tmp_dir, AsyncMock(side_effect=fake_workflow_run), storage)

            with patch(
                "EdennCode.Deployment.api_vocal_clone.prepare_audio_for_provider_b_vocal_clone",
                return_value=sample_path,
            ):
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/jobs/vocal-clone",
                        files={
                            "vocal_sample": (
                                "voice.wav",
                                b"fake-audio",
                                "audio/wav",
                            )
                        },
                    )

            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(payload["vocal_id"], "vocal_987")
            self.assertTrue(payload["vocal_sample_url"].startswith("https://example.test/uploads/"))
            self.assertEqual(len(storage.upload_calls), 1)


if __name__ == "__main__":
    unittest.main()
