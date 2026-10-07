from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import cv2
import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.api_common import ApiContext, JobStatus
from EdennCode.Deployment.api_video_generation import (
    AudioMetadataBlock,
    VideoMetadataBlock,
)
from EdennCode.Deployment.api_multi_image_generation import (
    MultiImageJobResponse,
    _AsyncJobState,
    _PreparedMultiImageJob,
    _async_job_max_in_flight_per_replica,
    _run_async_multi_image_job,
    create_multi_image_generation_router,
)
from EdennCode.Deployment.async_video_job_store import InMemoryAsyncVideoJobStore
from EdennCode.Deployment.multi_image_workflows import MultiImageGenerationResult
from EdennCode.MusicGenerationCore.models import SectionTiming, TimestampedWord


def _encode_png_bytes(*, width: int, height: int) -> bytes:
    frame = np.full((height, width, 3), 140, dtype=np.uint8)
    ok, encoded = cv2.imencode(".png", frame)
    if not ok:
        raise RuntimeError("Failed to encode PNG test image")
    return encoded.tobytes()


class _DisabledStorage:
    enabled = False


def _make_result(output_dir: Path) -> MultiImageGenerationResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    music_path = output_dir / "primary.mp3"
    video_path = output_dir / "slideshow.mp4"
    silent_path = output_dir / "slideshow_silent.mp4"
    for path, data in (
        (music_path, b"ID3"),
        (video_path, b"video"),
        (silent_path, b"silent"),
    ):
        path.write_bytes(data)
    return MultiImageGenerationResult(
        final_video_path=video_path,
        silent_video_path=silent_path,
        generated_music_path=music_path,
        full_track_paths=[music_path],
        processed_image_paths=[],
        planning_metadata={"image_order": [1, 2]},
        music_prompt="Bright pop",
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
        include_vocals=False,
        vocal_gender="female",
        lyrics_language="",
        user_requested_language="ENGLISH_US",
        used_music_model_spec="edenn_basic",
        compression_applied=False,
        vocal_id_used=None,
        job_received_timestamp=100,
        job_finished_timestamp=200,
    )


