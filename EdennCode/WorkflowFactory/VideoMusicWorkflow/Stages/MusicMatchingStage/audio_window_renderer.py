from __future__ import annotations

import os
from typing import Optional

from .media_tools import MediaTools


class AudioWindowRenderer:
    """
    Trim a window from the music track and apply fade-in/out, outputting audio only.
    """

    def __init__(
        self,
        *,
        fade_s: Optional[float] = None,
        fade_ratio: float = 0.05,
        audio_codec: str = "pcm_s16le",
        audio_bitrate: Optional[str] = None,
    ) -> None:
        self.fade_s = fade_s
        self.fade_ratio = fade_ratio
        self.audio_codec = audio_codec
        self.audio_bitrate = audio_bitrate

    def render_audio(
        self,
        *,
        music_path: str,
        music_start_s: float,
        duration_s: float,
        out_path: str,
    ) -> str:
        MediaTools.ensure("ffmpeg")
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

        dur = float(duration_s)
        fade = self.fade_s if self.fade_s is not None else self.fade_ratio * dur
        fade = float(max(0.0, min(fade, dur / 2.0 - 0.001)))
        fade_out_start = max(0.0, dur - fade)

        af = (
            f"atrim=start={music_start_s}:end={music_start_s + dur},asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={fade},afade=t=out:st={fade_out_start}:d={fade}"
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", music_path,
            "-filter:a", af,
            "-vn",
        ]
        cmd.extend(["-c:a", self.audio_codec])
        if self.audio_bitrate:
            cmd.extend(["-b:a", self.audio_bitrate])
        cmd.append(out_path)

        MediaTools.run(cmd)
        return out_path
