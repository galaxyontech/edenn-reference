"""
Annotation event emitted after Stage 1 (VideoPreprocess).

Captures raw video technical metadata that serves as supporting context for
all downstream generation and retrieval decisions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from EdennCode.Annotation.core.annotation_event import AnnotationEvent


@dataclass(kw_only=True)
class VideoFeatureEvent(AnnotationEvent):
    """
    Technical metadata extracted from the input video at ingest time.

    This event is emitted after the ``PreprocessStage`` completes.  It records
    the properties used by all downstream stages (duration drives music length,
    FPS and resolution influence scene analysis, audio activity drives ducking).

    Attributes
    ----------
    event_type:
        Always ``"video_feature"``.  Do not change.
    video_filename:
        Base filename of the input video (no directory path, for portability).
    duration_s:
        Video duration in seconds as reported by ffprobe.
    size_bytes:
        File size in bytes of the original video asset.
    width:
        Horizontal resolution in pixels, or ``None`` if not detected.
    height:
        Vertical resolution in pixels, or ``None`` if not detected.
    fps:
        Frames per second as a float (may be fractional for variable-rate content).
    video_codec:
        Codec name of the video stream (e.g. ``"h264"``), or ``None``.
    has_audio:
        Whether the input video contains an audio track.
    audio_codec:
        Codec name of the audio stream (e.g. ``"aac"``), or ``None``.
    audio_channels:
        Number of audio channels, or ``None`` if not detected.
    audio_sample_rate:
        Audio sample rate in Hz, or ``None`` if not detected.
    audio_activity_segments:
        List of ``(start_s, end_s)`` tuples indicating segments where the
        original audio track is active.  Used downstream for ducking.
    stage_latency_s:
        Wall-clock seconds the preprocess stage took.
    """

    event_type: str = "video_feature"
    video_filename: str = ""
    duration_s: float = 0.0
    size_bytes: int = 0
    width: Optional[int] = None
    height: Optional[int] = None
    fps: float = 0.0
    video_codec: Optional[str] = None
    has_audio: bool = False
    audio_codec: Optional[str] = None
    audio_channels: Optional[int] = None
    audio_sample_rate: Optional[int] = None
    audio_activity_segments: List[Tuple[float, float]] = field(default_factory=list)
    stage_latency_s: float = 0.0
