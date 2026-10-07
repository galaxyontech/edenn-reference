"""W0 spike — music-led video recomposition, vertical sliver.

Per ../MVP_PLAN.md W0: everything hard-coded except the core loop.

  sources (real clips w/ stored scene observations) + one real track
    -> librosa beat grid + energy curve            (AudioPlan)
    -> beat-aligned slots (denser cuts at higher energy)
    -> heuristic selector (mood/arousal fit, motion proxy, novelty)
    -> per-slot normalized renders -> concat -> music mux + loudnorm
    -> planned.mp4 + random-baseline.mp4 + timeline.json + cut report

No UI, no jobs, no LLM calls. Exit criterion: a human watches planned.mp4
and judges whether beat-cut assembly of real footage feels alive.

Run:
    .venv/bin/python EdennCode/EdennAgent/Recompose/spike/w0_recompose_spike.py \
        --workdir <dir with manifest.json>  [--window-s 45] [--seed 7]

manifest.json: {"sources": [{"name", "path", "observation"}...], "music": path}
observation JSON: the agentic_audio bootstrap observation (scenes with
start_timestamp/end_timestamp/visual_summary/key_actions/mood).
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

# ---------------------------------------------------------------- constants
FPS = 30
OUT_W, OUT_H = 720, 1280  # 9:16 portrait
MIN_SEG_HEADROOM_S = 0.20  # segment must exceed slot by this much
MAX_SOURCE_SHARE = 0.5

HIGH_AROUSAL = re.compile(
    r"explos|chaos|chaotic|panic|run|running|urgent|fast|intense|crowd|fire|"
    r"smoke|action|energetic|jump|dance|impact|rubble|collaps|debris|flee",
    re.I,
)
LOW_AROUSAL = re.compile(
    r"calm|quiet|still|somber|slow|gentle|serene|sits|sitting|close-up|"
    r"portrait|stares|looking|peaceful|soft|empty|aftermath|mourn",
    re.I,
)
PERSON = re.compile(r"child|person|people|man|woman|face|crowd|figure", re.I)


# ---------------------------------------------------------------- dataclasses
@dataclass
class Segment:
    source: str
    path: str
    start_s: float
    end_s: float
    text: str
    mood: str
    arousal: float = 0.5
    motion: float = 0.0  # raw luma-diff mean, normalized later

    @property
    def dur_s(self) -> float:
        return self.end_s - self.start_s


@dataclass
class Slot:
    index: int
    t_start: float  # position in the OUTPUT (== music window) timeline
    dur_s: float
    energy: float  # 0..1 local music energy
    role: str = "body"  # opening | body | hero | closing
    pick: Optional[Segment] = None
    seg_in_s: float = 0.0
    why: str = ""


# ---------------------------------------------------------------- audio plan
def build_audio_plan(track: Path, window_s: float) -> dict:
    import librosa

    y, sr = librosa.load(str(track), sr=22050, mono=True)
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    rms = librosa.feature.rms(y=y)[0]
    rms_t = librosa.frames_to_time(np.arange(len(rms)), sr=sr)

    def energy_at(t: float) -> float:
        i = int(np.searchsorted(rms_t, t))
        i = min(max(i, 0), len(rms) - 1)
        return float(rms[i])

    t0 = float(beat_times[0]) if len(beat_times) else 0.0
    beats = [float(b) for b in beat_times if t0 <= b <= t0 + window_s]
    e = np.array([energy_at(b) for b in beats])
    e_norm = (e - e.min()) / (e.max() - e.min() + 1e-9)
    return {
        "tempo_bpm": round(float(np.atleast_1d(tempo)[0]), 1),
        "window_start_s": t0,
        "beats": beats,
        "beat_energy": [round(float(x), 4) for x in e_norm],
    }


def build_slots(plan: dict) -> list[Slot]:
    """Denser cuts where the music is hotter: 4 beats (low) / 2 / 1 (peak)."""

    beats, energy = plan["beats"], plan["beat_energy"]
    q1, q2 = np.quantile(energy, 0.33), np.quantile(energy, 0.66)
    slots: list[Slot] = []
    i = 0
    while i < len(beats) - 1:
        e = energy[i]
        step = 4 if e < q1 else (2 if e < q2 else 1)
        j = min(i + step, len(beats) - 1)
        dur = beats[j] - beats[i]
        if dur < 0.30:  # merge ultra-short tail slots
            j = min(i + step + 1, len(beats) - 1)
            dur = beats[j] - beats[i]
        if dur <= 0:
            break
        slots.append(Slot(index=len(slots), t_start=beats[i] - beats[0],
                          dur_s=dur, energy=float(np.mean(energy[i:j]))))
        i = j
    if slots:
        slots[0].role = "opening"
        slots[-1].role = "closing"
        hero = max(slots[1:-1], key=lambda s: s.energy, default=None)
        if hero:
            hero.role = "hero"
    return slots


# ---------------------------------------------------------------- segments
def load_segments(manifest: dict) -> list[Segment]:
    segs: list[Segment] = []
    for src in manifest["sources"]:
        obs = json.loads(Path(src["observation"]).read_text())
        for sc in obs.get("scenes") or []:
            start = float(sc.get("start_timestamp") or 0)
            end = float(sc.get("end_timestamp") or 0)
            if end - start < 0.6:
                continue
            text = " ".join(
                str(sc.get(k) or "") for k in ("visual_summary", "key_actions")
            )
            segs.append(Segment(
                source=src["name"], path=src["path"], start_s=start, end_s=end,
                text=text, mood=str(sc.get("mood") or ""),
                arousal=arousal_score(text + " " + str(sc.get("mood") or "")),
            ))
    return segs


def arousal_score(text: str) -> float:
    hi = len(HIGH_AROUSAL.findall(text))
    lo = len(LOW_AROUSAL.findall(text))
    if hi == lo == 0:
        return 0.5
    return round(hi / (hi + lo), 3)


def measure_motion(seg: Segment) -> float:
    """Mean luma frame-difference over (max 3s of) the segment, downscaled."""

    dur = min(seg.dur_s, 3.0)
    cmd = [
        "ffmpeg", "-v", "info", "-ss", f"{seg.start_s:.3f}", "-t", f"{dur:.3f}",
        "-i", seg.path,
        "-vf", "scale=160:-2,fps=10,tblend=all_mode=difference,signalstats,"
               "metadata=print:key=lavfi.signalstats.YAVG:file=-",
        "-an", "-f", "null", "-",
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    vals = [float(m) for m in re.findall(r"YAVG=([0-9.]+)", out.stdout or "")]
    return round(statistics.fmean(vals), 3) if vals else 0.0


# ---------------------------------------------------------------- selection
def select(slots: list[Slot], segments: list[Segment], rng: random.Random,
           mode: str) -> None:
    max_per_source = max(2, int(len(slots) * MAX_SOURCE_SHARE))
    used: set[int] = set()
    per_source: dict[str, int] = {}
    last_source: list[str] = []

    motions = [s.motion for s in segments] or [0.0]
    m_lo, m_hi = min(motions), max(motions)

    def m_norm(seg: Segment) -> float:
        return (seg.motion - m_lo) / (m_hi - m_lo + 1e-9)

    for slot in slots:
        candidates = [
            (k, seg) for k, seg in enumerate(segments)
            if k not in used
            and seg.dur_s >= slot.dur_s + MIN_SEG_HEADROOM_S
            and per_source.get(seg.source, 0) < max_per_source
        ]
        if not candidates:  # relax: allow reuse of sources, then of segments
            candidates = [(k, s) for k, s in enumerate(segments)
                          if k not in used and s.dur_s >= slot.dur_s + MIN_SEG_HEADROOM_S]
        if not candidates:
            candidates = [(k, s) for k, s in enumerate(segments)
                          if s.dur_s >= slot.dur_s + MIN_SEG_HEADROOM_S]
        if not candidates:
            raise RuntimeError(f"no segment long enough for slot {slot.index} ({slot.dur_s:.2f}s)")

        if mode == "random":
            k, seg = rng.choice(candidates)
            slot.why = "random baseline"
        else:
            def score(seg: Segment) -> float:
                fit = 1.0 - abs(seg.arousal - slot.energy)
                mot = m_norm(seg) if slot.energy >= 0.5 else 1.0 - m_norm(seg)
                novelty = 0.0 if seg.source in last_source[-2:] else 1.0
                s = 0.45 * fit + 0.35 * mot + 0.20 * novelty
                if slot.role == "opening":
                    s += 0.25 * (1.0 if PERSON.search(seg.text) else 0.0) \
                         - 0.20 * abs(seg.arousal - 0.5)
                elif slot.role == "hero":
                    s += 0.30 * m_norm(seg)
                elif slot.role == "closing":
                    s += 0.30 * (1.0 - seg.arousal)
                return s

            k, seg = max(candidates, key=lambda kv: score(kv[1]))
            slot.why = (f"role={slot.role} energy={slot.energy:.2f} "
                        f"arousal={seg.arousal:.2f} motion={m_norm(seg):.2f} "
                        f"mood='{seg.mood[:40]}'")
        slot.pick = seg
        slot.seg_in_s = seg.start_s + max(0.0, (seg.dur_s - slot.dur_s) / 2)
        used.add(k)
        per_source[seg.source] = per_source.get(seg.source, 0) + 1
        last_source.append(seg.source)


# ---------------------------------------------------------------- assembly
def render(slots: list[Slot], plan: dict, track: Path, workdir: Path,
           label: str) -> Path:
    segdir = workdir / f"segs_{label}"
    segdir.mkdir(parents=True, exist_ok=True)
    listfile = segdir / "concat.txt"
    lines = []
    # Quantize each slot to whole frames while keeping CUMULATIVE drift <1 frame.
    t_target = 0.0
    t_actual = 0.0
    for slot in slots:
        t_target += slot.dur_s
        frames = round((t_target - t_actual) * FPS)
        dur = frames / FPS
        t_actual += dur
        out = segdir / f"slot{slot.index:03d}.mp4"
        vf = (f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
              f"crop={OUT_W}:{OUT_H},fps={FPS},setsar=1,format=yuv420p")
        cmd = ["ffmpeg", "-y", "-v", "error",
               "-ss", f"{slot.seg_in_s:.3f}", "-i", slot.pick.path,
               "-t", f"{dur:.4f}", "-vf", vf, "-frames:v", str(frames),
               "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
               "-video_track_timescale", "15360", str(out)]
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        lines.append(f"file '{out.name}'")
        slot.dur_render_s = dur  # type: ignore[attr-defined]
    listfile.write_text("\n".join(lines) + "\n")

    silent = workdir / f"{label}_silent.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", str(listfile), "-c", "copy", str(silent)],
                   check=True, capture_output=True, timeout=300)

    final = workdir / f"{label}.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-i", str(silent),
                    "-ss", f"{plan['window_start_s']:.3f}", "-i", str(track),
                    "-map", "0:v", "-map", "1:a",
                    "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                    "-shortest", str(final)],
                   check=True, capture_output=True, timeout=300)
    return final


# ---------------------------------------------------------------- report
def cut_report(slots: list[Slot], plan: dict, rendered: Path) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
    from EdennCode.Util.MediaUtils.ffmpeg_utils import detect_scene_cuts, get_video_duration

    beats = np.array(plan["beats"]) - plan["beats"][0]
    planned_cuts = []
    t = 0.0
    for slot in slots[:-1]:
        t += getattr(slot, "dur_render_s", slot.dur_s)
        planned_cuts.append(t)
    detected = detect_scene_cuts(rendered)
    offsets_beat = [float(np.min(np.abs(beats - c))) * 1000 for c in planned_cuts]
    offsets_detect = []
    for c in planned_cuts:
        if len(detected):
            offsets_detect.append(min(abs(d - c) for d in detected) * 1000)
    return {
        "duration_s": round(get_video_duration(rendered), 3),
        "n_slots": len(slots),
        "planned_cut_to_beat_ms": {
            "median": round(statistics.median(offsets_beat), 1),
            "p95": round(float(np.quantile(offsets_beat, 0.95)), 1),
        },
        "detected_cuts": len(detected),
        "detected_to_planned_ms": {
            "median": round(statistics.median(offsets_detect), 1) if offsets_detect else None,
            "p95": round(float(np.quantile(offsets_detect, 0.95)), 1) if offsets_detect else None,
        },
    }


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--window-s", type=float, default=45.0)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    workdir = Path(args.workdir)
    manifest = json.loads((workdir / "manifest.json").read_text())
    rng = random.Random(args.seed)

    print("[1/5] audio plan (librosa beat grid)")
    plan = build_audio_plan(Path(manifest["music"]), args.window_s)
    slots = build_slots(plan)
    print(f"      tempo={plan['tempo_bpm']}bpm beats={len(plan['beats'])} slots={len(slots)}")

    print("[2/5] segments from stored observations")
    segments = load_segments(manifest)
    print(f"      {len(segments)} usable scene-segments from {len(manifest['sources'])} sources")

    print("[3/5] motion proxies (ffmpeg luma-diff)")
    for seg in segments:
        seg.motion = measure_motion(seg)

    print("[4/5] select + render: planned")
    select(slots, segments, rng, mode="planned")
    planned = render(slots, plan, Path(manifest["music"]), workdir, "planned")
    timeline = [{
        "slot": s.index, "t": round(s.t_start, 3), "dur": round(getattr(s, "dur_render_s", s.dur_s), 3),
        "role": s.role, "energy": round(s.energy, 3),
        "source": s.pick.source, "in": round(s.seg_in_s, 3),
        "out": round(s.seg_in_s + s.dur_s, 3), "why": s.why,
    } for s in slots]
    (workdir / "timeline.json").write_text(json.dumps(timeline, indent=2))

    print("[5/5] select + render: random baseline")
    slots_b = build_slots(plan)
    select(slots_b, segments, rng, mode="random")
    baseline = render(slots_b, plan, Path(manifest["music"]), workdir, "random_baseline")

    report = {
        "audio_plan": {k: plan[k] for k in ("tempo_bpm", "window_start_s")},
        "planned": cut_report(slots, plan, planned),
        "random_baseline": cut_report(slots_b, plan, baseline),
        "outputs": {"planned": str(planned), "random_baseline": str(baseline),
                    "timeline": str(workdir / "timeline.json")},
    }
    (workdir / "w0_report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
