"""
Multi-clip SFX timeline rendering.

Successor to ``build_sfx_timeline_wav`` for the editable video→SFX loop:
per-clip gain/fades/mute, loop-to-fill beds, optional stereo output with
constant-power pan, and soft peak limiting instead of hard clipping. Non-WAV
clip inputs are transparently decoded through ffmpeg.
"""

from __future__ import annotations

import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import numpy as np

from EdennCode.Util.MediaUtils.ffmpeg_utils import (
    _read_wav_as_mono_float,
    _resample_mono_audio,
    resolve_ffmpeg_binary,
)


@dataclass
class SfxTimelineClip:
    audio_path: Path
    start_s: float
    gain_db: float = 0.0
    fade_in_s: float = 0.005
    fade_out_s: float = 0.05
    max_duration_s: Optional[float] = None
    loop_until_s: Optional[float] = None
    loop_crossfade_s: float = 0.05
    pan: float = 0.0  # -1 (left) .. 0 (center) .. 1 (right); stereo output only
    muted: bool = False
    # Timeline-time windows where THIS clip is attenuated by duck_db (smoothly
    # ramped). Used to sink a generated bed under discrete events.
    duck_windows: Optional[List[tuple]] = None  # [(start_s, end_s), ...]
    duck_db: float = -6.0
    duck_ramp_s: float = 0.08


def _apply_duck(
    samples: np.ndarray,
    *,
    clip_start_s: float,
    sample_rate: int,
    windows: List[tuple],
    duck_db: float,
    ramp_s: float,
) -> np.ndarray:
    if samples.size == 0 or not windows or duck_db >= 0:
        return samples
    gain = np.ones(samples.size, dtype=np.float32)
    duck_linear = float(10 ** (duck_db / 20.0))
    ramp = max(1, int(ramp_s * sample_rate))
    for win_start, win_end in windows:
        a = int(round((win_start - clip_start_s) * sample_rate))
        b = int(round((win_end - clip_start_s) * sample_rate))
        a, b = max(0, a), min(samples.size, b)
        if b <= a:
            continue
        gain[a:b] = np.minimum(gain[a:b], duck_linear)
        # smooth entry/exit ramps
        ra = np.linspace(1.0, duck_linear, num=min(ramp, a), dtype=np.float32)
        if ra.size:
            gain[a - ra.size: a] = np.minimum(gain[a - ra.size: a], ra)
        rb = np.linspace(duck_linear, 1.0, num=min(ramp, samples.size - b), dtype=np.float32)
        if rb.size:
            gain[b: b + rb.size] = np.minimum(gain[b: b + rb.size], rb)
    return samples * gain


def _decode_to_mono_float(path: Path, *, sample_rate: int) -> np.ndarray:
    """Read any audio file as mono float32 at the target rate."""
    path = Path(path)
    if path.suffix.lower() == ".wav":
        try:
            samples, src_rate = _read_wav_as_mono_float(path)
            if src_rate != sample_rate:
                samples = _resample_mono_audio(samples, src_rate=src_rate, dst_rate=sample_rate)
            return samples
        except (wave.Error, EOFError):
            pass  # non-PCM/malformed wav — fall through to ffmpeg decode
    ffmpeg_bin = resolve_ffmpeg_binary()
    with tempfile.TemporaryDirectory() as tmp:
        decoded = Path(tmp) / "decoded.wav"
        subprocess.run(
            [ffmpeg_bin, "-y", "-i", str(path), "-ac", "1", "-ar", str(sample_rate), str(decoded)],
            check=True,
            capture_output=True,
        )
        samples, _ = _read_wav_as_mono_float(decoded)
    return samples


def _apply_fades(samples: np.ndarray, *, sample_rate: int, fade_in_s: float, fade_out_s: float) -> np.ndarray:
    n = samples.size
    if n == 0:
        return samples
    out = samples.copy()
    fade_in = min(int(round(max(0.0, fade_in_s) * sample_rate)), n)
    if fade_in > 1:
        out[:fade_in] *= np.linspace(0.0, 1.0, num=fade_in, dtype=np.float32)
    fade_out = min(int(round(max(0.0, fade_out_s) * sample_rate)), n)
    if fade_out > 1:
        out[n - fade_out:] *= np.linspace(1.0, 0.0, num=fade_out, dtype=np.float32)
    return out


