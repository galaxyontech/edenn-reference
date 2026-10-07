from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from EdennCode.Deployment.api_common import download_public_file_to_disk
from EdennCode.Deployment.api_video_generation import (
    _guess_input_video_content_type,
    _inspect_public_video_metadata,
    _provider_neutral_blob_name,
)
from EdennCode.Deployment.async_pipeline_v2.models import AsyncV2Artifact
from EdennCode.Deployment.async_pipeline_v2.repositories import AsyncPipelineV2Repository
from EdennCode.Util.MediaUtils import compress_video_to_max_height, ensure_h264_video
from EdennCode.exceptions import EdennValidationError


logger = logging.getLogger(__name__)

# Short in-process retries for the source download: a transient blip (or an
# expired stored SAS) must not fail the task — with the no-retry default a
# task-level failure permanently fails the job.
SOURCE_DOWNLOAD_MAX_ATTEMPTS = 3
SOURCE_DOWNLOAD_RETRY_BACKOFF_S = 2.0


@dataclass(frozen=True)
class PreparedSourceVideo:
    """Source video selected for workflow execution.

    Async v2 jobs can start from an uploaded/staged source artifact, then
    optionally produce a compressed source artifact before the expensive video
    music workflow begins. This value object keeps those two concerns explicit:
    the local file path the worker should pass downstream, the artifact that
    represents that path durably, and metadata about whether compression was
    requested, applied, skipped, or reused.
    """

    path: Path
    artifact: AsyncV2Artifact
    compression_info: dict[str, Any]


def coerce_request_bool(value: Any) -> bool:
    """Parse a request flag using the same permissive rules as legacy workers."""

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


