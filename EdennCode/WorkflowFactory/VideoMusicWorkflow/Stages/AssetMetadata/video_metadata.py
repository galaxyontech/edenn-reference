from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from EdennCode.Util.MediaUtils.ffmpeg_utils import (
    get_display_dimensions_from_stream,
    resolve_ffmpeg_binary,
)


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_fps(stream: Dict[str, Any]) -> float:
    rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or ""
    try:
        if isinstance(rate, str) and "/" in rate:
            num, den = rate.split("/", 1)
            num_f = float(num)
            den_f = float(den)
            return num_f / den_f if den_f else 0.0
        return float(rate) if rate else 0.0
    except Exception:
        return 0.0


def _resolve_temp_dir(video_path: Path) -> Path:
    """
    Resolve a working directory for intermediates.

    By default this uses the system temp directory to avoid leaving repo-local state
    behind. For deterministic local debugging, set USE_LOCAL_TEMP_DIR=true and
    optionally LOCAL_TEMP_DIR to either a relative or absolute base directory.
    """
    safe_stem = "".join(
        ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in video_path.stem
    ) or "video"
    if os.getenv("USE_LOCAL_TEMP_DIR", "false").lower() in {"1", "true", "yes"}:
        configured_base = Path(
            os.getenv("LOCAL_TEMP_DIR", "temp_video_workdir")
        ).expanduser()
        base_dir = (
            configured_base
            if configured_base.is_absolute()
            else Path.cwd() / configured_base
        )
        target = base_dir / safe_stem
        target.mkdir(parents=True, exist_ok=True)
        return target
    return Path(tempfile.mkdtemp(prefix=f"edenn-{safe_stem}-"))


@dataclass
class VideoMetadata:
    """
    Container for video metadata gathered via ffprobe.
    """

    path: Path
    duration: float
    size_bytes: int
    width: Optional[int]
    height: Optional[int]
    fps: float
    video_codec: Optional[str]
    video_bit_rate: Optional[int]
    has_audio: bool
    audio_codec: Optional[str]
    audio_channels: Optional[int]
    audio_sample_rate: Optional[int]
    audio_bit_rate: Optional[int]
    temp_folder: str
    audio_activity: List[Tuple[float, float]] = field(default_factory=list)

    @classmethod
    def from_file(cls, video_path: Path) -> "VideoMetadata":
        """
        Inspect a video file and return parsed metadata.
        """
        path = video_path.resolve()
        if not path.exists():
            raise FileNotFoundError(path)

        ffprobe_bin = Path(resolve_ffmpeg_binary()).with_name("ffprobe")
        cmd = [
            str(ffprobe_bin),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        info = json.loads(result.stdout or "{}")

        streams = info.get("streams", [])
        format_info = info.get("format", {})

        video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})

        duration_val = format_info.get("duration") or video_stream.get("duration") or 0.0
        try:
            duration = float(duration_val)
        except (TypeError, ValueError):
            duration = 0.0
        width, height = get_display_dimensions_from_stream(video_stream)
        temporary_dir = _resolve_temp_dir(path)
        return cls(
            path=path,
            duration=duration,
            size_bytes=path.stat().st_size,
            width=width,
            height=height,
            fps=_parse_fps(video_stream),
            video_codec=video_stream.get("codec_name"),
            video_bit_rate=_safe_int(video_stream.get("bit_rate") or format_info.get("bit_rate")),
            has_audio=bool(audio_stream),
            audio_codec=audio_stream.get("codec_name"),
            audio_channels=_safe_int(audio_stream.get("channels")),
            audio_sample_rate=_safe_int(audio_stream.get("sample_rate")),
            audio_bit_rate=_safe_int(audio_stream.get("bit_rate")),
            temp_folder=str(temporary_dir),
            audio_activity=[],
        )

    def to_dict(self) -> Dict[str, Any]:
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
            "audio_activity": self.audio_activity,
        }


__all__ = ["VideoMetadata"]
