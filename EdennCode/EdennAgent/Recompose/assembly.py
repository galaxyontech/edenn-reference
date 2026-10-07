"""Assembly v2: RecomposePlan -> RenderedVariant (deterministic, slot-cached).

Every slot renders to its own normalized file keyed by content — a knob
tweak or single-slot swap re-renders only what changed, which is what makes
the agentic taste loop feel instant. Stills render as Ken Burns holds.
Cuts are hard cuts on beats (W0 verdict: reads more musical); passage-
boundary crossfades stay a polish item, tracked in DESIGN.md.

Cut report: planned-cut-to-beat offsets (the trustworthy E1 number) plus
detected-cut matching restricted to ±half a slot around planned boundaries
(the W0 findings fix — raw nearest-cut matching overstated error because
source scenes contain their own interior cuts).
"""

from __future__ import annotations

import hashlib
import statistics
import subprocess
from pathlib import Path

import numpy as np

from .domain import (
    AssetRecord,
    CutReport,
    MusicSheet,
    RecomposePlan,
    RenderedVariant,
    SegmentTree,
)

FPS = 30
OUT_W, OUT_H = 720, 1280
ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
          "-video_track_timescale", "15360"]


def _slot_cache_key(asset_path: str, seg_in: float, frames: int, is_still: bool,
                    audio_mode, restore: bool) -> str:
    raw = f"{asset_path}|{seg_in:.4f}|{frames}|{is_still}|{audio_mode}|{restore}|{OUT_W}x{OUT_H}@{FPS}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _run(cmd: list[str], timeout: int = 300) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {' '.join(cmd[:8])}…\n{proc.stderr[-800:]}")


AUDIO_ENCODE = ["-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k"]

# Deterministic exposure lift for underexposed/faded sources (EDIT tier):
# per-frame histogram stretch with temporal smoothing + gentle gamma/sat.
RESTORE_VF = "normalize=smoothing=30,eq=gamma=1.25:saturation=1.2"
RESTORE_LUMA_GATE = 48.0  # asset median luma below this -> auto restore


def _render_slot(
    asset: AssetRecord,
    seg_in: float,
    frames: int,
    is_still: bool,
    out: Path,
    *,
    with_audio: bool = False,
    silent_audio_track: bool = False,
    restore: bool = False,
) -> None:
    """One normalized slot file. ``with_audio`` keeps the ORIGINAL audio window
    (uniform aac/48k/stereo so concat -c copy holds; silence-padded when the
    source has none) — the duck path and sound bites. ``silent_audio_track``
    emits a silent (but present) track so bite and non-bite slots concat."""

    from EdennCode.Util.MediaUtils.ffmpeg_utils import has_audio_stream

    if silent_audio_track:
        with_audio, _force_silence = True, True
    else:
        _force_silence = False
    dur = frames / FPS
    if is_still:
        vf = (
            f"scale={OUT_W * 2}:{OUT_H * 2}:force_original_aspect_ratio=increase,"
            f"crop={OUT_W * 2}:{OUT_H * 2},"
            f"zoompan=z='min(1.0+0.04*on/{max(frames, 1)},1.08)':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={OUT_W}x{OUT_H}:fps={FPS},"
            + (f"{RESTORE_VF}," if restore else "")
            + "setsar=1,format=yuv420p"
        )
        cmd = ["ffmpeg", "-y", "-v", "error", "-loop", "1", "-i", asset.path]
        if with_audio:
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                    "-map", "0:v", "-map", "1:a"]
        cmd += ["-t", f"{dur:.4f}", "-vf", vf, "-frames:v", str(frames)]
        cmd += (AUDIO_ENCODE if with_audio else ["-an"]) + [*ENCODE, str(out)]
    else:
        vf = (f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
              f"crop={OUT_W}:{OUT_H},fps={FPS},"
              + (f"{RESTORE_VF}," if restore else "")
              + "setsar=1,format=yuv420p")
        source_audio = (with_audio and not _force_silence
                        and has_audio_stream(Path(asset.path)))
        cmd = ["ffmpeg", "-y", "-v", "error",
               "-ss", f"{seg_in:.3f}", "-i", asset.path]
        if with_audio and not source_audio:
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                    "-map", "0:v", "-map", "1:a"]
        cmd += ["-t", f"{dur:.4f}", "-vf", vf, "-frames:v", str(frames)]
        cmd += (AUDIO_ENCODE if with_audio else ["-an"]) + [*ENCODE, str(out)]
    _run(cmd)


