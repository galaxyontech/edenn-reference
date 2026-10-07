import time
from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Final, Optional

from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata import VideoMetadata
from EdennCode.Util.MediaUtils import detect_audio_activity
from EdennCode.Annotation.core.annotation_dispatcher import (
    AnnotationDispatcher,
    safe_emit_annotation,
)
from EdennCode.Annotation.events.video_feature_event import VideoFeatureEvent
from EdennCode.exceptions import EdennValidationError

logger = logging.getLogger(__name__)
MAX_INPUT_VIDEO_DURATION_S: Final[float] = 300.0


class InputMediaAssetTypes:
    VIDEO: Final[str] = "video"
    IMAGE: Final[str] = "image"


# Preserve the misspelled name for compatibility with existing imports.
InputMediaAssetTyps = InputMediaAssetTypes


@dataclass
class PreprocessStageInput:
    asset_path: str | Path
    asset_type: str
    job_id: str = ""
    annotation_dispatcher: Optional[AnnotationDispatcher] = None


@dataclass
class PreprocessStageOutput:
    video_metadata: VideoMetadata


def _resolve_local_asset_path(asset_path: str | Path) -> Path:
    if isinstance(asset_path, Path):
        resolved = asset_path.expanduser().resolve()
    elif isinstance(asset_path, str):
        resolved = Path(asset_path).expanduser().resolve()
    else:
        raise TypeError(f"asset_path must be str or Path, got {type(asset_path).__name__}")

    if not resolved.exists():
        raise FileNotFoundError(resolved)
    return resolved


def _normalize_asset_type(asset_type: str) -> str:
    return str(asset_type or "").strip().lower()


def validate_input_video_duration(
    duration_s: float,
    *,
    source_path: Path | None = None,
) -> None:
    duration = float(duration_s or 0.0)
    if duration <= MAX_INPUT_VIDEO_DURATION_S:
        return

    context = {
        "duration_s": duration,
        "max_duration_s": MAX_INPUT_VIDEO_DURATION_S,
    }
    if source_path is not None:
        context["source_path"] = str(source_path)
    raise EdennValidationError(
        (
            f"Input video is too long: {duration:.2f}s exceeds "
            f"{MAX_INPUT_VIDEO_DURATION_S:.0f}s."
        ),
        public_message=(
            f"Input video must be {MAX_INPUT_VIDEO_DURATION_S:.0f} seconds or shorter."
        ),
        component="video_preprocess",
        operation="validate_input_video_duration",
        context=context,
    )


class PreprocessStage:
    async def run(self, preprocess_stage_input: PreprocessStageInput) -> PreprocessStageOutput:
        """
        Extract metadata for the given asset. Currently, supports video assets.
        """
        _stage_start = time.time()
        resolved = _resolve_local_asset_path(preprocess_stage_input.asset_path)
        asset_type = _normalize_asset_type(preprocess_stage_input.asset_type)

        if asset_type == InputMediaAssetTypes.VIDEO:
            meta = VideoMetadata.from_file(resolved)
            validate_input_video_duration(meta.duration, source_path=meta.path)
            if meta.has_audio:
                meta.audio_activity = detect_audio_activity(
                    meta.path,
                    duration_hint=meta.duration,
                )
                if meta.audio_activity:
                    total_active = sum(e - s for s, e in meta.audio_activity)
                    logger.info(
                        "Detected %d audio segments (%.2fs total) in %s",
                        len(meta.audio_activity),
                        total_active,
                        meta.path,
                    )
            output = PreprocessStageOutput(video_metadata=meta)
            safe_emit_annotation(
                preprocess_stage_input.annotation_dispatcher,
                lambda: VideoFeatureEvent(
                    job_id=preprocess_stage_input.job_id,
                    video_filename=Path(preprocess_stage_input.asset_path).name,
                    duration_s=meta.duration,
                    size_bytes=meta.size_bytes,
                    width=meta.width,
                    height=meta.height,
                    fps=meta.fps,
                    video_codec=meta.video_codec,
                    has_audio=meta.has_audio,
                    audio_codec=meta.audio_codec,
                    audio_channels=meta.audio_channels,
                    audio_sample_rate=meta.audio_sample_rate,
                    audio_activity_segments=list(meta.audio_activity),
                    stage_latency_s=time.time() - _stage_start,
                ),
            )
            return output

        raise NotImplementedError(f"Metadata extraction not implemented for asset type: {asset_type}")


__all__ = [
    "InputMediaAssetTypes",
    "InputMediaAssetTyps",
    "PreprocessStage",
    "PreprocessStageInput",
    "PreprocessStageOutput",
    "MAX_INPUT_VIDEO_DURATION_S",
    "validate_input_video_duration",
]
