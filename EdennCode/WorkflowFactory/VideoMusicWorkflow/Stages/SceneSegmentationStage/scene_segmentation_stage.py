from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import math
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Tuple, Optional, Dict
from uuid import uuid4

import cv2
import numpy as np

from EdennCode.Util.MediaUtils import detect_scene_cuts, extract_frame
from EdennCode.Util.blob_paths import safe_blob_path_component
from EdennCode.ModelFactory.LanguageModelFactory import AzureMultimodalClient
from EdennCode.ModelFactory.PromptFactory.prompts import PromptBuilder, ResponseSchemas
from EdennCode.WorkflowFactory.VideoMusicWorkflow.PipelineDataModel.pipeline_datamodel import Language
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.datamodel import SceneUnderstanding
from EdennCode.Annotation.core.annotation_dispatcher import (
    AnnotationDispatcher,
    safe_emit_annotation,
)
from EdennCode.Annotation.events.scene_understanding_event import SceneUnderstandingEvent
from EdennCode.exceptions import (
    EdennConfigurationError,
    EdennContentPolicyViolationError,
    EdennProviderImageFetchTimeoutError,
    EdennStorageError,
)

logger = logging.getLogger(__name__)


def _env_positive_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        return max(1, int(raw_value))
    except ValueError:
        logger.warning("Invalid %s=%r; using %d", name, raw_value, default)
        return default


def _env_bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        return min(maximum, max(minimum, int(raw_value)))
    except ValueError:
        logger.warning("Invalid %s=%r; using %d", name, raw_value, default)
        return default


def _env_scene_image_transport() -> str:
    raw_value = os.getenv("SCENE_LLM_IMAGE_TRANSPORT", "inline").strip().lower()
    if raw_value in {"inline", "data_url", "data-url", "payload"}:
        return "inline"
    if raw_value in {"blob", "url", "sas"}:
        return "blob"
    logger.warning("Invalid SCENE_LLM_IMAGE_TRANSPORT=%r; using inline", raw_value)
    return "inline"


