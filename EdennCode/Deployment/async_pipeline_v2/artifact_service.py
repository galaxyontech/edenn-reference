from __future__ import annotations

import hashlib
import logging
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from EdennCode.Deployment.api_common import (
    download_public_file_to_disk,
    filename_from_url,
    guess_audio_content_type,
    guess_image_content_type,
    sanitize_filename,
)
from EdennCode.Deployment.async_pipeline_v2.models import AsyncV2Artifact
from EdennCode.Deployment.async_pipeline_v2.repositories import AsyncPipelineV2Repository
from EdennCode.Util.MediaUtils.image_utils import get_image_dimensions
from EdennCode.exceptions import EdennApiError
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata import VideoMetadata
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoPreprocessStage.media_preprocess_stage import (
    validate_input_video_duration,
)


logger = logging.getLogger(__name__)

# Short in-process retries for blob uploads. A transient storage blip must not
# fail the task: for output artifacts the paid generation has already happened,
# and a task-level retry would re-run (and re-bill) the whole pipeline.
UPLOAD_MAX_ATTEMPTS = 3
UPLOAD_RETRY_BACKOFF_S = 2.0

# Source images must be at least this many pixels on BOTH edges. Enforced at
# staging (uploads, URLs, and /assets/image pre-staging) so an undersized image
# is a clean 400 at submit rather than a blurry upscaled slideshow.
MIN_IMAGE_EDGE_PX = 720


def _validate_min_image_dimensions(path: Path) -> None:
    try:
        width, height = get_image_dimensions(path)
    except Exception as exc:  # noqa: BLE001 - unreadable image is a client error
        raise EdennApiError(
            f"Could not read image dimensions for {path.name}: {exc}",
            public_message=(
                "One of the provided images could not be read. Please supply valid "
                "image files (JPEG, PNG, or WebP)."
            ),
            status_code=400,
            component="api",
            operation="validate_image_dimensions",
        ) from exc
    if width < MIN_IMAGE_EDGE_PX or height < MIN_IMAGE_EDGE_PX:
        raise EdennApiError(
            f"Image {path.name} is {width}x{height}; below the {MIN_IMAGE_EDGE_PX}px minimum edge.",
            public_message=(
                f"Each image must be at least {MIN_IMAGE_EDGE_PX}px on both sides; "
                f"'{sanitize_filename(path.name)}' is {width}x{height}."
            ),
            status_code=400,
            component="api",
            operation="validate_image_dimensions",
        )


