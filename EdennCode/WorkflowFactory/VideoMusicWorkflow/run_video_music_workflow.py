from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Deployment.storage import create_storage_service
from EdennCode.Deployment.api_common import (
    guess_audio_content_type,
    guess_image_content_type,
    probe_audio_metrics,
)
from EdennCode.Deployment.api_video_generation import (
    VideoJobAssets,
    VideoJobResponse,
    _guess_input_video_content_type,
    _provider_neutral_blob_name,
    build_video_job_response,
)
from EdennCode.Deployment.recommendation_persistence import RecommendationAssetIds
from EdennCode.Deployment.workflows import (
    VideoGenerationOrchestrator,
    VideoGenerationResult,
)
from EdennCode.env import load_env
from EdennCode.Util.MediaUtils import compress_video_to_max_height


def _default_local_temp_dir(modelspec: str) -> str:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    return f"temp_video_workdir/video_music_cli_{modelspec}_{timestamp}"


def run_video_music_pipeline(
    *,
    video_path: Path,
    user_prompt: str,
    modelspec: str,
    include_vocals: bool = False,
    vocal_gender: str = "female",
    preserve_original_audio: bool = False,
    music_volume: float = 1.0,
    water_mark: bool = False,
    audio_output_format: str | None = None,
    local_temp_dir: str | None = None,
    compression_flag: bool = False,
    llm_pool_enabled: bool = False,
    llm_endpoint_label: str | None = None,
    west_model: str | None = None,
    west_api_version: str | None = None,
    west_api_mode: str | None = None,
) -> VideoGenerationResult:
    load_env()

    resolved_temp_dir = local_temp_dir or _default_local_temp_dir(modelspec)
    os.environ["USE_LOCAL_TEMP_DIR"] = "true"
    os.environ["LOCAL_TEMP_DIR"] = resolved_temp_dir
    if llm_pool_enabled:
        os.environ["AZURE_LLM_POOL_ENABLED"] = "true"
    if llm_endpoint_label:
        os.environ["AZURE_LLM_POOL_FORCE_LABEL"] = llm_endpoint_label
    if west_model:
        os.environ["AZURE_MODEL_WEST_US"] = west_model
    if west_api_version:
        os.environ["AZURE_API_VERSION_WEST_US"] = west_api_version
    if west_api_mode:
        os.environ["AZURE_API_MODE_WEST_US"] = west_api_mode
    source_video_path = video_path.expanduser().resolve()
    effective_video_path = source_video_path
    if compression_flag:
        temp_dir = Path(resolved_temp_dir).expanduser().resolve()
        temp_dir.mkdir(parents=True, exist_ok=True)
        effective_video_path = compress_video_to_max_height(
            video_path=source_video_path,
            output_path=temp_dir / f"{source_video_path.stem}_1280h.mp4",
            max_height=1280,
            repair_decode_errors=True,
            return_original_on_failure=True,
            validate_reencode=True,
        )

    settings = DeploymentSettings.from_env()
    storage = create_storage_service(settings)
    workflow = VideoGenerationOrchestrator(
        storage=storage,
        llm_image_container=settings.llm_image_container,
        llm_image_sas_ttl_minutes=settings.llm_image_sas_ttl_minutes,
        llm_image_cleanup_delay_seconds=settings.llm_image_cleanup_delay_seconds,
    )

    return asyncio.run(
        workflow.run(
            video_path=effective_video_path,
            preserve_original_audio=preserve_original_audio,
            music_volume=music_volume,
            water_mark=water_mark,
            include_vocals=include_vocals,
            vocal_gender=vocal_gender,
            user_prompt=user_prompt,
            modelspec=modelspec,
            audio_output_format=audio_output_format,
        )
    )


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if is_dataclass(value):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in fields(value)
        }
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump())
    if hasattr(value, "dict"):
        return _jsonable(value.dict())
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return str(value)


def _write_json_result(path: Path, result: VideoGenerationResult) -> None:
    payload = _jsonable(result)
    if isinstance(payload, dict):
        payload = {"status": "completed", **payload}
    _write_json_payload(path, payload)


