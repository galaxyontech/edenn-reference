"""Rendering a timed narration plan into one assembled track.

This lives on its own because the drift between two copies of it WAS the bug.
The local design server grew per-line synthesis, edge-silence trimming, timeline
resolution against real durations, cut snapping and retakes, while the production
worker still flat-synthesized the concatenated script and returned no placements
at all — so every timing decision the director made was silently discarded the
moment a session ran anywhere real. Both callers now import this.

The synthesiser is injected rather than imported: the two callers construct it
differently, and keeping it a parameter is what stops this module from pulling a
provider adapter into everything that touches narration.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from EdennCode.Util.MediaUtils.ffmpeg_utils import (
    get_video_duration,
    resolve_ffmpeg_binary,
    trim_edge_silence,
)

from .media import resolve_narration_timeline

logger = logging.getLogger(__name__)

# Past roughly this, a speed change reads as processing rather than performance.
MAX_SEGMENT_SPEED = 1.15
MIN_SEGMENT_SPEED = 0.85
# Escalating retakes for a line that cannot be made to fit its shot by moving.
RETAKE_SPEEDS = (1.08, 1.15)
# Deliveries that a speed-up actively fights. A "low, ominous, controlled" line
# read 15% brisk is a different performance than the director asked for — cap
# these at the first rung, and let a straddle stand before wrecking the read.
_RESTRAINED_DELIVERY = re.compile(
    r"restrain|calm|measured|soft|warm|gentle|delicate|intimate|quiet|hushed|"
    r"low|controlled|grave|solemn",
    re.IGNORECASE,
)
# Air between consecutive lines.
MIN_GAP_S = 0.4

# An async (script, voice, instructions, speed, out_path) -> None callable.
SynthesizeFn = Callable[..., Awaitable[Any]]


def _clamp_speed(value: Any, default: float = 1.0) -> float:
    try:
        return min(MAX_SEGMENT_SPEED, max(MIN_SEGMENT_SPEED, float(value or default)))
    except (TypeError, ValueError):
        return default


def _instructions_for(base: str, segment: dict[str, Any]) -> str:
    delivery = str(segment.get("delivery") or "").strip()
    if not delivery:
        return base.strip()
    return f"{base} Delivery for this line: {delivery}.".strip()


async def render_segmented_narration(
    *,
    segments: list[dict[str, Any]],
    synthesize: SynthesizeFn,
    voice: str,
    base_instructions: str = "",
    default_speed: float = 1.0,
    workdir: Path,
    out_path: Path,
    video_duration_s: float = 0.0,
    cuts: Optional[list[float]] = None,
    avoid_windows: Optional[list[list[float]]] = None,
    log: Callable[[str], None] = logger.info,
    speed_capable: bool = True,
    reuse: Optional[dict[str, Path]] = None,
) -> Optional[list[dict[str, Any]]]:
    """Synthesize each line, place it against the picture, assemble one track.

    Returns the REALIZED placements (start_s and measured duration_s per line)
    so the caller can report what the listener will actually hear, or None when
    nothing usable was synthesized. Each placement carries ``audio_path``: the
    line on its own, which is what makes a single-line retake possible instead
    of re-recording a whole script to change one word.

    ``reuse`` maps a segment id to audio already rendered for it. Those lines
    are NOT re-synthesized — the point of a retake is that the other nine lines
    do not get re-recorded and re-paid for because the tenth was wrong. A line
    whose text has changed must not be passed here; the caller owns that
    decision because only it knows what the user edited.
    """

    workdir.mkdir(parents=True, exist_ok=True)
    synthed: list[dict[str, Any]] = []

    for index, segment in enumerate(segments):
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        seg_out = workdir / f"voseg_{index:02d}.wav"
        speed = _clamp_speed(segment.get("speed", default_speed), default_speed)
        existing = (reuse or {}).get(str(segment.get("id") or ""))
        reused = bool(existing and Path(existing).exists())
        if reused:
            # Already recorded and unchanged. Copy it in rather than reading it
            # in place: the assembly indexes by workdir position, and the caller
            # should not have to care where its previous render lives.
            shutil.copyfile(existing, seg_out)
            log(f"  line {index + 1}: kept the take already recorded")
        else:
            await synthesize(
                script=text,
                voice=voice,
                instructions=_instructions_for(base_instructions, segment),
                speed=speed,
                out_path=seg_out,
            )
        if not (seg_out.exists() and seg_out.stat().st_size > 1000):
            continue
        # Measure the SPEECH, not the padding the provider wrapped it in: every
        # placement below is computed from this number, and the director's air
        # is added on top of it. A kept line was trimmed when it was recorded —
        # trimming it again would rewrite the file, and "kept" has to mean the
        # identical audio the user already approved, not a near-copy of it.
        if not reused:
            removed = trim_edge_silence(seg_out)
            if removed > 0.01:
                log(f"  line {index + 1}: trimmed {removed:.2f}s of provider air")
        synthed.append({
            **{k: segment.get(k) for k in ("id", "text", "delivery")},
            "start_s": max(0.0, float(segment.get("start_s") or 0.0)),
            "duration_s": round(get_video_duration(seg_out) or 0.0, 2),
            "speed": speed,
            # Whether this line cost anything. Already known here and thrown
            # away, which left the reuse path — the whole reason "re-read line
            # three" is not a full re-record — invisible to anything that wants
            # to say what a render consumed.
            "reused": reused,
            "_path": seg_out,
        })

    if not synthed:
        return None

    duration = float(video_duration_s or 0.0) or (
        synthed[-1]["start_s"] + synthed[-1]["duration_s"]
    )
    resolved, fits = resolve_narration_timeline(
        synthed, video_duration_s=duration, min_gap_s=MIN_GAP_S, cuts=cuts,
        avoid_windows=avoid_windows,
    )
    if not fits:
        log(f"narration is LONGER than the clip ({duration:.1f}s) — "
            "placed without overlaps; the tail will truncate at compose.")

    # A line can be unfixable by MOVING and still fixable: when the shot is 2.5s
    # and the take is 2.62s, no placement works but a brisker read does. Retake
    # only the offending lines, and keep the original unless the retake actually
    # clears — a faster line that still straddles is strictly worse.
    sorted_cuts = sorted(float(c) for c in (cuts or []))

    def _shot_window_after(start_s: float) -> float:
        """Seconds from this start to the next cut — the room a line has."""
        for c in sorted_cuts:
            if c > start_s + 0.05:
                return c - start_s
        return max(0.0, duration - start_s)

    if not speed_capable and any(s.get("crosses_cut") for s in resolved):
        # The engine ignores speed: a retake would re-spend for an identical
        # duration. Say what stands and why, once.
        log("engine has no speed control — keeping the straddling lines as read")

    for attempt_speed in (RETAKE_SPEEDS if speed_capable else ()):
        flagged = [s for s in resolved if s.get("crosses_cut")]
        if not flagged:
            break
        for seg in flagged:
            idx = next((i for i, x in enumerate(synthed)
                        if x.get("id") == seg.get("id")), None)
            if idx is None or synthed[idx].get("speed", 1.0) >= attempt_speed:
                continue
            # A retake must be able to WIN. On fast-cut footage nearly every
            # line straddles something, and the ladder was speeding up entire
            # reads for a shrink that could never clear the shot — the whole
            # narration came back 8-15% brisk and a listener called it "way
            # too fast". Two craft rules, learned from that session:
            #   * if even the top rung cannot fit the line inside its shot,
            #     keep the calm read — a rushed straddle is strictly worse;
            #   * a restrained delivery never takes the top rung at all.
            current_speed = float(synthed[idx].get("speed") or 1.0)
            predicted = synthed[idx]["duration_s"] * (current_speed / attempt_speed)
            window = _shot_window_after(float(seg.get("start_s") or 0.0))
            if sorted_cuts and predicted > window - 0.05:
                log(f"  {seg.get('id')}: longer than its shot at any speed "
                    f"({predicted:.2f}s into {window:.2f}s) — keeping the "
                    "calm read over a rushed straddle")
                continue
            if (attempt_speed > 1.08
                    and _RESTRAINED_DELIVERY.search(str(seg.get("delivery") or ""))):
                log(f"  {seg.get('id')}: restrained delivery — not taking "
                    f"the {attempt_speed}x rung")
                continue
            retake = workdir / f"voseg_{idx:02d}_at{int(attempt_speed * 100)}.wav"
            await synthesize(
                script=str(seg.get("text") or "").strip(),
                voice=voice,
                instructions=_instructions_for(base_instructions, seg),
                speed=attempt_speed,
                out_path=retake,
            )
            if not (retake.exists() and retake.stat().st_size > 1000):
                continue
            trim_edge_silence(retake)
            new_dur = round(get_video_duration(retake) or 0.0, 2)
            if new_dur <= 0 or new_dur >= synthed[idx]["duration_s"]:
                continue
            log(f"  retaking {seg.get('id')} at {attempt_speed}x "
                f"({synthed[idx]['duration_s']:.2f}s -> {new_dur:.2f}s) to clear a cut")
            synthed[idx] = {**synthed[idx], "duration_s": new_dur,
                            "speed": attempt_speed, "_path": retake}
        candidate, cand_fits = resolve_narration_timeline(
            synthed, video_duration_s=duration, min_gap_s=MIN_GAP_S, cuts=cuts,
            avoid_windows=avoid_windows,
        )
        if sum(1 for x in candidate if x.get("crosses_cut")) < len(flagged):
            resolved, fits = candidate, cand_fits
        else:
            resolved = candidate

    still_crossing = [f"{s.get('id')}@{s['crosses_cut']}s"
                      for s in resolved if s.get("crosses_cut")]
    if still_crossing:
        log(f"narration still crosses a cut (no legal move left): "
            f"{', '.join(still_crossing)}")
    elif cuts:
        log(f"narration snapped clear of all {len(cuts)} cuts")

    _assemble(resolved, out_path=out_path)
    # The per-line audio travels with the placement. Dropping it is what made a
    # one-line retake impossible: the parts existed, and then they did not.
    return [
        {**{k: v for k, v in s.items() if k != "_path"}, "audio_path": str(s["_path"])}
        for s in resolved
    ]


def _assemble(resolved: list[dict[str, Any]], *, out_path: Path) -> Path:
    """Lay each take at its resolved offset and mix them into one track."""

    ffmpeg_bin = resolve_ffmpeg_binary()
    cmd = [ffmpeg_bin, "-y"]
    for seg in resolved:
        cmd += ["-i", str(seg["_path"])]
    delays = ";".join(
        f"[{i}:a]adelay={int(seg['start_s'] * 1000)}|{int(seg['start_s'] * 1000)}[d{i}]"
        for i, seg in enumerate(resolved)
    )
    mix_in = "".join(f"[d{i}]" for i in range(len(resolved)))
    graph = f"{delays};{mix_in}amix=inputs={len(resolved)}:normalize=0[vo]"
    cmd += ["-filter_complex", graph, "-map", "[vo]", "-ar", "44100",
            str(out_path), "-loglevel", "error"]
    subprocess.run(cmd, check=True)
    return out_path


__all__ = ["render_segmented_narration", "MAX_SEGMENT_SPEED", "MIN_SEGMENT_SPEED"]