def upload_path_with_retry(
    storage: Any,
    *,
    container: str,
    path: Path,
    blob_name: Optional[str],
    content_type: Optional[str],
) -> Optional[str]:
    """storage.upload_path with short backoff retries for transient blips."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, UPLOAD_MAX_ATTEMPTS + 1):
        try:
            return storage.upload_path(
                container=container,
                path=path,
                blob_name=blob_name,
                content_type=content_type,
            )
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "blob upload attempt %d/%d failed (container=%s blob=%s): %s",
                attempt, UPLOAD_MAX_ATTEMPTS, container, blob_name, exc,
            )
            if attempt < UPLOAD_MAX_ATTEMPTS:
                time.sleep(UPLOAD_RETRY_BACKOFF_S * attempt)
    assert last_exc is not None
    raise last_exc


_VIDEO_CONTENT_TYPES = {
    ".avi": "video/x-msvideo",
    ".m4v": "video/x-m4v",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".mp4": "video/mp4",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".webm": "video/webm",
}


@dataclass(frozen=True)
class StagedVideoArtifact:
    artifact: AsyncV2Artifact
    local_path: Path
    video_metadata: dict[str, Any]
    video_hash: str
    content_type: str
    upload_blob: Optional[str]
    upload_url: Optional[str]


@dataclass(frozen=True)
class StagedInputArtifact:
    """Result of staging a non-video input asset (image / vocal sample)."""
    artifact: AsyncV2Artifact
    local_path: Path
    content_type: str
    sha256: str
    upload_blob: Optional[str]
    upload_url: Optional[str]


def _input_blob_name(*, job_id: str, kind: str, index: Optional[int], source_path: Path, default_suffix: str) -> str:
    suffix = source_path.suffix.lower()
    if not suffix or len(suffix) > 16:
        suffix = default_suffix
    safe_suffix = "".join(ch for ch in suffix if ch.isalnum() or ch == ".") or default_suffix
    if index is None:
        return f"jobs/{job_id}/input/{kind}{safe_suffix}"
    return f"jobs/{job_id}/input/{kind}s/{kind}_{index + 1}{safe_suffix}"


def guess_video_content_type(path: Path, *, upload_content_type: Optional[str] = None) -> str:
    if upload_content_type:
        return upload_content_type
    suffix = path.suffix.lower()
    if suffix in _VIDEO_CONTENT_TYPES:
        return _VIDEO_CONTENT_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def source_video_blob_name(*, job_id: str, source_path: Path) -> str:
    suffix = source_path.suffix.lower()
    if not suffix or len(suffix) > 16:
        suffix = ".mp4"
    safe_suffix = "".join(ch for ch in suffix if ch.isalnum() or ch == ".") or ".mp4"
    return f"jobs/{job_id}/input/source_video{safe_suffix}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class ArtifactStagingService:
    def __init__(
        self,
        *,
        repository: AsyncPipelineV2Repository,
        storage: Any = None,
        upload_container: str = "user-uploads",
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.upload_container = upload_container

    def stage_video_path(
        self,
        *,
        job_id: str,
        source_path: Path,
        artifact_id: Optional[str] = None,
        role: str = "input",
        source_kind: str = "local_path",
        source_url: Optional[str] = None,
        upload_content_type: Optional[str] = None,
        blob_name: Optional[str] = None,
        request_metadata: Optional[dict[str, Any]] = None,
        validate_duration: bool = True,
    ) -> StagedVideoArtifact:
        path = source_path.expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(path)
        if not path.is_file():
            raise ValueError(f"Source video path is not a file: {path}")

        metadata = VideoMetadata.from_file(path)
        if validate_duration:
            validate_input_video_duration(metadata.duration, source_path=path)

        content_type = guess_video_content_type(
            path,
            upload_content_type=upload_content_type,
        )
        video_hash = sha256_file(path)
        upload_blob = None
        upload_url = None
        container = None

        if self.storage is not None and getattr(self.storage, "enabled", True):
            container = self.upload_container
            resolved_blob_name = blob_name or source_video_blob_name(
                job_id=job_id,
                source_path=path,
            )
            upload_blob = upload_path_with_retry(
                self.storage,
                container=container,
                path=path,
                blob_name=resolved_blob_name,
                content_type=content_type,
            )
            if upload_blob and hasattr(self.storage, "generate_sas_url"):
                upload_url = self.storage.generate_sas_url(
                    container=container,
                    blob_name=upload_blob,
                )

        video_metadata = metadata.to_dict()
        video_metadata["path"] = path.name
        metadata_json = {
            "source_kind": source_kind,
            "source_filename": sanitize_filename(path.name),
            "source_url": source_url,
            "sha256": video_hash,
            "size_bytes": path.stat().st_size,
            "duration": metadata.duration,
            "width": metadata.width,
            "height": metadata.height,
            "fps": metadata.fps,
            "has_audio": metadata.has_audio,
            "video_codec": metadata.video_codec,
            "audio_codec": metadata.audio_codec,
            "video_metadata": video_metadata,
            "request": dict(request_metadata or {}),
            "uploaded": bool(upload_blob),
        }

        artifact = self.repository.add_artifact(
            artifact_id=artifact_id,
            job_id=job_id,
            artifact_type="source_video",
            role=role,
            container=container,
            blob_name=upload_blob,
            url=upload_url,
            content_type=content_type,
            local_path=str(path),
            metadata_json=metadata_json,
        )
        self.repository.add_event(
            job_id=job_id,
            event_type="asset.staged",
            stage_name="artifact-staging",
            message="Source video staged.",
            payload_json={
                "artifact_id": artifact.artifact_id,
                "artifact_type": artifact.artifact_type,
                "sha256": video_hash,
                "duration": metadata.duration,
                "width": metadata.width,
                "height": metadata.height,
                "uploaded": bool(upload_blob),
            },
        )
        return StagedVideoArtifact(
            artifact=artifact,
            local_path=path,
            video_metadata=video_metadata,
            video_hash=video_hash,
            content_type=content_type,
            upload_blob=upload_blob,
            upload_url=upload_url,
        )

    def stage_video_bytes(
        self,
        *,
        job_id: str,
        data: bytes,
        filename: str,
        destination_dir: Path,
        artifact_id: Optional[str] = None,
        upload_content_type: Optional[str] = None,
        request_metadata: Optional[dict[str, Any]] = None,
        validate_duration: bool = True,
    ) -> StagedVideoArtifact:
        safe_name = sanitize_filename(filename or "source_video.mp4")
        destination = destination_dir / safe_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return self.stage_video_path(
            job_id=job_id,
            source_path=destination,
            artifact_id=artifact_id,
            source_kind="upload_bytes",
            upload_content_type=upload_content_type,
            request_metadata=request_metadata,
            validate_duration=validate_duration,
        )

    async def stage_video_url(
        self,
        *,
        job_id: str,
        video_url: str,
        destination_dir: Path,
        artifact_id: Optional[str] = None,
        request_metadata: Optional[dict[str, Any]] = None,
        validate_duration: bool = True,
        max_bytes: Optional[int] = None,
    ) -> StagedVideoArtifact:
        url_value = video_url.strip()
        filename = filename_from_url(url_value) or "source_video.mp4"
        local_path = await download_public_file_to_disk(
            url=url_value,
            destination=destination_dir / filename,
            asset_label="video",
            max_bytes=max_bytes,
        )
        return self.stage_video_path(
            job_id=job_id,
            source_path=local_path,
            artifact_id=artifact_id,
            source_kind="remote_url",
            source_url=url_value,
            request_metadata=request_metadata,
            validate_duration=validate_duration,
        )

    # ------------------------------------------------------------------
    # Multi-image inputs: source images + optional vocal sample. These stage
    # exactly like source video (hash + upload + artifact + event) but with
    # image/audio content types and their own artifact_type/role conventions.
    # ------------------------------------------------------------------
    def _stage_input_path(
        self,
        *,
        job_id: str,
        source_path: Path,
        artifact_type: str,
        role: str,
        content_type: str,
        blob_name: str,
        artifact_id: Optional[str] = None,
        source_kind: str = "local_path",
        source_url: Optional[str] = None,
        request_metadata: Optional[dict[str, Any]] = None,
    ) -> StagedInputArtifact:
        path = source_path.expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(path)
        if not path.is_file():
            raise ValueError(f"Input asset path is not a file: {path}")

        if artifact_type == "source_image":
            _validate_min_image_dimensions(path)

        file_hash = sha256_file(path)
        upload_blob = None
        upload_url = None
        container = None
        if self.storage is not None and getattr(self.storage, "enabled", True):
            container = self.upload_container
            upload_blob = upload_path_with_retry(
                self.storage,
                container=container,
                path=path,
                blob_name=blob_name,
                content_type=content_type,
            )
            if upload_blob and hasattr(self.storage, "generate_sas_url"):
                upload_url = self.storage.generate_sas_url(
                    container=container,
                    blob_name=upload_blob,
                )

        metadata_json = {
            "source_kind": source_kind,
            "source_filename": sanitize_filename(path.name),
            "source_url": source_url,
            "sha256": file_hash,
            "size_bytes": path.stat().st_size,
            "request": dict(request_metadata or {}),
            "uploaded": bool(upload_blob),
        }
        artifact = self.repository.add_artifact(
            artifact_id=artifact_id,
            job_id=job_id,
            artifact_type=artifact_type,
            role=role,
            container=container,
            blob_name=upload_blob,
            url=upload_url,
            content_type=content_type,
            local_path=str(path),
            metadata_json=metadata_json,
        )
        self.repository.add_event(
            job_id=job_id,
            event_type="asset.staged",
            stage_name="artifact-staging",
            message=f"{artifact_type} staged.",
            payload_json={
                "artifact_id": artifact.artifact_id,
                "artifact_type": artifact_type,
                "role": role,
                "sha256": file_hash,
                "uploaded": bool(upload_blob),
            },
        )
        return StagedInputArtifact(
            artifact=artifact,
            local_path=path,
            content_type=content_type,
            sha256=file_hash,
            upload_blob=upload_blob,
            upload_url=upload_url,
        )

    def stage_image_bytes(
        self,
        *,
        job_id: str,
        data: bytes,
        filename: str,
        destination_dir: Path,
        index: int,
        artifact_id: Optional[str] = None,
        upload_content_type: Optional[str] = None,
        request_metadata: Optional[dict[str, Any]] = None,
    ) -> StagedInputArtifact:
        safe_name = sanitize_filename(filename or f"image_{index + 1}.png")
        destination = destination_dir / safe_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        content_type = upload_content_type or guess_image_content_type(destination)
        return self._stage_input_path(
            job_id=job_id,
            source_path=destination,
            artifact_type="source_image",
            role=f"image_{index}",
            content_type=content_type,
            blob_name=_input_blob_name(
                job_id=job_id, kind="image", index=index,
                source_path=destination, default_suffix=".png",
            ),
            artifact_id=artifact_id,
            source_kind="upload_bytes",
            request_metadata=request_metadata,
        )

    async def stage_image_url(
        self,
        *,
        job_id: str,
        image_url: str,
        destination_dir: Path,
        index: int,
        artifact_id: Optional[str] = None,
        request_metadata: Optional[dict[str, Any]] = None,
    ) -> StagedInputArtifact:
        url_value = image_url.strip()
        filename = filename_from_url(url_value) or f"image_{index + 1}.png"
        local_path = await download_public_file_to_disk(
            url=url_value,
            destination=destination_dir / filename,
            asset_label="image",
        )
        return self._stage_input_path(
            job_id=job_id,
            source_path=local_path,
            artifact_type="source_image",
            role=f"image_{index}",
            content_type=guess_image_content_type(local_path),
            blob_name=_input_blob_name(
                job_id=job_id, kind="image", index=index,
                source_path=local_path, default_suffix=".png",
            ),
            artifact_id=artifact_id,
            source_kind="remote_url",
            source_url=url_value,
            request_metadata=request_metadata,
        )

    def stage_vocal_sample_bytes(
        self,
        *,
        job_id: str,
        data: bytes,
        filename: str,
        destination_dir: Path,
        artifact_id: Optional[str] = None,
        upload_content_type: Optional[str] = None,
        request_metadata: Optional[dict[str, Any]] = None,
    ) -> StagedInputArtifact:
        safe_name = sanitize_filename(filename or "vocal_sample.m4a")
        destination = destination_dir / safe_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        content_type = upload_content_type or guess_audio_content_type(destination)
        return self._stage_input_path(
            job_id=job_id,
            source_path=destination,
            artifact_type="vocal_sample",
            role="vocal",
            content_type=content_type,
            blob_name=_input_blob_name(
                job_id=job_id, kind="vocal_sample", index=None,
                source_path=destination, default_suffix=".m4a",
            ),
            artifact_id=artifact_id,
            source_kind="upload_bytes",
            request_metadata=request_metadata,
        )

    async def stage_vocal_sample_url(
        self,
        *,
        job_id: str,
        vocal_sample_url: str,
        destination_dir: Path,
        artifact_id: Optional[str] = None,
        request_metadata: Optional[dict[str, Any]] = None,
    ) -> StagedInputArtifact:
        url_value = vocal_sample_url.strip()
        filename = filename_from_url(url_value) or "vocal_sample.m4a"
        local_path = await download_public_file_to_disk(
            url=url_value,
            destination=destination_dir / filename,
            asset_label="vocal sample",
        )
        return self._stage_input_path(
            job_id=job_id,
            source_path=local_path,
            artifact_type="vocal_sample",
            role="vocal",
            content_type=guess_audio_content_type(local_path),
            blob_name=_input_blob_name(
                job_id=job_id, kind="vocal_sample", index=None,
                source_path=local_path, default_suffix=".m4a",
            ),
            artifact_id=artifact_id,
            source_kind="remote_url",
            source_url=url_value,
            request_metadata=request_metadata,
        )


__all__ = [
    "ArtifactStagingService",
    "StagedInputArtifact",
    "StagedVideoArtifact",
    "guess_video_content_type",
    "sha256_file",
    "source_video_blob_name",
]
