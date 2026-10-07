import asyncio
from contextlib import ExitStack, contextmanager
import math
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_music import (
    ProviderBTimestampedLyrics,
)
from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Deployment.storage import AzureBlobStorageService, TemporaryBlobUpload
from EdennCode.exceptions import EdennProviderResponseError
from EdennCode.Util.MediaUtils.pipeline_util import build_azure_client
from EdennCode.WorkflowFactory.VideoMusicWorkflow.PipelineDataModel.pipeline_datamodel import (
    Language,
    VideoCategory,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicGenerationStage.music_generation_stage import (
    MusicGenerationStage,
    MusicGenerationStageOutput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.word_ts import WordTS
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.UserIntentUnderstandingStage.user_prompt_preprocessor import (
    UserPromptPreprocessorAgent,
    UserPromptPreprocessorResult,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.scene_segmentation_stage import (
    SceneSegmentationStage,
    SceneSegmentationStageInput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoPreprocessStage.media_preprocess_stage import (
    InputMediaAssetTyps,
    PreprocessStage,
    PreprocessStageInput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow import (
    VideoMusicWorkflowE2E,
    VideoMusicWorkflowE2EInput,
)
from EdennCode.TestSuites.helpers.integration import (
    handle_remote_failure,
    require_any_remote_env,
    require_remote_env,
    run_with_remote_rate_limit_retry,
)
from EdennCode.TestSuites.helpers.paths import (
    CONTENT_POLICY_SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH,
    SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH,
    WEIXIN_EXAMPLE_VIDEO_PATH,
)
from EdennCode.Util.MediaUtils.ffmpeg_utils import resolve_ffmpeg_binary


def _ffprobe_bin() -> str:
    ffmpeg_bin = resolve_ffmpeg_binary()
    return ffmpeg_bin[:-6] + "ffprobe" if ffmpeg_bin.endswith("ffmpeg") else "ffprobe"


def _audio_stream_duration_seconds(video_path: Path) -> float:
    """Duration of the video's first audio stream (0.0 if there is none)."""
    res = subprocess.run(
        [_ffprobe_bin(), "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=duration", "-of", "default=nk=1:nw=1", str(video_path)],
        check=True, capture_output=True, text=True,
    )
    lines = [line for line in res.stdout.strip().splitlines() if line.strip()]
    return float(lines[0]) if lines else 0.0


def _wrap_video_with_embedded_thumbnail(src: Path, dest_dir: Path) -> Path:
    """Return a copy of ``src`` with an extra single-frame mjpeg thumbnail stream.

    Reproduces the phone/app-export layout (main video + audio + embedded
    thumbnail) that triggered the silent-remix bug, so the real pipeline exercises
    the ``-map 0:v:0`` fix.
    """
    ffmpeg_bin = resolve_ffmpeg_binary()
    dest_dir.mkdir(parents=True, exist_ok=True)
    thumb = dest_dir / "thumb.png"
    subprocess.run(
        [ffmpeg_bin, "-y", "-f", "lavfi", "-i", "color=c=red:size=64x64",
         "-frames:v", "1", str(thumb)],
        check=True, capture_output=True,
    )
    out = dest_dir / f"{src.stem}_with_thumbnail.mp4"
    subprocess.run(
        [ffmpeg_bin, "-y", "-i", str(src), "-i", str(thumb),
         "-map", "0:v:0", "-map", "0:a:0", "-map", "1:v:0",
         "-c:v:0", "copy", "-c:a", "copy", "-c:v:1", "mjpeg",
         "-disposition:v:1", "0", str(out)],
        check=True, capture_output=True,
    )
    return out


def _write_sine_wav(path: Path, duration: float, sample_rate: int = 16000) -> None:
    frames = int(duration * sample_rate)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        for i in range(frames):
            val = int(32767 * 0.1 * math.sin(2 *
                      math.pi * 440 * (i / sample_rate)))
            wav.writeframes(struct.pack("<h", val))


class _FakeAsyncBasicProvider:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir

    async def generate(
        self,
        _prompt: str,
        *,
        music_length_ms: int,
        with_timestamps: bool,
        output_format=None,
    ):
        del output_format
        duration_s = max(1.0, float(music_length_ms) / 1000.0)
        output_path = self.output_dir / "fake_basic.wav"
        _write_sine_wav(output_path, duration=duration_s)
        timestamps = []
        if with_timestamps:
            timestamps = [WordTS(text="fallback", startS=0.0,
                                 endS=min(1.0, duration_s), i=0)]
        return output_path, timestamps


class _FakeAsyncProviderBProvider:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir

    def begin_warning_collection(self) -> object:
        return object()

    def finish_warning_collection(self, _token: object) -> None:
        return None

    async def clone_vocal(self, _audio_path: Path) -> str:
        return "fake_vocal_id"

    async def generate_with_variants_detailed(
        self,
        *,
        prompt: str,
        lyrics_prompt: str,
        vocal_id: str | None = None,
        n: int = 2,
        output_path: Path | None = None,
    ):
        del prompt, vocal_id, n
        primary_path = output_path or (
            self.output_dir / "fake_provider_b_primary.wav")
        secondary_path = self.output_dir / "fake_provider_b_secondary.wav"
        _write_sine_wav(primary_path, duration=20.0)
        _write_sine_wav(secondary_path, duration=20.0)
        primary_path.with_suffix(".lyrics.txt").write_text(
            lyrics_prompt, encoding="utf-8")
        secondary_path.with_suffix(".lyrics.txt").write_text(
            lyrics_prompt, encoding="utf-8")
        timestamps = ProviderBTimestampedLyrics(
            line_level=[
                WordTS(text=lyrics_prompt or "fallback line", startS=0.0, endS=2.0, i=0)],
            word_level=[WordTS(text="fallback", startS=0.0, endS=1.0, i=0)],
        )
        return primary_path, secondary_path, timestamps, timestamps

    async def extend_song_from_audio_detailed(
        self,
        *,
        audio_path: Path,
        prompt: str,
        lyrics: str,
        output_path: Path,
    ):
        del prompt, output_path
        timestamps = ProviderBTimestampedLyrics(
            line_level=[WordTS(text=lyrics or "fallback line",
                               startS=0.0, endS=2.0, i=0)],
            word_level=[WordTS(text="fallback", startS=0.0, endS=1.0, i=0)],
        )
        return audio_path, timestamps, lyrics


class _FakeStorageService:
    def __init__(self) -> None:
        self.enabled = True
        self.uploads = []
        self.deletes = []

    def upload_temporary_bytes(
        self,
        *,
        container: str,
        blob_name: str,
        data: bytes,
        content_type: str | None = None,
        ttl_minutes: int | None = None,
    ) -> TemporaryBlobUpload:
        self.uploads.append(
            {
                "container": container,
                "blob_name": blob_name,
                "content_type": content_type,
                "ttl_minutes": ttl_minutes,
                "size": len(data),
            }
        )
        return TemporaryBlobUpload(
            container=container,
            blob_name=blob_name,
            sas_url=f"https://example.test/{container}/{blob_name}",
        )

    def delete_blob(self, *, container: str, blob_name: str) -> None:
        self.deletes.append({"container": container, "blob_name": blob_name})


class _FakeAzureClient:
    def __init__(self) -> None:
        self.schema_calls = []

    async def complete_messages(self, _prompt, json_schema=None):
        schema_name = (json_schema or {}).get("name", "")
        self.schema_calls.append(schema_name)

        if schema_name == "scene_understanding":
            return (
                {
                    "visual_summary": "People move through an outdoor urban scene.",
                    "key_actions": "walking and looking around",
                    "mood": "observational",
                },
                {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            )

        if schema_name == "video_summary":
            return (
                {
                    "video_title": "City Walk Moments",
                    "video_description": "A short clip that follows motion through several visual beats.",
                    "summary": "The video moves across several short visual moments with human activity.",
                    "overall_mood": "curious",
                    "core_message": "Capture everyday movement with light momentum.",
                    "has_explicit_call_to_action": False,
                },
                {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
            )

        if schema_name == "video_music_alignment":
            return (
                {
                    "global_music_prompt": "Light rhythmic electronic underscore with steady momentum.",
                    "global_mood": "curious",
                    "tempo_bpm": 108,
                    "instruments": ["synth", "bass", "percussion"],
                },
                {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
            )

        raise AssertionError(f"Unexpected schema requested: {schema_name}")


def _raise_retryable_incomplete_scene_output(detail: str) -> None:
    raise EdennProviderResponseError(
        detail,
        retryable=True,
        status_code=503,
    )


def _assert_remote_scene_segmentation_is_complete(
    *,
    scene_messages,
    expected_min_scene_count: int,
    expected_duration: float,
) -> None:
    if len(scene_messages) < expected_min_scene_count:
        _raise_retryable_incomplete_scene_output(
            "Remote scene segmentation returned too few scenes after transient provider failures: "
            f"expected at least {expected_min_scene_count}, got {len(scene_messages)}."
        )
    if scene_messages[-1].end_timestamp < (expected_duration - 0.5):
        _raise_retryable_incomplete_scene_output(
            "Remote scene segmentation ended too early after transient provider failures: "
            f"expected >= {expected_duration - 0.5:.3f}s, got {scene_messages[-1].end_timestamp:.3f}s."
        )


class VideoMusicWorkflowE2EIntegrationTests(unittest.TestCase):
    def test_generate_runs_real_scene_segmentation_for_regression_video(self) -> None:
        self.assertTrue(SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.exists())

        fake_azure_client = _FakeAzureClient()
        fake_storage_service = _FakeStorageService()
        preprocessor_output = UserPromptPreprocessorResult(
            was_transformed=False,
            detected_include_vocals=False,
            transformed_prompt="Create a steady instrumental track for this video.",
            detected_references=[],
            detected_language=Language.EN,
            detected_category=VideoCategory.DEFAULT,
            detected_vocal_gender="unknown",
            detected_vocal_language="",
            reasoning="Instrumental request detected.",
            tokens_used=3,
            prompt_tokens=2,
            completion_tokens=1,
        )

        with tempfile.TemporaryDirectory(prefix="video-music-e2e-") as tmp:
            tmp_dir = Path(tmp)
            copied_video_path = tmp_dir / SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.name
            shutil.copy2(SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH,
                         copied_video_path)

            local_temp_dir = f"temp_video_workdir/test_video_music_workflow_e2e_integration_{tmp_dir.name}"
            local_temp_root = Path.cwd() / local_temp_dir
            self.addCleanup(lambda: shutil.rmtree(
                local_temp_root, ignore_errors=True))

            generated_audio_path = tmp_dir / "generated_music.wav"

            async def fake_music_run(stage_input):
                if not generated_audio_path.exists():
                    _write_sine_wav(
                        generated_audio_path,
                        duration=max(
                            1.0, stage_input.video_metadata.duration + 0.5),
                    )
                return MusicGenerationStageOutput(
                    music_path=generated_audio_path,
                    complete_music_path=generated_audio_path,
                    secondary_complete_music_path=None,
                    lyrics_timestamps=[],
                )

            with patch.dict(
                os.environ,
                {
                    "USE_LOCAL_TEMP_DIR": "true",
                    "LOCAL_TEMP_DIR": local_temp_dir,
                    # The default scene image transport is now "inline" (base64
                    # data URLs), which never touches storage. Force the "blob"
                    # transport here so this test continues to exercise the LLM
                    # image upload + cleanup lifecycle it asserts on below.
                    "SCENE_LLM_IMAGE_TRANSPORT": "blob",
                },
                clear=False,
            ), patch(
                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.build_azure_client",
                return_value=fake_azure_client,
            ), patch(
                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.build_music_provider",
                return_value=MagicMock(),
            ), patch(
                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.ProviderCApi",
                return_value=MagicMock(),
            ), patch(
                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.build_edenn_enhanced_music_provider",
                return_value=MagicMock(),
            ), patch.object(
                UserPromptPreprocessorAgent,
                "preprocess",
                new=AsyncMock(return_value=preprocessor_output),
            ), patch.object(
                MusicGenerationStage,
                "run",
                new=AsyncMock(side_effect=fake_music_run),
            ):
                workflow = VideoMusicWorkflowE2E(
                    storage_service=fake_storage_service,
                    llm_image_container="user-uploads",
                    llm_image_cleanup_delay_seconds=0,
                )
                output = asyncio.run(
                    workflow.generate(
                        VideoMusicWorkflowE2EInput(
                            video_path=str(copied_video_path),
                            user_prompt="Create a steady instrumental track for this video.",
                            music_model_spec="edenn_basic",
                            include_vocals=False,
                        )
                    )
                )

            self.assertEqual(output.video_metadata.path,
                             copied_video_path.resolve())
            self.assertGreater(output.video_metadata.duration, 18.0)
            self.assertEqual(output.video_metadata.fps, 30.0)
            self.assertGreater(len(output.scenes), 0)
            self.assertTrue(
                all(scene.visual_summary for scene in output.scenes))
            self.assertTrue(all(scene.end_timestamp >=
                            scene.start_timestamp for scene in output.scenes))
            self.assertIsNotNone(output.thumbnail_path)
            self.assertTrue(output.thumbnail_path.exists())
            self.assertTrue(output.complete_generated_music_path.exists())
            self.assertTrue(output.remixed_video_path.exists())
            self.assertEqual(output.video_title, "City Walk Moments")
            self.assertEqual(output.include_vocals, False)
            self.assertEqual(output.UsedMusicModelSpecs, "edenn_basic")
            self.assertGreaterEqual(
                fake_azure_client.schema_calls.count("scene_understanding"),
                len(output.scenes),
            )
            self.assertIn("video_summary", fake_azure_client.schema_calls)
            self.assertIn("video_music_alignment",
                          fake_azure_client.schema_calls)
            self.assertGreater(len(fake_storage_service.uploads), 0)
            self.assertEqual(len(fake_storage_service.deletes),
                             len(fake_storage_service.uploads))


if __name__ == "__main__":
    unittest.main()


@pytest.mark.remote_integration
def test_scene_segmentation_remote_recovers_for_content_policy_regression_video() -> None:
    require_remote_env(
        "AZURE_ENDPOINT",
        "AZURE_API_KEY",
        "AZURE_MODEL",
        "AZURE_STORAGE_CONNECTION_STRING",
    )
    assert CONTENT_POLICY_SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.exists()

    def _run() -> None:
        settings = DeploymentSettings.from_env()
        storage_service = AzureBlobStorageService(settings)

        tmp_dir = Path(tempfile.mkdtemp(
            prefix="scene-segmentation-content-policy-"))
        copied_video_path = tmp_dir / \
            CONTENT_POLICY_SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.name
        shutil.copy2(
            CONTENT_POLICY_SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH, copied_video_path)

        local_temp_dir = (
            "temp_video_workdir/"
            f"test_scene_segmentation_content_policy_{tmp_dir.name}"
        )
        local_temp_root = Path.cwd() / local_temp_dir
        try:
            with patch.dict(
                os.environ,
                {
                    "USE_LOCAL_TEMP_DIR": "true",
                    "LOCAL_TEMP_DIR": local_temp_dir,
                },
                clear=False,
            ):
                preprocess_output = asyncio.run(
                    PreprocessStage().run(
                        PreprocessStageInput(
                            asset_path=str(copied_video_path),
                            asset_type=InputMediaAssetTyps.VIDEO,
                        )
                    )
                )
                stage = SceneSegmentationStage(
                    llm_model_client=build_azure_client(),
                    scene_detector="pyscenedetect",
                    storage_service=storage_service,
                    llm_image_container=settings.llm_image_container,
                    llm_image_cleanup_delay_seconds=0,
                )
                output = asyncio.run(
                    stage.run(
                        SceneSegmentationStageInput(
                            video_path=preprocess_output.video_metadata.path,
                            duration=preprocess_output.video_metadata.duration,
                            fps=preprocess_output.video_metadata.fps,
                            preferred_language=Language.EN,
                        )
                    )
                )

            _assert_remote_scene_segmentation_is_complete(
                scene_messages=output.scene_understanding_messages,
                expected_min_scene_count=13,
                expected_duration=preprocess_output.video_metadata.duration,
            )
            assert all(
                scene.visual_summary for scene in output.scene_understanding_messages)
            assert all(scene.end_timestamp >=
                       scene.start_timestamp for scene in output.scene_understanding_messages)
            assert output.thumbnail_path is not None
            assert output.thumbnail_path.exists()
            assert output.thumbnail_path.resolve().parent == copied_video_path.resolve().parent
            assert output.token_usage is not None
            assert output.token_usage["total_tokens"] > 0
        except Exception as exc:
            handle_remote_failure(exc)
            raise
        finally:
            shutil.rmtree(local_temp_root, ignore_errors=True)
            shutil.rmtree(tmp_dir, ignore_errors=True)

    run_with_remote_rate_limit_retry(
        _run,
        operation_name="test_scene_segmentation_remote_recovers_for_content_policy_regression_video",
    )


@pytest.mark.remote_integration
def test_scene_segmentation_remote_handles_weixin_example_video() -> None:
    require_remote_env(
        "AZURE_ENDPOINT",
        "AZURE_API_KEY",
        "AZURE_MODEL",
        "AZURE_STORAGE_CONNECTION_STRING",
    )
    if not WEIXIN_EXAMPLE_VIDEO_PATH.exists():
        pytest.skip(
            f"Local example video not found: {WEIXIN_EXAMPLE_VIDEO_PATH}")

    def _run() -> None:
        settings = DeploymentSettings.from_env()
        storage_service = AzureBlobStorageService(settings)

        tmp_dir = Path(tempfile.mkdtemp(
            prefix="scene-segmentation-weixin-example-"))
        copied_video_path = tmp_dir / WEIXIN_EXAMPLE_VIDEO_PATH.name
        shutil.copy2(WEIXIN_EXAMPLE_VIDEO_PATH, copied_video_path)

        local_temp_dir = (
            "temp_video_workdir/"
            f"test_scene_segmentation_weixin_example_{tmp_dir.name}"
        )
        local_temp_root = Path.cwd() / local_temp_dir
        try:
            with patch.dict(
                os.environ,
                {
                    "USE_LOCAL_TEMP_DIR": "true",
                    "LOCAL_TEMP_DIR": local_temp_dir,
                },
                clear=False,
            ):
                preprocess_output = asyncio.run(
                    PreprocessStage().run(
                        PreprocessStageInput(
                            asset_path=str(copied_video_path),
                            asset_type=InputMediaAssetTyps.VIDEO,
                        )
                    )
                )
                stage = SceneSegmentationStage(
                    llm_model_client=build_azure_client(),
                    scene_detector="pyscenedetect",
                    storage_service=storage_service,
                    llm_image_container=settings.llm_image_container,
                    llm_image_cleanup_delay_seconds=0,
                )
                output = asyncio.run(
                    stage.run(
                        SceneSegmentationStageInput(
                            video_path=preprocess_output.video_metadata.path,
                            duration=preprocess_output.video_metadata.duration,
                            fps=preprocess_output.video_metadata.fps,
                            preferred_language=Language.EN,
                        )
                    )
                )

            assert preprocess_output.video_metadata.duration > 55.0
            _assert_remote_scene_segmentation_is_complete(
                scene_messages=output.scene_understanding_messages,
                expected_min_scene_count=10,
                expected_duration=preprocess_output.video_metadata.duration,
            )
            assert all(
                scene.visual_summary for scene in output.scene_understanding_messages)
            assert all(scene.end_timestamp >=
                       scene.start_timestamp for scene in output.scene_understanding_messages)
            assert output.thumbnail_path is not None
            assert output.thumbnail_path.exists()
            assert output.thumbnail_path.resolve().parent == copied_video_path.resolve().parent
            assert output.token_usage is not None
            assert output.token_usage["total_tokens"] > 0
        except Exception as exc:
            handle_remote_failure(exc)
            raise
        finally:
            shutil.rmtree(local_temp_root, ignore_errors=True)
            shutil.rmtree(tmp_dir, ignore_errors=True)

    run_with_remote_rate_limit_retry(
        _run,
        operation_name="test_scene_segmentation_remote_handles_weixin_example_video",
    )


@contextmanager
def _run_remote_workflow_on_regression_video(
    *,
    user_prompt: str,
    music_model_spec: str,
    include_vocals: bool,
    patch_provider_c_provider: bool = False,
    patch_basic_provider: bool = False,
    patch_enhanced_provider: bool = False,
    source_video_override: Path | None = None,
    preserve_original_audio: bool = False,
):
    require_remote_env(
        "AZURE_ENDPOINT",
        "AZURE_API_KEY",
        "AZURE_MODEL",
        "AZURE_STORAGE_CONNECTION_STRING",
    )
    source_video = source_video_override or SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH
    assert source_video.exists()

    settings = DeploymentSettings.from_env()
    storage_service = AzureBlobStorageService(settings)

    tmp_dir = Path(tempfile.mkdtemp(prefix="video-music-remote-e2e-"))
    copied_video_path = tmp_dir / source_video.name
    shutil.copy2(source_video, copied_video_path)
    fallback_audio_dir = tmp_dir / "patched-provider-audio"

    local_temp_dir = (
        "temp_video_workdir/"
        f"test_video_music_workflow_remote_{music_model_spec}_{tmp_dir.name}"
    )
    local_temp_root = Path.cwd() / local_temp_dir
    try:
        with ExitStack() as stack:
            stack.enter_context(
                patch.dict(
                    os.environ,
                    {
                        "USE_LOCAL_TEMP_DIR": "true",
                        "LOCAL_TEMP_DIR": local_temp_dir,
                    },
                    clear=False,
                )
            )
            if patch_provider_c_provider:
                stack.enter_context(
                    patch(
                        "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.ProviderCApi",
                        return_value=MagicMock(),
                    )
                )
            if patch_basic_provider:
                stack.enter_context(
                    patch(
                        "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.build_music_provider",
                        return_value=_FakeAsyncBasicProvider(
                            fallback_audio_dir),
                    )
                )
            if patch_enhanced_provider:
                stack.enter_context(
                    patch(
                        "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow.build_edenn_enhanced_music_provider",
                        return_value=_FakeAsyncProviderBProvider(
                            fallback_audio_dir),
                    )
                )

            workflow = VideoMusicWorkflowE2E(
                storage_service=storage_service,
                llm_image_container=settings.llm_image_container,
                llm_image_cleanup_delay_seconds=0,
            )
            output = asyncio.run(
                workflow.generate(
                    VideoMusicWorkflowE2EInput(
                        video_path=str(copied_video_path),
                        user_prompt=user_prompt,
                        music_model_spec=music_model_spec,
                        include_vocals=include_vocals,
                        preserve_original_audio=preserve_original_audio,
                    )
                )
            )
        yield output, copied_video_path
    except Exception as exc:
        handle_remote_failure(exc)
        raise
    finally:
        shutil.rmtree(local_temp_root, ignore_errors=True)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _assert_common_remote_output(
    *,
    output,
    copied_video_path: Path,
    expected_model_spec: str,
    expect_vocals: bool,
) -> None:
    music_output_path = output.complete_generated_music_path or output.generated_music_path

    if not output.scenes or music_output_path is None:
        handle_remote_failure(RuntimeError(
            "Remote workflow returned incomplete output."))

    assert output.video_metadata.path == copied_video_path.resolve()
    assert output.video_metadata.duration > 18.0
    assert output.video_metadata.fps == 30.0
    assert len(output.scenes) > 0
    assert all(scene.visual_summary for scene in output.scenes)
    assert all(scene.end_timestamp >=
               scene.start_timestamp for scene in output.scenes)
    assert output.thumbnail_path is not None
    assert output.thumbnail_path.exists()
    assert output.generated_music_path.exists()
    if output.complete_generated_music_path is not None:
        assert output.complete_generated_music_path.exists()
    assert output.secondary_complete_generated_music_path is None
    assert music_output_path.exists()
    assert output.remixed_video_path.exists()
    # The remixed video must actually carry a full-length audio track. A source
    # video with an embedded thumbnail stream previously truncated the muxed audio
    # to ~0s (silent video); guard every scenario against that regression.
    remixed_audio_s = _audio_stream_duration_seconds(output.remixed_video_path)
    assert remixed_audio_s >= output.video_metadata.duration * 0.8, (
        f"remixed video audio is truncated ({remixed_audio_s:.2f}s vs "
        f"video {output.video_metadata.duration:.2f}s) — the remix is effectively silent"
    )
    assert output.video_title.strip()
    assert output.video_description.strip()
    assert output.include_vocals is expect_vocals
    assert output.UsedMusicModelSpecs == expected_model_spec
    assert output.token_usage is not None
    assert output.token_usage["total_tokens"] > 0
    assert output.token_usage_breakdown is not None
    assert output.job_finished_timestamp >= output.job_received_timestamp
    assert output.user_requested_language
    assert isinstance(output.lyrics_timestamps, list)
    assert all(word.endS >= word.startS >=
               0 for word in output.lyrics_timestamps)


@pytest.mark.remote_integration
def test_generate_remote_workflow_edenn_basic_contract_on_regression_video() -> None:
    require_remote_env("PROVIDER_A_API_KEY")

    def _run() -> None:
        with _run_remote_workflow_on_regression_video(
            user_prompt="Create a steady instrumental electronic track for this video.",
            music_model_spec="edenn_basic",
            include_vocals=False,
            patch_provider_c_provider=True,
            patch_enhanced_provider=True,
        ) as (output, copied_video_path):
            _assert_common_remote_output(
                output=output,
                copied_video_path=copied_video_path,
                expected_model_spec="edenn_basic",
                expect_vocals=False,
            )
            assert {"global_music_prompt", "global_mood", "tempo_bpm", "instruments"} <= set(
                output.music_prompt
            )
            assert output.complete_generated_music_path is None
            assert output.secondary_complete_generated_music_path is None

    run_with_remote_rate_limit_retry(
        _run,
        operation_name="test_generate_remote_workflow_edenn_basic_contract_on_regression_video",
    )


@pytest.mark.remote_integration
def test_generate_remote_workflow_edenn_enhanced_contract_on_regression_video() -> None:
    require_any_remote_env(
        "EDENN_ENHANCED_PROVIDER_B_API_KEY",
        "PROVIDER_B_API_KEY",
        "PROVIDER_B_API_KEY_1",
        "PROVIDER_B_API_KEY_2",
    )

    def _run() -> None:
        with _run_remote_workflow_on_regression_video(
            user_prompt="Create a modern female vocal pop song with clear lyrics for this video.",
            music_model_spec="edenn_enhanced",
            include_vocals=True,
            patch_provider_c_provider=True,
            patch_basic_provider=True,
        ) as (output, copied_video_path):
            _assert_common_remote_output(
                output=output,
                copied_video_path=copied_video_path,
                expected_model_spec="edenn_enhanced",
                expect_vocals=True,
            )
            assert {"style_prompt", "lyrics_prompt"} <= set(
                output.music_prompt)
            assert output.complete_generated_music_path is not None
            assert output.generated_music_path != output.complete_generated_music_path

    run_with_remote_rate_limit_retry(
        _run,
        operation_name="test_generate_remote_workflow_edenn_enhanced_contract_on_regression_video",
    )


@pytest.mark.remote_integration
def test_generate_remote_workflow_edenn_enhanced_instrumental_contract_on_regression_video() -> None:
    require_any_remote_env(
        "EDENN_ENHANCED_PROVIDER_B_API_KEY",
        "PROVIDER_B_API_KEY",
        "PROVIDER_B_API_KEY_1",
        "PROVIDER_B_API_KEY_2",
    )

    def _run() -> None:
        with _run_remote_workflow_on_regression_video(
            user_prompt="Create a cinematic instrumental electronic score for this video. No vocals or lyrics.",
            music_model_spec="edenn_enhanced",
            include_vocals=False,
            patch_provider_c_provider=True,
            patch_basic_provider=True,
        ) as (output, copied_video_path):
            _assert_common_remote_output(
                output=output,
                copied_video_path=copied_video_path,
                expected_model_spec="edenn_enhanced",
                expect_vocals=False,
            )
            assert {"style_prompt", "lyrics_prompt"} <= set(
                output.music_prompt)
            assert output.music_prompt["lyrics_prompt"] == ""
            assert output.primary_full_lyrics is None
            assert output.complete_generated_music_path is not None
            assert output.generated_music_path != output.complete_generated_music_path

    run_with_remote_rate_limit_retry(
        _run,
        operation_name="test_generate_remote_workflow_edenn_enhanced_instrumental_contract_on_regression_video",
    )


@pytest.mark.remote_integration
def test_generate_remote_workflow_edenn_studio_contract_on_regression_video() -> None:
    require_remote_env("PROVIDER_C_API_KEY")

    def _run() -> None:
        with _run_remote_workflow_on_regression_video(
            user_prompt="Create a cinematic female vocal anthem with lyrics for this video.",
            music_model_spec="edenn_studio",
            include_vocals=True,
            patch_basic_provider=True,
            patch_enhanced_provider=True,
        ) as (output, copied_video_path):
            _assert_common_remote_output(
                output=output,
                copied_video_path=copied_video_path,
                expected_model_spec="edenn_studio",
                expect_vocals=True,
            )
            assert {"style_prompt", "lyrics_prompt"} <= set(
                output.music_prompt)
            assert output.complete_generated_music_path is not None
            assert output.generated_music_path != output.complete_generated_music_path

    run_with_remote_rate_limit_retry(
        _run,
        operation_name="test_generate_remote_workflow_edenn_studio_contract_on_regression_video",
    )


@pytest.mark.remote_integration
def test_generate_remote_workflow_edenn_studio_instrumental_contract_on_regression_video() -> None:
    require_remote_env("PROVIDER_C_API_KEY")

    def _run() -> None:
        with _run_remote_workflow_on_regression_video(
            user_prompt="Create a cinematic instrumental anthem for this video. No vocals or lyrics.",
            music_model_spec="edenn_studio",
            include_vocals=False,
            patch_basic_provider=True,
            patch_enhanced_provider=True,
        ) as (output, copied_video_path):
            _assert_common_remote_output(
                output=output,
                copied_video_path=copied_video_path,
                expected_model_spec="edenn_studio",
                expect_vocals=False,
            )
            assert {"prompt"} <= set(output.music_prompt)
            assert output.primary_full_lyrics is None
            assert output.complete_generated_music_path is not None
            assert output.generated_music_path != output.complete_generated_music_path

    run_with_remote_rate_limit_retry(
        _run,
        operation_name="test_generate_remote_workflow_edenn_studio_instrumental_contract_on_regression_video",
    )


@pytest.mark.remote_integration
def test_generate_remote_workflow_muxes_audio_over_video_with_embedded_thumbnail() -> None:
    """Reported bug: a source video with an embedded thumbnail stream produced a
    SILENT remix (audio truncated to ~0.1s). Runs the real pipeline on a
    thumbnail-bearing video and asserts the remix carries full audio.
    preserve_original_audio=False (music replaces the source audio)."""
    require_remote_env("PROVIDER_A_API_KEY")

    def _run() -> None:
        wrap_dir = Path(tempfile.mkdtemp(prefix="thumb-wrap-"))
        try:
            thumb_video = _wrap_video_with_embedded_thumbnail(
                SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH, wrap_dir
            )
            with _run_remote_workflow_on_regression_video(
                user_prompt="Create a steady instrumental electronic track for this video.",
                music_model_spec="edenn_basic",
                include_vocals=False,
                patch_provider_c_provider=True,
                patch_enhanced_provider=True,
                source_video_override=thumb_video,
                preserve_original_audio=False,
            ) as (output, copied_video_path):
                _assert_common_remote_output(
                    output=output,
                    copied_video_path=copied_video_path,
                    expected_model_spec="edenn_basic",
                    expect_vocals=False,
                )
        finally:
            shutil.rmtree(wrap_dir, ignore_errors=True)

    run_with_remote_rate_limit_retry(
        _run,
        operation_name="test_generate_remote_workflow_muxes_audio_over_video_with_embedded_thumbnail",
    )


@pytest.mark.remote_integration
def test_generate_remote_workflow_mixes_audio_over_thumbnail_video_preserving_original() -> None:
    """Same thumbnail-bearing video but preserve_original_audio=True (the mix/ducking
    remix branch), verifying that branch also keeps a full-length audio track."""
    require_remote_env("PROVIDER_A_API_KEY")

    def _run() -> None:
        wrap_dir = Path(tempfile.mkdtemp(prefix="thumb-wrap-mix-"))
        try:
            thumb_video = _wrap_video_with_embedded_thumbnail(
                SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH, wrap_dir
            )
            with _run_remote_workflow_on_regression_video(
                user_prompt="Create a steady instrumental electronic track for this video.",
                music_model_spec="edenn_basic",
                include_vocals=False,
                patch_provider_c_provider=True,
                patch_enhanced_provider=True,
                source_video_override=thumb_video,
                preserve_original_audio=True,
            ) as (output, copied_video_path):
                _assert_common_remote_output(
                    output=output,
                    copied_video_path=copied_video_path,
                    expected_model_spec="edenn_basic",
                    expect_vocals=False,
                )
        finally:
            shutil.rmtree(wrap_dir, ignore_errors=True)

    run_with_remote_rate_limit_retry(
        _run,
        operation_name="test_generate_remote_workflow_mixes_audio_over_thumbnail_video_preserving_original",
    )