def render_variant(
    plan: RecomposePlan,
    sheet: MusicSheet,
    assets: list[AssetRecord],
    trees: dict[str, SegmentTree],
    workdir: Path,
    *,
    label: str = "variant",
    narration_path: Path | None = None,
) -> RenderedVariant:
    by_id = {a.asset_id: a for a in assets}
    cache_dir = workdir / "slot_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    keep_source_audio = plan.knobs.source_audio == "duck"

    # Auto-restore per asset: gated on the tree's measured brightness so a
    # normal source is never touched (deterministic EDIT, recorded per slot).
    restore_asset: dict[str, bool] = {}
    if plan.knobs.restore == "auto":
        for aid, tree in trees.items():
            lumas = [n.brightness for n in tree.leaves() if n.brightness is not None]
            if lumas:
                restore_asset[aid] = statistics.median(lumas) < RESTORE_LUMA_GATE

    has_bites = any(s.spec.is_bite for s in plan.slots)
    # Sound bites need an audio track in every slot for uniform concat: bites
    # carry SOURCE audio; non-bite slots carry source (duck) or silence (mute).
    track_audio = keep_source_audio or has_bites

    # Frame quantization with cumulative drift correction (W0: median 7.6 ms).
    listfile_lines: list[str] = []
    slot_renders: dict[int, str] = {}
    rendered_durs: list[float] = []
    bite_spans: list[tuple[float, float]] = []  # output-time spans for ducking
    t_target = 0.0
    t_actual = 0.0
    for slot in plan.slots:
        t_target += slot.spec.dur_s
        frames = max(1, round((t_target - t_actual) * FPS))
        slot_audio = keep_source_audio or slot.spec.is_bite
        if slot.spec.is_bite:
            bite_spans.append((t_actual, t_actual + frames / FPS))
        t_actual += frames / FPS
        asset = by_id[slot.asset_id]
        node = trees[slot.asset_id].nodes[slot.node_id]
        restore = restore_asset.get(slot.asset_id, False)
        key = _slot_cache_key(asset.path, slot.seg_in_s, frames, node.is_still,
                              (slot_audio, track_audio), restore)
        out = cache_dir / f"{key}.mp4"
        if not out.exists():
            _render_slot(asset, slot.seg_in_s, frames, node.is_still, out,
                         with_audio=slot_audio, silent_audio_track=(track_audio and not slot_audio),
                         restore=restore)
        slot_renders[slot.spec.index] = str(out)
        listfile_lines.append(f"file '{out.resolve()}'")
        rendered_durs.append(frames / FPS)

    listfile = workdir / f"{label}_concat.txt"
    listfile.write_text("\n".join(listfile_lines) + "\n")
    concat = workdir / f"{label}_concat.mp4"
    _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
          "-i", str(listfile), "-c", "copy", str(concat)])

    final = workdir / f"{label}.mp4"
    knobs = plan.knobs
    if narration_path is not None:
        # Narration over music (over the original bed when ducking) — the
        # voiceover mix helper handles ducking + layer balance.
        from EdennCode.Util.MediaUtils.ffmpeg_utils import (
            compose_voiceover_mix_on_video,
            extract_audio_window,
        )

        music_window = workdir / f"{label}_music_window.m4a"
        extract_audio_window(Path(sheet.track_path), music_window,
                             start_s=sheet.window_start_s,
                             duration_s=sum(rendered_durs))
        compose_voiceover_mix_on_video(
            concat, music_window, Path(narration_path), final,
            music_volume=knobs.music_volume,
            voiceover_volume=knobs.narration_volume,
            voiceover_start_s=0.6,
            duck_gain_db=knobs.duck_gain_db,
            preserve_original_audio=keep_source_audio,
        )
    elif keep_source_audio or has_bites:
        # Music mixed WITH the footage's own audio; when sound bites exist the
        # music DUCKS under each bite so the source voice carries the moment.
        from EdennCode.Util.MediaUtils.ffmpeg_utils import overlay_music_on_video

        overlay_music_on_video(
            concat, Path(sheet.track_path), final,
            music_volume=knobs.music_volume,
            preserve_original_audio=True,
            music_start_s=sheet.window_start_s,
            ducking_segments=bite_spans or None,
            ducking_gain_db=knobs.duck_gain_db,
        )
    else:
        # Music-only replacement (original W0 path, loudness-normalized).
        _run(["ffmpeg", "-y", "-v", "error",
              "-i", str(concat),
              "-ss", f"{sheet.window_start_s:.3f}", "-i", sheet.track_path,
              "-map", "0:v", "-map", "1:a",
              "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
              "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
              "-shortest", str(final)])

    report = _cut_report(rendered_durs, sheet, final)
    return RenderedVariant(
        plan_id=plan.plan_id,
        path=str(final),
        cut_report=report,
        slot_renders=slot_renders,
    )


def _cut_report(rendered_durs: list[float], sheet: MusicSheet, final: Path) -> CutReport:
    from EdennCode.Util.MediaUtils.ffmpeg_utils import detect_scene_cuts, get_video_duration

    beats = np.array(sheet.beats) - sheet.beats[0]
    planned_cuts = list(np.cumsum(rendered_durs)[:-1])
    beat_offsets = [float(np.min(np.abs(beats - c))) * 1000 for c in planned_cuts]

    # Detected-cut check restricted to a half-slot window around each planned
    # boundary, so interior cuts native to the source do not count as error.
    notes: dict = {}
    try:
        detected = np.array(detect_scene_cuts(final))
        matched: list[float] = []
        for i, c in enumerate(planned_cuts):
            half = min(rendered_durs[i], rendered_durs[i + 1]) / 2
            near = detected[(detected >= c - half) & (detected <= c + half)]
            if near.size:
                matched.append(float(np.min(np.abs(near - c))) * 1000)
        notes["detected_matched"] = len(matched)
        notes["detected_matched_median_ms"] = (
            round(statistics.median(matched), 1) if matched else None
        )
    except Exception as exc:  # noqa: BLE001 - report survives detection failure
        notes["detected_error"] = str(exc)[:120]

    return CutReport(
        duration_s=round(get_video_duration(final), 3),
        n_slots=len(rendered_durs),
        cut_to_beat_ms_median=round(statistics.median(beat_offsets), 1) if beat_offsets else None,
        cut_to_beat_ms_p95=(
            round(float(np.quantile(beat_offsets, 0.95)), 1) if beat_offsets else None
        ),
        notes=notes,
    )
