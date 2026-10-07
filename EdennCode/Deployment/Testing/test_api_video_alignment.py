import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.alignment_workflows import (
    AudioAlignmentResult,
    AudioAlignmentSegmentResult,
)
from EdennCode.Deployment.api_common import ApiContext
from EdennCode.Deployment.api_video_alignment import create_video_alignment_router
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata.video_metadata import (
    VideoMetadata,
)
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

    def generate_sas_url(self, *, container: str, blob_name: str) -> str:
        return f"https://example.test/{container}/{blob_name}"


class VideoAlignmentApiTests(unittest.TestCase):
    def _build_app(
        self,
        tmp_dir: Path,
        workflow_run: AsyncMock,
        storage: _FakeStorage,
    ) -> FastAPI:
        settings = SimpleNamespace(
            workdir=tmp_dir / "jobs",
            audio_container_name="audio",
        )
        context = ApiContext(
            settings=settings,
            storage=storage,
            workflow=MagicMock(),
            alignment_workflow=SimpleNamespace(run=workflow_run),
            audio_creative_edit_workflow=MagicMock(),
            logger=MagicMock(),
        )
        app = FastAPI()
        app.include_router(create_video_alignment_router(context))
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
        router = create_video_alignment_router(context)
        paths = sorted(route.path for route in router.routes)  # type: ignore
        self.assertIn("/api/v1/jobs/video-align-audio", paths)

    def test_api_returns_ranked_alignment_segments(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-alignment-") as tmp:
            tmp_dir = Path(tmp)
            first_render = tmp_dir / "alignment_rank_1.wav"
            second_render = tmp_dir / "alignment_rank_2.wav"
            first_render.write_bytes(b"RIFF")
            second_render.write_bytes(b"RIFF")

            async def fake_workflow_run(**kwargs):
                self.assertEqual(kwargs["top_k"], 2)
                self.assertEqual(len(kwargs["lyrics_timestamps"]), 1)
                self.assertTrue(Path(kwargs["video_path"]).exists())
                self.assertTrue(Path(kwargs["audio_path"]).exists())
                segments = [
                    AudioAlignmentSegmentResult(
                        rank=1,
                        music_start_s=0.5,
                        music_end_s=4.5,
                        score=0.92,
                        details={"window_index": 0},
                        rendered_audio_path=first_render,
                        aligned_lyrics=[
                            WordTS(text="hey", startS=0.6, endS=1.0, i=0)],
                    ),
                    AudioAlignmentSegmentResult(
                        rank=2,
                        music_start_s=2.0,
                        music_end_s=6.0,
                        score=0.81,
                        details={"window_index": 1},
                        rendered_audio_path=second_render,
                        aligned_lyrics=[],
                    ),
                ]
                return AudioAlignmentResult(
                    video_metadata=VideoMetadata(
                        path=Path(kwargs["video_path"]),
                        duration=8.0,
                        size_bytes=128,
                        width=1280,
                        height=720,
                        fps=30.0,
                        video_codec="h264",
                        video_bit_rate=1_000_000,
                        has_audio=False,
                        audio_codec=None,
                        audio_channels=None,
                        audio_sample_rate=None,
                        audio_bit_rate=None,
                        temp_folder=str(tmp_dir / "temp"),
                        audio_activity=[],
                    ),
                    best_segment=segments[0],
                    segments=segments,
                    lyrics_provided=True,
                )

            storage = _FakeStorage()
            app = self._build_app(tmp_dir, AsyncMock(
                side_effect=fake_workflow_run), storage)

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video-align-audio",
                    data={
                        "top_k": "2",
                        "lyrics_timestamps_json": json.dumps(
                            [{"text": "hey", "startS": 0.6, "endS": 1.0, "i": 0}]
                        ),
                    },
                    files={
                        "video": ("input.mp4", b"fake-video", "video/mp4"),
                        "audio": ("input.wav", b"fake-audio", "audio/wav"),
                    },
                )

            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertTrue(payload["lyrics_provided"])
            self.assertEqual(payload["best_segment"]["rank"], 1)
            self.assertEqual(len(payload["segments"]), 2)
            self.assertEqual(payload["segments"][0]
                             ["aligned_lyrics"][0]["text"], "hey")
            self.assertTrue(payload["segments"][0]["matched_audio_url"].startswith(
                "https://example.test/audio/"))
            self.assertEqual(payload["video_metadata"]["width"], 1280)
            self.assertEqual(len(storage.upload_calls), 2)

    def test_api_rejects_invalid_top_k(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-alignment-") as tmp:
            tmp_dir = Path(tmp)
            storage = _FakeStorage()
            app = self._build_app(tmp_dir, AsyncMock(), storage)

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video-align-audio",
                    data={"top_k": "6"},
                    files={
                        "video": ("input.mp4", b"fake-video", "video/mp4"),
                        "audio": ("input.wav", b"fake-audio", "audio/wav"),
                    },
                )

            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("top_k must be between 1 and 5", response.text)


if __name__ == "__main__":
    unittest.main()
