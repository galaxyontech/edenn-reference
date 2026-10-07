import os
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import cv2
from fastapi import FastAPI
from fastapi.testclient import TestClient
import numpy as np

from EdennCode.Deployment import provider_vocabulary
from EdennCode.Deployment.api_common import ApiContext
from EdennCode.Deployment.api_video_generation import (
    LEGACY_MODEL_MAP,
    VALID_MUSIC_MODEL_SPECS,
    create_video_generation_router,
)
from EdennCode.Deployment.output_naming import contains_provider_token
from EdennCode.exceptions import EdennMediaProcessingError, EdennProviderTimeoutError
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata.video_metadata import (
    VideoMetadata,
)
from EdennCode.Util.MediaUtils.ffmpeg_utils import resolve_ffmpeg_binary
from EdennCode.TestSuites.helpers.paths import (
    ROTATED_SMOKE_VIDEO_PATH,
    SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH,
)


# An invented upstream: the vendor vocabulary is deployment configuration, and a
# test file is a reader-visible surface that must not name a real provider.
_VENDOR = "Acme"
_PROVIDER_NAME = "acmesound"
_VOCABULARY = {
    # ``acme`` covers ``acmesound`` through the matcher's ``\w*`` tail.
    "PROVIDER_SCRUB_TOKENS": "acme",
    "MUSIC_PROVIDER_NAMES": _PROVIDER_NAME,
}


def _legacy_alias_for(target_modelspec: str) -> str:
    """Return an inbound legacy client alias that normalises to target_modelspec.

    Derived from the shipped map rather than written out, so the aliases stay
    covered without spelling a vendor name in the tests.
    """
    for alias, mapped in sorted(LEGACY_MODEL_MAP.items()):
        if mapped == target_modelspec:
            return alias
    raise AssertionError(f"no legacy alias maps to {target_modelspec}")


def _write_test_video(path: Path, *, width: int, height: int, duration_s: float = 1.0) -> None:
    ffmpeg_bin = resolve_ffmpeg_binary()
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=blue:size={width}x{height}:rate=1:duration={duration_s}",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _probe_dimensions(video_path: Path) -> tuple[int, int]:
    ffprobe_bin = str(Path(resolve_ffmpeg_binary()).with_name("ffprobe"))
    result = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(video_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    width_raw, height_raw = result.stdout.strip().split("x", 1)
    return int(width_raw), int(height_raw)


def _assert_error_detail(
    test_case: unittest.TestCase,
    response: Any,
    *,
    status_code: int,
    error_code: int,
    message: str,
    retryable: bool = False,
) -> None:
    test_case.assertEqual(response.status_code, status_code, response.text)
    detail = response.json()["detail"]
    test_case.assertEqual(detail["status"], "failed")
    test_case.assertEqual(detail["error_code"], error_code)
    test_case.assertEqual(detail["message"], message)
    test_case.assertEqual(detail["retryable"], retryable)


@dataclass
class _FakeVideoMetadata:
    path: Path
    duration: float
    size_bytes: int
    width: int
    height: int
    fps: float
    video_codec: str = "h264"
    video_bit_rate: int | None = None
    has_audio: bool = False
    audio_codec: str | None = None
    audio_channels: int | None = None
    audio_sample_rate: int | None = None
    audio_bit_rate: int | None = None

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
            "audio_activity": [],
        }


class _FakeStorage:
    def __init__(self) -> None:
        self.enabled = True
        self.upload_calls: list[dict[str, str | Path]] = []

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


class _FakeRecommendationPersistence:
    def __init__(self) -> None:
        self.payloads: list[Any] = []

    def persist_video_generation(self, payload: Any) -> None:
        self.payloads.append(payload)


