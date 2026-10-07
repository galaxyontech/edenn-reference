"""Deterministic synthetic media for video→SFX tests (numpy + ffmpeg only)."""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path
from typing import Sequence

import numpy as np

from EdennCode.Util.MediaUtils.ffmpeg_utils import resolve_ffmpeg_binary


def make_flash_video(
    output_path: Path,
    *,
    duration_s: float = 3.0,
    flash_times_s: Sequence[float] = (1.5,),
    flash_duration_s: float = 0.12,
    fps: int = 24,
    width: int = 160,
    height: int = 90,
    with_silent_audio: bool = False,
) -> Path:
    """Black video with white flashes at known times — a visual-onset fixture."""
    frame_count = max(2, int(round(duration_s * fps)))
    frames = np.full((frame_count, height, width), 16, dtype=np.uint8)
    for flash_at in flash_times_s:
        start = int(round(flash_at * fps))
        end = min(frame_count, start + max(1, int(round(flash_duration_s * fps))))
        frames[start:end] = 235

    ffmpeg_bin = resolve_ffmpeg_binary()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin,
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-s",
        f"{width}x{height}",
        "-r",
        str(fps),
        "-i",
        "pipe:0",
    ]
    if with_silent_audio:
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={duration_s}"]
    cmd += ["-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast"]
    if with_silent_audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(output_path)]
    subprocess.run(cmd, input=frames.tobytes(), check=True, capture_output=True)
    return output_path


def make_click_wav(
    output_path: Path,
    *,
    duration_s: float = 1.0,
    click_at_s: float = 0.0,
    click_duration_s: float = 0.05,
    sample_rate: int = 44_100,
    amplitude: float = 0.8,
    frequency_hz: float = 1000.0,
) -> Path:
    """Silence with one tone burst at a known offset — an audio-onset fixture."""
    total = int(round(duration_s * sample_rate))
    samples = np.zeros(total, dtype=np.float32)
    start = int(round(click_at_s * sample_rate))
    length = max(1, int(round(click_duration_s * sample_rate)))
    end = min(total, start + length)
    t = np.arange(end - start) / sample_rate
    samples[start:end] = amplitude * np.sin(2 * np.pi * frequency_hz * t).astype(np.float32)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pcm16 = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(str(output_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())
    return output_path


def read_wav(path: Path) -> tuple[np.ndarray, int, int]:
    """Return (float samples shaped [frames, channels], sample_rate, channels)."""
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
    frames = len(samples) // channels
    return samples[: frames * channels].reshape(frames, channels), rate, channels
