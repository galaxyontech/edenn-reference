import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.api_common import ApiContext
from EdennCode.Deployment.api_video_generation import create_video_generation_router


class _FakeVideoMetadata:
    def __init__(
        self,
        path: Path,
        *,
        height: int = 720,
        temp_folder: Path | None = None,
    ) -> None:
        self.path = path.resolve()
        self.duration = 1.0
        self.size_bytes = 12
        self.width = 1280
        self.height = height
        self.fps = 30.0
        self.video_codec = "h264"
        self.video_bit_rate = None
        self.has_audio = False
        self.audio_codec = None
        self.audio_channels = None
        self.audio_sample_rate = None
        self.audio_bit_rate = None
        self.audio_activity = []
        self.temp_folder = str(temp_folder or path.parent / "metadata-temp")

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "duration": self.duration,
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "video_codec": self.video_codec,
            "video_bit_rate": self.video_bit_rate,
            "has_audio": self.has_audio,
            "audio_codec": self.audio_codec,
            "audio_channels": self.audio_channels,
            "audio_sample_rate": self.audio_sample_rate,
            "audio_bit_rate": self.audio_bit_rate,
            "audio_activity": self.audio_activity,
        }


class _FakeStorage:
    enabled = True

    def __init__(self) -> None:
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

    def generate_sas_url(self, *, container: str, blob_name: str) -> str:
        return f"https://storage.test/{container}/{blob_name}"


def _build_app(tmp_dir: Path, *, workflow: object, storage: _FakeStorage) -> FastAPI:
    settings = SimpleNamespace(
        workdir=tmp_dir / "jobs",
        music_volume=1.0,
        preserve_original_audio=False,
        upload_container="uploads",
        audio_container_name="audio",
        output_container="videos",
    )
    context = ApiContext(
        settings=settings,
        storage=storage,
        workflow=workflow,
        alignment_workflow=MagicMock(),
        audio_creative_edit_workflow=MagicMock(),
        logger=MagicMock(),
    )
    app = FastAPI()
    app.include_router(create_video_generation_router(context))
    return app


