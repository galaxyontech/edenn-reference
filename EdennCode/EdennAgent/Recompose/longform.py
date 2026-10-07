"""Long-form ingestion: chunked analysis for sources beyond the pipeline cap.

The understanding pipeline validates input length (300s cap in the
preprocess stage), so a long creator video is analyzed in overlapping-free
chunks and the per-chunk observations are merged with time offsets. This is
the long->short application's ingestion primitive (PRD §17.2): one long
source in, one coherent observation out — everything downstream (SegmentTree,
probe, planner, shorts) is unchanged.

Chunks are stream-copied (fast, keyframe-aligned; scene timestamps stay
accurate to well under a second, far finer than scene granularity).
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

CHUNK_S = 240.0  # safely under the 300s preprocess cap
ANALYZE_CONCURRENCY = 3

AnalyzeChunkFn = Callable[[Path], Awaitable[dict[str, Any]]]


def _chunk_video(path: Path, out_dir: Path, chunk_s: float = CHUNK_S) -> list[tuple[Path, float]]:
    """Split into [(chunk_path, offset_s)] via stream copy."""

    from EdennCode.Util.MediaUtils.ffmpeg_utils import get_video_duration

    duration = get_video_duration(path)
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[tuple[Path, float]] = []
    offset = 0.0
    i = 0
    while offset < duration - 1.0:
        dur = min(chunk_s, duration - offset)
        out = out_dir / f"chunk_{i:03d}.mp4"
        if not out.exists():
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-ss", f"{offset:.3f}",
                 "-i", str(path), "-t", f"{dur:.3f}", "-c", "copy", str(out)],
                check=True, capture_output=True, timeout=600,
            )
        chunks.append((out, offset))
        offset += dur
        i += 1
    return chunks


def _merge_observations(parts: list[tuple[dict[str, Any], float]], duration_s: float) -> dict[str, Any]:
    """Offset-shift scene timestamps and merge chunk observations into one."""

    merged_scenes: list[dict[str, Any]] = []
    for obs, offset in parts:
        for sc in obs.get("scenes") or []:
            shifted = dict(sc)
            shifted["start_timestamp"] = float(sc.get("start_timestamp") or 0.0) + offset
            shifted["end_timestamp"] = float(sc.get("end_timestamp") or 0.0) + offset
            merged_scenes.append(shifted)
    for i, sc in enumerate(merged_scenes):
        sc["scene_index"] = i

    base = dict(parts[0][0]) if parts else {}
    base["scenes"] = merged_scenes
    base["duration_s"] = duration_s
    # music_prompt: keep the first chunk's tempo/instruments, join the moods so
    # the whole arc is visible to fusion/generation.
    moods = []
    for obs, _ in parts:
        m = str((obs.get("music_prompt") or {}).get("global_mood") or "").strip()
        if m and m not in moods:
            moods.append(m)
    if base.get("music_prompt") and moods:
        base["music_prompt"] = dict(base["music_prompt"])
        base["music_prompt"]["global_mood"] = "; ".join(moods)[:300]
    return base


async def analyze_long_video(
    path: Path,
    analyze_chunk: AnalyzeChunkFn,
    *,
    workdir: Path,
    chunk_s: float = CHUNK_S,
    concurrency: int = ANALYZE_CONCURRENCY,
) -> dict[str, Any]:
    """Chunk -> analyze (bounded-concurrent) -> merged observation, cached."""

    from EdennCode.Util.MediaUtils.ffmpeg_utils import get_video_duration

    cache = workdir / f"{path.stem}.observation.json"
    if cache.exists():
        return json.loads(cache.read_text())

    duration = get_video_duration(path)
    chunks = _chunk_video(path, workdir / "chunks", chunk_s)
    logger.info("long-form analysis: %s -> %d chunks of ~%.0fs", path.name, len(chunks), chunk_s)

    sem = asyncio.Semaphore(concurrency)

    async def run(chunk: Path, offset: float) -> tuple[dict[str, Any], float]:
        async with sem:
            obs = await analyze_chunk(chunk)
            logger.info("chunk @%.0fs: %d scenes", offset, len(obs.get("scenes") or []))
            return obs, offset

    parts = await asyncio.gather(*(run(c, o) for c, o in chunks))
    merged = _merge_observations(sorted(parts, key=lambda p: p[1]), duration)
    workdir.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(merged))
    return merged
