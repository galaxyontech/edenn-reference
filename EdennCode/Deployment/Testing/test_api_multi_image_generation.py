import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import cv2
from fastapi import FastAPI
from fastapi.testclient import TestClient
import numpy as np

from EdennCode.Deployment.api_common import ApiContext
from EdennCode.Deployment.api_multi_image_generation import create_multi_image_generation_router
from EdennCode.Deployment.multi_image_workflows import MultiImageGenerationResult
from EdennCode.MusicGenerationCore.models import SectionTiming, TimestampedWord
from EdennCode.WorkflowFactory.VideoMusicWorkflow.PipelineDataModel.pipeline_datamodel import (
    Language,
)


def _encode_png_bytes(*, width: int, height: int) -> bytes:
    frame = np.full((height, width, 3), 140, dtype=np.uint8)
    ok, encoded = cv2.imencode(".png", frame)
    if not ok:
        raise RuntimeError("Failed to encode PNG test image")
    return encoded.tobytes()


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


class MultiImageGenerationApiTests(unittest.TestCase):
    def _build_app(
        self,
        tmp_dir: Path,
        workflow_run: AsyncMock,
        storage: _FakeStorage,
    ) -> FastAPI:
        settings = SimpleNamespace(
            workdir=tmp_dir / "jobs",
            music_volume=1.0,
            audio_container_name="audio",
            output_container="videos",
        )
        context = ApiContext(
            settings=settings,
            storage=storage,
            workflow=MagicMock(),
            alignment_workflow=MagicMock(),
            audio_creative_edit_workflow=MagicMock(),
            logger=MagicMock(),
            multi_image_workflow=SimpleNamespace(run=workflow_run),
        )
        app = FastAPI()
        app.include_router(create_multi_image_generation_router(context))
        return app

    def test_router_registers_route(self) -> None:
        context = ApiContext(
            settings=MagicMock(),
            storage=MagicMock(),
            workflow=MagicMock(),
            alignment_workflow=MagicMock(),
            audio_creative_edit_workflow=MagicMock(),
            logger=MagicMock(),
            multi_image_workflow=MagicMock(),
        )
        router = create_multi_image_generation_router(context)
        paths = sorted(route.path for route in router.routes)
        self.assertIn("/api/v1/jobs/multi-image", paths)

    def test_api_returns_descriptions_and_inferred_vocal_fields(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-multi-image-") as tmp:
            tmp_dir = Path(tmp)
            output_dir = tmp_dir / "workflow-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            generated_music_path = output_dir / "primary.mp3"
            final_video_path = output_dir / "slideshow.mp4"
            silent_video_path = output_dir / "slideshow_silent.mp4"
            alt_track_path = output_dir / "secondary.wav"
            processed_image = output_dir / "processed" / "frame1.png"
            for path, data in (
                (generated_music_path, b"ID3"),
                (final_video_path, b"video"),
                (silent_video_path, b"silent"),
                (alt_track_path, b"RIFF"),
                (processed_image, b"png"),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)

            async def fake_workflow_run(**kwargs):
                self.assertEqual(kwargs["modelspec"], "edenn_studio")
                self.assertEqual(kwargs["user_prompt"], "Make a Chinese female vocal pop anthem.")
                self.assertNotIn("include_vocals", kwargs)
                self.assertNotIn("vocal_gender", kwargs)
                self.assertNotIn("lyrics_language", kwargs)
                return MultiImageGenerationResult(
                    final_video_path=final_video_path,
                    silent_video_path=silent_video_path,
                    generated_music_path=generated_music_path,
                    full_track_paths=[generated_music_path, alt_track_path],
                    processed_image_paths=[processed_image],
                    planning_metadata={"image_order": [1, 2], "video_description": "A rising lifestyle reveal."},
                    music_prompt="Bright vocal pop",
                    lyrics_timestamps=[TimestampedWord(text="hi", startS=0.0, endS=0.5, i=0)],
                    section_timeline=[
                        SectionTiming(
                            section_id="intro",
                            expected_start_s=0.0,
                            expected_end_s=4.0,
                            actual_start_s=0.0,
                            actual_end_s=4.0,
                            confidence=1.0,
                        )
                    ],
                    video_title="Glow Frames",
                    music_title="Glow",
                    video_description="A rising lifestyle reveal.",
                    include_vocals=True,
                    vocal_gender="female",
                    lyrics_language="ZH",
                    user_requested_language=Language.CN,
                    used_music_model_spec="edenn_studio",
                    compression_applied=True,
                    vocal_id_used=None,
                    job_received_timestamp=100,
                    job_finished_timestamp=200,
                )

            storage = _FakeStorage()
            app = self._build_app(tmp_dir, AsyncMock(side_effect=fake_workflow_run), storage)

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/multi-image",
                    data={
                        "user_prompt": "Make a Chinese female vocal pop anthem.",
                        "modelspec": "provider_c",
                        "align_to_beats": "true",
                    },
                    files=[
                        ("images", ("frame1.png", _encode_png_bytes(width=64, height=64), "image/png")),
                        ("images", ("frame2.png", _encode_png_bytes(width=64, height=64), "image/png")),
                        ("images", ("frame3.png", _encode_png_bytes(width=64, height=64), "image/png")),
                    ],
                )

            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            video_metadata = payload["video_metadata"]
            summary = video_metadata["video_summary"]
            response_metadata = payload["response_metadata"]
            audio_metadata = payload["audio_metadata"]
            # Slideshow titles live in the summary (aligned with the video shape).
            self.assertEqual(summary["video_title"], "Glow Frames")
            self.assertEqual(summary["video_description"], "A rising lifestyle reveal.")
            # modelspec is top-level; request_metadata was removed in the alignment pass.
            self.assertEqual(payload["modelspec"], "edenn_studio")
            self.assertNotIn("request_metadata", payload)
            self.assertTrue(response_metadata["compression_applied"])
            self.assertTrue(
                audio_metadata["audio_url"].startswith("https://example.test/audio/")
            )
            # Alternate takes are never uploaded: only the generated track (there is
            # no distinct window here) plus the video.
            self.assertEqual(len(storage.upload_calls), 2)
            uploaded_paths = [call["path"] for call in storage.upload_calls]
            self.assertNotIn(alt_track_path, uploaded_paths)
            self.assertEqual(uploaded_paths, [generated_music_path, final_video_path])

    def test_api_forwards_edenn_enhanced_vocal_id_to_workflow(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-multi-image-") as tmp:
            tmp_dir = Path(tmp)
            output_dir = tmp_dir / "workflow-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            generated_music_path = output_dir / "primary.mp3"
            final_video_path = output_dir / "slideshow.mp4"
            silent_video_path = output_dir / "slideshow_silent.mp4"
            processed_image = output_dir / "processed" / "frame1.png"
            for path, data in (
                (generated_music_path, b"ID3"),
                (final_video_path, b"video"),
                (silent_video_path, b"silent"),
                (processed_image, b"png"),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)

            seen: dict = {}

            async def fake_workflow_run(**kwargs):
                seen.update(kwargs)
                return MultiImageGenerationResult(
                    final_video_path=final_video_path,
                    silent_video_path=silent_video_path,
                    generated_music_path=generated_music_path,
                    full_track_paths=[generated_music_path],
                    processed_image_paths=[processed_image],
                    planning_metadata={},
                    music_prompt="Bright vocal pop",
                    lyrics_timestamps=[],
                    section_timeline=[],
                    video_title="Glow Frames",
                    music_title="Glow",
                    video_description="A rising lifestyle reveal.",
                    include_vocals=True,
                    vocal_gender="female",
                    lyrics_language="EN",
                    user_requested_language=Language.EN,
                    used_music_model_spec="edenn_enhanced",
                    compression_applied=False,
                    vocal_id_used="vocal_321",
                    job_received_timestamp=100,
                    job_finished_timestamp=200,
                )

            storage = _FakeStorage()
            app = self._build_app(tmp_dir, AsyncMock(side_effect=fake_workflow_run), storage)

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/multi-image",
                    data={
                        "user_prompt": "Make a female vocal pop anthem.",
                        "modelspec": "edenn_enhanced",
                        "vocal_id": "vocal_321",
                    },
                    files=[
                        ("images", ("frame1.png", _encode_png_bytes(width=64, height=64), "image/png")),
                        ("images", ("frame2.png", _encode_png_bytes(width=64, height=64), "image/png")),
                        ("images", ("frame3.png", _encode_png_bytes(width=64, height=64), "image/png")),
                    ],
                )

            self.assertEqual(response.status_code, 200, response.text)
            # The response no longer echoes the vocal id, so verify the forwarding:
            # the pipeline must receive the caller's vocal_id under the enhanced spec.
            self.assertEqual(seen["modelspec"], "edenn_enhanced")
            self.assertEqual(seen["vocal_id"], "vocal_321")
            self.assertIsNone(seen["vocal_sample_path"])

    def test_api_rejects_invalid_modelspec(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-multi-image-") as tmp:
            tmp_dir = Path(tmp)
            storage = _FakeStorage()
            app = self._build_app(tmp_dir, AsyncMock(), storage)

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/multi-image",
                    data={"modelspec": "not_real"},
                    files=[
                        ("images", ("frame1.png", _encode_png_bytes(width=32, height=32), "image/png")),
                        ("images", ("frame2.png", _encode_png_bytes(width=32, height=32), "image/png")),
                        ("images", ("frame3.png", _encode_png_bytes(width=32, height=32), "image/png")),
                    ],
                )

            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("Invalid modelspec", response.text)

    def test_api_rejects_invalid_transition_name_in_list(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-multi-image-") as tmp:
            tmp_dir = Path(tmp)
            storage = _FakeStorage()
            app = self._build_app(tmp_dir, AsyncMock(), storage)

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/multi-image",
                    data={"transition": "fade,zoomout,wipeleft"},
                    files=[
                        ("images", ("f1.png", _encode_png_bytes(width=32, height=32), "image/png")),
                        ("images", ("f2.png", _encode_png_bytes(width=32, height=32), "image/png")),
                        ("images", ("f3.png", _encode_png_bytes(width=32, height=32), "image/png")),
                    ],
                )

            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("zoomout", response.text)

    def test_api_rejects_too_few_images_and_over_long_duration(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-multi-image-") as tmp:
            app = self._build_app(Path(tmp), AsyncMock(), _FakeStorage())
            with TestClient(app) as client:
                # Fewer than the 3-image minimum -> 400 (workflow never runs).
                too_few = client.post(
                    "/api/v1/jobs/multi-image",
                    data={"modelspec": "edenn_basic"},
                    files=[
                        ("images", ("f1.png", _encode_png_bytes(width=32, height=32), "image/png")),
                        ("images", ("f2.png", _encode_png_bytes(width=32, height=32), "image/png")),
                    ],
                )
                # Cumulative duration over the 150s cap (3 images x 60s = 180s) -> 400.
                too_long = client.post(
                    "/api/v1/jobs/multi-image",
                    data={"modelspec": "edenn_basic", "per_image_duration": "60"},
                    files=[
                        ("images", ("f1.png", _encode_png_bytes(width=32, height=32), "image/png")),
                        ("images", ("f2.png", _encode_png_bytes(width=32, height=32), "image/png")),
                        ("images", ("f3.png", _encode_png_bytes(width=32, height=32), "image/png")),
                    ],
                )
            self.assertEqual(too_few.status_code, 400, too_few.text)
            self.assertIn("At least 3 images", too_few.text)
            self.assertEqual(too_long.status_code, 400, too_long.text)
            self.assertIn("exceeds", too_long.text)

    def test_api_forwards_transition_list_to_workflow(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-multi-image-") as tmp:
            tmp_dir = Path(tmp)
            output_dir = tmp_dir / "workflow-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            final_video_path = output_dir / "slideshow.mp4"
            silent_video_path = output_dir / "slideshow_silent.mp4"
            generated_music_path = output_dir / "primary.mp3"
            processed_image = output_dir / "frame1.png"
            for path, data in (
                (final_video_path, b"video"),
                (silent_video_path, b"silent"),
                (generated_music_path, b"ID3"),
                (processed_image, b"png"),
            ):
                path.write_bytes(data)

            seen: dict = {}

            async def fake_workflow_run(**kwargs):
                seen.update(kwargs)
                return MultiImageGenerationResult(
                    final_video_path=final_video_path,
                    silent_video_path=silent_video_path,
                    generated_music_path=generated_music_path,
                    full_track_paths=[generated_music_path],
                    processed_image_paths=[processed_image],
                    planning_metadata={},
                    music_prompt="p",
                    lyrics_timestamps=[],
                    section_timeline=[],
                    video_title="t",
                    music_title="m",
                    video_description="d",
                    include_vocals=False,
                    vocal_gender="female",
                    lyrics_language="",
                    user_requested_language=Language.EN,
                    used_music_model_spec="edenn_basic",
                    compression_applied=False,
                    vocal_id_used=None,
                    job_received_timestamp=1,
                    job_finished_timestamp=2,
                )

            storage = _FakeStorage()
            app = self._build_app(tmp_dir, AsyncMock(side_effect=fake_workflow_run), storage)

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/multi-image",
                    data={"transition": "Fade, Dissolve , WipeLeft"},
                    files=[
                        ("images", ("f1.png", _encode_png_bytes(width=32, height=32), "image/png")),
                        ("images", ("f2.png", _encode_png_bytes(width=32, height=32), "image/png")),
                        ("images", ("f3.png", _encode_png_bytes(width=32, height=32), "image/png")),
                    ],
                )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(seen["transitions"], ["fade", "dissolve", "wipeleft"])
            self.assertEqual(seen["transition_mode"], "custom")


if __name__ == "__main__":
    unittest.main()


class WindowedLyricsTests(unittest.TestCase):
    """lyrics_timestamps are aligned to the delivered video (the window cut from
    the complete track); full_* keep the complete-track timeline. Regression for
    the live bug where a 9s slideshow carried lyrics spanning 20s-76s of the song.
    """

    @staticmethod
    def _result(**overrides):
        from EdennCode.Deployment.multi_image_workflows import MultiImageGenerationResult
        from EdennCode.MusicGenerationCore.models import TimestampedWord

        defaults = dict(
            final_video_path=Path("/tmp/x.mp4"),
            silent_video_path=Path("/tmp/s.mp4"),
            generated_music_path=Path("/tmp/a.mp3"),
            full_track_paths=[Path("/tmp/a.mp3")],
            processed_image_paths=[],
            planning_metadata={},
            music_prompt="p",
            # complete-track time (seconds): one line inside the window, one
            # straddling its end, one entirely after it.
            lyrics_timestamps=[
                TimestampedWord(text="in", startS=21.0, endS=23.0, i=0),
                TimestampedWord(text="straddle", startS=28.0, endS=31.0, i=1),
                TimestampedWord(text="after", startS=40.0, endS=44.0, i=2),
            ],
            section_timeline=[],
            video_title="t",
            music_title="m",
            video_description="d",
            include_vocals=True,
            vocal_gender="female",
            lyrics_language="EN",
            user_requested_language="EN",
            used_music_model_spec="edenn_enhanced",
            compression_applied=False,
            music_start_s=20.0,
        )
        defaults.update(overrides)
        return MultiImageGenerationResult(**defaults)

    def test_lyrics_are_windowed_and_rebased_full_keep_track_time(self) -> None:
        from EdennCode.Deployment.api_multi_image_generation import (
            _assemble_multi_image_response,
        )
        from EdennCode.Deployment.api_video_generation import VideoGeometry

        response = _assemble_multi_image_response(
            job_id="j",
            result=self._result(),
            audio_url="https://x/w.mp3",
            audio_duration_s=9.0,  # window = track time 20.0s .. 29.0s
            audio_size_bytes=1,
            video_url="https://x/v.mp4",
            geometry=VideoGeometry(duration_s=9.0),
        )
        am = response.audio_metadata

        # Windowed: "after" (40-44s) dropped; times rebased to the video and in ms;
        # the straddling line is clamped to the window end.
        windowed = [(w.text, w.startS, w.endS) for w in am.lyrics_timestamps]
        self.assertEqual(
            windowed, [("in", 1000.0, 3000.0), ("straddle", 8000.0, 9000.0)]
        )
        self.assertLessEqual(max(w.endS for w in am.lyrics_timestamps), 9.0 * 1000)

        # full_* keep every line, complete-track time (ms).
        self.assertEqual(
            [(w.text, w.startS, w.endS) for w in am.full_word_level_lyrics_timestamps],
            [("in", 21000.0, 23000.0), ("straddle", 28000.0, 31000.0),
             ("after", 40000.0, 44000.0)],
        )

    def test_zero_window_passes_everything_through(self) -> None:
        from EdennCode.Deployment.api_multi_image_generation import (
            _assemble_multi_image_response,
        )
        from EdennCode.Deployment.api_video_generation import VideoGeometry

        response = _assemble_multi_image_response(
            job_id="j",
            result=self._result(music_start_s=0.0),
            audio_url="https://x/w.mp3",
            audio_duration_s=60.0,
            audio_size_bytes=1,
            video_url="https://x/v.mp4",
            geometry=VideoGeometry(duration_s=60.0),
        )
        am = response.audio_metadata
        self.assertEqual(
            [w.text for w in am.lyrics_timestamps], ["in", "straddle", "after"]
        )
        self.assertEqual(
            [w.startS for w in am.lyrics_timestamps],
            [w.startS for w in am.full_word_level_lyrics_timestamps],
        )
