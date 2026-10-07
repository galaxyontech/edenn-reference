"""
A/B comparison: baseline alignment pipeline vs. improved alignment pipeline.

Baseline  — onset-only anchors, fixed sigma=0.18s, plain motion detection
Improved  — beat grid anchors, tempo-adaptive sigma, histogram cut detection,
            dedup penalty for multiply-claimed music events

Run:
    python -m EdennCode.WorkflowFactory.VideoAudioAlignmentWorkflow.Testing.run_alignment_ab_test

Optional env vars:
    ALIGN_AB_VIDEO   path to a local video file (defaults to smoke asset)
    ALIGN_AB_MUSIC   path to a local music file (defaults to cn_vocal_clone_sample_30s.m4a)
    ALIGN_AB_OUTDIR  directory to save rendered audio (defaults to ~/Desktop/edenn_ab_test)
"""

from __future__ import annotations

import math
import os
import struct
import sys
import tempfile
import wave
from pathlib import Path
from typing import List

import numpy as np

# ---------------------------------------------------------------------------
# Synthetic music generation
# ---------------------------------------------------------------------------

def _make_synthetic_music(path: Path, *, duration_s: float = 60.0, bpm: float = 120.0, sr: int = 22050) -> None:
    """
    Synthesise a music track with a clear beat grid for testing purposes.

    Structure:
      0–10 s   build-up  (rising amplitude)
      10–50 s  full energy
      50–60 s  fade-out  (falling amplitude)

    Sonic content:
      - 220 Hz + 330 Hz sine wave (harmonic background)
      - Kick impulse on every beat
      - Hi-hat impulse on every half-beat (quieter)
    """
    samples = int(duration_s * sr)
    t = np.linspace(0, duration_s, samples, endpoint=False)

    signal = 0.25 * np.sin(2 * np.pi * 220 * t) + 0.10 * np.sin(2 * np.pi * 330 * t)

    beat_period = 60.0 / bpm
    for beat_n in range(int(duration_s / beat_period) + 1):
        for subdivision, amp in [(0, 0.80), (0.5, 0.30)]:
            bt = beat_n * beat_period + subdivision * beat_period
            idx = int(bt * sr)
            if idx >= samples:
                continue
            impulse_len = min(int(0.04 * sr), samples - idx)
            decay = np.exp(-np.arange(impulse_len) / (0.008 * sr))
            signal[idx: idx + impulse_len] += amp * decay

    envelope = np.empty(samples)
    for i, ti in enumerate(t):
        if ti < 10.0:
            envelope[i] = 0.20 + 0.80 * (ti / 10.0)
        elif ti < 50.0:
            envelope[i] = 1.00
        else:
            envelope[i] = max(0.10, 1.0 - 0.90 * ((ti - 50.0) / 10.0))

    signal = np.clip(signal * envelope, -1.0, 1.0)

    with wave.open(str(path), "w") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sr)
        for s in signal:
            wav.writeframes(struct.pack("<h", int(s * 32767)))


# ---------------------------------------------------------------------------
# Baseline and improved matcher factories
# ---------------------------------------------------------------------------

def _make_baseline_matcher():
    from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.music_analyzer import MusicAnalyzer
    from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.video_event_extractor import VideoEventExtractor
    from EdennCode.WorkflowFactory.VideoAudioAlignmentWorkflow.Stages.AudioVideoAlignmentStage.alignment_window_scorer import AlignmentWindowScorer
    from EdennCode.WorkflowFactory.VideoAudioAlignmentWorkflow.Stages.AudioVideoAlignmentStage.audio_video_window_matcher import AudioVideoWindowMatcher

    return AudioVideoWindowMatcher(
        music_analyzer=MusicAnalyzer(hop_length=512, use_beats=False),
        video_extractor=VideoEventExtractor(detect_cuts=False),
        scorer=AlignmentWindowScorer(beat_adaptive_sigma=False, dedup_onset_penalty=False),
    )