class VideoGenerationCompressionApiTests(unittest.TestCase):
    def _configure_provider_vocabulary(self) -> None:
        """Install the invented vocabulary for the duration of one test.

        Both halves are load-bearing: without ``MUSIC_PROVIDER_NAMES`` the
        timeout triages as a generic AI failure rather than a music one, and
        without the scrub tokens ``contains_provider_token`` has nothing to
        match, so the leak check would pass on a response that still named the
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

    def _build_app(
        self,
        tmp_dir: Path,
        workflow_run: AsyncMock,
        storage: _FakeStorage,
        recommendation_persistence: object | None = None,
    ) -> FastAPI:
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
            workflow=SimpleNamespace(run=workflow_run),
            alignment_workflow=MagicMock(),
            audio_creative_edit_workflow=MagicMock(),
            logger=MagicMock(),
            recommendation_persistence=recommendation_persistence,
        )
        app = FastAPI()
        app.include_router(create_video_generation_router(context))
        return app

    @staticmethod
    def _write_thumbnail_webp(path: Path, *, width: int = 24, height: int = 16) -> None:
        frame = np.full((height, width, 3), 120, dtype=np.uint8)
        ok, encoded = cv2.imencode(".webp", frame)
        if not ok:
            raise RuntimeError("Failed to encode WebP test thumbnail")
        path.write_bytes(encoded.tobytes())

    @staticmethod
    def _build_success_result(
        *,
        video_path: Path,
        output_dir: Path,
        include_vocals: bool,
        used_music_model_spec: str,
        vocal_id_used: str | None = None,
    ) -> SimpleNamespace:
        generated_music_path = output_dir / "generated.mp3"
        remixed_video_path = output_dir / "remixed.mp4"
        generated_music_path.write_bytes(b"audio")
        remixed_video_path.write_bytes(b"video")
        return SimpleNamespace(
            video_metadata=_FakeVideoMetadata(
                path=video_path.resolve(),
                duration=1.0,
                size_bytes=video_path.stat().st_size,
                width=320,
                height=240,
                fps=30.0,
            ),
            scenes=[],
            video_summary={},
            music_prompt={},
            generated_music_path=generated_music_path,
            complete_generated_music_path=None,
            secondary_complete_generated_music_path=None,
            remixed_video_path=remixed_video_path,
            include_vocals=include_vocals,
            vocal_gender="female",
            lyrics_timestamps=[],
            word_level_lyrics_timestamps=[],
            user_requested_language="ENGLISH_US",
            token_usage=None,
            token_usage_breakdown=None,
            used_music_model_spec=used_music_model_spec,
            job_received_timestamp=None,
            job_finished_timestamp=None,
            thumbnail_path=None,
            vocal_id_used=vocal_id_used,
        )

    def test_api_passes_stable_job_and_asset_ids_to_workflow(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-rec-ids-") as tmp:
            tmp_dir = Path(tmp)
            source_path = SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH
            output_dir = tmp_dir / "workflow-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            persistence = _FakeRecommendationPersistence()

            async def fake_workflow_run(**kwargs):
                return self._build_success_result(
                    video_path=Path(kwargs["video_path"]),
                    output_dir=output_dir,
                    include_vocals=False,
                    used_music_model_spec="edenn_basic",
                )

            workflow_run = AsyncMock(side_effect=fake_workflow_run)
            app = self._build_app(
                tmp_dir,
                workflow_run,
                _FakeStorage(),
                recommendation_persistence=persistence,
            )

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video",
                    data={"modelspec": "edenn_basic"},
                    files={
                        "video": (
                            source_path.name,
                            source_path.read_bytes(),
                            "video/mp4",
                        )
                    },
                )

            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            assert workflow_run.await_args is not None
            workflow_kwargs = workflow_run.await_args.kwargs
            self.assertEqual(payload["job_id"], workflow_kwargs["job_id"])

            # The asset ids left the HTTP response but are still handed to the
            # workflow and still written to the recommendation records, so the
            # "same stable ids everywhere" contract is asserted there instead.
            for id_field in (
                "video_id",
                "creative_id",
                "primary_music_id",
                "selected_music_id",
                "alignment_id",
            ):
                self.assertTrue(workflow_kwargs[id_field], id_field)

            self.assertEqual(len(persistence.payloads), 1)
            stored_payload = persistence.payloads[0]
            generation_job = stored_payload.generation_job
            self.assertEqual(generation_job.job_id, workflow_kwargs["job_id"])
            self.assertEqual(generation_job.video_id, workflow_kwargs["video_id"])
            self.assertEqual(generation_job.creative_id, workflow_kwargs["creative_id"])
            self.assertEqual(
                generation_job.primary_music_id,
                workflow_kwargs["primary_music_id"],
            )
            self.assertEqual(
                generation_job.selected_music_id,
                workflow_kwargs["selected_music_id"],
            )
            self.assertEqual(
                generation_job.alignment_id,
                workflow_kwargs["alignment_id"],
            )
            self.assertEqual(
                stored_payload.video_asset.video_id,
                workflow_kwargs["video_id"],
            )
            self.assertEqual(
                stored_payload.creative.creative_id,
                workflow_kwargs["creative_id"],
            )
            self.assertEqual(
                stored_payload.primary_music_asset.music_id,
                workflow_kwargs["primary_music_id"],
            )
            self.assertEqual(
                stored_payload.alignment.alignment_id,
                workflow_kwargs["alignment_id"],
            )

    def test_api_forwards_verbose_instruction_split_prompts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-verbose-") as tmp:
            tmp_dir = Path(tmp)
            source_path = SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH
            output_dir = tmp_dir / "workflow-output"
            output_dir.mkdir(parents=True, exist_ok=True)

            async def fake_workflow_run(**kwargs):
                self.assertTrue(kwargs["verbose_instruction"])
                self.assertEqual(kwargs["user_prompt"], "")
                self.assertEqual(
                    kwargs["music_style_prompt"],
                    "Mandopop female vocals with bright drums.",
                )
                self.assertEqual(
                    kwargs["lyrics_prompt"],
                    "Lyrics about a coastal holiday launch.",
                )
                self.assertEqual(kwargs["modelspec"], "edenn_enhanced")
                return self._build_success_result(
                    video_path=Path(kwargs["video_path"]),
                    output_dir=output_dir,
                    include_vocals=True,
                    used_music_model_spec="edenn_enhanced",
                )

            workflow_run = AsyncMock(side_effect=fake_workflow_run)
            app = self._build_app(tmp_dir, workflow_run, _FakeStorage())

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video",
                    data={
                        "verbose_instruction": "true",
                        "music_style_prompt": " Mandopop female vocals with bright drums. ",
                        "lyrics_prompt": " Lyrics about a coastal holiday launch. ",
                        "modelspec": "edenn_enhanced",
                    },
                    files={
                        "video": (
                            source_path.name,
                            source_path.read_bytes(),
                            "video/mp4",
                        )
                    },
                )

            self.assertEqual(response.status_code, 200, response.text)
            workflow_run.assert_awaited_once()

    def test_api_allows_verbose_instruction_without_lyrics_prompt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-verbose-no-lyrics-") as tmp:
            tmp_dir = Path(tmp)
            source_path = SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH
            output_dir = tmp_dir / "workflow-output"
            output_dir.mkdir(parents=True, exist_ok=True)

            async def fake_workflow_run(**kwargs):
                self.assertTrue(kwargs["verbose_instruction"])
                self.assertEqual(
                    kwargs["music_style_prompt"],
                    "Female vocal cinematic pop in English.",
                )
                self.assertIsNone(kwargs["lyrics_prompt"])
                return self._build_success_result(
                    video_path=Path(kwargs["video_path"]),
                    output_dir=output_dir,
                    include_vocals=True,
                    used_music_model_spec="edenn_studio",
                )

            workflow_run = AsyncMock(side_effect=fake_workflow_run)
            app = self._build_app(tmp_dir, workflow_run, _FakeStorage())

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video",
                    data={
                        "verbose_instruction": "true",
                        "music_style_prompt": "Female vocal cinematic pop in English.",
                        "modelspec": "edenn_studio",
                    },
                    files={
                        "video": (
                            source_path.name,
                            source_path.read_bytes(),
                            "video/mp4",
                        )
                    },
                )

            self.assertEqual(response.status_code, 200, response.text)
            workflow_run.assert_awaited_once()

    def test_api_rejects_verbose_instruction_with_user_prompt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-verbose-invalid-") as tmp:
            tmp_dir = Path(tmp)
            source_path = SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH
            workflow_run = AsyncMock()
            app = self._build_app(tmp_dir, workflow_run, _FakeStorage())

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video",
                    data={
                        "verbose_instruction": "true",
                        "user_prompt": "legacy mixed prompt",
                        "music_style_prompt": "female vocal pop",
                        "modelspec": "edenn_enhanced",
                    },
                    files={
                        "video": (
                            source_path.name,
                            source_path.read_bytes(),
                            "video/mp4",
                        )
                    },
                )

            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("omit user_prompt", response.json()["detail"])
            workflow_run.assert_not_awaited()

    def test_api_rejects_verbose_instruction_without_music_style_prompt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-verbose-invalid-") as tmp:
            tmp_dir = Path(tmp)
            source_path = SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH
            workflow_run = AsyncMock()
            app = self._build_app(tmp_dir, workflow_run, _FakeStorage())

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video",
                    data={
                        "verbose_instruction": "true",
                        "modelspec": "edenn_enhanced",
                    },
                    files={
                        "video": (
                            source_path.name,
                            source_path.read_bytes(),
                            "video/mp4",
                        )
                    },
                )

            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("music_style_prompt is required", response.json()["detail"])
            workflow_run.assert_not_awaited()

    def test_api_rejects_verbose_instruction_for_basic_model(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-verbose-invalid-") as tmp:
            tmp_dir = Path(tmp)
            source_path = SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH
            workflow_run = AsyncMock()
            app = self._build_app(tmp_dir, workflow_run, _FakeStorage())

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video",
                    data={
                        "verbose_instruction": "true",
                        "music_style_prompt": "instrumental electronic underscore",
                        "modelspec": "edenn_basic",
                    },
                    files={
                        "video": (
                            source_path.name,
                            source_path.read_bytes(),
                            "video/mp4",
                        )
                    },
                )

            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn(
                "edenn_enhanced or modelspec=edenn_studio",
                response.json()["detail"],
            )
            workflow_run.assert_not_awaited()

    def test_api_rejects_split_prompts_without_verbose_instruction(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-verbose-invalid-") as tmp:
            tmp_dir = Path(tmp)
            source_path = SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH
            workflow_run = AsyncMock()
            app = self._build_app(tmp_dir, workflow_run, _FakeStorage())

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video",
                    data={
                        "music_style_prompt": "female vocal pop",
                        "modelspec": "edenn_enhanced",
                    },
                    files={
                        "video": (
                            source_path.name,
                            source_path.read_bytes(),
                            "video/mp4",
                        )
                    },
                )

            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("require verbose_instruction=true", response.json()["detail"])
            workflow_run.assert_not_awaited()

    def test_preview_endpoint_keeps_frozen_v1_verbose_contract(self) -> None:
        # The v1 preview endpoint shares _validate_prompt_fields, which stayed
        # on the old verbose contract when v2 moved to presence-keyed
        # lyrics_prompt. This is the freeze proof: a freestanding lyrics_prompt
        # (legal on v2) must still 400 here.
        with tempfile.TemporaryDirectory(prefix="api-video-preview-freeze-") as tmp:
            tmp_dir = Path(tmp)
            source_path = SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH
            workflow_run = AsyncMock()
            app = self._build_app(tmp_dir, workflow_run, _FakeStorage())

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video/pre-generation-preview",
                    data={
                        "lyrics_prompt": "themes of home",
                        "modelspec": "edenn_enhanced",
                    },
                    files={
                        "video": (
                            source_path.name,
                            source_path.read_bytes(),
                            "video/mp4",
                        )
                    },
                )

            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("require verbose_instruction=true", response.json()["detail"])
            workflow_run.assert_not_awaited()

    def test_api_persists_recommendation_payload_after_success(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-rec-persist-") as tmp:
            tmp_dir = Path(tmp)
            source_path = SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH
            output_dir = tmp_dir / "workflow-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            persistence = _FakeRecommendationPersistence()

            async def fake_workflow_run(**kwargs):
                result = self._build_success_result(
                    video_path=Path(kwargs["video_path"]),
                    output_dir=output_dir,
                    include_vocals=True,
                    used_music_model_spec="edenn_enhanced",
                )
                result.job_id = kwargs["job_id"]
                result.video_id = kwargs["video_id"]
                result.creative_id = kwargs["creative_id"]
                result.primary_music_id = kwargs["primary_music_id"]
                result.selected_music_id = kwargs["selected_music_id"]
                result.alignment_id = kwargs["alignment_id"]
                result.video_title = "Feed title"
                result.video_description = "Feed description"
                result.music_start_s = 1.25
                result.alignment_score = 0.91
                result.alignment_details = {"align_score": 0.91}
                result.primary_full_lyrics = "Full glow line\nFull hold line"
                result.primary_full_word_level_lyrics_timestamps = [
                    SimpleNamespace(text="Full", startS=0.0, endS=0.3, i=0),
                    SimpleNamespace(text="glow", startS=0.3, endS=0.8, i=1),
                ]
                result.primary_full_lyrics_timestamps = [
                    SimpleNamespace(text="Full glow line", startS=0.0, endS=1.2, i=0),
                ]
                result.lyrics_timestamps = [
                    SimpleNamespace(text="glow line", startS=0.0, endS=0.9, i=0),
                ]
                result.word_level_lyrics_timestamps = [
                    SimpleNamespace(text="glow", startS=0.0, endS=0.5, i=0),
                    SimpleNamespace(text="line", startS=0.5, endS=0.9, i=1),
                ]
                result.matching_used_track = "primary"
                return result

            workflow_run = AsyncMock(side_effect=fake_workflow_run)
            app = self._build_app(
                tmp_dir,
                workflow_run,
                _FakeStorage(),
                recommendation_persistence=persistence,
            )

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video",
                    data={"modelspec": "edenn_basic"},
                    files={
                        "video": (
                            source_path.name,
                            source_path.read_bytes(),
                            "video/mp4",
                        )
                    },
                )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(persistence.payloads), 1)
            stored_payload = persistence.payloads[0]
            response_payload = response.json()
            assert workflow_run.await_args is not None
            workflow_kwargs = workflow_run.await_args.kwargs
            self.assertEqual(stored_payload.generation_job.job_id, response_payload["job_id"])
            # creative_id is no longer echoed in the response; it is still the id
            # the workflow ran with, so the persisted record is checked against that.
            self.assertEqual(
                stored_payload.creative.creative_id,
                workflow_kwargs["creative_id"],
            )
            self.assertEqual(stored_payload.alignment.alignment_score, 0.91)
            self.assertEqual(stored_payload.alignment.selected_clip_start_s, 1.25)
            self.assertEqual(stored_payload.alignment.matching_used_track, "primary")
            self.assertEqual(stored_payload.creative.title, "Feed title")
            self.assertEqual(stored_payload.primary_music_asset.lyrics_text, "Full glow line\nFull hold line")
            self.assertEqual(
                stored_payload.primary_music_asset.lyrics_timestamp_json,
                [
                    {"text": "Full", "startS": 0.0, "endS": 0.3, "i": 0},
                    {"text": "glow", "startS": 0.3, "endS": 0.8, "i": 1},
                ],
            )

    def test_api_skips_compression_for_input_at_or_below_threshold(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-compression-") as tmp:
            tmp_dir = Path(tmp)
            source_path = tmp_dir / "input.mov"
            output_dir = tmp_dir / "workflow-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            _write_test_video(source_path, width=96, height=1200)

            storage = _FakeStorage()
            seen_paths: list[Path] = []

            async def fake_workflow_run(**kwargs):
                workflow_input_path = Path(kwargs["video_path"])
                seen_paths.append(workflow_input_path)
                width, height = _probe_dimensions(workflow_input_path)
                generated_music_path = output_dir / "generated.mp3"
                remixed_video_path = output_dir / "remixed.mp4"
                generated_music_path.write_bytes(b"audio")
                remixed_video_path.write_bytes(b"video")
                return SimpleNamespace(
                    video_metadata=_FakeVideoMetadata(
                        path=workflow_input_path.resolve(),
                        duration=1.0,
                        size_bytes=workflow_input_path.stat().st_size,
                        width=width,
                        height=height,
                        fps=1.0,
                    ),
                    scenes=[],
                    video_summary={},
                    music_prompt={},
                    generated_music_path=generated_music_path,
                    complete_generated_music_path=None,
                    secondary_complete_generated_music_path=None,
                    remixed_video_path=remixed_video_path,
                    include_vocals=False,
                    vocal_gender="female",
                    lyrics_timestamps=[],
                    user_requested_language="",
                    token_usage=None,
                    token_usage_breakdown=None,
                    used_music_model_spec="edenn_basic",
                    job_received_timestamp=None,
                    job_finished_timestamp=None,
                    thumbnail_path=None,
                )

            app = self._build_app(
                tmp_dir,
                AsyncMock(side_effect=fake_workflow_run),
                storage,
            )

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video",
                    data={"compression_flag": "true", "modelspec": "edenn_basic"},
                    files={
                        "video": (
                            source_path.name,
                            source_path.read_bytes(),
                            "video/quicktime",
                        )
                    },
                )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(seen_paths), 1)
            self.assertEqual(seen_paths[0].name, "input.mov")
            self.assertFalse(seen_paths[0].name.endswith("_1280h.mp4"))
            self.assertEqual(
                response.json()["video_metadata"]["geometry"]["height"], 1200
            )

            input_upload = storage.upload_calls[0]
            self.assertEqual(input_upload["content_type"], "video/quicktime")
            self.assertEqual(Path(input_upload["path"]).name, "input.mov")

    def test_api_exposes_word_level_lyrics_without_changing_existing_field(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-lyrics-") as tmp:
            tmp_dir = Path(tmp)
            source_path = SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH
            output_dir = tmp_dir / "workflow-output"
            output_dir.mkdir(parents=True, exist_ok=True)

            storage = _FakeStorage()

            async def fake_workflow_run(**kwargs):
                workflow_input_path = Path(kwargs["video_path"])
                generated_music_path = output_dir / "generated.mp3"
                remixed_video_path = output_dir / "remixed.mp4"
                generated_music_path.write_bytes(b"audio")
                remixed_video_path.write_bytes(b"video")
                return SimpleNamespace(
                    video_metadata=_FakeVideoMetadata(
                        path=workflow_input_path.resolve(),
                        duration=1.0,
                        size_bytes=workflow_input_path.stat().st_size,
                        width=320,
                        height=240,
                        fps=30.0,
                    ),
                    scenes=[],
                    video_summary={},
                    music_prompt={},
                    generated_music_path=generated_music_path,
                    complete_generated_music_path=None,
                    secondary_complete_generated_music_path=None,
                    remixed_video_path=remixed_video_path,
                    include_vocals=True,
                    vocal_gender="female",
                    primary_full_lyrics="Glow tonight\nHold the line",
                    primary_full_lyrics_timestamps=[
                        SimpleNamespace(text="Glow tonight", startS=0.0, endS=600.0, i=0),
                    ],
                    primary_full_word_level_lyrics_timestamps=[
                        SimpleNamespace(text="Glow", startS=0.0, endS=200.0, i=0),
                        SimpleNamespace(text="tonight", startS=200.0, endS=600.0, i=1),
                    ],
                    secondary_full_lyrics=None,
                    secondary_full_lyrics_timestamps=[],
                    secondary_full_word_level_lyrics_timestamps=[],
                    matching_used_track="primary",
                    lyrics_timestamps=[
                        SimpleNamespace(text="Glow tonight", startS=0.0, endS=600.0, i=0),
                    ],
                    word_level_lyrics_timestamps=[
                        SimpleNamespace(text="Glow", startS=0.0, endS=200.0, i=0),
                        SimpleNamespace(text="tonight", startS=200.0, endS=600.0, i=1),
                    ],
                    user_requested_language="ENGLISH_US",
                    token_usage=None,
                    token_usage_breakdown=None,
                    used_music_model_spec="edenn_enhanced",
                    job_received_timestamp=None,
                    job_finished_timestamp=None,
                    thumbnail_path=None,
                )

            app = self._build_app(
                tmp_dir,
                AsyncMock(side_effect=fake_workflow_run),
                storage,
            )

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video",
                    data={"modelspec": "edenn_enhanced", "include_vocals": "true"},
                    files={
                        "video": (
                            source_path.name,
                            source_path.read_bytes(),
                            "video/mp4",
                        )
                    },
                )

            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()["audio_metadata"]
            self.assertEqual(
                payload["lyrics_timestamps"],
                [{"text": "Glow tonight", "startS": 0.0, "endS": 600.0, "i": 0}],
            )
            self.assertEqual(
                payload["word_level_lyrics_timestamps"],
                [
                    {"text": "Glow", "startS": 0.0, "endS": 200.0, "i": 0},
                    {"text": "tonight", "startS": 200.0, "endS": 600.0, "i": 1},
                ],
            )
            self.assertEqual(payload["full_lyrics"], "Glow tonight\nHold the line")
            self.assertEqual(
                payload["full_lyrics_timestamps"],
                [{"text": "Glow tonight", "startS": 0.0, "endS": 600.0, "i": 0}],
            )
            self.assertEqual(
                payload["full_word_level_lyrics_timestamps"],
                [
                    {"text": "Glow", "startS": 0.0, "endS": 200.0, "i": 0},
                    {"text": "tonight", "startS": 200.0, "endS": 600.0, "i": 1},
                ],
            )

    def test_api_trims_and_forwards_edenn_enhanced_vocal_id(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-vocal-id-") as tmp:
            tmp_dir = Path(tmp)
            source_path = SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH
            output_dir = tmp_dir / "workflow-output"
            output_dir.mkdir(parents=True, exist_ok=True)

            storage = _FakeStorage()

            async def fake_workflow_run(**kwargs):
                self.assertEqual(kwargs["modelspec"], "edenn_enhanced")
                self.assertEqual(kwargs["vocal_id"], "vocal_123")
                self.assertIsNone(kwargs["vocal_sample_path"])
                return self._build_success_result(
                    video_path=source_path,
                    output_dir=output_dir,
                    include_vocals=True,
                    used_music_model_spec="edenn_enhanced",
                    vocal_id_used="vocal_123",
                )

            workflow_run = AsyncMock(side_effect=fake_workflow_run)
            app = self._build_app(
                tmp_dir,
                workflow_run,
                storage,
            )

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video",
                    data={
                        "modelspec": "edenn_enhanced",
                        "include_vocals": "true",
                        "vocal_id": "  vocal_123  ",
                    },
                    files={
                        "video": (
                            source_path.name,
                            source_path.read_bytes(),
                            "video/mp4",
                        )
                    },
                )

            self.assertEqual(response.status_code, 200, response.text)
            # vocal_id_used is no longer echoed in the response; the surviving
            # contract is that the trimmed id reaches the workflow.
            workflow_run.assert_awaited_once()
            assert workflow_run.await_args is not None
            self.assertEqual(workflow_run.await_args.kwargs["vocal_id"], "vocal_123")

    def test_api_forwards_prepared_edenn_enhanced_vocal_sample_upload(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-vocal-sample-upload-") as tmp:
            tmp_dir = Path(tmp)
            source_path = SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH
            output_dir = tmp_dir / "workflow-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            prepared_sample_path = tmp_dir / "prepared_vocal.m4a"
            prepared_sample_path.write_bytes(b"prepared")

            async def fake_workflow_run(**kwargs):
                self.assertEqual(kwargs["modelspec"], "edenn_enhanced")
                self.assertIsNone(kwargs["vocal_id"])
                self.assertEqual(kwargs["vocal_sample_path"], prepared_sample_path)
                return self._build_success_result(
                    video_path=source_path,
                    output_dir=output_dir,
                    include_vocals=True,
                    used_music_model_spec="edenn_enhanced",
                    vocal_id_used="vocal_from_upload",
                )

            workflow_run = AsyncMock(side_effect=fake_workflow_run)
            app = self._build_app(tmp_dir, workflow_run, _FakeStorage())

            with patch(
                "EdennCode.Deployment.api_video_generation.prepare_audio_for_provider_b_vocal_clone",
                return_value=prepared_sample_path,
            ) as prepare_audio:
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/jobs/video",
                        data={
                            "modelspec": "edenn_enhanced",
                            "include_vocals": "true",
                        },
                        files={
                            "video": (
                                source_path.name,
                                source_path.read_bytes(),
                                "video/mp4",
                            ),
                            "vocal_sample": (
                                "voice.wav",
                                b"fake-vocal-audio",
                                "audio/wav",
                            ),
                        },
                    )

            self.assertEqual(response.status_code, 200, response.text)
            workflow_run.assert_awaited_once()
            prepare_audio.assert_called_once()
            self.assertEqual(
                prepare_audio.call_args.kwargs["source_audio_path"].name,
                "voice.wav",
            )
            self.assertEqual(
                prepare_audio.call_args.kwargs["destination_dir"].name,
                "vocal",
            )

    def test_api_downloads_and_forwards_prepared_edenn_enhanced_vocal_sample_url(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-vocal-sample-url-") as tmp:
            tmp_dir = Path(tmp)
            source_path = SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH
            output_dir = tmp_dir / "workflow-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            prepared_sample_path = tmp_dir / "prepared_remote_vocal.m4a"
            prepared_sample_path.write_bytes(b"prepared")
            download_calls: list[tuple[str, Path, str]] = []

            async def fake_download_public_file_to_disk(*, url: str, destination: Path, asset_label: str) -> Path:
                download_calls.append((url, destination, asset_label))
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"remote-vocal")
                return destination

            async def fake_workflow_run(**kwargs):
                self.assertEqual(kwargs["modelspec"], "edenn_enhanced")
                self.assertIsNone(kwargs["vocal_id"])
                self.assertEqual(kwargs["vocal_sample_path"], prepared_sample_path)
                return self._build_success_result(
                    video_path=source_path,
                    output_dir=output_dir,
                    include_vocals=True,
                    used_music_model_spec="edenn_enhanced",
                )

            workflow_run = AsyncMock(side_effect=fake_workflow_run)
            app = self._build_app(tmp_dir, workflow_run, _FakeStorage())

            with patch(
                "EdennCode.Deployment.api_common.download_public_file_to_disk",
                new=AsyncMock(side_effect=fake_download_public_file_to_disk),
            ):
                with patch(
                    "EdennCode.Deployment.api_video_generation.prepare_audio_for_provider_b_vocal_clone",
                    return_value=prepared_sample_path,
                ) as prepare_audio:
                    with TestClient(app) as client:
                        response = client.post(
                            "/api/v1/jobs/video",
                            data={
                                "modelspec": "edenn_enhanced",
                                "include_vocals": "true",
                                "vocal_sample_url": "https://example.test/media/voice.wav",
                            },
                            files={
                                "video": (
                                    source_path.name,
                                    source_path.read_bytes(),
                                    "video/mp4",
                                )
                            },
                        )

            self.assertEqual(response.status_code, 200, response.text)
            workflow_run.assert_awaited_once()
            self.assertEqual(len(download_calls), 1)
            self.assertEqual(
                download_calls[0],
                (
                    "https://example.test/media/voice.wav",
                    download_calls[0][1],
                    "vocal sample",
                ),
            )
            self.assertEqual(download_calls[0][1].name, "voice.wav")
            prepare_audio.assert_called_once()
            self.assertEqual(
                prepare_audio.call_args.kwargs["source_audio_path"].name,
                "voice.wav",
            )

    def test_api_rejects_conflicting_edenn_enhanced_vocal_id_and_sample_upload(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-vocal-conflict-") as tmp:
            tmp_dir = Path(tmp)
            workflow_run = AsyncMock()
            app = self._build_app(tmp_dir, workflow_run, _FakeStorage())

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video",
                    data={
                        "modelspec": "edenn_enhanced",
                        "include_vocals": "true",
                        "vocal_id": "vocal_123",
                    },
                    files={
                        "video": (
                            SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.name,
                            SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.read_bytes(),
                            "video/mp4",
                        ),
                        "vocal_sample": (
                            "voice.wav",
                            b"fake-vocal-audio",
                            "audio/wav",
                        ),
                    },
                )

            _assert_error_detail(
                self,
                response,
                status_code=400,
                error_code=10001,
                message="Provide either vocal_id or vocal_sample/vocal_sample_url, not both.",
            )
            workflow_run.assert_not_awaited()

    def test_api_rejects_multiple_edenn_enhanced_vocal_sample_sources(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-vocal-double-source-") as tmp:
            tmp_dir = Path(tmp)
            workflow_run = AsyncMock()
            app = self._build_app(tmp_dir, workflow_run, _FakeStorage())

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video",
                    data={
                        "modelspec": "edenn_enhanced",
                        "include_vocals": "true",
                        "vocal_sample_url": "https://example.test/media/voice.wav",
                    },
                    files={
                        "video": (
                            SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.name,
                            SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.read_bytes(),
                            "video/mp4",
                        ),
                        "vocal_sample": (
                            "voice.wav",
                            b"fake-vocal-audio",
                            "audio/wav",
                        ),
                    },
                )

            _assert_error_detail(
                self,
                response,
                status_code=400,
                error_code=10001,
                message="Provide vocal sample upload or vocal sample URL, not both.",
            )
            workflow_run.assert_not_awaited()

    def test_api_rejects_vocal_id_for_non_enhanced_modelspecs(self) -> None:
        for modelspec in ("edenn_basic", "edenn_studio"):
            with self.subTest(modelspec=modelspec):
                with tempfile.TemporaryDirectory(prefix="api-video-vocal-id-reject-") as tmp:
                    tmp_dir = Path(tmp)
                    workflow_run = AsyncMock()
                    app = self._build_app(tmp_dir, workflow_run, _FakeStorage())

                    with TestClient(app) as client:
                        response = client.post(
                            "/api/v1/jobs/video",
                            data={
                                "modelspec": modelspec,
                                "include_vocals": "true",
                                "vocal_id": "vocal_123",
                            },
                            files={
                                "video": (
                                    SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.name,
                                    SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.read_bytes(),
                                    "video/mp4",
                                )
                            },
                        )

                    _assert_error_detail(
                        self,
                        response,
                        status_code=400,
                        error_code=10001,
                        message="Vocal clone inputs are only supported for modelspec=edenn_enhanced.",
                    )
                    workflow_run.assert_not_awaited()

    def test_api_rejects_vocal_sample_for_non_enhanced_modelspec_without_preparing(self) -> None:
        for modelspec in ("edenn_basic", "edenn_studio"):
            with self.subTest(modelspec=modelspec):
                with tempfile.TemporaryDirectory(prefix="api-video-vocal-sample-reject-") as tmp:
                    tmp_dir = Path(tmp)
                    workflow_run = AsyncMock()
                    app = self._build_app(tmp_dir, workflow_run, _FakeStorage())

                    with patch(
                        "EdennCode.Deployment.api_video_generation.prepare_audio_for_provider_b_vocal_clone",
                    ) as prepare_audio:
                        with TestClient(app) as client:
                            response = client.post(
                                "/api/v1/jobs/video",
                                data={
                                    "modelspec": modelspec,
                                    "include_vocals": "true",
                                },
                                files={
                                    "video": (
                                        SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.name,
                                        SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.read_bytes(),
                                        "video/mp4",
                                    ),
                                    "vocal_sample": (
                                        "voice.wav",
                                        b"fake-vocal-audio",
                                        "audio/wav",
                                    ),
                                },
                            )

                    _assert_error_detail(
                        self,
                        response,
                        status_code=400,
                        error_code=10001,
                        message="Vocal clone inputs are only supported for modelspec=edenn_enhanced.",
                    )
                    workflow_run.assert_not_awaited()
                    prepare_audio.assert_not_called()

    def test_api_maps_edenn_enhanced_vocal_sample_preparation_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-vocal-prep-failure-") as tmp:
            tmp_dir = Path(tmp)
            workflow_run = AsyncMock()
            app = self._build_app(tmp_dir, workflow_run, _FakeStorage())

            with patch(
                "EdennCode.Deployment.api_video_generation.prepare_audio_for_provider_b_vocal_clone",
                side_effect=EdennMediaProcessingError(
                    "Failed to prepare vocal sample.",
                    public_message="The vocal sample could not be prepared for vocal cloning.",
                    component="api",
                    operation="prepare_audio_for_provider_b_vocal_clone",
                ),
            ):
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/jobs/video",
                        data={
                            "modelspec": "edenn_enhanced",
                            "include_vocals": "true",
                        },
                        files={
                            "video": (
                                SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.name,
                                SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.read_bytes(),
                                "video/mp4",
                            ),
                            "vocal_sample": (
                                "voice.wav",
                                b"fake-vocal-audio",
                                "audio/wav",
                            ),
                        },
                    )

            _assert_error_detail(
                self,
                response,
                status_code=422,
                error_code=40001,
                message=(
                    "The uploaded media could not be processed. "
                    "Please check the file format and try again."
                ),
            )
            workflow_run.assert_not_awaited()

    def test_api_uses_compressed_mp4_for_input_above_threshold(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-compression-") as tmp:
            tmp_dir = Path(tmp)
            source_path = tmp_dir / "input.mov"
            output_dir = tmp_dir / "workflow-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            _write_test_video(source_path, width=96, height=1400)

            storage = _FakeStorage()
            seen_paths: list[Path] = []

            async def fake_workflow_run(**kwargs):
                workflow_input_path = Path(kwargs["video_path"])
                seen_paths.append(workflow_input_path)
                width, height = _probe_dimensions(workflow_input_path)
                generated_music_path = output_dir / "generated.mp3"
                remixed_video_path = output_dir / "remixed.mp4"
                generated_music_path.write_bytes(b"audio")
                remixed_video_path.write_bytes(b"video")
                return SimpleNamespace(
                    video_metadata=_FakeVideoMetadata(
                        path=workflow_input_path.resolve(),
                        duration=1.0,
                        size_bytes=workflow_input_path.stat().st_size,
                        width=width,
                        height=height,
                        fps=1.0,
                    ),
                    scenes=[],
                    video_summary={},
                    music_prompt={},
                    generated_music_path=generated_music_path,
                    complete_generated_music_path=None,
                    secondary_complete_generated_music_path=None,
                    remixed_video_path=remixed_video_path,
                    include_vocals=False,
                    vocal_gender="female",
                    lyrics_timestamps=[],
                    user_requested_language="",
                    token_usage=None,
                    token_usage_breakdown=None,
                    used_music_model_spec="edenn_basic",
                    job_received_timestamp=None,
                    job_finished_timestamp=None,
                    thumbnail_path=None,
                )

            app = self._build_app(
                tmp_dir,
                AsyncMock(side_effect=fake_workflow_run),
                storage,
            )

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video",
                    data={"compression_flag": "true", "modelspec": "edenn_basic"},
                    files={
                        "video": (
                            source_path.name,
                            source_path.read_bytes(),
                            "video/quicktime",
                        )
                    },
                )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(seen_paths), 1)
            self.assertTrue(seen_paths[0].name.endswith("_1280h.mp4"))
            self.assertEqual(
                response.json()["video_metadata"]["geometry"]["height"], 1280
            )

            input_upload = storage.upload_calls[0]
            self.assertEqual(input_upload["content_type"], "video/mp4")
            self.assertTrue(Path(input_upload["path"]).name.endswith("_1280h.mp4"))

    def test_api_downloads_video_from_url_when_provided(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-compression-") as tmp:
            tmp_dir = Path(tmp)
            remote_source_path = tmp_dir / "remote_source.mp4"
            output_dir = tmp_dir / "workflow-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            _write_test_video(remote_source_path, width=96, height=1200)

            storage = _FakeStorage()
            persistence = _FakeRecommendationPersistence()
            seen_paths: list[Path] = []
            download_calls: list[tuple[str, Path, str]] = []

            async def fake_download_public_file_to_disk(*, url: str, destination: Path, asset_label: str) -> Path:
                download_calls.append((url, destination, asset_label))
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(remote_source_path.read_bytes())
                return destination

            async def fake_workflow_run(**kwargs):
                workflow_input_path = Path(kwargs["video_path"])
                seen_paths.append(workflow_input_path)
                width, height = _probe_dimensions(workflow_input_path)
                generated_music_path = output_dir / "generated.mp3"
                remixed_video_path = output_dir / "remixed.mp4"
                generated_music_path.write_bytes(b"audio")
                remixed_video_path.write_bytes(b"video")
                return SimpleNamespace(
                    video_metadata=_FakeVideoMetadata(
                        path=workflow_input_path.resolve(),
                        duration=1.0,
                        size_bytes=workflow_input_path.stat().st_size,
                        width=width,
                        height=height,
                        fps=1.0,
                    ),
                    scenes=[],
                    video_summary={},
                    music_prompt={},
                    generated_music_path=generated_music_path,
                    complete_generated_music_path=None,
                    secondary_complete_generated_music_path=None,
                    remixed_video_path=remixed_video_path,
                    include_vocals=False,
                    vocal_gender="female",
                    lyrics_timestamps=[],
                    user_requested_language="",
                    token_usage=None,
                    token_usage_breakdown=None,
                    used_music_model_spec="edenn_basic",
                    job_received_timestamp=None,
                    job_finished_timestamp=None,
                    thumbnail_path=None,
                )

            app = self._build_app(
                tmp_dir,
                AsyncMock(side_effect=fake_workflow_run),
                storage,
                recommendation_persistence=persistence,
            )

            with patch(
                "EdennCode.Deployment.api_video_generation.download_public_file_to_disk",
                new=AsyncMock(side_effect=fake_download_public_file_to_disk),
            ):
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/jobs/video",
                        data={
                            "video_url": "https://example.test/media/remote_input.mov",
                            "compression_flag": "false",
                            "modelspec": "edenn_basic",
                        },
                    )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(download_calls), 1)
            self.assertEqual(download_calls[0][0], "https://example.test/media/remote_input.mov")
            self.assertEqual(download_calls[0][2], "video")
            self.assertEqual(len(seen_paths), 1)
            self.assertEqual(seen_paths[0].name, "remote_input.mov")
            self.assertFalse(
                any("/input/source_video" in call["blob_name"] for call in storage.upload_calls)
            )
            # The source blob/url left the HTTP response but are still recorded on
            # the persisted video asset: a URL-sourced input is never re-uploaded,
            # and the remote URL is what gets stored.
            self.assertEqual(len(persistence.payloads), 1)
            video_asset = persistence.payloads[0].video_asset
            self.assertIsNone(video_asset.source_video_blob)
            self.assertEqual(
                video_asset.source_video_url,
                "https://example.test/media/remote_input.mov",
            )

    def test_api_prefers_video_url_over_uploaded_video(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-compression-") as tmp:
            tmp_dir = Path(tmp)
            remote_source_path = tmp_dir / "remote_source.mp4"
            output_dir = tmp_dir / "workflow-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            _write_test_video(remote_source_path, width=96, height=1200)

            storage = _FakeStorage()
            seen_paths: list[Path] = []

            async def fake_download_public_file_to_disk(*, url: str, destination: Path, asset_label: str) -> Path:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(remote_source_path.read_bytes())
                return destination

            async def fake_workflow_run(**kwargs):
                workflow_input_path = Path(kwargs["video_path"])
                seen_paths.append(workflow_input_path)
                self.assertEqual(workflow_input_path.name, "preferred_remote.mov")
                self.assertNotEqual(workflow_input_path.read_bytes(), b"ignored-upload")
                generated_music_path = output_dir / "generated.mp3"
                remixed_video_path = output_dir / "remixed.mp4"
                generated_music_path.write_bytes(b"audio")
                remixed_video_path.write_bytes(b"video")
                return SimpleNamespace(
                    video_metadata=_FakeVideoMetadata(
                        path=workflow_input_path.resolve(),
                        duration=1.0,
                        size_bytes=workflow_input_path.stat().st_size,
                        width=96,
                        height=1200,
                        fps=1.0,
                    ),
                    scenes=[],
                    video_summary={},
                    music_prompt={},
                    generated_music_path=generated_music_path,
                    complete_generated_music_path=None,
                    secondary_complete_generated_music_path=None,
                    remixed_video_path=remixed_video_path,
                    include_vocals=False,
                    vocal_gender="female",
                    lyrics_timestamps=[],
                    user_requested_language="",
                    token_usage=None,
                    token_usage_breakdown=None,
                    used_music_model_spec="edenn_basic",
                    job_received_timestamp=None,
                    job_finished_timestamp=None,
                    thumbnail_path=None,
                )

            workflow_run = AsyncMock(side_effect=fake_workflow_run)
            app = self._build_app(tmp_dir, workflow_run, storage)

            with patch(
                "EdennCode.Deployment.api_video_generation.download_public_file_to_disk",
                new=AsyncMock(side_effect=fake_download_public_file_to_disk),
            ):
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/jobs/video",
                        data={
                            "video_url": "https://example.test/media/preferred_remote.mov",
                            "compression_flag": "false",
                            "modelspec": "edenn_basic",
                        },
                        files={
                            "video": (
                                "ignored.mp4",
                                b"ignored-upload",
                                "video/mp4",
                            )
                        },
                    )

            self.assertEqual(response.status_code, 200, response.text)
            workflow_run.assert_awaited_once()
            self.assertEqual(len(seen_paths), 1)
            self.assertEqual(seen_paths[0].name, "preferred_remote.mov")

    def test_api_compresses_remote_video_input_above_threshold(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-compression-") as tmp:
            tmp_dir = Path(tmp)
            remote_source_path = tmp_dir / "remote_source.mp4"
            output_dir = tmp_dir / "workflow-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            _write_test_video(remote_source_path, width=96, height=1400)

            storage = _FakeStorage()
            seen_paths: list[Path] = []

            async def fake_download_public_file_to_disk(*, url: str, destination: Path, asset_label: str) -> Path:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(remote_source_path.read_bytes())
                return destination

            async def fake_workflow_run(**kwargs):
                workflow_input_path = Path(kwargs["video_path"])
                seen_paths.append(workflow_input_path)
                width, height = _probe_dimensions(workflow_input_path)
                generated_music_path = output_dir / "generated.mp3"
                remixed_video_path = output_dir / "remixed.mp4"
                generated_music_path.write_bytes(b"audio")
                remixed_video_path.write_bytes(b"video")
                return SimpleNamespace(
                    video_metadata=_FakeVideoMetadata(
                        path=workflow_input_path.resolve(),
                        duration=1.0,
                        size_bytes=workflow_input_path.stat().st_size,
                        width=width,
                        height=height,
                        fps=1.0,
                    ),
                    scenes=[],
                    video_summary={},
                    music_prompt={},
                    generated_music_path=generated_music_path,
                    complete_generated_music_path=None,
                    secondary_complete_generated_music_path=None,
                    remixed_video_path=remixed_video_path,
                    include_vocals=False,
                    vocal_gender="female",
                    lyrics_timestamps=[],
                    user_requested_language="",
                    token_usage=None,
                    token_usage_breakdown=None,
                    used_music_model_spec="edenn_basic",
                    job_received_timestamp=None,
                    job_finished_timestamp=None,
                    thumbnail_path=None,
                )

            app = self._build_app(
                tmp_dir,
                AsyncMock(side_effect=fake_workflow_run),
                storage,
            )

            with patch(
                "EdennCode.Deployment.api_video_generation.download_public_file_to_disk",
                new=AsyncMock(side_effect=fake_download_public_file_to_disk),
            ):
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/jobs/video",
                        data={
                            "video_url": "https://example.test/media/remote_high.mov",
                            "compression_flag": "true",
                            "modelspec": "edenn_basic",
                        },
                    )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(seen_paths), 1)
            self.assertTrue(seen_paths[0].name.endswith("_1280h.mp4"))
            self.assertEqual(
                response.json()["video_metadata"]["geometry"]["height"], 1280
            )

            input_upload = storage.upload_calls[0]
            self.assertEqual(input_upload["content_type"], "video/mp4")
            self.assertTrue(Path(input_upload["path"]).name.endswith("_1280h.mp4"))

    def test_api_reports_rotation_aware_video_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-compression-") as tmp:
            tmp_dir = Path(tmp)
            output_dir = tmp_dir / "workflow-output"
            output_dir.mkdir(parents=True, exist_ok=True)

            storage = _FakeStorage()

            async def fake_workflow_run(**kwargs):
                workflow_input_path = Path(kwargs["video_path"])
                metadata = VideoMetadata.from_file(workflow_input_path)
                generated_music_path = output_dir / "generated.mp3"
                remixed_video_path = output_dir / "remixed.mp4"
                generated_music_path.write_bytes(b"audio")
                remixed_video_path.write_bytes(b"video")
                return SimpleNamespace(
                    video_metadata=metadata,
                    scenes=[],
                    video_summary={},
                    music_prompt={},
                    generated_music_path=generated_music_path,
                    complete_generated_music_path=None,
                    secondary_complete_generated_music_path=None,
                    remixed_video_path=remixed_video_path,
                    include_vocals=False,
                    vocal_gender="female",
                    lyrics_timestamps=[],
                    user_requested_language="",
                    token_usage=None,
                    token_usage_breakdown=None,
                    used_music_model_spec="edenn_basic",
                    job_received_timestamp=None,
                    job_finished_timestamp=None,
                    thumbnail_path=None,
                )

            app = self._build_app(
                tmp_dir,
                AsyncMock(side_effect=fake_workflow_run),
                storage,
            )

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video",
                    data={"compression_flag": "false", "modelspec": "edenn_basic"},
                    files={
                        "video": (
                            ROTATED_SMOKE_VIDEO_PATH.name,
                            ROTATED_SMOKE_VIDEO_PATH.read_bytes(),
                            "video/mp4",
                        )
                    },
                )

            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()["video_metadata"]["geometry"]
            self.assertEqual(payload["width"], 320)
            self.assertEqual(payload["height"], 568)
            self.assertAlmostEqual(payload["duration"], 20.585, places=3)

    def test_api_reports_unrotated_video_metadata_without_swapping(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-compression-") as tmp:
            tmp_dir = Path(tmp)
            output_dir = tmp_dir / "workflow-output"
            output_dir.mkdir(parents=True, exist_ok=True)

            storage = _FakeStorage()

            async def fake_workflow_run(**kwargs):
                workflow_input_path = Path(kwargs["video_path"])
                metadata = VideoMetadata.from_file(workflow_input_path)
                generated_music_path = output_dir / "generated.mp3"
                remixed_video_path = output_dir / "remixed.mp4"
                generated_music_path.write_bytes(b"audio")
                remixed_video_path.write_bytes(b"video")
                return SimpleNamespace(
                    video_metadata=metadata,
                    scenes=[],
                    video_summary={},
                    music_prompt={},
                    generated_music_path=generated_music_path,
                    complete_generated_music_path=None,
                    secondary_complete_generated_music_path=None,
                    remixed_video_path=remixed_video_path,
                    include_vocals=False,
                    vocal_gender="female",
                    lyrics_timestamps=[],
                    user_requested_language="",
                    token_usage=None,
                    token_usage_breakdown=None,
                    used_music_model_spec="edenn_basic",
                    job_received_timestamp=None,
                    job_finished_timestamp=None,
                    thumbnail_path=None,
                )

            app = self._build_app(
                tmp_dir,
                AsyncMock(side_effect=fake_workflow_run),
                storage,
            )

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video",
                    data={"compression_flag": "false", "modelspec": "edenn_basic"},
                    files={
                        "video": (
                            SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.name,
                            SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.read_bytes(),
                            "video/mp4",
                        )
                    },
                )

            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()["video_metadata"]["geometry"]
            self.assertEqual(payload["width"], 1280)
            self.assertEqual(payload["height"], 720)
            self.assertAlmostEqual(payload["duration"], 18.567, places=3)

    def test_api_rejects_invalid_modelspec_before_running_workflow(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-compression-") as tmp:
            tmp_dir = Path(tmp)
            source_path = tmp_dir / "input.mov"
            _write_test_video(source_path, width=96, height=1200)

            workflow_run = AsyncMock()
            app = self._build_app(tmp_dir, workflow_run, _FakeStorage())

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video",
                    data={"compression_flag": "false", "modelspec": "not_real"},
                    files={
                        "video": (
                            source_path.name,
                            source_path.read_bytes(),
                            "video/quicktime",
                        )
                    },
                )

            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("Invalid modelspec", response.json()["detail"]["message"])
            self.assertFalse(contains_provider_token(response.json()["detail"]["message"]))
            workflow_run.assert_not_awaited()

    def test_api_rejects_input_video_longer_than_300_seconds_before_running_workflow(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-too-long-") as tmp:
            tmp_dir = Path(tmp)
            source_path = tmp_dir / "input.mp4"
            _write_test_video(source_path, width=96, height=120)

            workflow_run = AsyncMock()
            app = self._build_app(tmp_dir, workflow_run, _FakeStorage())

            with patch(
                "EdennCode.Deployment.api_video_generation.get_video_duration",
                return_value=300.1,
            ):
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/jobs/video",
                        data={"compression_flag": "false", "modelspec": "edenn_basic"},
                        files={
                            "video": (
                                source_path.name,
                                source_path.read_bytes(),
                                "video/mp4",
                            )
                        },
                    )

            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("300 seconds", response.json()["detail"]["message"])
            workflow_run.assert_not_awaited()

    def test_api_provider_failure_hides_provider_details(self) -> None:
        self._configure_provider_vocabulary()
        with tempfile.TemporaryDirectory(prefix="api-video-provider-fail-") as tmp:
            tmp_dir = Path(tmp)
            source_path = tmp_dir / "input.mp4"
            _write_test_video(source_path, width=96, height=120)

            async def fake_workflow_run(**kwargs):
                raise EdennProviderTimeoutError(
                    f"{_VENDOR} task timed out while polling",
                    provider_name=_PROVIDER_NAME,
                    operation=f"{_PROVIDER_NAME}_poll",
                    retryable=True,
                )

            app = self._build_app(
                tmp_dir,
                AsyncMock(side_effect=fake_workflow_run),
                _FakeStorage(),
            )

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video",
                    data={"compression_flag": "false", "modelspec": "edenn_studio"},
                    files={
                        "video": (
                            source_path.name,
                            source_path.read_bytes(),
                            "video/mp4",
                        )
                    },
                )

            self.assertEqual(response.status_code, 504, response.text)
            detail = response.json()["detail"]
            self.assertEqual(detail["error_code"], 30200)
            self.assertFalse(contains_provider_token(str(detail)), detail)
            self.assertEqual(
                detail["message"],
                "The request took too long to complete. Please try again.",
            )

    def test_api_success_hides_provider_warning_details(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-warning-") as tmp:
            tmp_dir = Path(tmp)
            source_path = tmp_dir / "input.mp4"
            output_dir = tmp_dir / "workflow-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            _write_test_video(source_path, width=96, height=120)

            async def fake_workflow_run(**kwargs):
                result = self._build_success_result(
                    video_path=Path(kwargs["video_path"]),
                    output_dir=output_dir,
                    include_vocals=True,
                    used_music_model_spec="edenn_studio",
                )
                result.critical_warning = (
                    "edenn_enhanced: upstream provider_b key primary balance is below "
                    "safe operating threshold"
                )
                return result

            app = self._build_app(
                tmp_dir,
                AsyncMock(side_effect=fake_workflow_run),
                _FakeStorage(),
            )

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video",
                    data={"compression_flag": "false", "modelspec": "edenn_studio"},
                    files={
                        "video": (
                            source_path.name,
                            source_path.read_bytes(),
                            "video/mp4",
                        )
                    },
                )

            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertFalse(contains_provider_token(str(payload)), payload)

    def test_api_rejects_missing_video_source_before_running_workflow(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-compression-") as tmp:
            tmp_dir = Path(tmp)
            workflow_run = AsyncMock()
            app = self._build_app(tmp_dir, workflow_run, _FakeStorage())

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video",
                    data={"compression_flag": "false", "modelspec": "edenn_basic"},
                )

            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("video upload or video_url", response.json()["detail"]["message"])
            workflow_run.assert_not_awaited()

    def test_api_maps_legacy_modelspec_alias_before_workflow_run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-compression-") as tmp:
            tmp_dir = Path(tmp)
            source_path = tmp_dir / "input.mov"
            output_dir = tmp_dir / "workflow-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            _write_test_video(source_path, width=96, height=1200)

            storage = _FakeStorage()

            async def fake_workflow_run(**kwargs):
                workflow_input_path = Path(kwargs["video_path"])
                generated_music_path = output_dir / "generated.mp3"
                remixed_video_path = output_dir / "remixed.mp4"
                generated_music_path.write_bytes(b"audio")
                remixed_video_path.write_bytes(b"video")
                return SimpleNamespace(
                    video_metadata=_FakeVideoMetadata(
                        path=workflow_input_path.resolve(),
                        duration=1.0,
                        size_bytes=workflow_input_path.stat().st_size,
                        width=96,
                        height=1200,
                        fps=1.0,
                    ),
                    scenes=[],
                    video_summary={},
                    music_prompt={},
                    generated_music_path=generated_music_path,
                    complete_generated_music_path=None,
                    secondary_complete_generated_music_path=None,
                    remixed_video_path=remixed_video_path,
                    include_vocals=False,
                    vocal_gender="female",
                    lyrics_timestamps=[],
                    user_requested_language="",
                    token_usage=None,
                    token_usage_breakdown=None,
                    used_music_model_spec="edenn_studio",
                    job_received_timestamp=None,
                    job_finished_timestamp=None,
                    thumbnail_path=None,
                )

            workflow_run = AsyncMock(side_effect=fake_workflow_run)
            app = self._build_app(tmp_dir, workflow_run, storage)

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video",
                    data={
                        "compression_flag": "false",
                        "modelspec": _legacy_alias_for("edenn_studio"),
                    },
                    files={
                        "video": (
                            source_path.name,
                            source_path.read_bytes(),
                            "video/quicktime",
                        )
                    },
                )

            self.assertEqual(response.status_code, 200, response.text)
            workflow_run.assert_awaited_once()
            assert workflow_run.await_args is not None
            self.assertEqual(workflow_run.await_args.kwargs["modelspec"], "edenn_studio")
            # Every alias still in the map must normalise to a supported spec,
            # otherwise a live client gets a 400 from an alias we advertise.
            self.assertTrue(
                set(LEGACY_MODEL_MAP.values()) <= VALID_MUSIC_MODEL_SPECS,
                LEGACY_MODEL_MAP,
            )

    def test_api_uploads_webp_thumbnail_blob_and_returns_url(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-video-compression-") as tmp:
            tmp_dir = Path(tmp)
            source_path = tmp_dir / "input.mov"
            output_dir = tmp_dir / "workflow-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            _write_test_video(source_path, width=96, height=1200)

            thumbnail_path = output_dir / "thumbnail.webp"
            self._write_thumbnail_webp(thumbnail_path)
            storage = _FakeStorage()

            async def fake_workflow_run(**kwargs):
                workflow_input_path = Path(kwargs["video_path"])
                width, height = _probe_dimensions(workflow_input_path)
                generated_music_path = output_dir / "generated.mp3"
                remixed_video_path = output_dir / "remixed.mp4"
                generated_music_path.write_bytes(b"audio")
                remixed_video_path.write_bytes(b"video")
                return SimpleNamespace(
                    video_metadata=_FakeVideoMetadata(
                        path=workflow_input_path.resolve(),
                        duration=1.0,
                        size_bytes=workflow_input_path.stat().st_size,
                        width=width,
                        height=height,
                        fps=1.0,
                    ),
                    scenes=[],
                    video_summary={},
                    music_prompt={},
                    generated_music_path=generated_music_path,
                    complete_generated_music_path=None,
                    secondary_complete_generated_music_path=None,
                    remixed_video_path=remixed_video_path,
                    include_vocals=False,
                    vocal_gender="female",
                    lyrics_timestamps=[],
                    user_requested_language="",
                    token_usage=None,
                    token_usage_breakdown=None,
                    used_music_model_spec="edenn_basic",
                    job_received_timestamp=None,
                    job_finished_timestamp=None,
                    thumbnail_path=thumbnail_path,
                )

            app = self._build_app(
                tmp_dir,
                AsyncMock(side_effect=fake_workflow_run),
                storage,
            )

            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs/video",
                    data={"compression_flag": "false", "modelspec": "edenn_basic"},
                    files={
                        "video": (
                            source_path.name,
                            source_path.read_bytes(),
                            "video/quicktime",
                        )
                    },
                )

            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertTrue(
                payload["video_metadata"]["thumbnail_url"].endswith(
                    "/thumbnail/thumbnail.webp"
                )
            )

            # The blob name left the response, but the upload still happens: assert
            # it on the storage call instead.
            thumbnail_upload = next(
                call for call in storage.upload_calls if str(call["path"]).endswith(".webp")
            )
            self.assertEqual(thumbnail_upload["content_type"], "image/webp")
            self.assertTrue(str(thumbnail_upload["blob_name"]).endswith("/thumbnail.webp"))


if __name__ == "__main__":
    unittest.main()