class AsyncMultiImageApiTests(unittest.TestCase):
    def _build_context(
        self,
        tmp_dir: Path,
        *,
        workflow_run: AsyncMock | None = None,
        storage: object | None = None,
        store: InMemoryAsyncVideoJobStore | None = None,
    ) -> ApiContext:
        settings = SimpleNamespace(
            workdir=tmp_dir / "jobs",
            music_volume=1.0,
            audio_container_name="audio",
            output_container="videos",
        )
        return ApiContext(
            settings=settings,
            storage=storage or _DisabledStorage(),
            workflow=MagicMock(),
            alignment_workflow=MagicMock(),
            audio_creative_edit_workflow=MagicMock(),
            logger=MagicMock(),
            multi_image_workflow=SimpleNamespace(run=workflow_run or AsyncMock()),
            multi_image_async_job_store=store or InMemoryAsyncVideoJobStore(),
        )

    def _build_app(self, context: ApiContext) -> FastAPI:
        app = FastAPI()
        app.include_router(create_multi_image_generation_router(context))
        return app

    def test_async_post_registers_pending_job_and_forwards_inputs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-multi-image-async-") as tmp:
            store = InMemoryAsyncVideoJobStore()
            context = self._build_context(Path(tmp), store=store)
            app = self._build_app(context)
            calls: list[dict[str, object]] = []

            async def fake_run(**kwargs):
                calls.append(kwargs)

            with patch(
                "EdennCode.Deployment.api_multi_image_generation._run_async_multi_image_job",
                new=fake_run,
            ):
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/jobs/async_multi-image",
                        data={
                            "modelspec": "provider_c",
                            "align_to_beats": "true",
                            "callback_url": "https://callback.test/multi/job",
                        },
                        files=[
                            ("images", ("a.png", _encode_png_bytes(width=64, height=64), "image/png")),
                            ("images", ("b.png", _encode_png_bytes(width=64, height=64), "image/png")),
                            ("images", ("c.png", _encode_png_bytes(width=64, height=64), "image/png")),
                        ],
                    )

        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertEqual(payload["status"], "pending")
        job_id = payload["job_id"]
        self.assertIsNotNone(store.get(job_id))
        self.assertEqual(store.get(job_id).status, JobStatus.PENDING)
        self.assertEqual(len(calls), 1)
        prepared = calls[0]["prepared"]
        self.assertEqual(prepared.requested_modelspec, "edenn_studio")
        self.assertEqual(calls[0]["callback_url"], "https://callback.test/multi/job")

    def test_async_post_rejects_invalid_modelspec_without_registering(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-multi-image-async-bad-") as tmp:
            store = InMemoryAsyncVideoJobStore()
            context = self._build_context(Path(tmp), store=store)
            app = self._build_app(context)
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/async_multi-image",
                    data={"modelspec": "not_real"},
                    files=[
                        ("images", ("a.png", _encode_png_bytes(width=32, height=32), "image/png")),
                        ("images", ("b.png", _encode_png_bytes(width=32, height=32), "image/png")),
                        ("images", ("c.png", _encode_png_bytes(width=32, height=32), "image/png")),
                    ],
                )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("Invalid modelspec", response.text)
        self.assertEqual(store.jobs, {})

    def test_async_post_rejects_local_callback_url(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-multi-image-async-cb-") as tmp:
            store = InMemoryAsyncVideoJobStore()
            context = self._build_context(Path(tmp), store=store)
            app = self._build_app(context)
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/async_multi-image",
                    data={"callback_url": "http://localhost:9000/cb"},
                    files=[("images", ("a.png", _encode_png_bytes(width=32, height=32), "image/png"))],
                )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("localhost", response.json()["detail"])
        self.assertEqual(store.jobs, {})

    def test_async_get_returns_completed_result_from_store(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-multi-image-async-get-") as tmp:
            store = InMemoryAsyncVideoJobStore()
            job_id = "job_done"
            store.jobs[job_id] = _AsyncJobState(
                status=JobStatus.COMPLETED,
                result=MultiImageJobResponse(
                    job_id=job_id,
                    video_metadata=VideoMetadataBlock(
                        video_summary={"video_title": "Glow", "video_description": "desc"},
                    ),
                    audio_metadata=AudioMetadataBlock(),
                ),
            )
            context = self._build_context(Path(tmp), store=store)
            app = self._build_app(context)
            with TestClient(app) as client:
                response = client.get(f"/api/v1/jobs/async_multi-image/{job_id}")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["result"]["job_id"], job_id)
        self.assertIsNone(payload["error"])

    def test_async_get_unknown_job_returns_404(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-multi-image-async-missing-") as tmp:
            context = self._build_context(Path(tmp))
            app = self._build_app(context)
            with TestClient(app) as client:
                response = client.get("/api/v1/jobs/async_multi-image/missing")

        self.assertEqual(response.status_code, 404, response.text)
        self.assertIn("not found", response.json()["detail"])

    def test_max_in_flight_env_defaults_to_one_on_invalid_value(self) -> None:
        with patch.dict(
            "os.environ",
            {"ASYNC_MULTI_IMAGE_MAX_IN_FLIGHT_PER_REPLICA": "not-an-int"},
        ):
            self.assertEqual(_async_job_max_in_flight_per_replica(), 1)

    def test_run_async_job_success_stores_completed_response(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-multi-image-async-run-") as tmp:
            tmp_dir = Path(tmp)
            store = InMemoryAsyncVideoJobStore()
            job_id = "job_success"
            job_dir = tmp_dir / "jobs" / job_id
            job_dir.mkdir(parents=True, exist_ok=True)

            async def fake_run(**kwargs):
                return _make_result(tmp_dir / "out")

            context = self._build_context(
                tmp_dir, workflow_run=AsyncMock(side_effect=fake_run), store=store
            )
            store.register(job_id)
            prepared = _PreparedMultiImageJob(
                job_dir=job_dir,
                source_dir=job_dir / "source" / "images",
                output_video_path=job_dir / "output" / "story.mp4",
                requested_modelspec="edenn_basic",
                requested_volume=1.0,
                normalized_vocal_id=None,
                prepared_vocal_sample_path=None,
            )

            asyncio.run(
                _run_async_multi_image_job(
                    context=context,
                    job_id=job_id,
                    prepared=prepared,
                    user_prompt="bright pop",
                    align_to_beats=True,
                    per_image_duration=3.0,
                    water_mark=False,
                    callback_url=None,
                )
            )

            state = store.get(job_id)
            self.assertEqual(state.status, JobStatus.COMPLETED)
            self.assertIsNotNone(state.result)
            self.assertEqual(state.result.job_id, job_id)
            self.assertEqual(state.result.modelspec, "edenn_basic")
            # Background runner owns temp-dir cleanup.
            self.assertFalse(job_dir.exists())

    def test_run_async_job_failure_stores_error_detail(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-multi-image-async-fail-") as tmp:
            tmp_dir = Path(tmp)
            store = InMemoryAsyncVideoJobStore()
            job_id = "job_failure"
            job_dir = tmp_dir / "jobs" / job_id
            job_dir.mkdir(parents=True, exist_ok=True)

            async def fake_run(**kwargs):
                raise RuntimeError("provider failed")

            context = self._build_context(
                tmp_dir, workflow_run=AsyncMock(side_effect=fake_run), store=store
            )
            store.register(job_id)
            prepared = _PreparedMultiImageJob(
                job_dir=job_dir,
                source_dir=job_dir / "source" / "images",
                output_video_path=job_dir / "output" / "story.mp4",
                requested_modelspec="edenn_basic",
                requested_volume=1.0,
                normalized_vocal_id=None,
                prepared_vocal_sample_path=None,
            )

            asyncio.run(
                _run_async_multi_image_job(
                    context=context,
                    job_id=job_id,
                    prepared=prepared,
                    user_prompt="bright pop",
                    align_to_beats=True,
                    per_image_duration=3.0,
                    water_mark=False,
                    callback_url=None,
                )
            )

            state = store.get(job_id)
            self.assertEqual(state.status, JobStatus.FAILED)
            self.assertEqual(state.error["error_code"], 90001)
            self.assertIsNone(state.result)


if __name__ == "__main__":
    unittest.main()
