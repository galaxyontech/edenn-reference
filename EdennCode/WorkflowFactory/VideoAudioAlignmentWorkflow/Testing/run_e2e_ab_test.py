"""
E2E A/B test: full video-music pipeline on a real repo video, then compare
baseline vs improved alignment windows on the actual generated music.

The pipeline runs once (music generation costs one API call).
Both matchers then run on the same full generated track so the comparison
is apples-to-apples on identical audio.

Usage:
    python -m EdennCode.WorkflowFactory.VideoAudioAlignmentWorkflow.Testing.run_e2e_ab_test \\
        --video EdennCode/TestSuites/assets/production/videos/<file>.mp4 \\
        --prompt "upbeat cinematic" \\
        --modelspec edenn_studio \\
        --out-dir /tmp/edenn_ab_test

Output directory (`--out-dir` or `$ALIGN_AB_OUTDIR`, else `~/Desktop/edenn_ab_test/e2e_<timestamp>/`):
    pipeline_improved.mp4    full pipeline output (improved matcher already applied)
    baseline_matched.wav     baseline matcher applied to the same full track
    improved_matched.wav     improved matcher window as .wav (same as pipeline_improved.mp4 audio)
    baseline_remixed.mp4     video remixed with baseline-aligned audio
    report.txt               score / delta / timing comparison
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
import time
from pathlib import Path
from shutil import copy2
from typing import List, Optional

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_baseline_matcher():
    from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.music_analyzer import MusicAnalyzer
    from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.video_event_extractor import VideoEventExtractor
    from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.window_scorer import WindowScorer
    from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.matching import EdennMusicWindowMatcher

    return EdennMusicWindowMatcher(
        music_analyzer=MusicAnalyzer(hop_length=512, use_beats=False),
        video_extractor=VideoEventExtractor(detect_cuts=False),
        scorer=WindowScorer(beat_adaptive_sigma=False, dedup_onset_penalty=False),
    )


def _make_improved_matcher():
    from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.matching import EdennMusicWindowMatcher
    return EdennMusicWindowMatcher()  # all improved defaults


def _fmt_events(per_event: list) -> str:
    if not per_event:
        return "    (no events)"
    return "\n".join(
        f"    video_t={e['video_t']:.3f}s → music_t={e['nearest_music_event_t']:.3f}s "
        f"Δ={e['delta_s']*1000:.1f}ms  partial={e['partial_score']:.4f}"
        for e in per_event
    )


def _mean_delta_ms(window) -> float:
    evs = window.details.get("per_event_alignment", [])
    if not evs:
        return float("nan")
    return 1000.0 * sum(e["delta_s"] for e in evs) / len(evs)


def _write_report(path: Path, sections: List[str]) -> None:
    path.write_text("\n".join(sections), encoding="utf-8")


def _default_output_root() -> Path:
    configured = os.getenv("ALIGN_AB_OUTDIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Desktop" / "edenn_ab_test"


def _resolve_full_track(result) -> tuple[Optional[Path], str]:
    preferred = []
    if getattr(result, "matching_used_track", None) == "secondary":
        preferred = [
            "secondary_complete_generated_music_path",
            "complete_generated_music_path",
        ]
    else:
        preferred = [
            "complete_generated_music_path",
            "secondary_complete_generated_music_path",
        ]

    for attr in preferred:
        value = getattr(result, attr, None)
        if value is None:
            continue
        candidate = Path(value)
        if candidate.exists():
            return candidate, attr
    return None, "none"


def _normalize_word_ts_to_seconds(words, *, reference_duration_s: float):
    from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.word_ts import WordTS

    if not words:
        return []

    max_end = max(float(getattr(word, "endS", 0.0)) for word in words)
    scale = 0.001 if max_end > max(reference_duration_s * 3.0, 1000.0) else 1.0
    normalized = []
    for word in words:
        normalized.append(
            WordTS(
                text=getattr(word, "text", ""),
                startS=float(getattr(word, "startS", 0.0)) * scale,
                endS=float(getattr(word, "endS", 0.0)) * scale,
                i=getattr(word, "i", None),
            )
        )
    return normalized


def _resolve_full_track_lyrics(result, *, reference_duration_s: float):
    if getattr(result, "matching_used_track", None) == "secondary":
        preferred = [
            "secondary_full_word_level_lyrics_timestamps",
            "secondary_full_lyrics_timestamps",
            "primary_full_word_level_lyrics_timestamps",
            "primary_full_lyrics_timestamps",
        ]
    else:
        preferred = [
            "primary_full_word_level_lyrics_timestamps",
            "primary_full_lyrics_timestamps",
            "secondary_full_word_level_lyrics_timestamps",
            "secondary_full_lyrics_timestamps",
        ]

    for attr in preferred:
        words = list(getattr(result, attr, []) or [])
        if words:
            return _normalize_word_ts_to_seconds(
                words,
                reference_duration_s=reference_duration_s,
            ), attr
    return [], "none"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="E2E alignment A/B test on a real generated music track."
    )
    parser.add_argument(
        "--video",
        type=Path,
        default=Path(
            "EdennCode/TestSuites/assets/production/videos/"
            "sample_clip.mp4"
        ),
        help="Input video (defaults to 31s production asset)",
    )
    parser.add_argument("--prompt", default="upbeat cinematic background music")
    parser.add_argument(
        "--modelspec",
        default="edenn_studio",
        choices=["edenn_basic", "edenn_enhanced", "edenn_studio"],
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Optional root directory for saved A/B artifacts.",
    )
    parser.add_argument("--include-vocals", action="store_true")
    parser.add_argument("--vocal-gender", default="female")
    args = parser.parse_args()

    video_path = args.video.expanduser().resolve()
    if not video_path.exists():
        print(f"[ERROR] Video not found: {video_path}", file=sys.stderr)
        sys.exit(1)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    out_root = args.out_dir.expanduser() if args.out_dir else _default_output_root()
    out_dir = out_root / f"e2e_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Video      : {video_path}")
    print(f"Prompt     : {args.prompt}")
    print(f"Model      : {args.modelspec}")
    print(f"Output dir : {out_dir}")
    print()

    # ---- Step 1: Run full pipeline (uses improved matcher internally) ----
    print("=" * 60)
    print("Step 1/4 — Running full E2E pipeline (real music generation)…")
    print("=" * 60)
    pipeline_start = time.time()

    from EdennCode.WorkflowFactory.VideoMusicWorkflow.run_video_music_workflow import run_video_music_pipeline
    result = run_video_music_pipeline(
        video_path=video_path,
        user_prompt=args.prompt,
        modelspec=args.modelspec,
        include_vocals=args.include_vocals,
        vocal_gender=args.vocal_gender,
        local_temp_dir=str(out_dir / "workdir"),
    )
    pipeline_elapsed = time.time() - pipeline_start
    print(f"Pipeline done in {pipeline_elapsed:.1f}s")
    print(f"  Model used        : {result.used_music_model_spec}")
    print(f"  Generated music   : {result.generated_music_path}")
    print(f"  Full track        : {result.complete_generated_music_path}")
    print(f"  Remixed video     : {result.remixed_video_path}")

    # ---- Step 2: Check we have a full track for A/B ----
    full_track, full_track_source = _resolve_full_track(result)
    if full_track is None or not full_track.exists():
        print(
            "\n[WARN] No complete_generated_music_path found "
            f"(model={result.used_music_model_spec} doesn't produce a full track).\n"
            "A/B matching skipped — only pipeline output is saved.",
            file=sys.stderr,
        )
        improved_out = out_dir / "pipeline_improved.mp4"
        copy2(result.remixed_video_path, improved_out)
        print(f"\nSaved: {improved_out}")
        return

    from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.media_tools import MediaTools
    video_dur = result.video_metadata.duration
    music_dur = MediaTools.duration_seconds(str(full_track))

    lyrics, lyrics_source = _resolve_full_track_lyrics(
        result,
        reference_duration_s=music_dur,
    )

    print(f"\n  Full track source : {full_track_source}")
    print(f"  Full track length : {full_track.stat().st_size/1024:.0f} KB")
    print(f"  Lyrics source     : {lyrics_source}")
    print(f"  Lyrics tokens     : {len(lyrics)}")
    print(f"  Video duration    : {video_dur:.2f}s")
    print(f"  Music duration    : {music_dur:.2f}s")
    if not lyrics:
        print("  [WARN] No full-track lyric timestamps found; matcher will compare onset/beat alignment only.")

    # ---- Step 3: Run both matchers on the same full track ----
    print()
    print("=" * 60)
    print("Step 2/4 — Running BASELINE matcher on generated track…")
    print("=" * 60)
    t0 = time.time()
    baseline_matcher = _make_baseline_matcher()
    baseline_best, baseline_top = baseline_matcher.match(
        video_path=str(video_path),
        music_path=str(full_track),
        word_ts=lyrics,
        topk=3,
    )
    baseline_elapsed = time.time() - t0
    print(f"  Done in {baseline_elapsed:.2f}s  →  window start = {baseline_best.music_start_s:.3f}s  score = {baseline_best.score:.5f}")

    print()
    print("=" * 60)
    print("Step 3/4 — Running IMPROVED matcher on generated track…")
    print("=" * 60)
    t0 = time.time()
    improved_matcher = _make_improved_matcher()
    improved_best, improved_top = improved_matcher.match(
        video_path=str(video_path),
        music_path=str(full_track),
        word_ts=lyrics,
        topk=3,
    )
    improved_elapsed = time.time() - t0
    print(f"  Done in {improved_elapsed:.2f}s  →  window start = {improved_best.music_start_s:.3f}s  score = {improved_best.score:.5f}")

    # ---- Step 4: Render audio + remix videos ----
    print()
    print("=" * 60)
    print("Step 4/4 — Rendering audio & remixing videos…")
    print("=" * 60)

    from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.audio_window_renderer import AudioWindowRenderer
    from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoAudioRemixStage.video_audio_remix_stage import (
        VideoAudioRemixStage,
        VideoAudioRemixStageInput,
    )

    renderer = AudioWindowRenderer(audio_codec="pcm_s16le")

    baseline_wav = out_dir / "baseline_matched.wav"
    improved_wav = out_dir / "improved_matched.wav"

    renderer.render_audio(
        music_path=str(full_track),
        music_start_s=baseline_best.music_start_s,
        duration_s=video_dur,
        out_path=str(baseline_wav),
    )
    renderer.render_audio(
        music_path=str(full_track),
        music_start_s=improved_best.music_start_s,
        duration_s=video_dur,
        out_path=str(improved_wav),
    )
    print(f"  baseline_matched.wav  saved")
    print(f"  improved_matched.wav  saved")

    # Remix videos
    remix_stage = VideoAudioRemixStage()

    async def _remix(music_path: Path, tag: str) -> Path:
        stage_in = VideoAudioRemixStageInput(
            music_path=music_path,
            video_metadata=result.video_metadata,
            preserve_original_audio=False,
            music_volume=1.0,
        )
        out = await remix_stage.run(stage_in)
        src = Path(result.video_metadata.temp_folder) / out.remixed_video_path
        dst = out_dir / f"{tag}_remixed.mp4"
        copy2(src, dst)
        return dst

    baseline_video = asyncio.run(_remix(baseline_wav, "baseline"))
    improved_video = asyncio.run(_remix(improved_wav, "improved"))
    print(f"  baseline_remixed.mp4  saved")
    print(f"  improved_remixed.mp4  saved")

    # Also copy the original pipeline output (already improved)
    pipeline_out = out_dir / "pipeline_improved.mp4"
    copy2(result.remixed_video_path, pipeline_out)
    print(f"  pipeline_improved.mp4 saved  (original pipeline output for reference)")

    # ---- Report ----
    base_delta = _mean_delta_ms(baseline_best)
    impr_delta = _mean_delta_ms(improved_best)
    score_diff = improved_best.score - baseline_best.score
    window_diff = abs(improved_best.music_start_s - baseline_best.music_start_s)

    report_lines = [
        "=" * 60,
        "  E2E A/B ALIGNMENT REPORT",
        "=" * 60,
        f"  Video        : {video_path.name}",
        f"  Music model  : {result.used_music_model_spec}",
        f"  Track source : {full_track_source}",
        f"  Lyrics source: {lyrics_source}",
        f"  Prompt       : {args.prompt}",
        f"  Video dur    : {video_dur:.2f}s",
        f"  Music dur    : {music_dur:.2f}s",
        f"  Lyrics tokens: {len(lyrics)}",
        "",
        "─" * 60,
        "  BASELINE  (onset-only, fixed σ=180ms, motion-only)",
        "─" * 60,
        f"  Window         : {baseline_best.music_start_s:.3f}s → {baseline_best.music_end_s:.3f}s",
        f"  Total score    : {baseline_best.score:.5f}",
        f"  align_score    : {baseline_best.details.get('align_score',0):.5f}",
        f"  shape_score    : {baseline_best.details.get('shape_score',0):.5f}",
        f"  lyric_score    : {baseline_best.details.get('lyric_score',0):.5f}",
        f"  Mean event Δ   : {base_delta:.1f}ms",
        "  Per-event alignment:",
        _fmt_events(baseline_best.details.get("per_event_alignment", [])),
        "",
        "─" * 60,
        "  IMPROVED  (beat grid, adaptive σ, cut detection, dedup)",
        "─" * 60,
        f"  Window         : {improved_best.music_start_s:.3f}s → {improved_best.music_end_s:.3f}s",
        f"  Total score    : {improved_best.score:.5f}",
        f"  align_score    : {improved_best.details.get('align_score',0):.5f}",
        f"  shape_score    : {improved_best.details.get('shape_score',0):.5f}",
        f"  lyric_score    : {improved_best.details.get('lyric_score',0):.5f}",
        f"  Sigma          : {improved_best.details.get('sigma_s',0)*1000:.1f}ms  @  {improved_best.details.get('tempo_bpm',0):.1f} BPM",
        f"  Mean event Δ   : {impr_delta:.1f}ms",
        "  Per-event alignment:",
        _fmt_events(improved_best.details.get("per_event_alignment", [])),
        "",
        "─" * 60,
        "  SUMMARY",
        "─" * 60,
        f"  Score change   : {'↑' if score_diff>=0 else '↓'} {abs(score_diff):.5f}",
        f"  Δ change       : {'↓' if (impr_delta-base_delta)<=0 else '↑'} {abs(impr_delta-base_delta):.1f}ms",
        f"  Window shift   : {window_diff:.3f}s  ({'DIFFERENT' if window_diff>0.05 else 'same'})",
        "",
        "  Top-3 BASELINE:",
    ] + [f"    t0={w.music_start_s:.3f}s  score={w.score:.5f}" for w in baseline_top] + [
        "  Top-3 IMPROVED:",
    ] + [f"    t0={w.music_start_s:.3f}s  score={w.score:.5f}" for w in improved_top] + [
        "",
        "─" * 60,
        "  OUTPUT FILES",
        "─" * 60,
        f"  {out_dir / 'baseline_matched.wav'}",
        f"  {out_dir / 'improved_matched.wav'}",
        f"  {out_dir / 'baseline_remixed.mp4'}",
        f"  {out_dir / 'improved_remixed.mp4'}",
        f"  {out_dir / 'pipeline_improved.mp4'}  (original pipeline reference)",
    ]

    report_path = out_dir / "report.txt"
    _write_report(report_path, report_lines)

    print()
    print("\n".join(report_lines))
    print(f"\nReport saved to: {report_path}")
    print(f"\nAll files in: {out_dir}")


if __name__ == "__main__":
    main()
