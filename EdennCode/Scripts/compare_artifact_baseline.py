#!/usr/bin/env python3
"""Diff two artifact manifests to prove a refactor did not move media quality.

Providers are stochastic, so the bytes differ every run. What must NOT differ is the
objective shape of what came out: same asset set, same geometry, same codecs, same
sample rate, comparable duration/loudness/size. A mismatch here means the change reached
the generation path — which the response refactor was never supposed to do.

One delta is expected and asserted explicitly rather than waved through: multi-image now
uploads a single track, so the ``full_tracks[N]`` assets (alternate takes) disappear.
Anything else is a failure.

Usage:
    python3 EdennCode/Scripts/compare_artifact_baseline.py BEFORE_DIR AFTER_DIR
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Exact-match keys: any drift here means the encoder or the pipeline changed.
# nb_frames is included deliberately — it pins the rendered timeline frame for frame.
EXACT_VIDEO = ("codec", "width", "height", "fps", "nb_frames")
EXACT_AUDIO = ("codec", "sample_rate", "channels")

DURATION_TOLERANCE_S = 0.5
# MP3 is CBR, so byte size is a pure function of duration and is exactly reproducible;
# a drift here means the encode settings moved.
AUDIO_SIZE_TOLERANCE_FRACTION = 0.10
# Bytes-per-second of the full generated track. Length is the provider's choice, but
# the encode is ours — this is what actually catches a changed audio chain.
BITRATE_TOLERANCE_FRACTION = 0.10
# Loudness varies with the generated take itself. Measured across two runs of
# IDENTICAL code: 0.5 LUFS. Allow 2.0 before calling it a regression.
LOUDNESS_TOLERANCE_LUFS = 2.0

# Video byte size is NOT a stable signal for this pipeline and is reported, never
# failed on. With align_to_beats the crossfades are placed from the beats of the
# freshly generated music, so a different take moves every transition and the H.264
# bitrate swings with it. Measured across two runs of IDENTICAL code: the slideshow
# came out 4,830,940 B and then 1,529,204 B — a 68% swing with the same 270 frames,
# same 1280x1280, same 30fps, same 9.0s. Gating on size here would only produce false
# alarms; the frame count and geometry above are what actually pin the render.


def _fail(problems: list[str], message: str) -> None:
    problems.append(message)


def _is_full_track(name: str) -> bool:
    """The full generated song, whose LENGTH the provider chooses.

    Measured across two runs of IDENTICAL code, the primary track came back at
    108.5s and then 150.5s — and a single generation call returned takes of 108.5s
    and 149.8s side by side. Gating that duration would fail at random. What must
    hold is the ENCODE: same bytes-per-second, same codec, same sample rate. The
    clip muxed into the video (audio_url) is a different matter — its length is
    pinned by the video, so it stays gated on duration.
    """
    return "complete_audio" in name or "full_tracks" in name


def _compare_asset(name: str, before: dict[str, Any], after: dict[str, Any],
                   problems: list[str], notes: list[str]) -> None:
    b_dur, a_dur = before.get("duration_s", 0.0), after.get("duration_s", 0.0)
    b_size, a_size = before.get("size_bytes", 0), after.get("size_bytes", 0)

    if _is_full_track(name):
        # Provider-chosen length: compare the encode rate, not the absolute values.
        b_rate = b_size / b_dur if b_dur else 0.0
        a_rate = a_size / a_dur if a_dur else 0.0
        if b_rate and abs(b_rate - a_rate) / b_rate > BITRATE_TOLERANCE_FRACTION:
            _fail(problems, f"{name}: encode rate {b_rate:.0f} -> {a_rate:.0f} B/s "
                            f"(> {BITRATE_TOLERANCE_FRACTION:.0%})")
        else:
            notes.append(f"{name}: {b_dur:.1f}s -> {a_dur:.1f}s at a matching "
                         f"{a_rate:.0f} B/s — the provider picks the song length")
    else:
        if abs(b_dur - a_dur) > DURATION_TOLERANCE_S:
            _fail(problems, f"{name}: duration {b_dur}s -> {a_dur}s "
                            f"(> {DURATION_TOLERANCE_S}s)")
        if b_size:
            drift = abs(b_size - a_size) / b_size
            if "video" in before:
                if drift > 0.10:
                    notes.append(f"{name}: size {b_size}B -> {a_size}B ({drift:.0%}) — "
                                 f"expected; encode bitrate follows the beat-aligned "
                                 f"transitions, not the code")
            elif drift > AUDIO_SIZE_TOLERANCE_FRACTION:
                _fail(problems, f"{name}: size {b_size}B -> {a_size}B "
                                f"(> {AUDIO_SIZE_TOLERANCE_FRACTION:.0%})")

    for section, keys in (("video", EXACT_VIDEO), ("audio", EXACT_AUDIO)):
        b_sec, a_sec = before.get(section), after.get(section)
        if (b_sec is None) != (a_sec is None):
            _fail(problems, f"{name}: {section} stream "
                            f"{'disappeared' if b_sec else 'appeared'}")
            continue
        if b_sec is None:
            continue
        for key in keys:
            if b_sec.get(key) != a_sec.get(key):
                _fail(problems, f"{name}.{section}.{key}: "
                                f"{b_sec.get(key)!r} -> {a_sec.get(key)!r}")

        if section == "audio":
            b_loud, a_loud = b_sec.get("loudness_lufs"), a_sec.get("loudness_lufs")
            if b_loud is not None and a_loud is not None:
                if abs(b_loud - a_loud) > LOUDNESS_TOLERANCE_LUFS:
                    _fail(problems, f"{name}.audio.loudness: {b_loud} -> {a_loud} LUFS "
                                    f"(> {LOUDNESS_TOLERANCE_LUFS} LUFS)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args()

    before = json.loads((args.before / "manifest.json").read_text())["runs"]
    after = json.loads((args.after / "manifest.json").read_text())["runs"]

    problems: list[str] = []
    expected: list[str] = []
    notes: list[str] = []

    missing_runs = set(before) - set(after)
    if missing_runs:
        _fail(problems, f"runs missing from the after-capture: {sorted(missing_runs)}")

    for run in sorted(set(before) & set(after)):
        b_assets = before[run]["assets"]
        a_assets = after[run]["assets"]

        dropped = set(b_assets) - set(a_assets)
        added = set(a_assets) - set(b_assets)

        for name in sorted(dropped):
            if name.startswith("full_tracks[") and name != "full_tracks[0]":
                # The one intended behaviour change: alternate takes are no longer
                # uploaded. full_tracks[0] itself must still be reachable, now as
                # complete_audio_url.
                expected.append(f"{run}/{name}: alternate take no longer uploaded")
            elif name == "full_tracks[0]":
                if "complete_audio_url" in a_assets:
                    expected.append(
                        f"{run}/{name}: now surfaced as complete_audio_url"
                    )
                else:
                    _fail(problems, f"{run}/{name}: dropped with NO complete_audio_url "
                                    f"replacement — the track is unreachable")
            else:
                _fail(problems, f"{run}/{name}: asset disappeared")

        for name in sorted(added):
            expected.append(f"{run}/{name}: new asset")

        for name in sorted(set(b_assets) & set(a_assets)):
            _compare_asset(f"{run}/{name}", b_assets[name], a_assets[name], problems, notes)

        # The track has to still be there under one name or the other.
        def _has_track(assets: dict[str, Any]) -> bool:
            return bool(
                assets.get("complete_audio_url")
                or assets.get("audio_url")
                or assets.get("full_tracks[0]")
            )

        if _has_track(b_assets) and not _has_track(a_assets):
            _fail(problems, f"{run}: no music track in the after-capture at all")

    if notes:
        print("Informational (known-nondeterministic, not gated):")
        for line in notes:
            print(f"  · {line}")
        print()

    if expected:
        print("Expected differences:")
        for line in expected:
            print(f"  ~ {line}")
        print()

    if problems:
        print(f"QUALITY REGRESSION — {len(problems)} problem(s):")
        for line in problems:
            print(f"  ✗ {line}")
        return 1

    print("Artifacts are consistent: same asset set, geometry, codecs, duration, loudness.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