class VideoGenerationPreviewEndpointTests(unittest.TestCase):
    def test_compression_endpoint_does_not_upload_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            storage = _FakeStorage()
            workflow = SimpleNamespace(
                run=AsyncMock(),
                preview_pre_generation=AsyncMock(),
            )
            app = _build_app(tmp_dir, workflow=workflow, storage=storage)

            def _fake_compress(*, output_path: Path, **_kwargs):
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"compressed")
                return output_path

            with (
                patch(
                    "EdennCode.Deployment.api_video_generation._validate_staged_input_video_duration"
                ),
                patch(
                    "EdennCode.Deployment.api_video_generation.compress_video_to_max_height",
                    side_effect=_fake_compress,
                ) as mock_compress,
                patch(
                    "EdennCode.Deployment.api_video_generation.VideoMetadata.from_file",
                    side_effect=[
                        _FakeVideoMetadata(tmp_dir / "input.mp4", height=1600),
                        _FakeVideoMetadata(tmp_dir / "input_1280h.mp4", height=1280),
                    ],
                ),
            ):
                response = TestClient(app).post(
                    "/api/v1/jobs/video/compress",
                    files={"video": ("input.mp4", b"video", "video/mp4")},
                )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["compression_applied"])
        self.assertFalse(body["output_uploaded"])
        self.assertEqual(body["max_height"], 1280)
        self.assertEqual(body["output_video_metadata"]["height"], 1280)
        self.assertIsNone(body["output_blob"])
        self.assertIsNone(body["output_url"])
        self.assertTrue(body["output_path"].endswith("input_1280h.mp4"))
        timing = body["server_timing"]
        self.assertIsInstance(timing["hostname"], str)
        self.assertGreater(timing["pid"], 0)
        self.assertGreaterEqual(timing["total_server_s"], 0)
        self.assertIn("input_resolve_s", timing["stages"])
        self.assertIn("ffmpeg_compress_s", timing["stages"])
        self.assertEqual(timing["stages"]["output_upload_s"], 0.0)
        self.assertEqual(timing["stages"]["cleanup_s"], 0.0)
        self.assertEqual(mock_compress.call_count, 1)
        self.assertEqual(storage.upload_calls, [])

    def test_compression_endpoint_uploads_compressed_video_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            storage = _FakeStorage()
            workflow = SimpleNamespace(
                run=AsyncMock(),
                preview_pre_generation=AsyncMock(),
            )
            app = _build_app(tmp_dir, workflow=workflow, storage=storage)

            def _fake_compress(*, output_path: Path, **_kwargs):
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"compressed")
                return output_path

            with (
                patch(
                    "EdennCode.Deployment.api_video_generation._validate_staged_input_video_duration"
                ),
                patch(
                    "EdennCode.Deployment.api_video_generation.compress_video_to_max_height",
                    side_effect=_fake_compress,
                ) as mock_compress,
                patch(
                    "EdennCode.Deployment.api_video_generation.VideoMetadata.from_file",
                    side_effect=[
                        _FakeVideoMetadata(tmp_dir / "input.mp4", height=1600),
                        _FakeVideoMetadata(tmp_dir / "input_1280h.mp4", height=1280),
                    ],
                ),
            ):
                response = TestClient(app).post(
                    "/api/v1/jobs/video/compress",
                    files={"video": ("input.mp4", b"video", "video/mp4")},
                    data={"upload_output": "true"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["compression_applied"])
        self.assertTrue(body["output_uploaded"])
        self.assertEqual(body["max_height"], 1280)
        self.assertEqual(body["output_video_metadata"]["height"], 1280)
        self.assertTrue(body["output_url"].startswith("https://storage.test/uploads/"))
        self.assertIsNone(body["output_path"])
        timing = body["server_timing"]
        self.assertGreaterEqual(timing["total_server_s"], 0)
        self.assertIn("output_upload_s", timing["stages"])
        self.assertIn("cleanup_s", timing["stages"])
        self.assertEqual(mock_compress.call_count, 1)
        self.assertEqual(storage.upload_calls[0]["content_type"], "video/mp4")
        self.assertIn("/input/compressed/", storage.upload_calls[0]["blob_name"])

    def test_pre_generation_preview_endpoint_stops_before_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            thumbnail_path = tmp_dir / "thumbnail.webp"
            thumbnail_path.write_bytes(b"thumb")
            metadata = _FakeVideoMetadata(
                tmp_dir / "input.mp4",
                temp_folder=tmp_dir / "preview-temp",
            )
            result = SimpleNamespace(
                video_metadata=metadata,
                scenes=[
                    SimpleNamespace(
                        scene_index=0,
                        start_timestamp=0.0,
                        end_timestamp=1.0,
                        visual_summary="Blue title card",
                        key_actions="Static frame",
                        mood="calm",
                    )
                ],
                video_summary={"video_title": "Preview", "overall_mood": "calm"},
                video_title="Preview",
                video_description="A short preview video.",
                music_prompt={"style_prompt": "gentle synth pulse"},
                include_vocals=False,
                vocal_gender="female",
                used_music_model_spec="edenn_basic",
                user_requested_language="ENGLISH_US",
                sanitized_prompt="gentle brand background music",
                sanitized_style_prompt=None,
                sanitized_lyrics_prompt=None,
                detected_category="ADVERTISEMENT",
                was_transformed=False,
                detected_references=[],
                thumbnail_path=thumbnail_path,
                token_usage={
                    "prompt_tokens": 3,
                    "completion_tokens": 4,
                    "total_tokens": 7,
                },
                token_usage_breakdown={},
                job_received_timestamp=1,
                job_finished_timestamp=2,
                video_id="video-id",
                creative_id="creative-id",
                primary_music_id="primary-music-id",
                secondary_music_id=None,
                selected_music_id="primary-music-id",
                alignment_id="alignment-id",
                stage_timing_s={
                    "user_intent_s": 0.1,
                    "scene_segmentation_s": 0.2,
                    "video_understanding_s": 0.3,
                    "music_prompt_orchestration_s": 0.4,
                    "pre_generation_total_s": 1.2,
                },
            )
            workflow = SimpleNamespace(
                run=AsyncMock(),
                preview_pre_generation=AsyncMock(return_value=result),
            )
            storage = _FakeStorage()
            app = _build_app(tmp_dir, workflow=workflow, storage=storage)

            with patch(
                "EdennCode.Deployment.api_video_generation._validate_staged_input_video_duration"
            ):
                response = TestClient(app).post(
                    "/api/v1/jobs/video/pre-generation-preview",
                    files={"video": ("input.mp4", b"video", "video/mp4")},
                    data={
                        "user_prompt": "gentle brand background music",
                        "modelspec": "edenn_basic",
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["video_id"], "video-id")
        self.assertEqual(body["music_prompt"]["style_prompt"], "gentle synth pulse")
        self.assertEqual(body["scenes"][0]["visual_summary"], "Blue title card")
        self.assertEqual(body["token_usage"], 7)
        self.assertTrue(body["source_video_url"].startswith("https://storage.test/uploads/"))
        self.assertTrue(body["thumbnail_url"].startswith("https://storage.test/videos/"))
        timing = body["server_timing"]
        self.assertGreaterEqual(timing["total_server_s"], 0)
        self.assertIn("workflow_pre_generation_s", timing["stages"])
        self.assertEqual(timing["stages"]["workflow.user_intent_s"], 0.1)
        self.assertEqual(timing["stages"]["workflow.scene_segmentation_s"], 0.2)
        self.assertEqual(timing["stages"]["workflow.video_understanding_s"], 0.3)
        self.assertEqual(timing["stages"]["workflow.music_prompt_orchestration_s"], 0.4)
        self.assertEqual(timing["stages"]["workflow.llm_related_total_s"], 1.0)
        self.assertIn("source_upload_s", timing["stages"])
        self.assertIn("thumbnail_upload_s", timing["stages"])
        self.assertIn("cleanup_s", timing["stages"])
        workflow.preview_pre_generation.assert_awaited_once()
        workflow.run.assert_not_awaited()

    def test_pre_generation_preview_reuses_video_url_without_source_reupload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            thumbnail_path = tmp_dir / "thumbnail.webp"
            thumbnail_path.write_bytes(b"thumb")
            remote_video_url = "https://cdn.test/video/source.mp4"
            metadata = _FakeVideoMetadata(tmp_dir / "downloaded.mp4")
            result = SimpleNamespace(
                video_metadata=metadata,
                scenes=[
                    SimpleNamespace(
                        scene_index=0,
                        start_timestamp=0.0,
                        end_timestamp=1.0,
                        visual_summary="Downloaded input",
                        key_actions="Static frame",
                        mood="calm",
                    )
                ],
                video_summary={"video_title": "Preview", "overall_mood": "calm"},
                video_title="Preview",
                video_description="A URL based preview.",
                music_prompt={"style_prompt": "gentle synth pulse"},
                include_vocals=False,
                vocal_gender="female",
                used_music_model_spec="edenn_basic",
                user_requested_language="ENGLISH_US",
                sanitized_prompt="gentle brand background music",
                sanitized_style_prompt=None,
                sanitized_lyrics_prompt=None,
                detected_category="ADVERTISEMENT",
                was_transformed=False,
                detected_references=[],
                thumbnail_path=thumbnail_path,
                token_usage={},
                token_usage_breakdown={},
                job_received_timestamp=1,
                job_finished_timestamp=2,
                video_id="video-id",
                creative_id="creative-id",
                primary_music_id="primary-music-id",
                secondary_music_id=None,
                selected_music_id="primary-music-id",
                alignment_id="alignment-id",
                stage_timing_s={},
            )
            workflow = SimpleNamespace(
                run=AsyncMock(),
                preview_pre_generation=AsyncMock(return_value=result),
            )
            storage = _FakeStorage()
            app = _build_app(tmp_dir, workflow=workflow, storage=storage)

            async def _fake_download(*, destination: Path, **_kwargs) -> Path:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"video")
                return destination

            with (
                patch(
                    "EdennCode.Deployment.api_video_generation._validate_staged_input_video_duration"
                ),
                patch(
                    "EdennCode.Deployment.api_video_generation.download_public_file_to_disk",
                    side_effect=_fake_download,
                ) as mock_download,
            ):
                response = TestClient(app).post(
                    "/api/v1/jobs/video/pre-generation-preview",
                    data={
                        "video_url": remote_video_url,
                        "user_prompt": "gentle brand background music",
                        "modelspec": "edenn_basic",
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIsNone(body["source_video_blob"])
        self.assertEqual(body["source_video_url"], remote_video_url)
        self.assertIsNone(body["source_video_path"])
        self.assertTrue(body["thumbnail_url"].startswith("https://storage.test/videos/"))
        timing = body["server_timing"]
        self.assertEqual(timing["stages"]["source_upload_s"], 0.0)
        self.assertIn("thumbnail_upload_s", timing["stages"])
        self.assertEqual(len(storage.upload_calls), 1)
        self.assertIn("/thumbnail/", storage.upload_calls[0]["blob_name"])
        self.assertNotIn("/input/source_video", storage.upload_calls[0]["blob_name"])
        mock_download.assert_awaited_once()
        workflow.preview_pre_generation.assert_awaited_once()
        workflow.run.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