def _loop_to_length(samples: np.ndarray, *, target_len: int, crossfade: int) -> np.ndarray:
    """Tile a clip to target_len samples with a linear crossfade at each seam."""
    if samples.size == 0 or target_len <= samples.size:
        return samples[:target_len]
    crossfade = max(0, min(crossfade, samples.size // 2))
    if crossfade < 2:
        # A 1-sample "crossfade" would zero the seam sample (linspace(0,1,1)==[0]);
        # mirror _apply_fades' >1 guard and tile without a ramp instead.
        crossfade = 0
    out = np.zeros(target_len, dtype=np.float32)
    hop = samples.size - crossfade if crossfade else samples.size
    ramp_up = np.linspace(0.0, 1.0, num=crossfade, dtype=np.float32) if crossfade else None
    position = 0
    first = True
    while position < target_len:
        chunk = samples.copy()
        if crossfade and not first:
            chunk[:crossfade] *= ramp_up
            out_region = out[position: position + crossfade]
            # complementary ramp-down on what is already in the buffer
            out[position: position + crossfade] = out_region * ramp_up[::-1][: out_region.size]
        end = min(target_len, position + chunk.size)
        out[position:end] += chunk[: end - position]
        position += hop if hop else chunk.size
        first = False
    return out


def render_sfx_timeline_wav(
    clips: List[SfxTimelineClip],
    output_path: Path,
    *,
    total_duration_s: float,
    sample_rate: int = 44_100,
    channels: int = 1,
    master_gain_db: float = 0.0,
    peak_ceiling: float = 0.985,
) -> Path:
    """
    Render clips onto one timeline WAV.

    Unlike hard clipping, when the mix exceeds ``peak_ceiling`` the whole
    timeline is scaled down so transients keep their shape.
    """
    if channels not in (1, 2):
        raise ValueError("channels must be 1 or 2")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total_samples = max(1, int(round(max(total_duration_s, 0.0) * sample_rate)))
    timeline = np.zeros((total_samples, channels), dtype=np.float32)

    for clip in clips:
        if clip.muted:
            continue
        samples = _decode_to_mono_float(clip.audio_path, sample_rate=sample_rate)
        if samples.size == 0:
            continue

        if clip.max_duration_s is not None:
            samples = samples[: max(0, int(round(clip.max_duration_s * sample_rate)))]
        if clip.loop_until_s is not None:
            loop_len = int(round(max(0.0, clip.loop_until_s - clip.start_s) * sample_rate))
            samples = _loop_to_length(
                samples,
                target_len=loop_len,
                crossfade=int(round(clip.loop_crossfade_s * sample_rate)),
            )
        if samples.size == 0:
            continue

        samples = _apply_fades(
            samples,
            sample_rate=sample_rate,
            fade_in_s=clip.fade_in_s,
            fade_out_s=clip.fade_out_s,
        )
        samples = samples * float(10 ** (clip.gain_db / 20.0))
        if clip.duck_windows:
            samples = _apply_duck(
                samples,
                clip_start_s=clip.start_s,
                sample_rate=sample_rate,
                windows=clip.duck_windows,
                duck_db=clip.duck_db,
                ramp_s=clip.duck_ramp_s,
            )

        start_idx = int(round(clip.start_s * sample_rate))
        if start_idx >= total_samples:
            continue
        if start_idx < 0:
            samples = samples[-start_idx:]
            start_idx = 0
        end_idx = min(total_samples, start_idx + samples.size)
        if end_idx <= start_idx:
            continue
        segment = samples[: end_idx - start_idx]

        if channels == 1:
            timeline[start_idx:end_idx, 0] += segment
        else:
            pan = float(np.clip(clip.pan, -1.0, 1.0))
            angle = (pan + 1.0) * (np.pi / 4.0)  # constant-power pan law
            timeline[start_idx:end_idx, 0] += segment * np.cos(angle)
            timeline[start_idx:end_idx, 1] += segment * np.sin(angle)

    timeline *= float(10 ** (master_gain_db / 20.0))
    peak = float(np.max(np.abs(timeline))) if timeline.size else 0.0
    if peak > peak_ceiling > 0.0:
        timeline *= peak_ceiling / peak

    pcm16 = (np.clip(timeline, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(str(output_path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())
    return output_path