def _write_json_payload(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def upload_video_music_result_assets(
    *,
    result: VideoGenerationResult,
    source_video_path: Path,
    modelspec: str,
) -> VideoJobResponse:
    settings = DeploymentSettings.from_env()
    storage = create_storage_service(settings)
    if not getattr(storage, "enabled", False):
        raise RuntimeError(
            "Storage is not enabled. Set AZURE_STORAGE_CONNECTION_STRING or "
            "AZURE_STORAGE_ACCOUNT_URL before using --upload-results."
        )

    job_id = getattr(result, "job_id", "") or uuid4().hex
    asset_ids = RecommendationAssetIds.create(job_id=job_id)
    source_video_path = source_video_path.expanduser().resolve()
    generated_music_path = Path(result.generated_music_path).expanduser()
    remixed_video_path = Path(result.remixed_video_path).expanduser()

    upload_blob = storage.upload_path(
        container=settings.upload_container,
        path=source_video_path,
        blob_name=_provider_neutral_blob_name(
            job_id=job_id,
            folder="input",
            label="source_video",
            source_path=source_video_path,
        ),
        content_type=_guess_input_video_content_type(
            source_path=source_video_path,
            upload_content_type=None,
            compression_applied=False,
        ),
    )
    audio_blob = storage.upload_path(
        container=settings.audio_container_name,
        path=generated_music_path,
        blob_name=_provider_neutral_blob_name(
            job_id=job_id,
            folder="audio",
            label="matched_audio",
            source_path=generated_music_path,
        ),
        content_type=guess_audio_content_type(generated_music_path),
    )

    complete_audio_blob = None
    complete_music_path = getattr(result, "complete_generated_music_path", None)
    complete_music_path = (
        Path(complete_music_path).expanduser() if complete_music_path else None
    )
    if complete_music_path and complete_music_path.exists():
        complete_audio_blob = storage.upload_path(
            container=settings.audio_container_name,
            path=complete_music_path,
            blob_name=_provider_neutral_blob_name(
                job_id=job_id,
                folder="audio/complete",
                label="complete_audio",
                source_path=complete_music_path,
            ),
            content_type=guess_audio_content_type(complete_music_path),
        )

    secondary_complete_audio_blob = None
    secondary_complete_music_path = getattr(
        result, "secondary_complete_generated_music_path", None
    )
    secondary_complete_music_path = (
        Path(secondary_complete_music_path).expanduser()
        if secondary_complete_music_path
        else None
    )
    if secondary_complete_music_path and secondary_complete_music_path.exists():
        secondary_complete_audio_blob = storage.upload_path(
            container=settings.audio_container_name,
            path=secondary_complete_music_path,
            blob_name=_provider_neutral_blob_name(
                job_id=job_id,
                folder="audio/complete/secondary",
                label="secondary_audio",
                source_path=secondary_complete_music_path,
            ),
            content_type=guess_audio_content_type(secondary_complete_music_path),
        )

    video_blob = storage.upload_path(
        container=settings.output_container,
        path=remixed_video_path,
        blob_name=_provider_neutral_blob_name(
            job_id=job_id,
            folder="video",
            label="remixed_video",
            source_path=remixed_video_path,
        ),
        content_type="video/mp4",
    )

    thumbnail_blob = None
    thumbnail_path = getattr(result, "thumbnail_path", None)
    thumbnail_path = Path(thumbnail_path).expanduser() if thumbnail_path else None
    if thumbnail_path and thumbnail_path.exists():
        thumbnail_blob = storage.upload_path(
            container=settings.output_container,
            path=thumbnail_path,
            blob_name=_provider_neutral_blob_name(
                job_id=job_id,
                folder="thumbnail",
                label="thumbnail",
                source_path=thumbnail_path,
            ),
            content_type=guess_image_content_type(thumbnail_path),
        )

    upload_url = (
        storage.generate_sas_url(container=settings.upload_container, blob_name=upload_blob)
        if upload_blob
        else None
    )
    audio_url = (
        storage.generate_sas_url(container=settings.audio_container_name, blob_name=audio_blob)
        if audio_blob
        else None
    )
    complete_audio_url = (
        storage.generate_sas_url(
            container=settings.audio_container_name,
            blob_name=complete_audio_blob,
        )
        if complete_audio_blob
        else None
    )
    secondary_complete_audio_url = (
        storage.generate_sas_url(
            container=settings.audio_container_name,
            blob_name=secondary_complete_audio_blob,
        )
        if secondary_complete_audio_blob
        else None
    )
    video_url = (
        storage.generate_sas_url(container=settings.output_container, blob_name=video_blob)
        if video_blob
        else None
    )
    thumbnail_url = (
        storage.generate_sas_url(
            container=settings.output_container,
            blob_name=thumbnail_blob,
        )
        if thumbnail_blob
        else None
    )

    audio_duration_s, audio_size_bytes = probe_audio_metrics(generated_music_path)
    complete_audio_duration_s, complete_audio_size_bytes = probe_audio_metrics(
        complete_music_path
        if complete_music_path and complete_music_path.exists()
        else None
    )

    return build_video_job_response(
        job_id=job_id,
        result=result,
        requested_modelspec=modelspec,
        assets=VideoJobAssets(
            audio_url=audio_url,
            audio_duration_s=audio_duration_s,
            audio_size_bytes=audio_size_bytes,
            complete_audio_url=complete_audio_url,
            complete_audio_duration_s=complete_audio_duration_s,
            complete_audio_size_bytes=complete_audio_size_bytes,
            video_url=video_url,
            thumbnail_url=thumbnail_url,
        ),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the video-to-music workflow on a local video file.",
    )
    parser.add_argument(
        "--video",
        type=Path,
        required=True,
        help="Path to the input video file.",
    )
    parser.add_argument(
        "--modelspec",
        default="edenn_basic",
        choices=["edenn_basic", "edenn_enhanced", "edenn_studio"],
        help="Music generation branch to use.",
    )
    parser.add_argument(
        "--user-prompt",
        required=True,
        help="Creative prompt used for music generation.",
    )
    parser.add_argument(
        "--include-vocals",
        action="store_true",
        help="Hint that the request should contain vocals.",
    )
    parser.add_argument(
        "--vocal-gender",
        default="female",
        help="Preferred vocal gender when vocals are requested.",
    )
    parser.add_argument(
        "--preserve-original-audio",
        action="store_true",
        help="Keep the video's original audio during final remix.",
    )
    parser.add_argument(
        "--music-volume",
        type=float,
        default=1.0,
        help="Music gain multiplier used during final mux.",
    )
    parser.add_argument(
        "--water-mark",
        action="store_true",
        help="Append the Edenn spoken watermark to complete/full-track audio outputs.",
    )
    parser.add_argument(
        "--audio-output-format",
        default=None,
        help="Optional provider-specific audio output format.",
    )
    parser.add_argument(
        "--local-temp-dir",
        default=None,
        help="Optional local temp workdir override.",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Optional path to write the structured workflow result as JSON.",
    )
    parser.add_argument(
        "--upload-results",
        action="store_true",
        help=(
            "Upload generated assets to configured storage and write an "
            "API-style JSON response with signed blob URLs."
        ),
    )
    parser.add_argument(
        "--compression-flag",
        action="store_true",
        help="Compress the input video to max height 1280 before workflow execution.",
    )
    parser.add_argument(
        "--llm-pool-enabled",
        action="store_true",
        help="Enable the Azure LLM endpoint pool for this run.",
    )
    parser.add_argument(
        "--llm-endpoint-label",
        default=None,
        help="Force a specific Azure LLM endpoint label, e.g. primary or west_us.",
    )
    parser.add_argument(
        "--west-model",
        default=None,
        help="Override AZURE_MODEL_WEST_US for this run.",
    )
    parser.add_argument(
        "--west-api-version",
        default=None,
        help="Override AZURE_API_VERSION_WEST_US for this run.",
    )
    parser.add_argument(
        "--west-api-mode",
        default=None,
        help="Override AZURE_API_MODE_WEST_US for this run.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    result = run_video_music_pipeline(
        video_path=args.video,
        user_prompt=args.user_prompt,
        modelspec=args.modelspec,
        include_vocals=args.include_vocals,
        vocal_gender=args.vocal_gender,
        preserve_original_audio=args.preserve_original_audio,
        music_volume=args.music_volume,
        water_mark=args.water_mark,
        audio_output_format=args.audio_output_format,
        local_temp_dir=args.local_temp_dir,
        compression_flag=args.compression_flag,
        llm_pool_enabled=args.llm_pool_enabled,
        llm_endpoint_label=args.llm_endpoint_label,
        west_model=args.west_model,
        west_api_version=args.west_api_version,
        west_api_mode=args.west_api_mode,
    )
    print("Used model:", result.used_music_model_spec)
    print("Detected language:", result.user_requested_language)
    print("Include vocals:", result.include_vocals)
    print("Vocal gender:", result.vocal_gender)
    print("Thumbnail:", result.thumbnail_path)
    print("Generated music:", result.generated_music_path)
    print("Complete music:", result.complete_generated_music_path)
    print("Secondary complete music:", result.secondary_complete_generated_music_path)
    print("Remixed video:", result.remixed_video_path)
    uploaded_response = None
    if args.upload_results:
        uploaded_response = upload_video_music_result_assets(
            result=result,
            source_video_path=args.video,
            modelspec=args.modelspec,
        )
        print("Music title:", uploaded_response.audio_metadata.music_title)
        print("Audio URL:", uploaded_response.audio_metadata.audio_url)
        print("Complete audio URL:", uploaded_response.audio_metadata.complete_audio_url)
        print("Video URL:", uploaded_response.video_metadata.video_url)
    if args.output_json:
        if uploaded_response is not None:
            _write_json_payload(
                args.output_json,
                uploaded_response.model_dump(mode="json"),
            )
        else:
            _write_json_result(args.output_json, result)
        print("Output JSON:", args.output_json)
