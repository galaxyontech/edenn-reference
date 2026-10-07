from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment import provider_vocabulary
from EdennCode.Deployment.api_common import ApiContext, JobStatus
from EdennCode.Deployment.api_video_generation import (
    AudioMetadataBlock,
    VideoJobResponse,
    VideoMetadataBlock,
    _AsyncJobState,
    _async_job_store,
    _async_video_job_max_in_flight_per_replica,
    _memory_async_video_job_store,
    _fire_callback,
    _run_async_video_job,
    create_video_generation_router,
)
from EdennCode.Deployment.output_naming import contains_provider_token
from EdennCode.Deployment.recommendation_persistence import RecommendationAssetIds
from EdennCode.exceptions import EdennContentPolicyViolationError, EdennProviderTimeoutError
from EdennCode.TestSuites.helpers.paths import SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH


# An invented upstream: the vendor vocabulary is deployment configuration, and a
# test file is a reader-visible surface that must not name a real provider.
_VENDOR = "Acme"
_PROVIDER_NAME = "acmesound"
_VOCABULARY = {
    # ``acme`` covers ``acmesound`` through the matcher's ``\w*`` tail.
    "PROVIDER_SCRUB_TOKENS": "acme",
    "MUSIC_PROVIDER_NAMES": _PROVIDER_NAME,
}


class _DisabledStorage:
    enabled = False


class _RecordingStorage:
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
        return f"https://example.test/{container}/{blob_name}?sig=test"


class _FakeVideoMetadata:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.duration = 1.0
        self.size_bytes = path.stat().st_size
        self.width = 320
        self.height = 240
        self.fps = 30.0

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "duration": self.duration,
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "audio_activity": [],
        }


class AsyncVideoGenerationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        _async_job_store.clear()

    def tearDown(self) -> None:
        _async_job_store.clear()

    def _configure_provider_vocabulary(self) -> None:
        """Install the invented vocabulary for the duration of one test.

        Both halves are load-bearing: without ``MUSIC_PROVIDER_NAMES`` the
        timeout triages as a generic AI failure rather than a music one, and
        without the scrub tokens ``contains_provider_token`` has nothing to
        match, so the leak check would pass on a payload that still named the
        vendor. The vocabulary is process-global and memoised, hence the cleanup.
        """
        prior = {key: os.environ.get(key) for key in _VOCABULARY}
        os.environ.update(_VOCABULARY)
        provider_vocabulary.reset_cache()

        def _restore() -> None:
            for key, value in prior.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            provider_vocabulary.reset_cache()

        self.addCleanup(_restore)

    def _build_context(
        self,
        tmp_dir: Path,
        workflow_run: AsyncMock,
        storage: object | None = None,
    ) -> ApiContext:
        settings = SimpleNamespace(
            workdir=tmp_dir / "jobs",
            music_volume=1.0,
            preserve_original_audio=False,
            upload_container="uploads",
            audio_container_name="audio",
            output_container="videos",
        )
        return ApiContext(
            settings=settings,
            storage=storage or _DisabledStorage(),
            workflow=SimpleNamespace(run=workflow_run),
            alignment_workflow=MagicMock(),
            audio_creative_edit_workflow=MagicMock(),
            logger=MagicMock(),
            async_video_job_store=_memory_async_video_job_store,
        )

    def _build_app(self, tmp_dir: Path, workflow_run: AsyncMock | None = None) -> FastAPI:
        context = self._build_context(tmp_dir, workflow_run or AsyncMock())
        app = FastAPI()
        app.include_router(create_video_generation_router(context))
        return app

    @staticmethod
    def _minimal_video_response(job_id: str) -> VideoJobResponse:
        return VideoJobResponse(
            job_id=job_id,
            video_metadata=VideoMetadataBlock(
                scenes=[],
                video_summary={},
            ),
            audio_metadata=AudioMetadataBlock(
                music_description="bright pop",
            ),
        )

    @staticmethod
    def _success_result(video_path: Path, output_dir: Path) -> SimpleNamespace:
        output_dir.mkdir(parents=True, exist_ok=True)
        generated_music_path = output_dir / "generated.mp3"
        remixed_video_path = output_dir / "remixed.mp4"
        generated_music_path.write_bytes(b"audio")
        remixed_video_path.write_bytes(b"video")
        return SimpleNamespace(
            video_metadata=_FakeVideoMetadata(video_path),
            scenes=[],
            video_summary={"video_title": "Async test"},
            music_prompt={"style_prompt": "bright pop"},
            generated_music_path=generated_music_path,
            complete_generated_music_path=None,
            secondary_complete_generated_music_path=None,
            remixed_video_path=remixed_video_path,
            include_vocals=False,
            vocal_gender="female",
            lyrics_timestamps=[],
            word_level_lyrics_timestamps=[],
            user_requested_language="ENGLISH_US",
            token_usage=None,
            token_usage_breakdown=None,
            used_music_model_spec="edenn_basic",
            job_received_timestamp=None,
            job_finished_timestamp=None,
            thumbnail_path=None,
        )

    @staticmethod
    def _provider_named_success_result(video_path: Path, output_dir: Path) -> SimpleNamespace:
        output_dir.mkdir(parents=True, exist_ok=True)
        generated_music_path = output_dir / "eleven_music_primary.mp3"
        complete_music_path = output_dir / "provider_b_song_full.mp3"
        secondary_music_path = output_dir / "provider_c_secondary.wav"
        remixed_video_path = output_dir / "provider_c_remix.mp4"
        thumbnail_path = output_dir / "provider_b_thumb.webp"
        for path, data in (
            (generated_music_path, b"audio"),
            (complete_music_path, b"complete"),
            (secondary_music_path, b"secondary"),
            (remixed_video_path, b"video"),
            (thumbnail_path, b"image"),
        ):
            path.write_bytes(data)
        return SimpleNamespace(
            video_metadata=_FakeVideoMetadata(video_path),
            scenes=[],
            video_summary={"video_title": "Provider naming regression"},
            music_prompt={"style_prompt": "bright pop", "lyrics_prompt": "Sing it clear."},
            generated_music_path=generated_music_path,
            complete_generated_music_path=complete_music_path,
            secondary_complete_generated_music_path=secondary_music_path,
            remixed_video_path=remixed_video_path,
            include_vocals=True,
            vocal_gender="female",
            primary_full_lyrics="Full glow line\nFull hold line",
            primary_full_lyrics_timestamps=[
                SimpleNamespace(text="Full glow line", startS=0.0, endS=1.2, i=0),
                SimpleNamespace(text="Full hold line", startS=1.2, endS=2.4, i=1),
            ],
            primary_full_word_level_lyrics_timestamps=[
                SimpleNamespace(text="Full", startS=0.0, endS=0.3, i=0),
                SimpleNamespace(text="glow", startS=0.3, endS=0.8, i=1),
                SimpleNamespace(text="line", startS=0.8, endS=1.2, i=2),
            ],
            secondary_full_lyrics=None,
            secondary_full_lyrics_timestamps=[],
            secondary_full_word_level_lyrics_timestamps=[],
            matching_used_track="primary",
            lyrics_timestamps=[
                SimpleNamespace(text="glow line", startS=0.0, endS=0.9, i=0),
            ],
            word_level_lyrics_timestamps=[
                SimpleNamespace(text="glow", startS=0.0, endS=0.5, i=0),
                SimpleNamespace(text="line", startS=0.5, endS=0.9, i=1),
            ],
            user_requested_language="ENGLISH_US",
            token_usage=None,
            token_usage_breakdown=None,
            used_music_model_spec="edenn_studio",
            job_received_timestamp=None,
            job_finished_timestamp=None,
            thumbnail_path=thumbnail_path,
            critical_warning=(
                "edenn_enhanced: upstream provider_b key primary balance is below "
                "safe operating threshold"
            ),
        )

    def test_async_post_registers_pending_job_and_forwards_normalized_inputs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-async-") as tmp:
            tmp_dir = Path(tmp)
            app = self._build_app(tmp_dir)
            calls: list[dict[str, object]] = []

            async def fake_run_async_video_job(**kwargs):
                calls.append(kwargs)

            with patch(
                "EdennCode.Deployment.api_video_generation._run_async_video_job",
                new=fake_run_async_video_job,
            ):
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/jobs/async_video_music_gen",
                        data={
                            "modelspec": "provider_c",
                            "music_volume": "0.7",
                            "vocal_gender": "Male",
                            "callback_url": "https://callback.test/video/job",
                        },
                        files={
                            "video": (
                                SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.name,
                                SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.read_bytes(),
                                "video/mp4",
                            )
                        },
                    )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["status"], "pending")
        self.assertIn(payload["job_id"], _async_job_store)
        self.assertEqual(len(calls), 1)
        forwarded = calls[0]
        self.assertEqual(forwarded["job_id"], payload["job_id"])
        self.assertEqual(forwarded["requested_modelspec"], "edenn_studio")
        self.assertEqual(forwarded["requested_volume"], 0.7)
        self.assertEqual(forwarded["vocal_gender"], "male")
        self.assertEqual(forwarded["callback_url"], "https://callback.test/video/job")

    def test_async_post_rejects_invalid_preflight_without_registering_job(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-async-invalid-") as tmp:
            app = self._build_app(Path(tmp))
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/async_video_music_gen",
                    data={"music_volume": "1.5"},
                )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("music_volume", response.json()["detail"])
        self.assertEqual(_async_job_store, {})

    def test_async_post_rejects_input_video_longer_than_300_seconds_without_registering_job(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-async-too-long-") as tmp:
            app = self._build_app(Path(tmp))
            with patch(
                "EdennCode.Deployment.api_video_generation.get_video_duration",
                return_value=300.1,
            ):
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/jobs/async_video_music_gen",
                        data={"modelspec": "edenn_basic"},
                        files={
                            "video": (
                                SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.name,
                                SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.read_bytes(),
                                "video/mp4",
                            )
                        },
                    )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("300 seconds", response.json()["detail"]["message"])
        self.assertEqual(_async_job_store, {})

    def test_async_post_rejects_local_callback_url(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-async-callback-") as tmp:
            app = self._build_app(Path(tmp))
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/async_video_music_gen",
                    data={"callback_url": "http://localhost:9999/callback"},
                )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("localhost", response.json()["detail"])
        self.assertEqual(_async_job_store, {})

    def test_async_max_in_flight_env_defaults_to_one_on_invalid_value(self) -> None:
        with patch.dict(
            "os.environ",
            {"ASYNC_VIDEO_MUSIC_MAX_IN_FLIGHT_PER_REPLICA": "not-an-int"},
        ):
            self.assertEqual(_async_video_job_max_in_flight_per_replica(), 1)

    def test_async_get_returns_completed_result_from_store(self) -> None:
        job_id = "job_done"
        _async_job_store[job_id] = _AsyncJobState(
            status=JobStatus.COMPLETED,
            result=self._minimal_video_response(job_id),
        )

        with tempfile.TemporaryDirectory(prefix="api-video-async-get-") as tmp:
            app = self._build_app(Path(tmp))
            with TestClient(app) as client:
                response = client.get(f"/api/v1/jobs/async_video_music_gen/{job_id}")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["result"]["job_id"], job_id)
        self.assertIsNone(payload["error"])

    def test_async_get_unknown_job_returns_404(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-async-missing-") as tmp:
            app = self._build_app(Path(tmp))
            with TestClient(app) as client:
                response = client.get("/api/v1/jobs/async_video_music_gen/missing")

        self.assertEqual(response.status_code, 404, response.text)
        self.assertIn("not found", response.json()["detail"])

    def test_async_get_expires_terminal_jobs_by_ttl(self) -> None:
        job_id = "old_job"
        _async_job_store[job_id] = _AsyncJobState(
            status=JobStatus.COMPLETED,
            finished_at=1,
        )

        with tempfile.TemporaryDirectory(prefix="api-video-async-ttl-") as tmp:
            app = self._build_app(Path(tmp))
            with patch.dict("os.environ", {"ASYNC_VIDEO_MUSIC_JOB_TTL_SECONDS": "1"}):
                with TestClient(app) as client:
                    response = client.get(f"/api/v1/jobs/async_video_music_gen/{job_id}")

        self.assertEqual(response.status_code, 404, response.text)
        self.assertNotIn(job_id, _async_job_store)

    def test_run_async_video_job_success_stores_completed_response(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-async-run-") as tmp:
            tmp_dir = Path(tmp)
            job_id = "job_success"
            job_dir = tmp_dir / "job"
            job_dir.mkdir(parents=True, exist_ok=True)
            input_video = job_dir / "input.mp4"
            input_video.write_bytes(b"video")
            output_dir = tmp_dir / "output"

            async def fake_workflow_run(**kwargs):
                return self._success_result(Path(kwargs["video_path"]), output_dir)

            context = self._build_context(tmp_dir, AsyncMock(side_effect=fake_workflow_run))
            asset_ids = RecommendationAssetIds.create(job_id=job_id)
            _async_job_store[job_id] = _AsyncJobState(status=JobStatus.PENDING)

            asyncio.run(
                _run_async_video_job(
                    context=context,
                    job_id=job_id,
                    job_dir=job_dir,
                    asset_ids=asset_ids,
                    effective_input_video_path=input_video,
                    upload_content_type="video/mp4",
                    compression_applied=False,
                    preserve_original_audio=False,
                    requested_volume=1.0,
                    include_vocals=False,
                    vocal_gender="female",
                    user_prompt="bright pop",
                    verbose_instruction=False,
                    music_style_prompt=None,
                    lyrics_prompt=None,
                    requested_modelspec="edenn_basic",
                    audio_output_format=None,
                    vocal_id=None,
                    vocal_sample_path=None,
                    user_id=None,
                    callback_url=None,
                )
            )

        state = _async_job_store[job_id]
        self.assertEqual(state.status, JobStatus.COMPLETED)
        self.assertIsNotNone(state.result)
        assert state.result is not None
        self.assertEqual(state.result.job_id, job_id)
        self.assertEqual(state.result.modelspec, "edenn_basic")

    def test_run_async_video_job_response_hides_provider_named_artifacts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-async-public-") as tmp:
            tmp_dir = Path(tmp)
            job_id = "job_public_names"
            job_dir = tmp_dir / "job"
            job_dir.mkdir(parents=True, exist_ok=True)
            input_video = job_dir / "provider_c_uploaded_clip.mp4"
            input_video.write_bytes(b"video")
            output_dir = tmp_dir / "output"

            async def fake_workflow_run(**kwargs):
                return self._provider_named_success_result(
                    Path(kwargs["video_path"]),
                    output_dir,
                )

            storage = _RecordingStorage()
            context = self._build_context(
                tmp_dir,
                AsyncMock(side_effect=fake_workflow_run),
                storage=storage,
            )
            asset_ids = RecommendationAssetIds.create(job_id=job_id)
            _async_job_store[job_id] = _AsyncJobState(status=JobStatus.PENDING)

            asyncio.run(
                _run_async_video_job(
                    context=context,
                    job_id=job_id,
                    job_dir=job_dir,
                    asset_ids=asset_ids,
                    effective_input_video_path=input_video,
                    upload_content_type="video/mp4",
                    compression_applied=False,
                    preserve_original_audio=False,
                    requested_volume=1.0,
                    include_vocals=True,
                    vocal_gender="female",
                    user_prompt="bright pop",
                    verbose_instruction=False,
                    music_style_prompt=None,
                    lyrics_prompt=None,
                    requested_modelspec="edenn_studio",
                    audio_output_format=None,
                    vocal_id=None,
                    vocal_sample_path=None,
                    user_id=None,
                    callback_url=None,
                )
            )

        state = _async_job_store[job_id]
        self.assertEqual(state.status, JobStatus.COMPLETED)
        self.assertIsNotNone(state.result)
        assert state.result is not None
        payload = state.result.model_dump(mode="json")
        serialized = json.dumps(payload, sort_keys=True)

        self.assertFalse(contains_provider_token(serialized), serialized)
        audio_metadata = payload["audio_metadata"]
        video_metadata = payload["video_metadata"]
        # Probe extras (path, codecs, bitrates, audio activity) are stripped from
        # the response geometry — only client-facing dimensions survive.
        self.assertNotIn("path", video_metadata["geometry"])
        self.assertEqual(
            set(video_metadata["geometry"]),
            {"width", "height", "duration", "fps"},
        )
        self.assertEqual(audio_metadata["full_lyrics"], "Full glow line\nFull hold line")
        self.assertEqual(
            audio_metadata["full_lyrics_timestamps"],
            [
                {"text": "Full glow line", "startS": 0.0, "endS": 1.2, "i": 0},
                {"text": "Full hold line", "startS": 1.2, "endS": 2.4, "i": 1},
            ],
        )
        self.assertEqual(
            audio_metadata["full_word_level_lyrics_timestamps"],
            [
                {"text": "Full", "startS": 0.0, "endS": 0.3, "i": 0},
                {"text": "glow", "startS": 0.3, "endS": 0.8, "i": 1},
                {"text": "line", "startS": 0.8, "endS": 1.2, "i": 2},
            ],
        )
        self.assertEqual(
            audio_metadata["lyrics_timestamps"],
            [{"text": "glow line", "startS": 0.0, "endS": 0.9, "i": 0}],
        )
        self.assertEqual(
            audio_metadata["word_level_lyrics_timestamps"],
            [
                {"text": "glow", "startS": 0.0, "endS": 0.5, "i": 0},
                {"text": "line", "startS": 0.5, "endS": 0.9, "i": 1},
            ],
        )
        # Blob names never reach the response any more, so the naming scheme is
        # asserted where it still exists: on what the pipeline handed to storage.
        self.assertEqual(len(storage.upload_calls), 6)
        self.assertEqual(
            [str(upload_call["blob_name"]) for upload_call in storage.upload_calls],
            [
                f"jobs/{job_id}/input/source_video.mp4",
                f"jobs/{job_id}/audio/matched_audio.mp3",
                f"jobs/{job_id}/audio/complete/complete_audio.mp3",
                f"jobs/{job_id}/audio/complete/secondary/secondary_audio.wav",
                f"jobs/{job_id}/video/remixed_video.mp4",
                f"jobs/{job_id}/thumbnail/thumbnail.webp",
            ],
        )
        for upload_call in storage.upload_calls:
            self.assertFalse(
                contains_provider_token(str(upload_call["blob_name"])),
                upload_call["blob_name"],
            )

    def test_run_async_video_job_failure_stores_error_detail(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-async-fail-") as tmp:
            tmp_dir = Path(tmp)
            job_id = "job_failure"
            job_dir = tmp_dir / "job"
            job_dir.mkdir(parents=True, exist_ok=True)
            input_video = job_dir / "input.mp4"
            input_video.write_bytes(b"video")

            async def fake_workflow_run(**kwargs):
                raise RuntimeError("provider failed")

            context = self._build_context(tmp_dir, AsyncMock(side_effect=fake_workflow_run))
            _async_job_store[job_id] = _AsyncJobState(status=JobStatus.PENDING)

            asyncio.run(
                _run_async_video_job(
                    context=context,
                    job_id=job_id,
                    job_dir=job_dir,
                    asset_ids=RecommendationAssetIds.create(job_id=job_id),
                    effective_input_video_path=input_video,
                    upload_content_type="video/mp4",
                    compression_applied=False,
                    preserve_original_audio=False,
                    requested_volume=1.0,
                    include_vocals=False,
                    vocal_gender="female",
                    user_prompt="bright pop",
                    verbose_instruction=False,
                    music_style_prompt=None,
                    lyrics_prompt=None,
                    requested_modelspec="edenn_basic",
                    audio_output_format=None,
                    vocal_id=None,
                    vocal_sample_path=None,
                    user_id=None,
                    callback_url=None,
                )
            )

        state = _async_job_store[job_id]
        self.assertEqual(state.status, JobStatus.FAILED)
        self.assertEqual(state.error["error_code"], 90001)
        self.assertIsNone(state.result)

    def test_run_async_video_job_failure_hides_provider_details(self) -> None:
        self._configure_provider_vocabulary()
        with tempfile.TemporaryDirectory(prefix="api-video-async-provider-fail-") as tmp:
            tmp_dir = Path(tmp)
            job_id = "job_provider_failure"
            job_dir = tmp_dir / "job"
            job_dir.mkdir(parents=True, exist_ok=True)
            input_video = job_dir / "input.mp4"
            input_video.write_bytes(b"video")

            async def fake_workflow_run(**kwargs):
                raise EdennProviderTimeoutError(
                    f"{_VENDOR} task timed out",
                    provider_name=_PROVIDER_NAME,
                    operation=f"{_PROVIDER_NAME}_generate",
                    retryable=True,
                )

            context = self._build_context(tmp_dir, AsyncMock(side_effect=fake_workflow_run))
            _async_job_store[job_id] = _AsyncJobState(status=JobStatus.PENDING)

            asyncio.run(
                _run_async_video_job(
                    context=context,
                    job_id=job_id,
                    job_dir=job_dir,
                    asset_ids=RecommendationAssetIds.create(job_id=job_id),
                    effective_input_video_path=input_video,
                    upload_content_type="video/mp4",
                    compression_applied=False,
                    preserve_original_audio=False,
                    requested_volume=1.0,
                    include_vocals=False,
                    vocal_gender="female",
                    user_prompt="bright pop",
                    verbose_instruction=False,
                    music_style_prompt=None,
                    lyrics_prompt=None,
                    requested_modelspec="edenn_studio",
                    audio_output_format=None,
                    vocal_id=None,
                    vocal_sample_path=None,
                    user_id=None,
                    callback_url=None,
                )
            )

        state = _async_job_store[job_id]
        self.assertEqual(state.status, JobStatus.FAILED)
        self.assertIsNone(state.result)
        assert state.error is not None
        serialized = json.dumps(state.error, sort_keys=True)
        self.assertFalse(contains_provider_token(serialized), serialized)
        self.assertEqual(state.error["status"], "failed")
        self.assertEqual(state.error["error_code"], 30200)
        self.assertEqual(
            state.error["message"],
            "The request took too long to complete. Please try again.",
        )
        self.assertTrue(state.error["retryable"])
        self.assertNotIn("provider_name", state.error)
        self.assertNotIn("component", state.error)
        self.assertNotIn("operation", state.error)
        self.assertNotIn("type", state.error)

    def test_run_async_video_job_content_policy_error_is_publicly_structured(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-async-content-policy-") as tmp:
            tmp_dir = Path(tmp)
            job_id = "job_content_policy"
            job_dir = tmp_dir / "job"
            job_dir.mkdir(parents=True, exist_ok=True)
            input_video = job_dir / "input.mp4"
            input_video.write_bytes(b"video")

            async def fake_workflow_run(**kwargs):
                raise EdennContentPolicyViolationError(
                    "the model gateway content policy violation for model chat-test",
                    provider_name="model_gateway",
                    operation="chat.completions.create",
                    policy_code="ResponsibleAIPolicyViolation",
                    provider_error_code="content_filter",
                    param="prompt",
                    filter_results={
                        "sexual": {"filtered": True, "severity": "high"},
                    },
                    retryable=False,
                )

            context = self._build_context(tmp_dir, AsyncMock(side_effect=fake_workflow_run))
            _async_job_store[job_id] = _AsyncJobState(status=JobStatus.PENDING)

            asyncio.run(
                _run_async_video_job(
                    context=context,
                    job_id=job_id,
                    job_dir=job_dir,
                    asset_ids=RecommendationAssetIds.create(job_id=job_id),
                    effective_input_video_path=input_video,
                    upload_content_type="video/mp4",
                    compression_applied=False,
                    preserve_original_audio=False,
                    requested_volume=1.0,
                    include_vocals=True,
                    vocal_gender="female",
                    user_prompt="cinematic vocals",
                    verbose_instruction=False,
                    music_style_prompt=None,
                    lyrics_prompt=None,
                    requested_modelspec="edenn_studio",
                    audio_output_format=None,
                    vocal_id=None,
                    vocal_sample_path=None,
                    user_id=None,
                    callback_url=None,
                )
            )

        state = _async_job_store[job_id]
        self.assertEqual(state.status, JobStatus.FAILED)
        self.assertIsNone(state.result)
        assert state.error is not None
        self.assertEqual(state.error["status"], "failed")
        self.assertEqual(state.error["error_code"], 11001)
        self.assertEqual(
            state.error["message"],
            "The request was flagged by our content safety system. "
            "Please modify your input and try again.",
        )
        self.assertFalse(state.error["retryable"])
        self.assertNotIn("filter_results", state.error)
        self.assertNotIn("provider_name", state.error)

    def test_fire_callback_posts_final_payload(self) -> None:
        posts: list[tuple[str, dict[str, object]]] = []

        class FakeAsyncClient:
            def __init__(self, *, timeout: int) -> None:
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def post(self, url: str, *, json: dict[str, object]):
                posts.append((url, json))
                return SimpleNamespace(status_code=202)

        state = _AsyncJobState(
            status=JobStatus.COMPLETED,
            created_at=123,
            result=self._minimal_video_response("job_callback"),
        )
        logger = MagicMock()

        with patch(
            "EdennCode.Deployment.api_video_generation.httpx.AsyncClient",
            new=FakeAsyncClient,
        ):
            asyncio.run(
                _fire_callback(
                    "https://callback.test/done",
                    "job_callback",
                    state,
                    logger=logger,
                )
            )

        self.assertEqual(len(posts), 1)
        url, payload = posts[0]
        self.assertEqual(url, "https://callback.test/done")
        self.assertEqual(payload["job_id"], "job_callback")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["created_at"], 123)
        self.assertEqual(payload["result"]["job_id"], "job_callback")
        logger.warning.assert_not_called()

    def test_fire_callback_failure_is_logged_but_not_raised(self) -> None:
        class FakeAsyncClient:
            def __init__(self, *, timeout: int) -> None:
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def post(self, url: str, *, json: dict[str, object]):
                raise RuntimeError("callback down")

        logger = MagicMock()
        with patch(
            "EdennCode.Deployment.api_video_generation.httpx.AsyncClient",
            new=FakeAsyncClient,
        ):
            asyncio.run(
                _fire_callback(
                    "https://callback.test/down",
                    "job_callback",
                    _AsyncJobState(status=JobStatus.FAILED, error={"message": "failed"}),
                    logger=logger,
                )
            )

        logger.warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