class VideoSourcePreparationService:
    """Resolve and optionally compress source videos for async v2 workers.

    The service is intentionally narrow. It owns durable source-video I/O:
    resolving an artifact to a readable local file, applying the request-level
    compression policy, uploading/recording a compressed source artifact when
    compression succeeds, and reusing that artifact on retries. It does not run
    prompt analysis, provider calls, matching, rendering, or any business logic
    from the video-music workflow. Keeping this boundary small lets monolith and
    split workers share CPU-heavy preparation without changing generation
    behavior.
    """

    def __init__(
        self,
        *,
        repository: AsyncPipelineV2Repository,
        settings: Any,
        storage: Any = None,
        stage_name: str = "video_source_preparation",
        diagnostic_logger: Optional[logging.Logger] = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.storage = storage
        self.stage_name = stage_name
        self.logger = diagnostic_logger or logger

    def workdir(self, job_id: str, *parts: str) -> Path:
        """Return the async v2 job-local working directory for source files."""

        return (
            Path(getattr(self.settings, "workdir", tempfile.gettempdir()))
            / "async_pipeline_v2"
            / job_id
            / Path(*parts)
        )

    async def resolve_source_video(
        self,
        artifact: AsyncV2Artifact,
        *,
        max_bytes: Optional[int] = None,
    ) -> Path:
        """Resolve a source-video artifact to a readable local file.

        Workers may see artifacts that are already local, or artifacts that only
        carry a durable URL because another container staged them. This method
        preserves the existing resolution order: prefer local_path when it still
        exists, otherwise download the artifact URL into the job input workdir.
        ``max_bytes`` bounds the download for guardrailed sources; exceeding it
        raises the too-large input violation without further retries.
        """

        local_path = Path(artifact.local_path).expanduser() if artifact.local_path else None
        if local_path and local_path.exists():
            return local_path.resolve()

        destination_dir = self.workdir(artifact.job_id, "input")
        filename = (
            artifact.metadata_json.get("source_filename")
            or Path(artifact.blob_name or "source_video.mp4").name
        )
        last_exc: Optional[Exception] = None
        for attempt in range(1, SOURCE_DOWNLOAD_MAX_ATTEMPTS + 1):
            # Re-mint a FRESH SAS every attempt (parity with the multi-image
            # input path): the stored artifact URL may have expired between
            # staging and lease, and a stale signature must not be reused on
            # retry. Fall back to the durable staged URL when re-signing is
            # unavailable.
            url = None
            if (
                self.storage is not None
                and artifact.container
                and artifact.blob_name
                and hasattr(self.storage, "generate_sas_url")
            ):
                url = self.storage.generate_sas_url(
                    container=artifact.container,
                    blob_name=artifact.blob_name,
                    require_signed=True,
                )
            if not url:
                url = artifact.url or artifact.metadata_json.get("source_url")
            if not url:
                raise FileNotFoundError(
                    f"Source video artifact {artifact.artifact_id} has no readable local_path or durable URL."
                )
            destination_dir.mkdir(parents=True, exist_ok=True)
            try:
                result = await download_public_file_to_disk(
                    url=str(url),
                    destination=destination_dir / str(filename),
                    asset_label="video",
                    max_bytes=max_bytes,
                )
                if attempt > 1:
                    self.logger.info(
                        "resolve_source_video artifact=%s recovered on attempt %d/%d",
                        artifact.artifact_id, attempt, SOURCE_DOWNLOAD_MAX_ATTEMPTS,
                    )
                return result
            except EdennValidationError:
                # Input-guardrail violations (e.g. too-large abort) are permanent:
                # retrying would re-download the same oversized source.
                raise
            except Exception as exc:
                last_exc = exc
                self.logger.warning(
                    "resolve_source_video download attempt %d/%d failed artifact=%s: %s",
                    attempt, SOURCE_DOWNLOAD_MAX_ATTEMPTS, artifact.artifact_id, exc,
                )
                if attempt < SOURCE_DOWNLOAD_MAX_ATTEMPTS:
                    await asyncio.sleep(SOURCE_DOWNLOAD_RETRY_BACKOFF_S * attempt)
        assert last_exc is not None
        raise last_exc

    async def prepare_for_workflow(
        self,
        *,
        job_id: str,
        request: dict[str, Any],
        source_artifact: AsyncV2Artifact,
        source_video_path: Optional[Path] = None,
    ) -> PreparedSourceVideo:
        """Return the source video artifact/path that downstream stages should use.

        Compression is ON unless the request explicitly opts out with
        `compression_flag: false`. Opting out only skips downscaling — the
        H.264 delivery guarantee is unconditional: any source whose video
        stream is not already h264 (e.g. iPhone HEVC) is re-encoded to H.264
        at original resolution regardless of the flag. Already-H.264 sources
        within the height limit pass through untouched. When a re-encode
        happens, the method records a deterministic
        `source_video:compressed_input` artifact and returns that artifact on the
        first attempt and every retry. This idempotent artifact boundary is what
        lets later split workers avoid repeating CPU-heavy preparation.
        """

        original_path = source_video_path or await self.resolve_source_video(source_artifact)
        requested = coerce_request_bool(request.get("compression_flag", True))
        max_height = int(request.get("compression_max_height") or 1280)
        compression_info: dict[str, Any] = {
            "requested": requested,
            "applied": False,
            "max_height": max_height,
            "source_artifact_id": source_artifact.artifact_id,
        }
        compressed_artifact_id = f"{job_id}:source_video:compressed_input"
        existing = self.repository.get_artifact(compressed_artifact_id)
        if existing is not None:
            compressed_path = await self.resolve_source_video(existing)
            existing_info = existing.metadata_json.get("compression")
            if isinstance(existing_info, dict):
                compression_info.update(existing_info)
            compression_info.update(
                {
                    "requested": requested,
                    "applied": bool(existing.metadata_json.get("compression_applied", True)),
                    "reused_existing_artifact": True,
                    "compressed_artifact_id": existing.artifact_id,
                }
            )
            return PreparedSourceVideo(
                path=compressed_path,
                artifact=existing,
                compression_info=compression_info,
            )

        started = time.perf_counter()
        # ffmpeg re-encode is CPU-heavy; run it off the event loop so the worker's
        # lease heartbeat keeps beating and the reaper does not requeue this task.
        if requested:
            if max_height <= 0:
                raise ValueError("compression_max_height must be greater than 0.")
            compressed_path = original_path.with_name(
                f"{original_path.stem}_{max_height}h.mp4")
            effective_path = await asyncio.to_thread(
                compress_video_to_max_height,
                video_path=original_path,
                output_path=compressed_path,
                max_height=max_height,
                repair_decode_errors=True,
                return_original_on_failure=True,
                validate_reencode=True,
            )
        else:
            # Opting out of compression only skips downscaling: delivered videos
            # must always carry an H.264 stream, so a non-H.264 source is still
            # re-encoded, at its original resolution.
            effective_path = await asyncio.to_thread(
                ensure_h264_video,
                video_path=original_path,
                output_path=original_path.with_name(
                    f"{original_path.stem}_h264.mp4"),
                return_original_on_failure=True,
                validate_reencode=True,
            )
        duration_s = round(time.perf_counter() - started, 3)
        compression_applied = effective_path.resolve() != original_path.resolve()
        compression_info.update(
            {
                "applied": compression_applied,
                "duration_s": duration_s,
                "source_size_bytes": original_path.stat().st_size
                if original_path.exists()
                else None,
                "output_size_bytes": effective_path.stat().st_size
                if effective_path.exists()
                else None,
            }
        )
        if not compression_applied:
            return PreparedSourceVideo(
                path=original_path,
                artifact=source_artifact,
                compression_info=compression_info,
            )

        source_metadata = source_artifact.metadata_json.get("video_metadata")
        if not isinstance(source_metadata, dict):
            source_metadata = _inspect_public_video_metadata(original_path, logger=self.logger)
        output_metadata = _inspect_public_video_metadata(effective_path, logger=self.logger)

        container = getattr(self.settings, "upload_container", "user-uploads")
        upload_blob = None
        upload_url = None
        if self.storage is not None and getattr(self.storage, "enabled", False):
            upload_blob = self.storage.upload_path(
                container=container,
                path=effective_path,
                blob_name=_provider_neutral_blob_name(
                    job_id=job_id,
                    folder="input/compressed",
                    label="source_video",
                    source_path=effective_path,
                ),
                content_type=_guess_input_video_content_type(
                    source_path=effective_path,
                    upload_content_type=source_artifact.content_type,
                    compression_applied=True,
                ),
            )
            if upload_blob and hasattr(self.storage, "generate_sas_url"):
                upload_url = self.storage.generate_sas_url(
                    container=container,
                    blob_name=upload_blob,
                )

        metadata_json = {
            "source_filename": effective_path.name,
            "size_bytes": effective_path.stat().st_size if effective_path.exists() else None,
            "uploaded": bool(upload_blob),
            "source_artifact_id": source_artifact.artifact_id,
            "compression_applied": True,
            "compression": compression_info,
            "source_video_metadata": source_metadata,
            "output_video_metadata": output_metadata,
        }
        compressed_artifact = self.repository.add_artifact(
            artifact_id=compressed_artifact_id,
            job_id=job_id,
            artifact_type="source_video",
            role="compressed_input",
            container=container if upload_blob else None,
            blob_name=upload_blob,
            url=upload_url,
            content_type="video/mp4",
            local_path=str(effective_path),
            metadata_json=metadata_json,
        )
        self.repository.add_event(
            job_id=job_id,
            event_type="artifact.created",
            stage_name=self.stage_name,
            message="compressed source_video artifact recorded.",
            payload_json={
                # NOTE: no signed "url" here — the /events endpoint replays event
                # payloads to clients, and a persisted SAS URL is a stale credential
                # that expires. blob_name is enough for internal debugging; clients
                # get fresh, re-signed URLs from the status endpoint.
                "artifact_id": compressed_artifact.artifact_id,
                "artifact_type": compressed_artifact.artifact_type,
                "role": compressed_artifact.role,
                "blob_name": upload_blob,
                "compression": compression_info,
            },
        )
        compression_info["compressed_artifact_id"] = compressed_artifact.artifact_id
        return PreparedSourceVideo(
            path=effective_path,
            artifact=compressed_artifact,
            compression_info=compression_info,
        )


__all__ = [
    "PreparedSourceVideo",
    "VideoSourcePreparationService",
    "coerce_request_bool",
]
