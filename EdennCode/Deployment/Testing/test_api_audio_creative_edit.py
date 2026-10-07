import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.api_audio_creative_edit import create_audio_creative_edit_router
from EdennCode.Deployment.api_common import ApiContext
from EdennCode.Deployment.audio_edit_workflows import AudioCreativeEditResult
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.word_ts import (
    WordTS,
)


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


class AudioCreativeEditApiTests(unittest.TestCase):
    def _build_app(
        self,
        tmp_dir: Path,
        workflow_run: AsyncMock,
        storage: _FakeStorage,
    ) -> FastAPI:
        settings = SimpleNamespace(
            workdir=tmp_dir / "jobs",
            upload_container="uploads",
            audio_container_name="audio",
            output_container="videos",
        )
        context = ApiContext(
            settings=settings,
            storage=storage,
            workflow=MagicMock(),
            alignment_workflow=MagicMock(),
            audio_creative_edit_workflow=SimpleNamespace(run=workflow_run),
            logger=MagicMock(),
        )
        app = FastAPI()
        app.include_router(create_audio_creative_edit_router(context))
        return app

    def test_router_registers_route(self) -> None:
        context = ApiContext(
            settings=MagicMock(),
            storage=MagicMock(),
            workflow=MagicMock(),
            alignment_workflow=MagicMock(),
            audio_creative_edit_workflow=MagicMock(),
            logger=MagicMock(),
        )
        router = create_audio_creative_edit_router(context)
        paths = sorted(route.path for route in router.routes)
        self.assertIn("/api/v1/jobs/audio-creative-edit", paths)

    def test_api_returns_staged_audio_urls_and_prompt_payload(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-audio-creative-edit-") as tmp:
            tmp_dir = Path(tmp)
            output_dir = tmp_dir / "workflow-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            source_audio = output_dir / "source.wav"
            edited_audio = output_dir / "edited.wav"
            secondary_audio = output_dir / "edited_alt.wav"
            thumbnail_path = output_dir / "thumbnail.png"
            for path, data in (
                (source_audio, b"RIFF"),
                (edited_audio, b"RIFF"),
                (secondary_audio, b"RIFF"),
                (thumbnail_path, b"\x89PNG"),
            ):
                path.write_bytes(data)

            async def fake_workflow_run(**kwargs):
                self.assertEqual(kwargs["modelspec"], "edenn_studio")
                self.assertEqual(kwargs["user_prompt"], "Turn this into a dramatic vocal cover.")
                self.assertIsNone(kwargs["video_path"])
                self.assertEqual(kwargs["image_paths"], [])
                self.assertFalse(kwargs["provider_c_custom_mode"])
                self.assertIsNone(kwargs["provider_c_style_weight"])
                self.assertIsNone(kwargs["provider_c_audio_weight"])
                self.assertIsNone(kwargs["provider_c_weirdness_constraint"])
                self.assertTrue(kwargs["source_audio_provider_url"].startswith("https://example.test/uploads/"))
                return AudioCreativeEditResult(
                    source_audio_path=source_audio,
                    edited_audio_path=edited_audio,
                    secondary_edited_audio_path=secondary_audio,
                    visual_analysis=SimpleNamespace(
                        input_type="none",
                        summary="",
                        overall_mood="",
                        visual_style="",
                        creative_direction="",
                        key_elements=[],
                        scenes=[],
                        thumbnail_path=thumbnail_path,
                    ),
                    creative_edit_prompt={
                        "title": "Night Pulse",
                        "edit_intent_summary": "Lift the source into a dramatic chorus-led cover.",
                        "style_prompt": "dramatic synth-pop cover with strong lift",
                        "lyrics_prompt": "glow through the city night",
                    },
                    lyrics_timestamps=[WordTS(text="glow", startS=0.0, endS=0.6, i=0)],
                    include_vocals=True,
                    vocal_gender="female",
                    user_requested_language="ENGLISH_US",
                    token_usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                    token_usage_breakdown={
                        "user_prompt_preprocessor": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
                        "visual_analysis": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                        "creative_edit_prompt": {"prompt_tokens": 9, "completion_tokens": 6, "total_tokens": 15},
                    },
                    used_music_model_spec="edenn_studio",
                    job_received_timestamp=100,
                    job_finished_timestamp=200,
                )

            storage = _FakeStorage()
            app = self._build_app(tmp_dir, AsyncMock(side_effect=fake_workflow_run), storage)

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/audio-creative-edit",
                    data={
                        "user_prompt": "Turn this into a dramatic vocal cover.",
                        "modelspec": "provider_c",
                    },
                    files={
                        "audio": (
                            "source.wav",
                            b"fake-audio",
                            "audio/wav",
                        )
                    },
                )

            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(payload["modelspec"], "edenn_studio")
            self.assertEqual(payload["creative_edit_prompt"]["title"], "Night Pulse")
            self.assertEqual(payload["vocal_gender"], "female")
            self.assertEqual(payload["user_requested_language"], "ENGLISH_US")
            self.assertEqual(payload["token_usage"], 18)
            self.assertEqual(len(payload["lyrics_timestamps"]), 1)
            self.assertTrue(payload["source_audio_url"].startswith("https://example.test/uploads/"))
            self.assertTrue(payload["edited_audio_url"].startswith("https://example.test/audio/"))
            self.assertTrue(payload["secondary_edited_audio_url"].startswith("https://example.test/audio/"))
            self.assertTrue(payload["thumbnail_url"].startswith("https://example.test/videos/"))
            self.assertEqual(len(storage.upload_calls), 4)

    def test_api_forwards_edenn_studio_custom_mode_and_weights(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-audio-creative-edit-") as tmp:
            tmp_dir = Path(tmp)
            source_audio = tmp_dir / "source.wav"
            edited_audio = tmp_dir / "edited.wav"
            for path, data in (
                (source_audio, b"RIFF"),
                (edited_audio, b"RIFF"),
            ):
                path.write_bytes(data)

            async def fake_workflow_run(**kwargs):
                self.assertEqual(kwargs["modelspec"], "edenn_studio")
                self.assertTrue(kwargs["provider_c_custom_mode"])
                self.assertEqual(kwargs["provider_c_style_weight"], 0.65)
                self.assertEqual(kwargs["provider_c_audio_weight"], 0.75)
                self.assertEqual(kwargs["provider_c_weirdness_constraint"], 0.2)
                return AudioCreativeEditResult(
                    source_audio_path=source_audio,
                    edited_audio_path=edited_audio,
                    secondary_edited_audio_path=None,
                    visual_analysis=SimpleNamespace(
                        input_type="none",
                        summary="",
                        overall_mood="",
                        visual_style="",
                        creative_direction="",
                        key_elements=[],
                        scenes=[],
                        thumbnail_path=None,
                    ),
                    creative_edit_prompt={},
                    lyrics_timestamps=[],
                    include_vocals=False,
                    vocal_gender="female",
                    user_requested_language="ENGLISH_US",
                    token_usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    token_usage_breakdown={},
                    used_music_model_spec="edenn_studio",
                    job_received_timestamp=100,
                    job_finished_timestamp=200,
                )

            storage = _FakeStorage()
            app = self._build_app(tmp_dir, AsyncMock(side_effect=fake_workflow_run), storage)

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/audio-creative-edit",
                    data={
                        "user_prompt": "Make this darker and more cinematic.",
                        "modelspec": "edenn_studio",
                        "studio_mode": "custom",
                        "studio_style_weight": "0.65",
                        "studio_audio_weight": "0.75",
                        "studio_weirdness_constraint": "0.20",
                    },
                    files={
                        "audio": (
                            "source.wav",
                            b"fake-audio",
                            "audio/wav",
                        )
                    },
                )

            self.assertEqual(response.status_code, 200, response.text)

    def test_api_forwards_edenn_enhanced_vocal_id_and_returns_vocal_id_used(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-audio-creative-edit-") as tmp:
            tmp_dir = Path(tmp)
            source_audio = tmp_dir / "source.wav"
            edited_audio = tmp_dir / "edited.wav"
            for path, data in (
                (source_audio, b"RIFF"),
                (edited_audio, b"RIFF"),
            ):
                path.write_bytes(data)

            async def fake_workflow_run(**kwargs):
                self.assertEqual(kwargs["modelspec"], "edenn_enhanced")
                self.assertEqual(kwargs["vocal_id"], "vocal_123")
                self.assertIsNone(kwargs["vocal_sample_path"])
                return AudioCreativeEditResult(
                    source_audio_path=source_audio,
                    edited_audio_path=edited_audio,
                    secondary_edited_audio_path=None,
                    visual_analysis=SimpleNamespace(
                        input_type="none",
                        summary="",
                        overall_mood="",
                        visual_style="",
                        creative_direction="",
                        key_elements=[],
                        scenes=[],
                        thumbnail_path=None,
                    ),
                    creative_edit_prompt={},
                    lyrics_timestamps=[],
                    include_vocals=True,
                    vocal_gender="female",
                    user_requested_language="ENGLISH_US",
                    token_usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    token_usage_breakdown={},
                    used_music_model_spec="edenn_enhanced",
                    job_received_timestamp=100,
                    job_finished_timestamp=200,
                    vocal_id_used="vocal_123",
                )

            storage = _FakeStorage()
            app = self._build_app(tmp_dir, AsyncMock(side_effect=fake_workflow_run), storage)

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/audio-creative-edit",
                    data={
                        "user_prompt": "Turn this into a modern female vocal pop cover.",
                        "modelspec": "edenn_enhanced",
                        "vocal_id": "vocal_123",
                    },
                    files={
                        "audio": (
                            "source.wav",
                            b"fake-audio",
                            "audio/wav",
                        )
                    },
                )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["vocal_id_used"], "vocal_123")

    def test_api_rejects_studio_weights_in_simple_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-audio-creative-edit-") as tmp:
            tmp_dir = Path(tmp)
            storage = _FakeStorage()
            app = self._build_app(tmp_dir, AsyncMock(), storage)

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/audio-creative-edit",
                    data={
                        "modelspec": "edenn_studio",
                        "studio_mode": "simple",
                        "studio_style_weight": "0.65",
                    },
                    files={
                        "audio": (
                            "source.wav",
                            b"fake-audio",
                            "audio/wav",
                        )
                    },
                )

            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("studio_mode=custom", response.text)

    def test_api_rejects_invalid_modelspec(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-audio-creative-edit-") as tmp:
            tmp_dir = Path(tmp)
            storage = _FakeStorage()
            app = self._build_app(tmp_dir, AsyncMock(), storage)

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/audio-creative-edit",
                    data={"modelspec": "not_real"},
                    files={
                        "audio": (
                            "source.wav",
                            b"fake-audio",
                            "audio/wav",
                        )
                    },
                )

            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("Invalid modelspec", response.text)


if __name__ == "__main__":
    unittest.main()