def _make_improved_matcher():
    from EdennCode.WorkflowFactory.VideoAudioAlignmentWorkflow.Stages.AudioVideoAlignmentStage.audio_video_window_matcher import AudioVideoWindowMatcher
    # All improved defaults: use_beats=True, detect_cuts=True,
    # beat_adaptive_sigma=True, dedup_onset_penalty=True
    return AudioVideoWindowMatcher()


# ---------------------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------------------

def _fmt_events(per_event: list) -> str:
    lines = []
    for ev in per_event:
        lines.append(
            f"    video_t={ev['video_t']:.3f}s → music_t={ev['nearest_music_event_t']:.3f}s "
            f"Δ={ev['delta_s']*1000:.1f}ms  partial={ev['partial_score']:.4f}"
        )
    return "\n".join(lines) if lines else "    (none)"


def _print_window(label: str, window, *, video_duration_s: float, music_duration_s: float) -> None:
    d = window.details
    print(f"\n{'─'*60}")
    print(f"  {label}")
    print(f"{'─'*60}")
    print(f"  Music window : {window.music_start_s:.3f}s → {window.music_end_s:.3f}s")
    print(f"  (out of {music_duration_s:.1f}s track, video is {video_duration_s:.1f}s)")
    print(f"  Total score  : {window.score:.5f}")
    print(f"  align_score  : {d.get('align_score', 0):.5f}  (w=1.20)")
    print(f"  shape_score  : {d.get('shape_score', 0):.5f}  (w=1.00)")
    print(f"  lyric_score  : {d.get('lyric_score', 0):.5f}  (w=0.35)")
    if "sigma_s" in d:
        print(f"  sigma        : {d['sigma_s']*1000:.1f}ms  @  tempo={d.get('tempo_bpm',120):.1f} BPM")
    print(f"  Per-event alignment:")
    print(_fmt_events(d.get("per_event_alignment", [])))


