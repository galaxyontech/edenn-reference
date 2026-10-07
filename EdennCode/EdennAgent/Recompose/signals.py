"""Cheap numeric signals per SegmentNode: motion and brightness.

Both come from one downscaled ffmpeg pass each (proven in the W0 spike);
they are the planner's non-semantic half — the textual half lives on the
node already. Batched with a thread pool: ~2 s x N segments was W0's wall
clock and it is embarrassingly parallel.
"""

from __future__ import annotations

import logging
import re
import statistics
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .domain import AssetRecord, SegmentTree

logger = logging.getLogger(__name__)

_YAVG = re.compile(r"YAVG=([0-9.]+)")
SAMPLE_MAX_S = 3.0


def _yavg_pass(path: str, start_s: float, dur_s: float, vf: str) -> Optional[float]:
    cmd = [
        "ffmpeg", "-v", "info", "-ss", f"{start_s:.3f}", "-t", f"{dur_s:.3f}",
        "-i", path, "-vf", vf, "-an", "-f", "null", "-",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None
    vals = [float(m) for m in _YAVG.findall(out.stdout or "")]
    return round(statistics.fmean(vals), 3) if vals else None


def measure_motion(path: str, start_s: float, dur_s: float) -> Optional[float]:
    """Mean luma frame-difference (0..255-ish) over a downscaled sample."""

    return _yavg_pass(
        path, start_s, min(dur_s, SAMPLE_MAX_S),
        "scale=160:-2,fps=10,tblend=all_mode=difference,signalstats,"
        "metadata=print:key=lavfi.signalstats.YAVG:file=-",
    )


def measure_brightness(path: str, start_s: float, dur_s: float) -> Optional[float]:
    """Mean luma (0..255) — lets the planner keep night shots out of slots
    they were not chosen for (W0 finding #4)."""

    return _yavg_pass(
        path, start_s, min(dur_s, SAMPLE_MAX_S),
        "scale=160:-2,fps=6,signalstats,"
        "metadata=print:key=lavfi.signalstats.YAVG:file=-",
    )


_MEANVOL = re.compile(r"mean_volume:\s*(-?[0-9.]+)")


def _mean_volume_db(path: str, start_s: float, dur_s: float, af: str) -> Optional[float]:
    cmd = ["ffmpeg", "-hide_banner", "-ss", f"{start_s:.3f}", "-t", f"{dur_s:.3f}",
           "-i", path, "-af", af, "-vn", "-f", "null", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None
    m = _MEANVOL.search(out.stderr or "")
    return float(m.group(1)) if m else None


def measure_speech(path: str, start_s: float, dur_s: float) -> Optional[float]:
    """0..1 speech likelihood in the segment's ORIGINAL audio.

    Heuristic (no new deps): energy concentration in the vocal band
    (300 Hz–3 kHz) relative to the full band. Music spreads energy wider;
    narration concentrates it. Returns None when the source has no audio.
    ~-3 dB band loss ≈ speech-dominant; ≥ -20 dB loss ≈ no speech.
    """

    window = min(dur_s, 6.0)
    full = _mean_volume_db(path, start_s, window, "volumedetect")
    if full is None or full < -60.0:  # silent or unreadable
        return 0.0 if full is not None else None
    band = _mean_volume_db(
        path, start_s, window,
        "highpass=f=300,lowpass=f=3000,volumedetect",
    )
    if band is None:
        return None
    band_loss_db = full - band  # how much energy lives OUTSIDE the vocal band
    score = 1.0 - (band_loss_db - 2.0) / 10.0  # 2dB loss -> 1.0, 12dB -> 0.0
    return round(max(0.0, min(1.0, score)), 3)


def annotate_tree_signals(
    tree: SegmentTree,
    asset: AssetRecord,
    *,
    only_leaves: bool = True,
    max_workers: int = 6,
) -> int:
    """Fill motion/brightness for unmeasured (leaf) nodes. Returns count measured."""

    from EdennCode.Util.MediaUtils.ffmpeg_utils import (
        detect_audio_activity,
        has_audio_stream,
    )
    from pathlib import Path as _Path

    nodes = tree.leaves() if only_leaves else list(tree.nodes.values())
    todo = [n for n in nodes if not n.is_still
            and (n.motion is None or n.brightness is None or n.speech is None)]
    asset_has_audio = has_audio_stream(_Path(asset.path))
    if asset_has_audio and not tree.audio_activity:
        # asset-level speech/audio spans — the raw material for sound bites
        tree.audio_activity = [
            (round(a, 3), round(b, 3))
            for a, b in detect_audio_activity(_Path(asset.path))
        ]

    def work(node) -> None:
        if node.motion is None:
            node.motion = measure_motion(asset.path, node.start_s, node.dur_s)
        if node.brightness is None:
            node.brightness = measure_brightness(asset.path, node.start_s, node.dur_s)
        if node.brightness is not None and node.brightness < 24.0:
            if "dark" not in node.quality_flags:
                node.quality_flags.append("dark")
        if node.speech is None:
            node.speech = (
                measure_speech(asset.path, node.start_s, node.dur_s)
                if asset_has_audio else 0.0
            )
        if node.speech is not None and node.speech >= 0.6:
            if "speech" not in node.quality_flags:
                node.quality_flags.append("speech")

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        list(pool.map(work, todo))
    return len(todo)