def _prepare_llm_frame_jpeg_bytes(frame_bytes: bytes) -> bytes:
    max_dimension = _env_positive_int("SCENE_LLM_FRAME_MAX_DIMENSION", 1024)
    jpeg_quality = _env_bounded_int("SCENE_LLM_FRAME_JPEG_QUALITY", 80, 1, 100)

    data = np.frombuffer(frame_bytes, dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        return frame_bytes

    height, width = image.shape[:2]
    longest_edge = max(width, height)
    if longest_edge > max_dimension:
        scale = float(max_dimension) / float(longest_edge)
        resized_width = max(1, int(round(width * scale)))
        resized_height = max(1, int(round(height * scale)))
        image = cv2.resize(
            image,
            (resized_width, resized_height),
            interpolation=cv2.INTER_AREA,
        )

    success, encoded = cv2.imencode(
        ".jpg",
        image,
        [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality],
    )
    if not success:
        return frame_bytes
    return encoded.tobytes()


def _extract_frame_jpeg_bytes_with_ffmpeg(
    video_path: Path,
    timestamp: float,
    *,
    duration: Optional[float] = None,
    fps: Optional[float] = None,
) -> bytes:
    with tempfile.TemporaryDirectory(prefix="scene-frame-") as tmp_dir:
        output_path = Path(tmp_dir) / "frame.jpg"
        extract_frame(
            video_path,
            timestamp,
            output_path,
            duration=duration,
            fps=fps,
        )
        frame_bytes = output_path.read_bytes()
    if not frame_bytes:
        raise RuntimeError(f"ffmpeg extracted an empty frame at {timestamp:.3f}s")
    return frame_bytes


def extract_frame_jpeg_bytes(
    video_path: Path,
    timestamp: float,
    *,
    duration_hint: Optional[float] = None,
) -> bytes:
    """Extract a frame at the given timestamp and return encoded JPEG bytes.

    Uses time-based seeking with small backoff steps to avoid end-of-file decode
    errors that can occur near the final frame (common after VFR transcodes)."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        logger.warning(
            "OpenCV could not open %s for frame extraction at %.3fs; retrying with ffmpeg",
            video_path,
            timestamp,
        )
        try:
            return _extract_frame_jpeg_bytes_with_ffmpeg(
                video_path,
                timestamp,
                duration=duration_hint,
            )
        except Exception as exc:
            raise RuntimeError(f"Could not open video: {video_path}") from exc

    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        est_duration = 0.0
        if total_frames > 0 and fps > 0:
            est_duration = total_frames / fps
        duration = duration_hint or est_duration or max(timestamp, 0.0)

        # Clamp target time just inside the video duration.
        step = 1.0 / fps if fps > 0 else 0.04
        margin = max(step, 0.05)
        clamp_ts = min(timestamp, max(0.0, duration - margin))

        # Try the exact target, then a nearby whole-second frame, then small backoffs.
        floor_ts = math.floor(clamp_ts)
        candidates = [
            clamp_ts,
            float(floor_ts),
            max(0.0, clamp_ts - margin),
            max(0.0, clamp_ts - 0.25),
            max(0.0, clamp_ts - 0.5),
        ]
        seen = set()
        ok = False
        frame = None
        for ts in candidates:
            ts_key = round(ts, 6)
            if ts_key in seen:
                continue
            seen.add(ts_key)
            cap.set(cv2.CAP_PROP_POS_MSEC, ts * 1000.0)
            ok, frame = cap.read()
            if ok and frame is not None:
                break

        # Final fallback: last frame if frame count is known.
        if (not ok or frame is None) and total_frames > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, total_frames - 1)
            ok, frame = cap.read()

        if ok and frame is not None:
            ok, buf = cv2.imencode(".jpg", frame)
            if ok:
                return buf.tobytes()

        logger.warning(
            "OpenCV frame extraction failed at %.3fs for %s; retrying with ffmpeg",
            timestamp,
            video_path,
        )
        try:
            return _extract_frame_jpeg_bytes_with_ffmpeg(
                video_path,
                timestamp,
                duration=duration,
                fps=fps if fps > 0 else None,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to extract frame at {timestamp:.3f}s via OpenCV and ffmpeg fallback"
            ) from exc
    finally:
        cap.release()


def extract_frame_b64(
    video_path: Path,
    timestamp: float,
    *,
    duration_hint: Optional[float] = None,
) -> str:
    return base64.b64encode(
        extract_frame_jpeg_bytes(
            video_path,
            timestamp,
            duration_hint=duration_hint,
        )
    ).decode("utf-8")


@dataclass
class SceneSegmentationStageInput:
    video_path: Path
    duration: float
    fps: float
    preferred_language: str = Language.EN
    scene_threshold: Optional[float] = None
    min_scene_length: Optional[float] = None
    scene_detector: Optional[str] = None
    scene_detection_method: Optional[str] = None
    max_scenes: Optional[int] = None
    min_scene_count: Optional[int] = None
    fallback_strategy: Optional[str] = None
    threshold_sweep_steps: Optional[int] = None
    job_id: str = ""
    annotation_dispatcher: Optional[AnnotationDispatcher] = None


@dataclass
class SceneSegmentationStageOutput:
    scene_understanding_messages: List[SceneUnderstanding]
    token_usage: Optional[Dict[str, int]] = None
    thumbnail_path: Optional[Path] = None


@dataclass(frozen=True)
class _SceneLlmResult:
    start_timestamp: float
    end_timestamp: float
    content: Dict[str, Any]
    usage: Dict[str, int]
    thumbnail_b64: Optional[str]


class SceneSegmentationStage:
    """Simplified scene segmentation using scene detection with sensible fallbacks."""

    CONTENT_POLICY_FRAME_RETRY_MAX_ATTEMPTS = 3
    CONTENT_POLICY_FRAME_RETRY_STEP_S = 0.2

    def __init__(
            self,
            llm_model_client: AzureMultimodalClient,
            scene_threshold: float = 0.3,
            min_scene_length: float = 1.0,
            max_scenes: int = 30,
            scene_detector: str = "auto",
            scene_detection_method: str = "adaptive",
            storage_service: Any = None,
            llm_image_container: Optional[str] = None,
            llm_image_sas_ttl_minutes: int = 5,
            llm_image_cleanup_delay_seconds: float = 2.0,
            max_concurrent_llm_calls: Optional[int] = None,
            llm_image_transport: Optional[str] = None,
            threshold_sweep_steps: Optional[int] = None,
            **kwargs  # Accept and ignore legacy parameters for compatibility
    ) -> None:
        self.llm_model_client = llm_model_client
        self.scene_threshold = scene_threshold
        self.min_scene_length = min_scene_length
        self.max_scenes = max_scenes
        self.scene_detector = scene_detector
        self.scene_detection_method = scene_detection_method
        self.storage_service = storage_service
        self.llm_image_container = (llm_image_container or "").strip()
        self.llm_image_sas_ttl_minutes = max(1, int(llm_image_sas_ttl_minutes))
        self.llm_image_cleanup_delay_seconds = max(0.0, float(llm_image_cleanup_delay_seconds))
        self.llm_image_transport = self._normalize_llm_image_transport(
            llm_image_transport or _env_scene_image_transport()
        )
        self.max_concurrent_llm_calls = max(
            1,
            int(
                max_concurrent_llm_calls
                if max_concurrent_llm_calls is not None
                else _env_positive_int("SCENE_LLM_MAX_CONCURRENT_CALLS", 10)
            ),
        )
        self.threshold_sweep_steps = max(
            1,
            int(
                threshold_sweep_steps
                if threshold_sweep_steps is not None
                else _env_positive_int("SCENE_THRESHOLD_SWEEP_STEPS", 5)
            ),
        )

    async def run(self, stage_input: SceneSegmentationStageInput) -> SceneSegmentationStageOutput:
        """Run scene segmentation and understanding."""
        _stage_start = time.time()
        # Use stage input values if provided, otherwise fall back to defaults
        threshold = stage_input.scene_threshold or self.scene_threshold
        min_length = stage_input.min_scene_length or self.min_scene_length
        max_scenes = stage_input.max_scenes or self.max_scenes
        detector = (stage_input.scene_detector or self.scene_detector).lower()
        method = (
            stage_input.scene_detection_method or self.scene_detection_method).lower()
        threshold_sweep_steps = (
            stage_input.threshold_sweep_steps or self.threshold_sweep_steps
        )

        # Get scene windows
        windows = self._get_scene_windows(
            stage_input.video_path,
            stage_input.duration,
            threshold,
            min_length,
            max_scenes,
            detector,
            method,
            min_scene_count=stage_input.min_scene_count,
            threshold_sweep_steps=threshold_sweep_steps,
        )
        # Analyze scenes with LLM
        scene_understanding = await self._analyze_scenes(
            stage_input.video_path,
            windows,
            stage_input.duration,
            stage_input.fps,
            stage_input.preferred_language,
            max_concurrent_llm_calls=self.max_concurrent_llm_calls,
        )

        output = SceneSegmentationStageOutput(
            scene_understanding_messages=scene_understanding['scenes'],
            token_usage=scene_understanding['usage'],
            thumbnail_path=self._persist_thumbnail_from_b64(
                stage_input.video_path,
                scene_understanding.get("thumbnail_b64"),
            ),
        )
        safe_emit_annotation(
            stage_input.annotation_dispatcher,
            lambda: SceneUnderstandingEvent.from_scene_understandings(
                job_id=stage_input.job_id,
                scene_understandings=output.scene_understanding_messages,
                stage_latency_s=time.time() - _stage_start,
                token_usage=output.token_usage or {},
            ),
        )
        return output

    def _get_scene_windows(
            self,
            video_path: Path,
            duration: float,
            threshold: float,
            min_length: float,
            max_scenes: int,
            detector: str,
            method: str,
            *,
            min_scene_count: Optional[int] = None,
            threshold_sweep_steps: int = 1,
    ) -> List[Tuple[float, float]]:
        """Get scene windows using detection, with uniform fallback."""
        if duration <= min_length:
            return [(0.0, duration)]

        # Calculate target scene count (between 1 and max_scenes)
        safe_min_length = max(min_length, 0.001)
        target_max = max(1, min(max_scenes, math.ceil(duration / safe_min_length)))
        target_min = max(
            1,
            min_scene_count
            if min_scene_count is not None
            else math.ceil(duration / 10.0),
        )
        target_min = min(target_min, target_max)

        best_over_limit: Optional[List[Tuple[float, float]]] = None
        best_under_limit: Optional[List[Tuple[float, float]]] = None

        # Try scene detection. If the base threshold over-detects, increase the
        # threshold before falling back to downsampling so we keep stronger cuts.
        for threshold_value in self._threshold_sweep_values(
            threshold,
            threshold_sweep_steps,
            detector,
        ):
            try:
                cuts: List[float] = detect_scene_cuts(
                    video_path,
                    threshold_value,
                    detector=detector,
                    pyscene_method=method,
                    pyscene_adaptive_threshold=threshold_value if method == "adaptive" else None,
                    pyscene_content_threshold=threshold_value if method == "content" else None,
                    min_scene_len_s=min_length,
                )
                windows = self._cuts_to_windows(cuts, duration, min_length)
            except Exception:
                # If detection fails, use uniform windows below.
                windows = []
                break

            scene_count = len(windows)
            if target_min <= scene_count <= target_max:
                if threshold_value != threshold:
                    logger.info(
                        "Scene detection threshold adjusted from %.3f to %.3f "
                        "to keep %d scenes within target range %d-%d",
                        threshold,
                        threshold_value,
                        scene_count,
                        target_min,
                        target_max,
                    )
                return windows

            if scene_count > target_max:
                best_over_limit = windows
                continue

            best_under_limit = windows
            break

        if best_over_limit is not None:
            return self._downsample(best_over_limit, target_max)

        if best_under_limit is not None:
            return self._uniform_windows(duration, min_length, target_min)

        return self._uniform_windows(duration, min_length, target_min)

    @staticmethod
    def _threshold_sweep_values(
        threshold: float,
        threshold_sweep_steps: int,
        detector: str,
    ) -> List[float]:
        steps = max(1, int(threshold_sweep_steps))
        base_threshold = max(float(threshold), 0.001)

        if detector == "ffprobe":
            if steps == 1:
                return [min(base_threshold, 0.95)]
            max_threshold = 0.95
            if base_threshold >= max_threshold:
                return [max_threshold]
            step_size = (max_threshold - base_threshold) / float(steps - 1)
            values = [base_threshold + (step_size * idx) for idx in range(steps)]
        else:
            values = [base_threshold * (2.0 ** idx) for idx in range(steps)]

        deduped = []
        seen = set()
        for value in values:
            normalized = round(value, 6)
            if normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(value)
        return deduped

    @staticmethod
    def _cuts_to_windows(
            cuts: List[float],
            duration: float,
            min_length: float
    ) -> List[Tuple[float, float]]:
        """Convert cut timestamps to scene windows."""
        if not cuts:
            return [(0.0, duration)]

        # Add start and end boundaries
        boundaries = sorted(set([0.0] + cuts + [duration]))

        # Filter out scenes shorter than min_length
        windows = []
        for start, end in zip(boundaries[:-1], boundaries[1:]):
            if end - start >= min_length:
                windows.append((start, end))

        # Always return at least one window
        return windows if windows else [(0.0, duration)]

    @staticmethod
    def _uniform_windows(
            duration: float,
            min_length: float,
            target_count: int
    ) -> List[Tuple[float, float]]:
        """Create uniformly spaced scene windows."""
        # Respect minimum scene length
        max_possible = int(
            duration / min_length) if min_length > 0 else target_count
        count = min(max(1, target_count), max_possible)

        if count == 1:
            return [(0.0, duration)]

        segment = duration / count
        return [(i * segment, (i + 1) * segment if i < count - 1 else duration)
                for i in range(count)]

    @staticmethod
    def _downsample(windows: List[Tuple[float, float]], max_count: int) -> List[Tuple[float, float]]:
        """Downsample windows to max_count by selecting evenly spaced scenes."""
        if len(windows) <= max_count:
            return windows

        if max_count == 1:
            return [windows[len(windows) // 2]]

        # Select evenly spaced indices
        step = (len(windows) - 1) / (max_count - 1)
        indices = [min(int(round(i * step)), len(windows) - 1)
                   for i in range(max_count)]

        # Remove duplicates while preserving order
        seen = set()
        unique_indices = []
        for idx in indices:
            if idx not in seen:
                seen.add(idx)
                unique_indices.append(idx)

        return [windows[i] for i in unique_indices]

    async def _analyze_scenes(
            self,
            video_path: Path,
            windows: List[Tuple[float, float]],
            duration: float,
            fps: float,
            preferred_language: str = Language.EN,
            max_concurrent_llm_calls: int = 10,
    ) -> Dict:
        """Analyze scenes using LLM with parallel requests."""
        if self._uses_blob_image_transport():
            self._ensure_llm_image_storage()
        prompt_builder = PromptBuilder()
        semaphore = asyncio.Semaphore(max_concurrent_llm_calls)

        async def _throttled_scene(scene_idx: int, start: float, end: float):
            async with semaphore:
                return await self._analyze_single_scene(
                    prompt_builder=prompt_builder,
                    video_path=video_path,
                    scene_idx=scene_idx,
                    start=start,
                    end=end,
                    duration=duration,
                    fps=fps,
                    preferred_language=preferred_language,
                )

        llm_tasks = [
            _throttled_scene(scene_idx, start, end)
            for scene_idx, (start, end) in enumerate(windows)
        ]

        gathered_results = await asyncio.gather(*llm_tasks, return_exceptions=True)
        results: List[Tuple[int, _SceneLlmResult]] = []
        scene_errors: List[Tuple[int, Exception]] = []
        skipped_scene_count = 0

        for scene_idx, result in enumerate(gathered_results):
            if isinstance(result, Exception):
                scene_errors.append((scene_idx, result))
                logger.warning(
                    "Skipping failed scene %d for %s: %s",
                    scene_idx,
                    video_path,
                    result,
                )
                continue
            if result is None:
                skipped_scene_count += 1
                continue
            results.append((scene_idx, result))

        if skipped_scene_count:
            logger.warning(
                "Skipped %d scene(s) during frame extraction fallback for %s",
                skipped_scene_count,
                video_path,
            )
        if scene_errors and not results:
            raise scene_errors[0][1]

        # Aggregate results
        scenes = []
        total_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

        for scene_idx, result in results:
            scenes.append(SceneUnderstanding(
                scene_index=scene_idx,
                start_timestamp=result.start_timestamp,
                end_timestamp=result.end_timestamp,
                visual_summary=result.content['visual_summary'],
                key_actions=result.content['key_actions'],
                mood=result.content['mood'],
            ))

            for key in total_usage:
                total_usage[key] += result.usage.get(key, 0)

        return {
            'scenes': scenes,
            'usage': total_usage,
            'thumbnail_b64': self._pick_first_non_black_thumbnail_b64(
                [(result.start_timestamp, result.thumbnail_b64 or "") for _, result in results]
            ),
        }

    def _ensure_llm_image_storage(self) -> None:
        if self.storage_service is None or not getattr(self.storage_service, "enabled", False):
            raise EdennConfigurationError(
                "Scene understanding blob image transport requires Azure Blob Storage for temporary LLM image URLs.",
                public_message="The service is temporarily unavailable. Please try again later.",
                component="scene_segmentation",
                operation="llm_image_upload",
                context={"requires_storage": True},
            )
        if not self.llm_image_container:
            raise EdennConfigurationError(
                "AZURE_STORAGE_LLM_IMAGE_CONTAINER must be configured for temporary LLM image uploads.",
                component="scene_segmentation",
                operation="llm_image_upload",
            )

    async def _analyze_single_scene(
        self,
        *,
        prompt_builder: PromptBuilder,
        video_path: Path,
        scene_idx: int,
        start: float,
        end: float,
        duration: float,
        fps: float,
        preferred_language: str = Language.EN,
    ) -> Optional[_SceneLlmResult]:
        safe_start = self._clamp_timestamp(start, duration, fps)
        safe_end = self._clamp_timestamp(end, duration, fps)
        if safe_end < safe_start:
            safe_end = safe_start

        candidate_attempts = self._build_content_policy_retry_attempts(
            start_timestamp=safe_start,
            end_timestamp=safe_end,
            duration=duration,
            fps=fps,
        )
        last_content_policy_error: Optional[Exception] = None

        for attempt_idx, (first_timestamp, last_timestamp) in enumerate(candidate_attempts):
            first_frame = self._try_extract_scene_frame(
                video_path=video_path,
                scene_idx=scene_idx,
                frame_label="first",
                timestamp=first_timestamp,
                duration=duration,
            )
            last_frame = self._try_extract_scene_frame(
                video_path=video_path,
                scene_idx=scene_idx,
                frame_label="last",
                timestamp=last_timestamp,
                duration=duration,
            )
            if first_frame is None and last_frame is None:
                if attempt_idx == 0:
                    logger.warning(
                        "Skipping scene %d (%.3fs-%.3fs): could not extract either representative frame",
                        scene_idx,
                        start,
                        end,
                    )
                    return None
                continue

            thumbnail_b64 = first_frame[1] if first_frame is not None else last_frame[1]
            temporary_blobs: List[Any] = []

            try:
                first_frame_blob = None
                last_frame_blob = None
                initial_inline_labels = set()
                if self._uses_blob_image_transport():
                    if first_frame is not None:
                        first_frame_blob = await self._upload_scene_frame(
                            video_path=video_path,
                            scene_idx=scene_idx,
                            frame_label="first",
                            frame_bytes=first_frame[0],
                        )
                        temporary_blobs.append(first_frame_blob)

                    if last_frame is not None:
                        last_frame_blob = await self._upload_scene_frame(
                            video_path=video_path,
                            scene_idx=scene_idx,
                            frame_label="last",
                            frame_bytes=last_frame[0],
                        )
                        temporary_blobs.append(last_frame_blob)
                else:
                    if first_frame is not None:
                        initial_inline_labels.add("first")
                    if last_frame is not None:
                        initial_inline_labels.add("last")

                completion = await self._complete_scene_understanding_with_image_fetch_fallback(
                    prompt_builder=prompt_builder,
                    scene_idx=scene_idx,
                    start=start,
                    end=end,
                    preferred_language=preferred_language,
                    first_frame_url=(
                        first_frame_blob.sas_url if first_frame_blob is not None else None
                    ),
                    first_frame_b64=first_frame[1] if first_frame is not None else None,
                    last_frame_url=(
                        last_frame_blob.sas_url if last_frame_blob is not None else None
                    ),
                    last_frame_b64=last_frame[1] if last_frame is not None else None,
                    initial_inline_labels=initial_inline_labels,
                )
                if completion is None:
                    return None
                content, usage = completion
                return _SceneLlmResult(
                    start_timestamp=start,
                    end_timestamp=end,
                    content=content,
                    usage=usage,
                    thumbnail_b64=thumbnail_b64,
                )
            except Exception as exc:
                if not self._is_content_policy_error(exc):
                    raise
                last_content_policy_error = exc
                if attempt_idx >= len(candidate_attempts) - 1:
                    raise

                next_first_timestamp, next_last_timestamp = candidate_attempts[attempt_idx + 1]
                logger.warning(
                    "Scene %d representative frame(s) at %.3fs/%.3fs triggered content policy; "
                    "retrying neighboring frames at %.3fs/%.3fs (%d/%d)",
                    scene_idx,
                    first_timestamp,
                    last_timestamp,
                    next_first_timestamp,
                    next_last_timestamp,
                    attempt_idx + 1,
                    len(candidate_attempts) - 1,
                )
            finally:
                await self._cleanup_temporary_blobs(temporary_blobs)

        if last_content_policy_error is not None:
            raise last_content_policy_error
        return None

    @staticmethod
    def _try_extract_scene_frame(
        *,
        video_path: Path,
        scene_idx: int,
        frame_label: str,
        timestamp: float,
        duration: float,
    ) -> Optional[Tuple[bytes, str]]:
        try:
            frame_bytes = extract_frame_jpeg_bytes(
                video_path,
                timestamp,
                duration_hint=duration,
            )
        except RuntimeError as exc:
            logger.warning(
                "Scene %d %s frame extraction failed at %.3fs for %s: %s",
                scene_idx,
                frame_label,
                timestamp,
                video_path,
                exc,
            )
            return None
        frame_bytes = _prepare_llm_frame_jpeg_bytes(frame_bytes)
        return frame_bytes, base64.b64encode(frame_bytes).decode("utf-8")

    async def _complete_scene_understanding_with_image_fetch_fallback(
        self,
        *,
        prompt_builder: PromptBuilder,
        scene_idx: int,
        start: float,
        end: float,
        preferred_language: str,
        first_frame_url: Optional[str],
        first_frame_b64: Optional[str],
        last_frame_url: Optional[str],
        last_frame_b64: Optional[str],
        initial_inline_labels: Optional[set[str]] = None,
    ) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
        frame_urls = {
            "first": first_frame_url,
            "last": last_frame_url,
        }
        frame_b64s = {
            "first": first_frame_b64,
            "last": last_frame_b64,
        }
        initial_inline_labels = initial_inline_labels or set()
        active_labels = [
            label for label in frame_urls
            if frame_b64s.get(label) and (
                frame_urls.get(label) or label in initial_inline_labels
            )
        ]
        inline_labels = set(initial_inline_labels)
        dropped_labels = set()
        last_fetch_error: Optional[EdennProviderImageFetchTimeoutError] = None

        max_attempts = 1 + (len(active_labels) * 2)
        for _ in range(max_attempts):
            current_urls = {
                label: self._frame_prompt_url(
                    frame_url=frame_urls[label],
                    frame_b64=frame_b64s[label],
                    use_inline=label in inline_labels,
                )
                for label in active_labels
                if label not in dropped_labels
            }
            if not current_urls:
                if last_fetch_error is not None:
                    logger.warning(
                        "Scene %d exhausted image-fetch fallback after Azure/ModelGateway image read failure: %s",
                        scene_idx,
                        last_fetch_error,
                    )
                    raise last_fetch_error
                logger.warning(
                    "Skipping scene %d (%.3fs-%.3fs): Azure/ModelGateway could not read any representative frame",
                    scene_idx,
                    start,
                    end,
                )
                return None

            try:
                return await self._complete_scene_understanding(
                    prompt_builder=prompt_builder,
                    scene_idx=scene_idx,
                    start=start,
                    end=end,
                    preferred_language=preferred_language,
                    first_frame_url=current_urls.get("first"),
                    last_frame_url=current_urls.get("last"),
                )
            except Exception as exc:
                normalized_fetch_error = self._normalize_image_fetch_timeout_error(exc)
                if normalized_fetch_error is not None:
                    last_fetch_error = normalized_fetch_error
                    logger.warning(
                        "Scene %d Azure/ModelGateway image fetch failed: %s",
                        scene_idx,
                        normalized_fetch_error,
                    )
                    failed_label = self._match_failed_frame_label(
                        failed_image_url=normalized_fetch_error.failed_image_url,
                        current_frame_urls=current_urls,
                    )
                    if failed_label is None:
                        url_backed_labels = [
                            label for label in current_urls
                            if label not in inline_labels
                        ]
                        if not url_backed_labels:
                            raise normalized_fetch_error
                        inline_labels.update(url_backed_labels)
                        logger.warning(
                            "Scene %d Azure/ModelGateway timed out downloading an unrecognized frame URL; "
                            "retrying %d URL-backed frame(s) inline",
                            scene_idx,
                            len(url_backed_labels),
                        )
                        continue

                    if failed_label not in inline_labels:
                        inline_labels.add(failed_label)
                        logger.warning(
                            "Scene %d Azure/ModelGateway timed out downloading %s frame URL; retrying that frame inline",
                            scene_idx,
                            failed_label,
                        )
                        continue

                    dropped_labels.add(failed_label)
                    logger.warning(
                        "Scene %d Azure/ModelGateway could not read %s frame after inline retry; dropping that frame",
                        scene_idx,
                        failed_label,
                    )
                    continue

                if self._is_content_policy_error(exc):
                    raise

                inline_current_labels = [
                    label for label in current_urls
                    if label in inline_labels and label not in dropped_labels
                ]
                if not inline_current_labels:
                    raise

                dropped_labels.update(inline_current_labels)
                logger.warning(
                    "Scene %d inline frame retry failed with %s; dropping inline frame(s): %s",
                    scene_idx,
                    type(exc).__name__,
                    ", ".join(inline_current_labels),
                )

        if last_fetch_error is not None:
            raise last_fetch_error
        return None

    def _normalize_image_fetch_timeout_error(
        self,
        exc: Exception,
    ) -> Optional[EdennProviderImageFetchTimeoutError]:
        if isinstance(exc, EdennProviderImageFetchTimeoutError):
            return exc

        azure_model = getattr(self.llm_model_client, "azure_model", None)
        if not isinstance(azure_model, str) or not azure_model.strip():
            return None

        mapped_error = AzureMultimodalClient.map_bad_request_like_error(
            exc,
            operation="chat.completions.create",
            azure_model=azure_model,
        )
        if isinstance(mapped_error, EdennProviderImageFetchTimeoutError):
            return mapped_error
        return None

    async def _complete_scene_understanding(
        self,
        *,
        prompt_builder: PromptBuilder,
        scene_idx: int,
        start: float,
        end: float,
        preferred_language: str,
        first_frame_url: Optional[str],
        last_frame_url: Optional[str],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        prompt = prompt_builder.build_scene_understanding_messages(
            first_frame_url=first_frame_url,
            last_frame_url=last_frame_url,
            scene_index=scene_idx,
            start_time=start,
            end_time=end,
            language=preferred_language,
        )
        return await self.llm_model_client.complete_messages(
            prompt,
            json_schema=ResponseSchemas().scene_understanding(),
        )

    @staticmethod
    def _normalize_llm_image_transport(value: str) -> str:
        normalized = (value or "").strip().lower()
        if normalized in {"inline", "data_url", "data-url", "payload"}:
            return "inline"
        if normalized in {"blob", "url", "sas"}:
            return "blob"
        raise ValueError("llm_image_transport must be 'inline' or 'blob'.")

    def _uses_blob_image_transport(self) -> bool:
        return self.llm_image_transport == "blob"

    @staticmethod
    def _frame_prompt_url(
        *,
        frame_url: Optional[str],
        frame_b64: Optional[str],
        use_inline: bool,
    ) -> Optional[str]:
        if use_inline:
            return f"data:image/jpeg;base64,{frame_b64}" if frame_b64 else None
        return frame_url

    @classmethod
    def _match_failed_frame_label(
        cls,
        *,
        failed_image_url: Optional[str],
        current_frame_urls: Dict[str, str],
    ) -> Optional[str]:
        normalized_failed_url = cls._normalize_image_url_for_match(failed_image_url)
        if not normalized_failed_url:
            return None

        for label, url in current_frame_urls.items():
            if normalized_failed_url == cls._normalize_image_url_for_match(url):
                return label
        return None

    @staticmethod
    def _normalize_image_url_for_match(url: Optional[str]) -> str:
        if not url:
            return ""
        return url.strip().rstrip(".").partition("?")[0]

    async def _upload_scene_frame(
        self,
        *,
        video_path: Path,
        scene_idx: int,
        frame_label: str,
        frame_bytes: bytes,
    ) -> Any:
        safe_video_name = safe_blob_path_component(
            video_path.stem,
            fallback_prefix="video",
        )
        blob_name = (
            f"llm-inputs/{safe_video_name}/scene_{scene_idx:03d}_{frame_label}_{uuid4().hex}.jpg"
        )
        uploaded_blob = await asyncio.to_thread(
            self.storage_service.upload_temporary_bytes,
            container=self.llm_image_container,
            blob_name=blob_name,
            data=frame_bytes,
            content_type="image/jpeg",
            ttl_minutes=self.llm_image_sas_ttl_minutes,
        )
        if uploaded_blob is None:
            raise EdennStorageError(
                f"Failed to upload temporary scene frame for scene {scene_idx}.",
                component="scene_segmentation",
                operation="upload_temporary_frame",
                context={
                    "scene_index": scene_idx,
                    "frame_label": frame_label,
                    "container": self.llm_image_container,
                },
            )
        return uploaded_blob

    async def _cleanup_temporary_blobs(self, temporary_blobs: List[Any]) -> None:
        if not temporary_blobs or self.storage_service is None:
            return
        if self.llm_image_cleanup_delay_seconds > 0:
            await asyncio.sleep(self.llm_image_cleanup_delay_seconds)
        for blob in temporary_blobs:
            try:
                await asyncio.to_thread(
                    self.storage_service.delete_blob,
                    container=blob.container,
                    blob_name=blob.blob_name,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to delete temporary LLM image blob %s/%s: %s",
                    getattr(blob, "container", ""),
                    getattr(blob, "blob_name", ""),
                    exc,
                )

    @staticmethod
    def _clamp_timestamp(timestamp: float, duration: float, fps: float) -> float:
        if duration <= 0:
            return max(0.0, timestamp)
        step = 1.0 / fps if fps > 0 else 0.04
        max_ts = max(0.0, duration - step)
        return max(0.0, min(timestamp, max_ts))

    @classmethod
    def _clamp_scene_timestamp(
        cls,
        timestamp: float,
        *,
        scene_start: float,
        scene_end: float,
        duration: float,
        fps: float,
    ) -> float:
        clamped = cls._clamp_timestamp(timestamp, duration, fps)
        lower = min(scene_start, scene_end)
        upper = max(scene_start, scene_end)
        return min(max(clamped, lower), upper)

    @classmethod
    def _content_policy_retry_step_s(cls, fps: float) -> float:
        if fps > 0:
            return max(cls.CONTENT_POLICY_FRAME_RETRY_STEP_S, 6.0 / fps)
        return cls.CONTENT_POLICY_FRAME_RETRY_STEP_S

    @classmethod
    def _build_content_policy_retry_attempts(
        cls,
        *,
        start_timestamp: float,
        end_timestamp: float,
        duration: float,
        fps: float,
    ) -> List[Tuple[float, float]]:
        step = cls._content_policy_retry_step_s(fps)
        attempts = [(start_timestamp, end_timestamp)]

        for retry_idx in range(1, cls.CONTENT_POLICY_FRAME_RETRY_MAX_ATTEMPTS + 1):
            if retry_idx == 1:
                attempts.append(
                    (
                        cls._clamp_scene_timestamp(
                            start_timestamp + step,
                            scene_start=start_timestamp,
                            scene_end=end_timestamp,
                            duration=duration,
                            fps=fps,
                        ),
                        end_timestamp,
                    )
                )
            elif retry_idx == 2:
                attempts.append(
                    (
                        start_timestamp,
                        cls._clamp_scene_timestamp(
                            end_timestamp - step,
                            scene_start=start_timestamp,
                            scene_end=end_timestamp,
                            duration=duration,
                            fps=fps,
                        ),
                    )
                )
            else:
                attempts.append(
                    (
                        cls._clamp_scene_timestamp(
                            start_timestamp + (step * 2.0),
                            scene_start=start_timestamp,
                            scene_end=end_timestamp,
                            duration=duration,
                            fps=fps,
                        ),
                        cls._clamp_scene_timestamp(
                            end_timestamp - (step * 2.0),
                            scene_start=start_timestamp,
                            scene_end=end_timestamp,
                            duration=duration,
                            fps=fps,
                        ),
                    )
                )

        deduped_attempts: List[Tuple[float, float]] = []
        seen = set()
        for first_timestamp, last_timestamp in attempts:
            key = (round(first_timestamp, 6), round(last_timestamp, 6))
            if key in seen:
                continue
            seen.add(key)
            deduped_attempts.append((first_timestamp, last_timestamp))
        return deduped_attempts

    @staticmethod
    def _is_content_policy_error(exc: BaseException) -> bool:
        if isinstance(exc, EdennContentPolicyViolationError):
            return True

        provider_error_code = str(getattr(exc, "provider_error_code", "") or "").strip()
        policy_code = str(getattr(exc, "policy_code", "") or "").strip()
        if provider_error_code in {"content_filter", "content_policy_violation"}:
            return True
        if policy_code in {"ResponsibleAIPolicyViolation", "content_policy_violation"}:
            return True

        body = getattr(exc, "body", None)
        payloads: List[Dict[str, Any]] = []
        if isinstance(body, dict):
            payloads.append(body)
            nested_error = body.get("error")
            if isinstance(nested_error, dict):
                payloads.append(nested_error)

        for payload in payloads:
            code = str(payload.get("code", "") or "").strip()
            message = str(payload.get("message", "") or "").lower()
            if code in {"content_filter", "content_policy_violation"}:
                return True
            if "content policy" in message or "content safety" in message:
                return True

        return False

    @staticmethod
    def _frame_is_non_black_content(frame) -> bool:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mean, std = cv2.meanStdDev(gray)
        return float(mean[0][0]) > 16.0 and float(std[0][0]) > 8.0

    @classmethod
    def _pick_first_non_black_thumbnail_b64(cls, frame_candidates: List[Tuple[float, str]]) -> Optional[str]:
        if not frame_candidates:
            return None
        sorted_candidates = sorted(frame_candidates, key=lambda item: item[0])
        fallback_b64: Optional[str] = None
        for _, frame_b64 in sorted_candidates:
            if not frame_b64:
                continue
            if fallback_b64 is None:
                fallback_b64 = frame_b64
            try:
                raw = base64.b64decode(frame_b64)
                buf = np.frombuffer(raw, dtype=np.uint8)
                frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
                if frame is None:
                    continue
                if cls._frame_is_non_black_content(frame):
                    return frame_b64
            except (ValueError, TypeError, binascii.Error):
                continue
        return fallback_b64

    @staticmethod
    def _persist_thumbnail_from_b64(video_path: Path, thumbnail_b64: Optional[str]) -> Optional[Path]:
        if not thumbnail_b64:
            return None
        try:
            raw = base64.b64decode(thumbnail_b64)
        except (ValueError, TypeError, binascii.Error):
            return None

        frame = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return None

        ok, encoded = cv2.imencode(".webp", frame)
        if not ok:
            return None

        thumbnail_path = video_path.parent / f"{video_path.stem}_thumbnail.webp"
        try:
            thumbnail_path.write_bytes(encoded.tobytes())
            return thumbnail_path
        except OSError:
            return None