def _mean_delta_ms(window) -> float:
    events = window.details.get("per_event_alignment", [])
    if not events:
        return float("nan")
    return 1000.0 * sum(e["delta_s"] for e in events) / len(events)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    video_path_env = os.getenv("ALIGN_AB_VIDEO")
    music_path_env = os.getenv("ALIGN_AB_MUSIC")

    out_dir_env = os.getenv("ALIGN_AB_OUTDIR")
    out_dir = Path(out_dir_env).expanduser() if out_dir_env else Path.home() / "Desktop" / "edenn_ab_test"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Resolve video
    if video_path_env:
        video_path = Path(video_path_env)
    else:
        from EdennCode.TestSuites.helpers.paths import SMOKE_VIDEO_PATH
        video_path = SMOKE_VIDEO_PATH

    if not video_path.exists():
        print(f"[ERROR] Video not found: {video_path}", file=sys.stderr)
        sys.exit(1)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        # Resolve music — prefer the real 30s vocal sample, fall back to synthetic
        if music_path_env:
            music_path = Path(music_path_env)
            if not music_path.exists():
                print(f"[ERROR] Music not found: {music_path}", file=sys.stderr)
                sys.exit(1)
            generated = False
        else:
            from EdennCode.TestSuites.helpers.paths import SMOKE_AUDIO_DIR
            candidate = SMOKE_AUDIO_DIR / "cn_vocal_clone_sample_30s.m4a"
            if candidate.exists():
                music_path = candidate
                generated = False
            else:
                music_path = tmp_dir / "synthetic_120bpm.wav"
                print("Generating synthetic 120 BPM music (60 s)…")
                _make_synthetic_music(music_path, duration_s=60.0, bpm=120.0)
                generated = True

        print(f"\nVideo : {video_path}")
        print(f"Music : {music_path}  {'[synthetic]' if generated else '[provided]'}")

        # Determine video duration for display
        from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.media_tools import MediaTools
        video_dur = MediaTools.duration_seconds(str(video_path))
        music_dur = MediaTools.duration_seconds(str(music_path))

        print(f"\nVideo duration : {video_dur:.2f}s")
        print(f"Music duration : {music_dur:.2f}s")

        # ---- Baseline ----
        print("\n[1/2] Running BASELINE matcher…")
        baseline_matcher = _make_baseline_matcher()
        baseline_best, baseline_top = baseline_matcher.match(
            video_path=str(video_path),
            music_path=str(music_path),
            topk=3,
        )

        # ---- Improved ----
        print("[2/2] Running IMPROVED matcher…")
        improved_matcher = _make_improved_matcher()
        improved_best, improved_top = improved_matcher.match(
            video_path=str(video_path),
            music_path=str(music_path),
            topk=3,
        )

    # ---- Render audio windows ----
    from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.audio_window_renderer import AudioWindowRenderer
    renderer = AudioWindowRenderer(audio_codec="pcm_s16le")

    baseline_wav = out_dir / "baseline_aligned.wav"
    improved_wav = out_dir / "improved_aligned.wav"

    print(f"\nRendering audio windows to {out_dir} …")
    renderer.render_audio(
        music_path=str(music_path),
        music_start_s=baseline_best.music_start_s,
        duration_s=video_dur,
        out_path=str(baseline_wav),
    )
    renderer.render_audio(
        music_path=str(music_path),
        music_start_s=improved_best.music_start_s,
        duration_s=video_dur,
        out_path=str(improved_wav),
    )
    print(f"  baseline_aligned.wav  → t={baseline_best.music_start_s:.3f}s")
    print(f"  improved_aligned.wav  → t={improved_best.music_start_s:.3f}s")

    # ---- Report ----
    print("\n" + "═" * 60)
    print("  A/B ALIGNMENT COMPARISON")
    print("═" * 60)

    _print_window("BASELINE  (onset-only, fixed σ=180ms, motion-only)", baseline_best,
                  video_duration_s=video_dur, music_duration_s=music_dur)
    _print_window("IMPROVED  (beat grid, adaptive σ, cut detection, dedup)", improved_best,
                  video_duration_s=video_dur, music_duration_s=music_dur)

    base_delta = _mean_delta_ms(baseline_best)
    impr_delta = _mean_delta_ms(improved_best)

    print("\n" + "═" * 60)
    print("  SUMMARY")
    print("═" * 60)

    score_delta = improved_best.score - baseline_best.score
    score_sign = "↑" if score_delta >= 0 else "↓"
    print(f"  Score change          : {score_sign} {abs(score_delta):.5f} ({improved_best.score:.5f} vs {baseline_best.score:.5f})")

    if not math.isnan(base_delta) and not math.isnan(impr_delta):
        delta_delta = impr_delta - base_delta
        delta_sign = "↓" if delta_delta <= 0 else "↑"
        print(f"  Mean event Δ change   : {delta_sign} {abs(delta_delta):.1f}ms ({impr_delta:.1f}ms vs {base_delta:.1f}ms)")

    window_moved = abs(improved_best.music_start_s - baseline_best.music_start_s) > 0.01
    if window_moved:
        print(f"  Window position       : DIFFERENT  "
              f"(baseline={baseline_best.music_start_s:.3f}s, improved={improved_best.music_start_s:.3f}s)")
    else:
        print(f"  Window position       : same  ({improved_best.music_start_s:.3f}s)")

    if generated:
        print(f"\n  [info] Synthetic music has beats every {60.0/120.0*1000:.0f}ms.")
        impr_sigma = improved_best.details.get("sigma_s")
        if impr_sigma:
            print(f"  [info] Improved sigma={impr_sigma*1000:.1f}ms was tuned to ¼ beat-period.")

    print(f"\n  Rendered audio saved to:")
    print(f"    {baseline_wav}")
    print(f"    {improved_wav}")

    print("\nTop-3 BASELINE windows:")
    for w in baseline_top:
        print(f"  t0={w.music_start_s:.3f}s  score={w.score:.5f}")
    print("Top-3 IMPROVED windows:")
    for w in improved_top:
        print(f"  t0={w.music_start_s:.3f}s  score={w.score:.5f}")


if __name__ == "__main__":
    main()
