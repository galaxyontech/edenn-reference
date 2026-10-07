from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Dict, List, Optional, Literal, Sequence, Tuple
import time
import logging

import numpy as np
from EdennCode.Util.MediaUtils.image_utils import get_image_dimensions

logger = logging.getLogger(__name__)

_FFMPEG_HAS_FPS_MODE: Optional[bool] = None


def _truncate_for_log(value: object, *, limit: int = 2000) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated {len(text) - limit} chars]"


def _escape_ffmpeg_path(path: Path) -> str:
    """Escape a path for ffmpeg concat demuxer file directives."""
    return str(path).replace("\\", "\\\\").replace("'", "'\\''")


def _ffmpeg_supports_fps_mode(ffmpeg_bin: str) -> bool:
    """
    Detect whether the current ffmpeg binary supports -fps_mode.
    Cache the result to avoid repeated probing.
    """
    global _FFMPEG_HAS_FPS_MODE
    if _FFMPEG_HAS_FPS_MODE is not None:
        return _FFMPEG_HAS_FPS_MODE
    try:
        probe_cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:size=2x2:rate=1:duration=0.01",
            "-frames:v",
            "1",
            "-fps_mode",
            "vfr",
            "-f",
            "null",
            "-",
        ]
        res = subprocess.run(probe_cmd, capture_output=True)
        _FFMPEG_HAS_FPS_MODE = res.returncode == 0
    except Exception:
        _FFMPEG_HAS_FPS_MODE = False
    return _FFMPEG_HAS_FPS_MODE


try:
    from scenedetect import open_video, SceneManager
    from scenedetect.detectors import AdaptiveDetector, ContentDetector
except Exception:  # pragma: no cover - optional dependency
    open_video = None
    SceneManager = None
    AdaptiveDetector = None
    ContentDetector = None


def _pad_audio_to_picture(video_path: Path) -> str:
    """Filter tail that pads audio out to the picture length — and no further.

    A bare ``apad`` is an endless source, which leaves termination entirely to
    ``-shortest``. How ffmpeg handles an infinite filter input under ``-shortest``
    is version dependent: the n7.1 build these images ship never returns, so the
    worker sits in ``waitpid`` until its lease expires, while a newer local build
    exits immediately on the identical command. Bounding the pad makes the graph
    finite, so the mux terminates on any version and the intent is unchanged —
    short audio must never shorten the delivered picture.

    Returns "" when the duration cannot be read: that drops back to unpadded
    behaviour, where a short track can shorten the delivery. Losing seconds is
    bad; wedging the encoder until the lease expires is worse.
    """

    try:
        duration_s = get_video_duration(video_path)
    except Exception:  # noqa: BLE001
        return ""
    if not duration_s or duration_s <= 0:
        return ""
    return f",apad=whole_dur={duration_s:.3f}"


def _run_ffmpeg_cmd(cmd: List[str], **kwargs) -> subprocess.CompletedProcess:
    cmd = list(cmd)
    is_ffmpeg = bool(cmd) and "ffprobe" not in Path(str(cmd[0])).name
    if is_ffmpeg and "-nostdin" not in cmd:
        # Never let ffmpeg touch stdin. The worker container runs four
        # processes off one supervisor shell, and an ffmpeg that decides to
        # read its (shared, possibly never-closing) stdin blocks with no
        # output and no error — indistinguishable from a hang.
        cmd.insert(1, "-nostdin")
    # Every command here processes bounded media, so unbounded waiting is never
    # right: two production-shaped jobs sat 26 and 94 minutes inside a mux that
    # completes in under a second on the same inputs elsewhere, and each burned
    # its whole lease before dying as LeaseExpiredMaxAttempts. A generous
    # ceiling turns that wedge into a fast, attributable failure. Callers with
    # genuinely long encodes can pass their own timeout.
    kwargs.setdefault("timeout", float(os.getenv("EDENN_FFMPEG_TIMEOUT_S", "1800")))
    cmd_str = " ".join(str(c) for c in cmd)
    logger.info(f"Running ffmpeg: {cmd_str}")
    start = time.perf_counter()
    try:
        res = subprocess.run(cmd, **kwargs)
        dur = time.perf_counter() - start
        logger.info(f"Finished ffmpeg in {dur:.2f}s")
        return res
    except subprocess.TimeoutExpired:
        dur = time.perf_counter() - start
        logger.error(
            "ffmpeg exceeded its %.0fs ceiling and was killed after %.2fs: %s",
            kwargs.get("timeout") or 0.0, dur, cmd_str,
        )
        raise
    except Exception as e:
        dur = time.perf_counter() - start
        logger.error(f"Failed ffmpeg in {dur:.2f}s: {e}")
        raise


def resolve_ffmpeg_binary() -> str:
    """
    Locate ffmpeg via env override or PATH. Raises if not available.
    """
    ffmpeg_bin = os.getenv("FFMPEG_BIN") or shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise FileNotFoundError(
            "ffmpeg not found on PATH. Install it (e.g., brew install ffmpeg) or set FFMPEG_BIN."
        )
    return ffmpeg_bin


def has_video_decode_errors(video_path: Path) -> bool:
    """
    Return True when ffmpeg reports a fatal decode error for the first video stream.

    This is intentionally conservative: callers can use it as a signal to repair a
    file before downstream frame extraction, but a failing scan should not by
    itself make the original file unusable.
    """
    ffmpeg_bin = resolve_ffmpeg_binary()
    source_path = video_path.expanduser().resolve()
    if not source_path.exists():
        raise FileNotFoundError(source_path)

    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-nostats",
        "-v",
        "error",
        "-xerror",
        "-i",
        str(source_path),
        "-map",
        "0:v:0",
        "-f",
        "null",
        "-",
    ]
    try:
        _run_ffmpeg_cmd(cmd, check=True, capture_output=True, text=True)
        return False
    except subprocess.CalledProcessError as exc:
        stderr = (
            exc.stderr.strip()
            if isinstance(exc.stderr, str) and exc.stderr.strip()
            else str(exc)
        )
        logger.warning(
            "Video decode preflight detected errors for %s: %s",
            source_path,
            _truncate_for_log(stderr),
        )
        return True


def mux_image_with_audio(image_path: Path, audio_path: Path, output_path: Path) -> None:
    """
    Use ffmpeg to combine a still image and an audio file into a short MP4 clip.
    """
    ffmpeg_bin = resolve_ffmpeg_binary()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin,
        "-y",
        "-loop",
        "1",
        "-i",
        str(image_path),
        "-i",
        str(audio_path),
        "-c:v",
        "libx264",
        "-tune",
        "stillimage",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        "-movflags", "+faststart",
        str(output_path),
    ]
    _run_ffmpeg_cmd(cmd, check=True)


# Every xfade transition the pipeline accepts, mapped to a short human
# description (used for the public docs and API error messages). The names are
# exactly ffmpeg's built-in ``xfade`` transition set; all of them are available
# in the n7.1 production build and any ffmpeg >= 5.0. ``custom`` is intentionally
# excluded — it needs an ``expr`` and is not a standalone effect.
SUPPORTED_TRANSITIONS: Dict[str, str] = {
    "fade": "Crossfade (dissolve one frame into the next)",
    "fadeblack": "Fade out to black, then in from black",
    "fadewhite": "Fade out to white, then in from white",
    "fadegrays": "Fade through grayscale into the next frame",
    "fadefast": "Crossfade weighted toward a fast start",
    "fadeslow": "Crossfade weighted toward a slow start",
    "dissolve": "Noise-masked pixel dissolve (grainy hand-off)",
    "pixelize": "Mosaic out, then resolve back in",
    "distance": "Distance-field based blend",
    "hblur": "Blur out, swap, blur back in",
    "wipeleft": "Hard-edged wipe moving left",
    "wiperight": "Hard-edged wipe moving right",
    "wipeup": "Hard-edged wipe moving up",
    "wipedown": "Hard-edged wipe moving down",
    "wipetl": "Hard-edged wipe from the top-left corner",
    "wipetr": "Hard-edged wipe from the top-right corner",
    "wipebl": "Hard-edged wipe from the bottom-left corner",
    "wipebr": "Hard-edged wipe from the bottom-right corner",
    "smoothleft": "Soft-edged wipe moving left",
    "smoothright": "Soft-edged wipe moving right",
    "smoothup": "Soft-edged wipe moving up",
    "smoothdown": "Soft-edged wipe moving down",
    "slideleft": "New frame slides in, pushing the old one left",
    "slideright": "New frame slides in, pushing the old one right",
    "slideup": "New frame slides in, pushing the old one up",
    "slidedown": "New frame slides in, pushing the old one down",
    "coverleft": "New frame slides over the old one, moving left",
    "coverright": "New frame slides over the old one, moving right",
    "coverup": "New frame slides over the old one, moving up",
    "coverdown": "New frame slides over the old one, moving down",
    "revealleft": "Old frame slides away left to reveal the new one",
    "revealright": "Old frame slides away right to reveal the new one",
    "revealup": "Old frame slides away up to reveal the new one",
    "revealdown": "Old frame slides away down to reveal the new one",
    "circleopen": "Circular reveal expanding from the center",
    "circleclose": "Circular reveal contracting to the center",
    "circlecrop": "Circular crop closes, then opens on the new frame",
    "rectcrop": "Rectangular crop closes, then opens on the new frame",
    "radial": "Radial clock-wipe around the center",
    "vertopen": "Two vertical halves open outward",
    "vertclose": "Two vertical halves close inward",
    "horzopen": "Two horizontal halves open outward",
    "horzclose": "Two horizontal halves close inward",
    "diagtl": "Diagonal wipe from the top-left",
    "diagtr": "Diagonal wipe from the top-right",
    "diagbl": "Diagonal wipe from the bottom-left",
    "diagbr": "Diagonal wipe from the bottom-right",
    "hlslice": "Horizontal left slice reveal",
    "hrslice": "Horizontal right slice reveal",
    "vuslice": "Vertical up slice reveal",
    "vdslice": "Vertical down slice reveal",
    "hlwind": "Horizontal left wind-streak reveal",
    "hrwind": "Horizontal right wind-streak reveal",
    "vuwind": "Vertical up wind-streak reveal",
    "vdwind": "Vertical down wind-streak reveal",
    "squeezeh": "Horizontal squeeze",
    "squeezev": "Vertical squeeze",
    "zoomin": "Zoom into the new frame",
}

# Curated pool used for weighted random selection: subtle blends are favored
# over showy wipes. This is a subset of SUPPORTED_TRANSITIONS; users can still
# request any supported effect explicitly (single name or per-boundary list).
XFADE_TRANSITIONS: Dict[str, int] = {
    "fade": 4,
    "dissolve": 2,
    "fadeblack": 2,
    "fadewhite": 1,
    "smoothleft": 2,
    "smoothright": 2,
    "smoothup": 1,
    "smoothdown": 1,
    "wipeleft": 1,
    "wiperight": 1,
    "slideleft": 1,
    "slideright": 1,
    "circleopen": 1,
    "circleclose": 1,
    "radial": 1,
    "pixelize": 1,
}

# Guard against a curated-pool name drifting out of the supported set.
assert set(XFADE_TRANSITIONS).issubset(SUPPORTED_TRANSITIONS), (
    "XFADE_TRANSITIONS must be a subset of SUPPORTED_TRANSITIONS"
)


def parse_transition_spec(raw: object) -> Tuple[str, Optional[List[str]]]:
    """Parse a ``transition`` request string into ``(mode, transitions)``.

    A comma-separated value is an explicit per-boundary list (``"custom"`` mode
    with the parsed names); a single token is a mode (``none``/``random``) or a
    single effect name; empty input defaults to ``none``. Name validity is not
    checked here — callers validate against ``SUPPORTED_TRANSITIONS`` so they can
    surface the right error (HTTP 400 vs ValueError).
    """

    text = "" if raw is None else str(raw)
    parts = [part.strip().lower() for part in text.split(",") if part.strip()]
    if not parts:
        return "none", None
    if len(parts) == 1:
        return parts[0], None
    return "custom", parts


def normalize_transition_list(
    names: Sequence[str],
    boundary_count: int,
) -> List[str]:
    """Validate ``names`` against ``SUPPORTED_TRANSITIONS`` and fit them to boundaries.

    A user-supplied list is cycled when shorter than ``boundary_count`` (so
    ``[fade, dissolve]`` over four boundaries alternates) and truncated when
    longer. Raises ``ValueError`` on any unsupported name.
    """

    cleaned = [str(name).strip().lower() for name in names if str(name).strip()]
    if not cleaned:
        return []
    unknown = [name for name in cleaned if name not in SUPPORTED_TRANSITIONS]
    if unknown:
        raise ValueError(
            f"Unsupported transitions: {', '.join(sorted(set(unknown)))}. "
            f"Allowed: {', '.join(sorted(SUPPORTED_TRANSITIONS))}."
        )
    if boundary_count <= 0:
        return []
    return [cleaned[idx % len(cleaned)] for idx in range(boundary_count)]


def pick_random_transitions(
    count: int,
    *,
    rng: Optional[random.Random] = None,
) -> List[str]:
    """Pick ``count`` weighted-random transitions from ``XFADE_TRANSITIONS``."""

    if count <= 0:
        return []
    chooser = rng if rng is not None else random
    names = list(XFADE_TRANSITIONS)
    weights = [XFADE_TRANSITIONS[name] for name in names]
    return chooser.choices(names, weights=weights, k=count)


def effective_transition_duration_s(
    durations: Sequence[float],
    requested_s: float,
    *,
    fps: float = 30.0,
) -> float:
    """Clamp a single transition duration so xfade overlaps stay inside every clip.

    Each xfade steals ``d`` seconds of overlap from both neighbours, so ``d``
    must stay below half the shortest clip. Returns 0.0 when the usable window
    is under two frames — at that point a hard cut is visually identical and
    the xfade offset math would degenerate.
    """

    if not durations or requested_s <= 0:
        return 0.0
    sanitized = [max(0.1, float(dur)) for dur in durations]
    clamped = min(float(requested_s), 0.5 * min(sanitized))
    if clamped < 2.0 / float(fps):
        return 0.0
    return clamped


def effective_transition_durations(
    clip_durations: Sequence[float],
    requested: "float | Sequence[float]",
    *,
    fps: float = 30.0,
) -> List[float]:
    """Per-boundary transition durations, each clamped to its own neighbours.

    Boundary ``k`` (between clip ``k`` and ``k+1``) can only overlap up to half
    the shorter of the two clips it joins, so each requested duration is clamped
    against that pair rather than the global minimum. ``requested`` may be a
    single value (applied to every boundary) or one value per boundary. A clamp
    that falls under two frames becomes ``0.0`` (that boundary hard-cuts).
    Returns one entry per boundary (``len(clip_durations) - 1``).
    """

    n = len(clip_durations)
    if n < 2:
        return []
    boundary_count = n - 1
    if isinstance(requested, (int, float)):
        req = [float(requested)] * boundary_count
    else:
        req = [float(x) for x in requested]
        if len(req) != boundary_count:
            raise ValueError(
                f"transition durations must have {boundary_count} entries "
                f"(one per image boundary), got {len(req)}."
            )
    sanitized = [max(0.1, float(dur)) for dur in clip_durations]
    floor = 2.0 / float(fps)
    out: List[float] = []
    for k in range(boundary_count):
        if req[k] <= 0:
            out.append(0.0)
            continue
        clamped = min(req[k], 0.5 * min(sanitized[k], sanitized[k + 1]))
        out.append(clamped if clamped >= floor else 0.0)
    return out


def _slideshow_filter_complex(
    *,
    sanitized_durations: Sequence[float],
    fit_builder,
    fps: float,
    transitions: Optional[Sequence[str]],
    transition_durations: Sequence[float],
) -> str:
    """Build the slideshow filtergraph: concat hard cuts or a chained xfade.

    ``fit_builder(idx, trim_length, out_label)`` returns the per-image filterchain
    that fits input ``idx`` onto the shared canvas (letterbox or blur), trims it to
    ``trim_length`` seconds, and ends at ``[out_label]``. Keeping the fitting behind
    this callable lets any background mode compose with any transition mode.

    ``transition_durations`` carries one already-clamped blend length per image
    boundary (``count - 1`` entries). The xfade path preserves the concat timeline
    exactly: each transition of length ``d_k`` is centered on the concat boundary
    ``B_k`` (offset = B_k - d_k/2) so beat-aligned switch points stay on the beat,
    and clip trims are extended by the overlap so the total duration remains
    ``sum(durations)``. It reduces to the uniform case when every ``d_k`` is equal.

    xfade needs a single running chain, so it is all-or-nothing: if any boundary's
    clamped duration is 0 (a clip too short to blend) the whole slideshow falls
    back to hard cuts rather than splicing concat into the middle of the chain.
    """

    count = len(sanitized_durations)
    frame_s = 1.0 / float(fps)

    if count == 1:
        return fit_builder(0, sanitized_durations[0], "vout")

    use_xfade = (
        transitions is not None
        and len(transitions) == count - 1
        and len(transition_durations) == count - 1
        and all(d > 0 for d in transition_durations)
    )
    if use_xfade:
        unknown = sorted(set(transitions) - set(SUPPORTED_TRANSITIONS))
        if unknown:
            raise ValueError(
                f"Unsupported xfade transitions: {', '.join(unknown)}. "
                f"Allowed: {', '.join(sorted(SUPPORTED_TRANSITIONS))}."
            )

    if not use_xfade:
        filter_steps = []
        concat_inputs = []
        for idx, dur in enumerate(sanitized_durations):
            filter_steps.append(fit_builder(idx, dur, f"v{idx}"))
            concat_inputs.append(f"[v{idx}]")
        filter_steps.append(
            "".join(concat_inputs) + f"concat=n={count}:v=1:a=0[vout]"
        )
        return ";".join(filter_steps)

    d = [float(x) for x in transition_durations]
    total = sum(sanitized_durations)
    boundaries: List[float] = []
    running = 0.0
    for dur in sanitized_durations[:-1]:
        running += dur
        boundaries.append(running)
    # Quantize offsets to the frame grid so consecutive xfades never land on
    # fractional frames and accumulate half-frame seams.
    offsets = [
        round((boundaries[k] - d[k] / 2.0) * fps) / fps for k in range(count - 1)
    ]

    # Clip lengths are derived from the offsets: each clip must cover its own
    # display window plus the overlaps it participates in. Middle clips get one
    # frame of safety margin (xfade drops surplus frames but freezes on a
    # shortfall); the last clip is exact because it defines the output tail.
    # ``d[idx]`` is the OUTGOING transition of clip ``idx`` (the boundary it opens),
    # so it is the blend length the clip must extend past its display window.
    trim_lengths: List[float] = []
    for idx in range(count):
        if idx == 0:
            length = offsets[0] + d[0] + frame_s
        elif idx == count - 1:
            length = total - offsets[-1]
        else:
            length = offsets[idx] + d[idx] - offsets[idx - 1] + frame_s
        trim_lengths.append(length)

    filter_steps = [
        fit_builder(idx, length, f"v{idx}")
        for idx, length in enumerate(trim_lengths)
    ]
    prev_label = "v0"
    for idx in range(count - 1):
        out_label = "vout" if idx == count - 2 else f"x{idx + 1}"
        filter_steps.append(
            f"[{prev_label}][v{idx + 1}]"
            f"xfade=transition={transitions[idx]}:duration={d[idx]:.6f}:offset={offsets[idx]:.6f}"
            f"[{out_label}]"
        )
        prev_label = out_label
    return ";".join(filter_steps)


def build_slideshow_video(
    image_paths: List[Path],
    duration_per_image: float,
    output_path: Path,
    *,
    fps: float = 30.0,
    custom_durations: Optional[List[float]] = None,
    background_mode: Literal["blur", "black"] = "blur",
    background_blur_sigma: float = 20.0,
    transitions: Optional[Sequence[str]] = None,
    transition_duration_s: "float | Sequence[float]" = 0.4,
) -> Path:
    """Create a silent slideshow video from still images with stable per-frame durations.

    Every still is fit into a shared canvas (the max width/height across the batch).
    ``background_mode`` controls how the leftover area is filled when an image's
    aspect ratio differs from the canvas:

    - ``"blur"`` (default): fill with a scaled, blurred copy of the same image so
      mixed portrait/landscape uploads never show black bars.
    - ``"black"``: legacy letterbox with solid black padding.

    ``transitions`` optionally names one xfade effect per image boundary
    (``len(image_paths) - 1`` entries from ``XFADE_TRANSITIONS``). When omitted,
    or when the clips are too short to blend, images are hard-cut. Total duration
    and switch points are identical with or without transitions, and any fitting
    mode composes with any transition.
    """

    if not image_paths:
        raise ValueError("image_paths must contain at least one element.")
    if duration_per_image <= 0:
        raise ValueError("duration_per_image must be > 0.")

    ffmpeg_bin = resolve_ffmpeg_binary()
    normalized: List[Path] = []
    for path in image_paths:
        resolved = path.expanduser().resolve()
        if not resolved.exists():
            raise FileNotFoundError(resolved)
        normalized.append(resolved)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    durations = custom_durations or [duration_per_image] * len(normalized)
    if len(durations) != len(normalized):
        raise ValueError("custom_durations must match image count")
    sanitized_durations = [max(0.1, float(dur)) for dur in durations]
    target_width = 0
    target_height = 0
    for image in normalized:
        width, height = get_image_dimensions(image)
        target_width = max(target_width, width)
        target_height = max(target_height, height)
    target_width = max(2, target_width + (target_width % 2))
    target_height = max(2, target_height + (target_height % 2))

    cmd = [ffmpeg_bin, "-y"]
    for image in normalized:
        # Treat each still as an infinite input stream and trim it explicitly in the filter graph.
        cmd += ["-loop", "1", "-i", str(image)]

    fps_expr = f"{float(fps):.6f}".rstrip("0").rstrip(".")
    blur_sigma = max(0.1, float(background_blur_sigma))

    def _per_image_chain(idx: int, trim_length: float, out_label: str) -> str:
        """Fit input ``idx`` onto the canvas, trim to ``trim_length``, end at ``[out_label]``.

        ``format=yuv420p`` before any blend keeps inputs in one pixel format; mixed
        PNG (rgb24) and JPEG (yuvj420p) sources would otherwise negotiate per-link
        and blend with visible brightness shifts.

        ``fps`` is the LAST filter, applied after ``trim``/``setpts``. ffmpeg's
        ``xfade`` requires constant-frame-rate inputs, and on FFmpeg 7.x a ``setpts``
        after ``fps`` drops the stream's frame-rate metadata (reported as ``1/0``),
        which makes xfade abort. Ending each chain with ``fps`` re-establishes CFR
        right before the join. This also gives every clip the same timebase, so no
        explicit ``settb`` is needed.
        """
        tail = (
            f"setsar=1,format=yuv420p,"
            f"trim=duration={trim_length:.6f},setpts=PTS-STARTPTS,fps={fps_expr}[{out_label}]"
        )
        if background_mode == "black":
            return (
                f"[{idx}:v]"
                f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
                f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"{tail}"
            )
        # Blurred fill: a scaled+blurred copy of the image covers the whole canvas,
        # with the untouched, aspect-preserved image centered on top.
        return (
            f"[{idx}:v]split=2[bg{idx}][fg{idx}];"
            f"[bg{idx}]scale={target_width}:{target_height}:force_original_aspect_ratio=increase,"
            f"crop={target_width}:{target_height},gblur=sigma={blur_sigma:.3f},setsar=1[bgb{idx}];"
            f"[fg{idx}]scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
            f"setsar=1[fgf{idx}];"
            f"[bgb{idx}][fgf{idx}]overlay=(W-w)/2:(H-h)/2:format=auto,"
            f"{tail}"
        )

    # One clamped blend length per image boundary (scalar requests broadcast to
    # every boundary). xfade is all-or-nothing per this slideshow: if any boundary
    # is too short to blend, drop transitions entirely rather than mixing modes.
    effective_ds = effective_transition_durations(
        sanitized_durations, transition_duration_s, fps=fps
    )
    can_blend = bool(effective_ds) and all(dd > 0 for dd in effective_ds)
    if transitions and not can_blend:
        logger.info(
            "Slideshow transitions disabled: clips too short for the requested "
            "blend (min duration %.2fs); falling back to hard cuts.",
            min(sanitized_durations),
        )
    filter_complex = _slideshow_filter_complex(
        sanitized_durations=sanitized_durations,
        fit_builder=_per_image_chain,
        fps=fps,
        transitions=transitions if can_blend else None,
        transition_durations=effective_ds,
    )

    cmd += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[vout]",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ]
    _run_ffmpeg_cmd(cmd, check=True)

    return output_path


def get_video_duration(video_path: Path) -> float:
    """
    Return duration in seconds via ffprobe.
    """
    ffprobe = resolve_ffmpeg_binary().replace("ffmpeg", "ffprobe")

    result = _run_ffmpeg_cmd(

        [ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def _safe_float(value: object) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _safe_int(value: object) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_frame_rate(value: object) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    if "/" not in value:
        return _safe_float(value)
    numerator_raw, denominator_raw = value.split("/", 1)
    numerator = _safe_float(numerator_raw)
    denominator = _safe_float(denominator_raw)
    if not numerator or not denominator:
        return None
    return numerator / denominator


def _normalize_rotation_degrees(value: object) -> Optional[int]:
    try:
        return int(round(float(value))) % 360
    except (TypeError, ValueError):
        return None


def _rotation_degrees_from_stream(stream: dict) -> Optional[int]:
    for side_data in stream.get("side_data_list") or []:
        rotation = _normalize_rotation_degrees(side_data.get("rotation"))
        if rotation is not None:
            return rotation

    tags = stream.get("tags") or {}
    return _normalize_rotation_degrees(tags.get("rotate"))


def get_display_dimensions_from_stream(stream: dict) -> Tuple[Optional[int], Optional[int]]:
    """
    Return display-oriented dimensions for a probed video stream.
    """
    width = _safe_int(stream.get("width"))
    height = _safe_int(stream.get("height"))
    if width is None or height is None:
        return width, height

    rotation = _rotation_degrees_from_stream(stream)
    if rotation in {90, 270}:
        return height, width
    return width, height


def _get_video_dimensions(video_path: Path) -> Tuple[Optional[int], Optional[int]]:
    """
    Return display-oriented dimensions for the first video stream via ffprobe.
    """
    ffprobe = resolve_ffmpeg_binary().replace("ffmpeg", "ffprobe")
    result = _run_ffmpeg_cmd(
        [
            ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-select_streams",
            "v:0",
            "-of",
            "json",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    try:
        info = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None, None

    streams = info.get("streams") or []
    if not streams:
        return None, None
    return get_display_dimensions_from_stream(streams[0])


def get_video_dimensions(video_path: Path) -> Tuple[Optional[int], Optional[int]]:
    """Public wrapper returning display-oriented (width, height) for the first video stream.

    Returns ``(None, None)`` when the dimensions cannot be probed so callers can
    treat metadata as best-effort without failing the surrounding job.
    """
    return _get_video_dimensions(video_path)


def _get_video_stream_info(video_path: Path) -> Tuple[float, Optional[int], Optional[float]]:
    """
    Return duration, frame count, and effective FPS for the first video stream.

    Format duration can be misleading for corrupted/partially re-encoded files
    because audio may keep the container duration long after video frames end.
    """
    ffprobe = resolve_ffmpeg_binary().replace("ffmpeg", "ffprobe")
    result = _run_ffmpeg_cmd(
        [
            ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-select_streams",
            "v:0",
            "-of",
            "json",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    try:
        info = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return 0.0

    streams = info.get("streams") or []
    if not streams:
        return 0.0, None, None
    stream = streams[0]

    duration = _safe_float(stream.get("duration"))
    frame_count = _safe_int(stream.get("nb_frames"))
    frame_rate = (
        _parse_frame_rate(stream.get("avg_frame_rate"))
        or _parse_frame_rate(stream.get("r_frame_rate"))
    )
    if (not duration or duration <= 0) and frame_count and frame_rate and frame_rate > 0:
        duration = frame_count / frame_rate
    duration = duration or 0.0
    effective_fps = (
        frame_count / duration
        if frame_count and duration > 0
        else frame_rate
    )
    return duration, frame_count, effective_fps


def _get_video_stream_duration(video_path: Path) -> float:
    return _get_video_stream_info(video_path)[0]


def _reencoded_video_validation_error(source_path: Path, output_path: Path) -> Optional[str]:
    if not output_path.exists():
        return f"output file was not created: {output_path}"

    try:
        if has_video_decode_errors(output_path):
            return "output video stream still has decode errors"
    except Exception as exc:
        return f"output decode validation failed: {exc}"

    try:
        source_stream_duration, _source_frames, source_effective_fps = (
            _get_video_stream_info(source_path)
        )
        output_video_duration, _output_frames, output_effective_fps = (
            _get_video_stream_info(output_path)
        )
        source_duration = source_stream_duration or get_video_duration(source_path)
    except Exception as exc:
        return f"output duration validation failed: {exc}"

    if source_duration >= 5.0 and output_video_duration <= 0:
        return "output video stream duration could not be determined"
    if (
        source_duration >= 5.0
        and output_video_duration < source_duration * 0.85
    ):
        return (
            f"output video stream duration {output_video_duration:.2f}s is shorter "
            f"than source duration {source_duration:.2f}s"
        )
    if (
        source_effective_fps
        and output_effective_fps
        and source_effective_fps >= 10.0
        and output_effective_fps < max(8.0, source_effective_fps * 0.35)
    ):
        return (
            f"output effective frame rate {output_effective_fps:.2f} fps is much "
            f"lower than source {source_effective_fps:.2f} fps"
        )
    return None


def detect_scene_cuts(
    video_path: Path,
    scene_threshold: float = 0.2,
    *,
    detector: Literal["auto", "pyscenedetect", "ffprobe"] = "auto",
    pyscene_method: Literal["adaptive", "content"] = "adaptive",
    pyscene_adaptive_threshold: Optional[float] = None,
    pyscene_content_threshold: Optional[float] = None,
    min_scene_len_s: Optional[float] = None,
    backend: Literal["opencv", "pyav"] = "opencv",
) -> List[float]:
    """Detect scene boundaries and return a sorted list of timestamps (seconds)."""

    detector = detector.lower()
    if detector not in {"auto", "pyscenedetect", "ffprobe"}:
        raise ValueError(
            "detector must be 'auto', 'pyscenedetect', or 'ffprobe'")

    if pyscene_method not in {"adaptive", "content"}:
        raise ValueError("pyscene_method must be 'adaptive' or 'content'")

    if backend not in {"opencv", "pyav"}:
        raise ValueError("backend must be 'opencv' or 'pyav'")

    if pyscene_adaptive_threshold is None:
        pyscene_adaptive_threshold = 3.0
    if pyscene_content_threshold is None:
        pyscene_content_threshold = 27.0
    if min_scene_len_s is None:
        min_scene_len_s = 0.5

    attempts = ["pyscenedetect",
                "ffprobe"] if detector == "auto" else [detector]
    last_exc: Optional[Exception] = None
    for name in attempts:
        try:
            if name == "pyscenedetect":
                return _detect_scene_cuts_pyscenedetect(
                    video_path,
                    method=pyscene_method,
                    adaptive_threshold=pyscene_adaptive_threshold,
                    content_threshold=pyscene_content_threshold,
                    min_scene_len_s=min_scene_len_s,
                    backend=backend,
                )
            if name == "ffprobe":
                return _detect_scene_cuts_ffprobe(video_path, scene_threshold)
        except Exception as exc:  # noqa: BLE001 - propagate best effort below
            last_exc = exc
            continue

    if last_exc:
        raise last_exc
    raise RuntimeError("Scene detection failed without raising an exception")


def _detect_scene_cuts_pyscenedetect(
    video_path: Path,
    *,
    method: Literal["adaptive", "content"],
    adaptive_threshold: float,
    content_threshold: float,
    min_scene_len_s: float,
    backend: Literal["opencv", "pyav"],
) -> List[float]:
    if (
        SceneManager is None
        or open_video is None
        or AdaptiveDetector is None
        or ContentDetector is None
    ):
        raise ImportError("scenedetect is not installed")

    video = open_video(str(video_path), backend=backend)
    try:
        fps = float(video.frame_rate) if video.frame_rate else 30.0
    except Exception:  # pragma: no cover - fallback for older backends
        fps = 30.0

    min_scene_len_frames = max(1, int(round(min_scene_len_s * fps)))
    scene_manager = SceneManager()

    if method == "adaptive":
        detector = AdaptiveDetector(
            adaptive_threshold=adaptive_threshold,
            min_scene_len=min_scene_len_frames,
        )
    else:
        detector = ContentDetector(
            threshold=content_threshold,
            min_scene_len=min_scene_len_frames,
        )

    scene_manager.add_detector(detector)
    try:
        scene_manager.detect_scenes(video=video, show_progress=False)
    finally:
        try:
            video.release()
        except Exception:  # pragma: no cover - backend specific cleanup
            pass

    scene_list = scene_manager.get_scene_list()
    cuts = {0.0}
    cuts.update(start.get_seconds() for start, _end in scene_list)
    return sorted(cuts)


def _detect_scene_cuts_ffprobe(video_path: Path, scene_threshold: float) -> List[float]:
    ffprobe_bin = Path(resolve_ffmpeg_binary()).with_name("ffprobe")

    safe_path = video_path.resolve().as_posix().replace(":", "\\:")
    cmd = [
        str(ffprobe_bin),
        "-v",
        "error",
        "-show_entries",
        "frame=pkt_pts_time",
        "-of",
        "csv=p=0",
        "-f",
        "lavfi",
        f"movie='{safe_path}',select=gt(scene\\,{scene_threshold})",
    ]

    result = _run_ffmpeg_cmd(cmd, capture_output=True, text=True, check=True)

    cuts = [0.0]
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            ts = float(line.strip())
            cuts.append(ts)
        except ValueError:
            continue
    return sorted(set(cuts))


# Example Usage:
# cuts = detect_scene_cuts(Path("C:/Videos/my_video.mp4"))
# print(f"Found {len(cuts)} scenes."

def has_audio_stream(video_path: Path) -> bool:
    """
    Check if the video contains at least one audio stream.
    """
    ffprobe = resolve_ffmpeg_binary().replace("ffmpeg", "ffprobe")

    result = _run_ffmpeg_cmd(

        [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
            "stream=codec_type", "-of", "csv=p=0", str(video_path)],
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def get_audio_codec(media_path: Path) -> Optional[str]:
    """
    Return the codec name for the first audio stream, or None if missing/unknown.
    """
    ffprobe = resolve_ffmpeg_binary().replace("ffmpeg", "ffprobe")
    try:
        result = _run_ffmpeg_cmd(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "csv=p=0",
                str(media_path),
            ],
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    codec = result.stdout.strip().lower()
    return codec or None


def get_video_codec(media_path: Path) -> Optional[str]:
    """
    Return the codec name for the first video stream, or None if missing/unknown.
    """
    ffprobe = resolve_ffmpeg_binary().replace("ffmpeg", "ffprobe")
    try:
        result = _run_ffmpeg_cmd(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "default=nk=1:nw=1",
                str(media_path),
            ],
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    # Streams with side data (e.g. rotation) can emit stray separators or
    # blank lines depending on the writer; keep only the first clean token.
    for line in (result.stdout or "").splitlines():
        codec = line.strip().strip(",").lower()
        if codec:
            return codec
    return None


def detect_audio_activity(
    media_path: Path,
    *,
    duration_hint: Optional[float] = None,
    silence_threshold_db: float = -35.0,
    min_silence_dur: float = 0.3,
    min_activity_dur: float = 0.2,
) -> List[Tuple[float, float]]:
    """
    Heuristically detect non-silent audio regions using ffmpeg's silencedetect.

    Returns a list of (start, end) tuples in seconds where audio is present.
    The detector is lightweight and avoids external dependencies.
    """
    ffmpeg_bin = resolve_ffmpeg_binary()
    path = Path(media_path).resolve()
    if not path.exists():
        return []

    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-af",
        f"silencedetect=noise={silence_threshold_db}dB:d={min_silence_dur}",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        logger.warning("silencedetect failed for %s: %s",
                       path, exc.stderr or exc)
        return []

    silence_starts: List[float] = []
    silence_ends: List[float] = []
    for line in (result.stderr or "").splitlines():
        line = line.strip()
        if "silence_start" in line:
            try:
                silence_starts.append(
                    float(line.split("silence_start:")[1].strip()))
            except (IndexError, ValueError):
                continue
        if "silence_end" in line:
            try:
                part = line.split("silence_end:")[1]
                silence_ends.append(float(part.split()[0]))
            except (IndexError, ValueError):
                continue

    # Pair starts/ends; assume leading sound until first silence_start.
    last_silence_end = 0.0
    regions: List[Tuple[float, float]] = []

    for s in silence_starts:
        if s - last_silence_end >= min_activity_dur:
            regions.append((last_silence_end, s))
        # Match the corresponding silence_end if present
        # Pop first silence_end that is >= s
        next_end = None
        for idx, val in enumerate(silence_ends):
            if val >= s:
                next_end = silence_ends.pop(idx)
                break
        last_silence_end = next_end if next_end is not None else s

    media_duration = duration_hint or get_video_duration(path)
    tail_start = max(last_silence_end, 0.0)
    if media_duration and media_duration - tail_start >= min_activity_dur:
        regions.append((tail_start, media_duration))

    # Merge overlapping/adjacent regions
    merged: List[Tuple[float, float]] = []
    for start, end in sorted(regions):
        if not merged:
            merged.append((start, end))
            continue
        prev_start, prev_end = merged[-1]
        if start <= prev_end + 0.05:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))

    # Clamp to media duration and drop trivial segments.
    final: List[Tuple[float, float]] = []
    for start, end in merged:
        if media_duration:
            start = max(0.0, min(start, media_duration))
            end = max(0.0, min(end, media_duration))
        if end - start >= min_activity_dur:
            final.append((start, end))

    return final


def get_video_fps(video_path: Path) -> float:
    """
    Return frames per second for the first video stream, or 0.0 if unknown.
    """
    ffprobe = resolve_ffmpeg_binary().replace("ffmpeg", "ffprobe")

    result = _run_ffmpeg_cmd(

        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        capture_output=True,
        text=True,
    )
    rate = result.stdout.strip()
    try:
        if "/" in rate:
            num, den = rate.split("/", 1)
            fps = float(num) / float(den)
        else:
            fps = float(rate)
        return fps if fps > 0 else 0.0
    except Exception:
        return 0.0


def extract_frame(
    video_path: Path,
    timestamp: float,
    output_path: Path,
    *,
    duration: Optional[float] = None,
    fps: Optional[float] = None,
) -> Path:
    """
    Extract a single frame at the given timestamp (seconds).
    """
    ffmpeg_bin = resolve_ffmpeg_binary()
    margin = 0.05
    if fps and fps > 0:
        margin = max(margin, 1.0 / fps)
    elif duration:
        margin = max(margin, 0.25)

    if duration is not None:
        # Keep frame extraction safely within video bounds.
        timestamp = max(0.0, min(timestamp, max(0.0, duration - margin)))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run_ffmpeg_cmd(
            [
                ffmpeg_bin,
                "-y",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(output_path),
            ],
            check=True,
            capture_output=True,
        )

    except subprocess.CalledProcessError as err:
        raise RuntimeError(
            f"ffmpeg failed to extract frame at {timestamp:.3f}s from {video_path}: {err.stderr}"
        ) from err
    return output_path


def _mux_video_codec_args(video_path: Path) -> List[str]:
    """
    Video codec args for muxing audio onto an existing video: stream-copy when
    the source is already H.264, re-encode otherwise so every delivered video
    carries an H.264 stream even if upstream normalization was bypassed.
    """
    source_codec = get_video_codec(video_path)
    if source_codec == "h264":
        return ["-c:v", "copy"]
    logger.info(
        "Re-encoding video stream to h264 while muxing %s (source codec: %s)",
        video_path,
        source_codec or "unknown",
    )
    return [
        # libx264 + yuv420p reject odd dimensions; coerce to even. Safe next to
        # the audio-only -filter_complex graphs these mux commands build.
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
    ]


def overlay_music_on_video(
    video_path: Path,
    music_path: Path,
    output_path: Path,
    *,
    music_volume: float = 1.0,
    preserve_original_audio: bool = False,
    music_start_s: float = 0.0,
    audio_codec: str = "aac",
    audio_bitrate: str = "192k",
    ducking_segments: Optional[List[Tuple[float, float]]] = None,
    ducking_gain_db: float = -9.0,
    ducking_padding: float = 0.25,
    music_envelope: Optional[List[Tuple[float, float, float]]] = None,
) -> Path:
    """
    Overlay or replace the video's audio with generated music.
    If preserve_original_audio=True and the video has audio, the original and music are mixed.
    Otherwise the original audio is dropped and replaced by the generated music.
    When no mixing/volume change is needed, stream-copy audio if possible to avoid re-encoding.
    """
    ffmpeg_bin = resolve_ffmpeg_binary()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    has_audio = has_audio_stream(video_path)
    video_codec_args = _mux_video_codec_args(video_path)
    filter_complex: Optional[str] = None
    maps: List[str] = []
    # Map ONLY the primary video stream (0:v:0). Phone/app exports often embed a
    # second video stream (a single-frame mjpeg thumbnail, not flagged
    # attached_pic). Mapping all video streams (0:v) together with -shortest makes
    # ffmpeg truncate the whole output to that ~0-duration thumbnail, yielding a
    # silent/short remix. Selecting the first video stream drops the thumbnail.

    def _build_ducking_volume_expr() -> str:
        regions = ducking_segments or []
        if music_envelope:
            # A user-placed fade is the same kind of move as a duck, so it goes
            # through the same builder — which also means the two compose
            # instead of one silently winning.
            return music_volume_expression(
                music_volume,
                duck_windows=regions or None,
                duck_gain_db=ducking_gain_db,
                padding=ducking_padding,
                envelope=music_envelope,
            )
        if not regions:
            return f"{music_volume}"
        padded_terms: List[str] = []
        for start, end in regions:
            s = max(0.0, start - ducking_padding)
            e = max(s, end + ducking_padding)
            # Escape commas so the filtergraph parser doesn't split on them.
            padded_terms.append(f"between(t\\,{s:.3f}\\,{e:.3f})")
        if not padded_terms:
            return f"{music_volume}"
        duck_linear = 10 ** (ducking_gain_db / 20.0)
        sum_terms = "+".join(padded_terms)
        return f"{music_volume}*if(gt({sum_terms}\\,0)\\,{duck_linear:.4f}\\,1)"

    audio_pad = _pad_audio_to_picture(video_path)
    if preserve_original_audio and has_audio:
        music_volume_expr = _build_ducking_volume_expr()
        # eval=frame so a time-dependent ducking expression re-evaluates per frame
        # (without it the expr is evaluated once at t=0 and ducking never engages).
        filter_complex = (
            f"[1:a]volume={music_volume_expr}:eval=frame[music];"
            f"[0:a][music]amix=inputs=2:duration=first:dropout_transition=0"
            f"{audio_pad}[aout]"
        )
        maps = ["-map", "0:v:0", "-map", "[aout]"]
    else:
        # eval=frame ONLY when the expression actually moves. An envelope makes
        # this term depend on t, and without per-frame evaluation ffmpeg does not
        # merely ignore the fade — it fails the whole render, which is how this
        # was found: by measuring the output instead of reading the filter.
        if music_envelope:
            music_volume_expr = (
                music_volume_expression(music_volume, envelope=music_envelope)
                + ":eval=frame"
            )
        else:
            music_volume_expr = f"{music_volume}"
        filter_complex = f"[1:a]volume={music_volume_expr}{audio_pad}[aout]"
        maps = ["-map", "0:v:0", "-map", "[aout]"]

    mixing_needed = preserve_original_audio and has_audio
    # Stream-copying skips the filtergraph, so the audio cannot be padded — and
    # with ``-shortest`` an audio track shorter than the picture would silently
    # cut the delivered video short. Only copy when the audio already covers it.
    music_covers_video = True
    try:
        video_len = get_video_duration(video_path)
        music_len = get_video_duration(music_path) - max(0.0, float(music_start_s or 0.0))
        music_covers_video = music_len >= video_len - 0.05
    except Exception:  # noqa: BLE001
        music_covers_video = False
    can_stream_copy_audio = (
        (not mixing_needed)
        and abs(music_volume - 1.0) < 1e-6
        and not ducking_segments
        # A copy applies no filter at all, so it would silently drop the fades.
        and not music_envelope
        and music_covers_video
    )
    if can_stream_copy_audio:
        audio_codec_name = get_audio_codec(music_path)
        output_ext = output_path.suffix.lower()
        if output_ext in {".mp4", ".m4v", ".mov"}:
            can_stream_copy_audio = audio_codec_name in {"aac", "alac"}
        elif output_ext == ".webm":
            can_stream_copy_audio = audio_codec_name in {"opus", "vorbis"}
        else:
            can_stream_copy_audio = audio_codec_name is not None
    if can_stream_copy_audio:
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i",
            str(video_path),
        ]
        if music_start_s and music_start_s > 0:
            cmd += ["-ss", f"{music_start_s:.3f}"]
        cmd += ["-i", str(music_path)]
        cmd += ["-map", "0:v:0", "-map", "1:a", *video_codec_args, "-c:a", "copy"]
        if output_path.suffix.lower() in {".mp4", ".m4v", ".mov"}:
            cmd += ["-movflags", "+faststart"]
        cmd += ["-shortest", str(output_path)]
        try:
            _run_ffmpeg_cmd(cmd, check=True)
            return output_path
        except subprocess.CalledProcessError:
            logger.warning(
                "Audio stream copy failed for %s -> %s; falling back to re-encode.",
                music_path,
                output_path,
            )

    # Prefer the requested codec, but fall back to native AAC if unavailable.
    resolved_audio_codec = audio_codec
    if audio_codec == "libfdk_aac":
        resolved_audio_codec = "aac"

    cmd = [
        ffmpeg_bin,
        "-y",
        "-i",
        str(video_path),
    ]
    if music_start_s and music_start_s > 0:
        cmd += ["-ss", f"{music_start_s:.3f}"]
    cmd += ["-i", str(music_path)]
    cmd += ["-filter_complex", filter_complex,
            *video_codec_args, "-c:a", resolved_audio_codec]
    if audio_bitrate:
        cmd += ["-b:a", audio_bitrate]
    cmd += maps
    if output_path.suffix.lower() in {".mp4", ".m4v", ".mov"}:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-shortest", str(output_path)]

    _run_ffmpeg_cmd(cmd, check=True)

    return output_path


def _audio_codec_args_for_suffix(suffix: str) -> List[str]:
    """Pick an encoder for a target audio container by its file suffix."""
    ext = suffix.lower()
    if ext == ".wav":
        return ["-c:a", "pcm_s16le"]
    if ext == ".mp3":
        return ["-c:a", "libmp3lame", "-b:a", "192k"]
    if ext in {".m4a", ".mp4", ".aac"}:
        return ["-c:a", "aac", "-b:a", "192k"]
    if ext == ".flac":
        return ["-c:a", "flac"]
    return ["-c:a", "aac", "-b:a", "192k"]


def extract_audio_window(
    source_path: Path,
    output_path: Path,
    *,
    start_s: float,
    duration_s: Optional[float] = None,
) -> Path:
    """Cut ``[start_s, start_s + duration_s]`` out of an audio file.

    Mirrors the window ``overlay_music_on_video`` muxes into a slideshow (``-ss``
    seek before the input, then a length clamp) so the extracted clip is the same
    slice of the track the viewer hears. A stream copy is attempted first: it
    preserves the source codec (so the clip stays as compact as the full track,
    not bloated by re-encoding a compressed source to PCM) and seeks the same way
    the mux does. If the copy fails (e.g. an incompatible container) the audio is
    re-encoded with a codec chosen from ``output_path``'s suffix. When
    ``duration_s`` is ``None`` (or non-positive) the clip runs to the end.
    """
    ffmpeg_bin = resolve_ffmpeg_binary()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def _base_cmd() -> List[str]:
        cmd = [ffmpeg_bin, "-y"]
        if start_s and start_s > 0:
            cmd += ["-ss", f"{start_s:.3f}"]
        cmd += ["-i", str(source_path)]
        if duration_s and duration_s > 0:
            cmd += ["-t", f"{duration_s:.3f}"]
        return cmd

    copy_cmd = _base_cmd() + ["-map", "0:a:0", "-c:a", "copy", str(output_path)]
    try:
        _run_ffmpeg_cmd(copy_cmd, check=True, capture_output=True)
        return output_path
    except subprocess.CalledProcessError:
        logger.warning(
            "Audio window stream copy failed for %s; falling back to re-encode.",
            source_path,
        )

    reencode_cmd = (
        _base_cmd() + ["-vn"] + _audio_codec_args_for_suffix(output_path.suffix) + [str(output_path)]
    )
    _run_ffmpeg_cmd(reencode_cmd, check=True)
    return output_path


def trim_edge_silence(
    audio_path: Path,
    *,
    threshold_db: float = -45.0,
    min_silence_s: float = 0.05,
    keep_head_s: float = 0.02,
) -> float:
    """Strip leading and trailing silence from a speech take, in place.

    A synthesized line arrives padded with a variable amount of dead air at both
    ends. That padding is invisible to the planner but real in the mix: it is
    measured as part of the line's duration, so a placement computed from that
    duration puts the audible speech somewhere other than its cue, and the air
    stacks on top of the gap the director actually asked for. Two lines can then
    read at wildly different apparent paces purely because of how much silence
    came back with each.

    Trimming before the take is measured makes the measured duration mean the
    thing the timeline assumes it means: how long this line takes to say.
    ``keep_head_s`` leaves a sliver of the original head so the first phoneme
    is never clipped.

    Returns the number of seconds removed (0.0 when nothing was trimmed or the
    trim could not be performed — the file is then left exactly as it was).
    """

    if not audio_path.exists():
        return 0.0
    before = get_video_duration(audio_path)
    if before <= 0.0:
        return 0.0

    ffmpeg_bin = resolve_ffmpeg_binary()
    trimmed = audio_path.with_name(f"{audio_path.stem}__trim{audio_path.suffix}")
    # silenceremove only ever trims the START, so the tail is done by reversing
    # the stream, trimming its new start, and reversing back.
    one_pass = (
        f"silenceremove=start_periods=1:start_silence={min_silence_s}"
        f":start_threshold={threshold_db}dB:detection=peak"
    )
    cmd = [
        ffmpeg_bin, "-y", "-i", str(audio_path),
        "-af", f"{one_pass},areverse,{one_pass},areverse",
        *_audio_codec_args_for_suffix(trimmed.suffix),
        str(trimmed), "-loglevel", "error",
    ]
    try:
        _run_ffmpeg_cmd(cmd, check=True, capture_output=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.warning("Edge-silence trim failed for %s: %s", audio_path, exc)
        trimmed.unlink(missing_ok=True)
        return 0.0

    after = get_video_duration(trimmed)
    # A trim that removes everything means the take was silent (or the threshold
    # was wrong for this voice) — keep the original rather than ship an empty
    # line that would silently vanish from the mix.
    if after <= 0.05:
        logger.warning(
            "Edge-silence trim emptied %s (%.2fs -> %.2fs); keeping the original.",
            audio_path, before, after,
        )
        trimmed.unlink(missing_ok=True)
        return 0.0

    if keep_head_s > 0.0:
        padded = audio_path.with_name(f"{audio_path.stem}__pad{audio_path.suffix}")
        pad_cmd = [
            ffmpeg_bin, "-y", "-i", str(trimmed),
            "-af", f"adelay={int(keep_head_s * 1000)}|{int(keep_head_s * 1000)}",
            *_audio_codec_args_for_suffix(padded.suffix),
            str(padded), "-loglevel", "error",
        ]
        try:
            _run_ffmpeg_cmd(pad_cmd, check=True, capture_output=True)
            trimmed.unlink(missing_ok=True)
            trimmed = padded
            after = get_video_duration(trimmed)
        except (subprocess.CalledProcessError, OSError):
            padded.unlink(missing_ok=True)

    trimmed.replace(audio_path)
    return max(0.0, round(before - after, 3))


def overlay_voiceover_on_video(
    video_path: Path,
    voiceover_path: Path,
    output_path: Path,
    *,
    voiceover_volume: float = 1.0,
    voiceover_start_s: float = 0.0,
    preserve_original_audio: bool = False,
    audio_codec: str = "aac",
    audio_bitrate: str = "192k",
) -> Path:
    """Lay a voice-over over a video starting at ``voiceover_start_s`` — the
    voice-over-only deliverable (no music bed).

    The narration is DELAYED to ``voiceover_start_s`` (``adelay``), not seeked into
    (``overlay_music_on_video``'s ``music_start_s`` seeks, which would truncate a
    short narration clip). Without the original audio the narration is padded with
    silence so a short clip doesn't truncate the video; with it, the two are mixed.
    """

    ffmpeg_bin = resolve_ffmpeg_binary()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    start_ms = int(max(0.0, float(voiceover_start_s)) * 1000)
    has_original = preserve_original_audio and has_audio_stream(video_path)
    audio_pad = _pad_audio_to_picture(video_path)
    if has_original:
        filters = [
            f"[1:a]adelay={start_ms}|{start_ms},volume={voiceover_volume}[vo]",
            "[0:a][vo]amix=inputs=2:duration=first:dropout_transition=0:normalize=0"
            f"{audio_pad}[aout]",
        ]
    else:
        filters = [
            f"[1:a]adelay={start_ms}|{start_ms},volume={voiceover_volume}{audio_pad}[aout]",
        ]

    cmd = [
        ffmpeg_bin, "-y",
        "-i", str(video_path),
        "-i", str(voiceover_path),
        "-filter_complex", ";".join(filters),
        "-map", "0:v:0", "-map", "[aout]",
        *_mux_video_codec_args(video_path),
        "-c:a", "aac" if audio_codec == "libfdk_aac" else audio_codec,
    ]
    if audio_bitrate:
        cmd += ["-b:a", audio_bitrate]
    if output_path.suffix.lower() in {".mp4", ".m4v", ".mov"}:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-shortest", str(output_path)]

    _run_ffmpeg_cmd(cmd, check=True)
    return output_path


def compose_voiceover_mix_on_video(
    video_path: Path,
    music_path: Path,
    voiceover_path: Path,
    output_path: Path,
    *,
    music_volume: float = 0.85,
    voiceover_volume: float = 1.0,
    voiceover_start_s: float = 0.0,
    duck_gain_db: float = -9.0,
    duck_padding: float = 0.25,
    voiceover_duration_s: Optional[float] = None,
    preserve_original_audio: bool = False,
    audio_codec: str = "aac",
    audio_bitrate: str = "192k",
) -> Path:
    """Layer a music bed and a voice-over track onto a video.

    The voice-over is placed at ``voiceover_start_s`` (and runs for its own
    duration); the music is ducked by ``duck_gain_db`` for the span the narration
    is active (with a little padding). ``music_volume`` / ``voiceover_volume`` set
    the layer balance. ``amix`` normalization is disabled so those volumes — i.e.
    the user-controlled ratio — are respected. The video stream is stream-copied
    when already H.264, otherwise re-encoded to H.264.
    """

    ffmpeg_bin = resolve_ffmpeg_binary()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if voiceover_duration_s is None:
        voiceover_duration_s = get_video_duration(voiceover_path)
    start = max(0.0, float(voiceover_start_s))
    end = start + max(0.0, float(voiceover_duration_s))
    duck_start = max(0.0, start - duck_padding)
    duck_end = max(duck_start, end + duck_padding)
    duck_linear = 10 ** (duck_gain_db / 20.0)
    # Escape commas so the filtergraph parser doesn't split the expression.
    music_expr = (
        f"{music_volume}*if(between(t\\,{duck_start:.3f}\\,{duck_end:.3f})"
        f"\\,{duck_linear:.4f}\\,1)"
    )
    start_ms = int(start * 1000)

    has_original = preserve_original_audio and has_audio_stream(video_path)
    filters = [
        # eval=frame is REQUIRED: the volume expression is time-dependent (it ducks
        # only during the narration). Without it ffmpeg evaluates the expr once at
        # t=0, so between(t,...) is always false and the music never ducks.
        f"[1:a]volume={music_expr}:eval=frame[m]",
        f"[2:a]adelay={start_ms}|{start_ms},volume={voiceover_volume}[vo]",
    ]
    if has_original:
        mix_inputs, n_inputs = "[0:a][m][vo]", 3
    else:
        mix_inputs, n_inputs = "[m][vo]", 2
    filters.append(
        f"{mix_inputs}amix=inputs={n_inputs}:duration=first:"
        f"dropout_transition=0:normalize=0{_pad_audio_to_picture(video_path)}[aout]"
    )

    cmd = [
        ffmpeg_bin,
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(music_path),
        "-i",
        str(voiceover_path),
        "-filter_complex",
        ";".join(filters),
        "-map",
        "0:v:0",
        "-map",
        "[aout]",
        *_mux_video_codec_args(video_path),
        "-c:a",
        "aac" if audio_codec == "libfdk_aac" else audio_codec,
    ]
    if audio_bitrate:
        cmd += ["-b:a", audio_bitrate]
    if output_path.suffix.lower() in {".mp4", ".m4v", ".mov"}:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-shortest", str(output_path)]

    _run_ffmpeg_cmd(cmd, check=True)
    return output_path


def narration_duck_expr(
    music_volume: float,
    windows: List[Tuple[float, float]],
    *,
    gain_db: float = -9.0,
    padding: float = 0.25,
    ramp_s: float = 0.25,
) -> str:
    """Music-volume expression that dips per spoken LINE and recovers between.

    One duck window spanning the whole narration holds the score down through
    every pause in it: a read with three short lines can flatten fourteen seconds
    of music so six seconds of speech can happen. The wordless beats between
    lines are exactly where the music is supposed to carry the piece, so they are
    the worst possible thing to suppress — and the sparser the narration, the
    more of the track gets needlessly buried.

    Ducking is expressed as an amount ``d`` in 0..1 per line, trapezoidal so the
    level slides rather than steps (a hard gate on a musical bed pumps audibly),
    then combined with ``max`` so overlapping lines never duck twice as far.

    Returns a plain constant when there are no windows, so callers can use this
    unconditionally.
    """

    if not windows:
        return f"{music_volume}"
    ramp = max(0.01, ramp_s)
    gain = 10 ** (gain_db / 20.0)
    terms: List[str] = []
    for start, end in windows:
        a = max(0.0, start - padding) - ramp
        b = max(start, end) + padding + ramp
        # Trapezoid: 0 outside [a, b], 1 across the padded line, linear between.
        terms.append(
            f"max(0\\,min(1\\,min((t-{a:.3f})/{ramp:.3f}\\,({b:.3f}-t)/{ramp:.3f})))"
        )
    depth = terms[0]
    for term in terms[1:]:
        depth = f"max({depth}\\,{term})"
    # volume = music * (1 - d * (1 - gain)) -> music at d=0, music*gain at d=1.
    return f"{music_volume}*(1-({depth})*{1.0 - gain:.4f})"


def _trapezoid_term(start: float, end: float, *, padding: float, ramp: float) -> str:
    """A 0..1 ramp that rises into a window, holds across it, and falls out.

    A hard gate on a musical bed pumps audibly, so every level move in this file
    slides rather than steps.
    """

    a = max(0.0, start - padding) - ramp
    b = max(start, end) + padding + ramp
    return f"max(0\\,min(1\\,min((t-{a:.3f})/{ramp:.3f}\\,({b:.3f}-t)/{ramp:.3f})))"


def splice_audio_windows(
    source_path: Path,
    output_path: Path,
    *,
    windows: List[Tuple[float, float]],
    crossfade_s: float = 0.04,
) -> Path:
    """Assemble one piece of audio from several windows of another.

    A take has always been ONE window of a longer track — the matcher picks
    where to cut and the user can move that cut, but not build from two places
    at once. "Open on the quiet part and let the drop land on the product shot"
    needs exactly that, and no amount of moving a single window gets there.

    ``windows`` is ``(start_s, duration_s)`` in source time, taken in the order
    given: repeats are allowed, and so is going backwards, because both are
    things an arrangement does.

    Joined with a short equal-power crossfade rather than butt-joined. Two
    pieces of music meeting at a sample boundary click almost every time, and a
    click is the one artefact that makes an edit sound like a mistake rather
    than a choice. The fade is deliberately tiny — long enough to hide the
    seam, short enough that it is not heard as a transition.
    """

    if not windows:
        raise ValueError("splice_audio_windows needs at least one window")

    ffmpeg_bin = resolve_ffmpeg_binary()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fade = max(0.0, float(crossfade_s))

    cmd: List[str] = [ffmpeg_bin, "-y", "-hide_banner"]
    filters: List[str] = []
    for index, (start, duration) in enumerate(windows):
        # Each window is its own input so the seek is exact per piece.
        cmd += ["-ss", f"{max(0.0, float(start)):.3f}",
                "-t", f"{max(0.01, float(duration)):.3f}",
                "-i", str(source_path)]
        filters.append(f"[{index}:a]asetpts=PTS-STARTPTS[w{index}]")

    if len(windows) == 1 or fade <= 0:
        joined = "".join(f"[w{i}]" for i in range(len(windows)))
        filters.append(f"{joined}concat=n={len(windows)}:v=0:a=1[out]")
    else:
        current = "w0"
        for index in range(1, len(windows)):
            nxt = f"x{index}"
            filters.append(
                f"[{current}][w{index}]acrossfade=d={fade:.3f}:c1=tri:c2=tri[{nxt}]"
            )
            current = nxt
        filters.append(f"[{current}]anull[out]")

    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[out]",
    ] + _audio_codec_args_for_suffix(output_path.suffix) + [str(output_path)]
    _run_ffmpeg_cmd(cmd, check=True)
    return output_path


def music_volume_expression(
    music_volume: float,
    *,
    duck_windows: Optional[List[Tuple[float, float]]] = None,
    duck_gain_db: float = -9.0,
    padding: float = 0.25,
    ramp_s: float = 0.25,
    envelope: Optional[List[Tuple[float, float, float]]] = None,
) -> str:
    """The music's level over time: automatic ducking and the user's own fades.

    Two different things want to move this one number. The mixer ducks under
    each spoken line on its own; the user asks for a fade at a particular
    moment ("drop it under the voice at 0:40", "bring it down for the last five
    seconds"). Before this, only the first existed and every user-facing volume
    was a single scalar for the whole timeline.

    ``envelope`` is a list of ``(start_s, end_s, gain_db)`` the user placed.
    Each becomes its own trapezoid, and everything combines by taking the
    QUIETEST level anything asks for at that instant rather than multiplying
    them together. Two overlapping fades compounding into silence is not what
    anybody means by asking for both, and it is the behaviour that would be
    hardest to explain when it happened.

    Returns a plain constant when nothing moves, so callers can use this
    unconditionally.
    """

    ramp = max(0.01, ramp_s)
    factors: List[str] = []

    windows = list(duck_windows or [])
    if windows:
        gain = 10 ** (duck_gain_db / 20.0)
        depth = _trapezoid_term(windows[0][0], windows[0][1], padding=padding, ramp=ramp)
        for start, end in windows[1:]:
            depth = (
                f"max({depth}\\,"
                f"{_trapezoid_term(start, end, padding=padding, ramp=ramp)})"
            )
        # Overlapping lines never duck twice as far.
        factors.append(f"(1-({depth})*{1.0 - gain:.4f})")

    for start, end, gain_db in (envelope or []):
        target = 10 ** (float(gain_db) / 20.0)
        term = _trapezoid_term(float(start), float(end), padding=0.0, ramp=ramp)
        factors.append(f"(1-({term})*{1.0 - target:.4f})")

    if not factors:
        return f"{music_volume}"
    combined = factors[0]
    for factor in factors[1:]:
        combined = f"min({combined}\\,{factor})"
    return f"{music_volume}*{combined}"


def compose_master_mix_on_video(
    video_path: Path,
    output_path: Path,
    *,
    music_path: Optional[Path] = None,
    voiceover_path: Optional[Path] = None,
    sfx_path: Optional[Path] = None,
    music_volume: float = 0.85,
    voiceover_volume: float = 1.0,
    voiceover_start_s: float = 0.0,
    duck_gain_db: float = -9.0,
    duck_padding: float = 0.25,
    music_envelope: Optional[List[Tuple[float, float, float]]] = None,
    sfx_volume: float = 1.0,
    voiceover_segments: Optional[List[Tuple[float, float]]] = None,
    music_start_s: float = 0.0,
    preserve_original_audio: bool = False,
    audio_codec: str = "aac",
    audio_bitrate: str = "192k",
) -> Path:
    """One MASTER mix: any subset of music / narration / SFX onto the video.

    Extends ``compose_voiceover_mix_on_video`` to a third layer: the SFX bed is
    added at ``sfx_volume`` with no time shift (its event offsets are baked into
    the track by the SFX render). ``amix`` normalization stays disabled so the
    user-controlled layer ratios are respected. The video stream is stream-copied
    when already H.264, otherwise re-encoded to H.264.

    ``voiceover_segments`` are the REALIZED [start, end] windows of the spoken
    lines. Given them, the music ducks per line and recovers in between; without
    them it falls back to one window over the whole narration, which is all the
    caller can honestly do when it does not know where the lines are.
    """

    if not (music_path or voiceover_path or sfx_path):
        raise ValueError("compose_master_mix_on_video needs at least one audio layer.")

    ffmpeg_bin = resolve_ffmpeg_binary()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [ffmpeg_bin, "-y", "-i", str(video_path)]
    filters: List[str] = []
    mix_labels: List[str] = []

    if preserve_original_audio and has_audio_stream(video_path):
        mix_labels.append("[0:a]")

    idx = 1
    if music_path is not None:
        if voiceover_path is not None:
            offset = max(0.0, float(voiceover_start_s))
            windows = [
                (offset + max(0.0, float(s)), offset + max(float(s), float(e)))
                for s, e in (voiceover_segments or [])
                if e is not None and s is not None
            ]
            if not windows:
                # No realized line windows: duck across the whole narration span.
                vo_dur = get_video_duration(voiceover_path)
                end = offset + max(0.0, float(vo_dur or 0.0))
                windows = [(offset, end)]
            music_expr = music_volume_expression(
                music_volume,
                duck_windows=windows,
                duck_gain_db=duck_gain_db,
                padding=duck_padding,
                envelope=music_envelope,
            )
            # eval=frame: the duck expression is time-dependent (see the two-layer
            # helper) — without it the music never ducks.
            filters.append(f"[{idx}:a]volume={music_expr}:eval=frame[m]")
        elif music_envelope:
            # No narration to duck under, but the user placed fades of their
            # own — the whole point of an envelope is that it does not need a
            # voice to justify it.
            filters.append(
                f"[{idx}:a]volume="
                f"{music_volume_expression(music_volume, envelope=music_envelope)}"
                ":eval=frame[m]"
            )
        else:
            filters.append(f"[{idx}:a]volume={music_volume}[m]")
        # Seek INTO the music before it enters the graph, so a chosen window of a
        # longer track can be laid under the picture. Applied as an input option
        # (before -i) so the decoder starts there rather than the filter dropping
        # audio it already decoded. The single-layer overlay has taken this for a
        # long time; without it here, a window chosen while the session was
        # music-only silently reset the moment narration or SFX joined and the
        # mix switched to this renderer.
        if float(music_start_s or 0.0) > 0:
            cmd += ["-ss", f"{float(music_start_s):.3f}"]
        cmd += ["-i", str(music_path)]
        mix_labels.append("[m]")
        idx += 1
    if voiceover_path is not None:
        start_ms = int(max(0.0, float(voiceover_start_s)) * 1000)
        filters.append(f"[{idx}:a]adelay={start_ms}|{start_ms},volume={voiceover_volume}[vo]")
        cmd += ["-i", str(voiceover_path)]
        mix_labels.append("[vo]")
        idx += 1
    if sfx_path is not None:
        filters.append(f"[{idx}:a]volume={sfx_volume}[fx]")
        cmd += ["-i", str(sfx_path)]
        mix_labels.append("[fx]")
        idx += 1

    audio_pad = _pad_audio_to_picture(video_path)
    if len(mix_labels) == 1:
        # A lone layer still needs a filter to attach the pad to; anull is the
        # no-op that carries it when the duration is unreadable.
        filters.append(f"{mix_labels[0]}anull{audio_pad}[aout]")
    else:
        filters.append(
            f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:duration=first:"
            f"dropout_transition=0:normalize=0{audio_pad}[aout]"
        )

    cmd += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        "0:v:0",
        "-map",
        "[aout]",
        *_mux_video_codec_args(video_path),
        "-c:a",
        "aac" if audio_codec == "libfdk_aac" else audio_codec,
    ]
    if audio_bitrate:
        cmd += ["-b:a", audio_bitrate]
    if output_path.suffix.lower() in {".mp4", ".m4v", ".mov"}:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-shortest", str(output_path)]

    _run_ffmpeg_cmd(cmd, check=True)
    return output_path


def build_sfx_timeline_wav(
    events: List[tuple[Path, float, float]],
    output_path: Path,
    *,
    total_duration_s: float,
    sample_rate: int = 44_100,
) -> Path:
    """
    Build one mono WAV timeline from SFX clips.

    Args:
        events: list of (audio_path, start_time_seconds, gain_db)
        output_path: destination wav path
        total_duration_s: output timeline duration in seconds
        sample_rate: output sample rate
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    total_samples = max(
        1, int(round(max(total_duration_s, 0.0) * sample_rate)))
    timeline = np.zeros(total_samples, dtype=np.float32)

    for audio_path, start_time, gain_db in events:
        path = Path(audio_path)
        clip, clip_sr = _read_wav_as_mono_float(path)
        if clip_sr != sample_rate:
            clip = _resample_mono_audio(
                clip, src_rate=clip_sr, dst_rate=sample_rate)

        start_idx = max(0, int(round(max(0.0, start_time) * sample_rate)))
        if start_idx >= total_samples:
            continue
        gain = float(10 ** (gain_db / 20.0))
        scaled = clip * gain
        end_idx = min(total_samples, start_idx + len(scaled))
        if end_idx <= start_idx:
            continue
        timeline[start_idx:end_idx] += scaled[: end_idx - start_idx]

    timeline = np.clip(timeline, -1.0, 1.0)
    pcm16 = (timeline * 32767.0).astype(np.int16)
    with wave.open(str(output_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())
    return output_path


def _read_wav_as_mono_float(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        channels = int(wf.getnchannels())
        sample_rate = int(wf.getframerate())
        nframes = int(wf.getnframes())
        raw = wf.readframes(nframes)

    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
    if channels > 1:
        frame_count = len(samples) // channels
        samples = samples[: frame_count *
                          channels].reshape(frame_count, channels).mean(axis=1)
    return samples, sample_rate


def _resample_mono_audio(samples: np.ndarray, *, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or samples.size == 0:
        return samples
    dst_len = max(
        1, int(round(samples.size * float(dst_rate) / float(src_rate))))
    src_positions = np.linspace(0.0, 1.0, num=samples.size, endpoint=False)
    dst_positions = np.linspace(0.0, 1.0, num=dst_len, endpoint=False)
    return np.interp(dst_positions, src_positions, samples).astype(np.float32)


def compress_video_to_max_height(
    video_path: Path,
    output_path: Optional[Path] = None,
    *,
    max_height: int = 1280,
    crf: int = 23,
    preset: str = "veryfast",
    force_reencode: bool = False,
    repair_decode_errors: bool = False,
    return_original_on_failure: bool = False,
    validate_reencode: bool = False,
) -> Path:
    """
    Downscale a video to a maximum height while preserving aspect ratio.
    The helper does not upscale smaller videos.

    The returned file always carries an H.264 video stream: sources within
    max_height whose codec is not h264 (e.g. iPhone HEVC) are re-encoded at
    their original resolution, so downstream ``-c:v copy`` muxing never
    propagates a non-H.264 codec into delivered outputs.
    """
    ffmpeg_bin = resolve_ffmpeg_binary()
    source_path = video_path.expanduser().resolve()
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    if max_height <= 0:
        raise ValueError("max_height must be greater than 0")

    _, source_height = _get_video_dimensions(source_path)
    within_height_limit = source_height is not None and source_height <= max_height

    if within_height_limit and not force_reencode:
        source_codec = get_video_codec(source_path)
        if source_codec != "h264":
            force_reencode = True
            logger.info(
                "Forcing video re-encode for %s because source codec %s is not h264",
                source_path,
                source_codec or "unknown",
            )

    if repair_decode_errors and not force_reencode and within_height_limit:
        force_reencode = has_video_decode_errors(source_path)
        if force_reencode:
            logger.warning(
                "Forcing video re-encode for %s because decode errors were detected",
                source_path,
            )

    if within_height_limit and not force_reencode:
        logger.info(
            "Skipping video compression for %s because height %s is within max_height %s",
            source_path,
            source_height,
            max_height,
        )
        return source_path

    destination = output_path or source_path.with_name(
        f"{source_path.stem}_{max_height}h.mp4")
    destination = destination.expanduser().resolve()
    if destination == source_path:
        destination = source_path.with_name(
            f"{source_path.stem}_reencoded{source_path.suffix}"
        ).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)

    # Regenerate PTS; VFR iOS HEVC often has timestamp oddities.
    global_fflags = ["-fflags", "+genpts"]
    mux_queue_args = ["-max_muxing_queue_size", "2048"]

    audio_present = has_audio_stream(source_path)
    # Keep aspect ratio and avoid upscaling:
    # - If input height <= max_height, keep original size (coerced to even —
    #   libx264 + yuv420p reject odd dimensions, and without the coercion an
    #   odd-sized source would silently fall back to the MPEG-4 encoder).
    # - If > max_height, scale down to height max_height.
    # Using -2 makes ffmpeg pick an even width automatically.
    even_max_height = max_height - (max_height % 2)
    scale_filter = (
        f"scale='if(gt(ih,{max_height}),-2,trunc(iw/2)*2)'"
        f":'if(gt(ih,{max_height}),{even_max_height},trunc(ih/2)*2)'"
    )

    def _build_cmd(video_codec: str) -> List[str]:
        cmd: List[str] = [
            ffmpeg_bin,
            "-v",
            "error",
            "-y",
            *global_fflags,
            "-i",
            str(source_path),
            # map first video, optional audio
            # "-map", "0:v:0",
            # "-map", "0:a?",
            "-vf",
            scale_filter,
            # Modern replacement for -vsync; keep variable FPS if source is VFR
            "-fps_mode",
            "vfr",
            "-c:v",
            video_codec,
            "-threads",
            "0",
            "-pix_fmt",
            "yuv420p",
        ]

        if video_codec == "libx264":
            cmd += ["-preset", preset, "-crf", str(crf)]
            # Optional: make H.264 more universally decodable. Level 3.1 only
            # holds up to ~720x1280; larger targets let x264 pick a conformant
            # level on its own.
            cmd += ["-profile:v", "high"]
            if max_height <= 1280:
                cmd += ["-level", "3.1"]
        else:
            # MPEG-4 Part 2 fallback (older, but widely supported)
            cmd += ["-q:v", "4"]

        if audio_present:
            cmd += [
                "-c:a",
                "copy",
            ]
        cmd += ["-movflags", "+faststart", *mux_queue_args, str(destination)]
        return cmd

    last_error: Optional[Exception] = None
    for video_codec in ("libx264", "mpeg4"):
        try:
            _run_ffmpeg_cmd(
                _build_cmd(video_codec),
                check=True,
                capture_output=True,
                text=True,
            )
            if validate_reencode:
                validation_error = _reencoded_video_validation_error(
                    source_path,
                    destination,
                )
                if validation_error:
                    last_error = RuntimeError(validation_error)
                    logger.warning(
                        "Video re-encode validation failed with codec %s for %s: %s",
                        video_codec,
                        source_path,
                        validation_error,
                    )
                    continue
            return destination
        except subprocess.CalledProcessError as exc:
            last_error = exc
            logger.warning(
                "Video compression failed with codec %s for %s: %s",
                video_codec,
                source_path,
                exc.stderr or exc,
            )
            continue

    if last_error:
        if return_original_on_failure:
            details = getattr(last_error, "stderr", None) or str(last_error)
            logger.warning(
                "Video re-encode failed for %s; continuing with original input: %s",
                source_path,
                _truncate_for_log(details),
            )
            return source_path
        raise last_error
    raise RuntimeError(
        f"Video compression failed unexpectedly for {source_path}")


# Sentinel max_height that no real video exceeds: the scale filter becomes a
# no-op, so compress_video_to_max_height only normalizes the codec.
NO_DOWNSCALE_MAX_HEIGHT = 1_000_000_000


def ensure_h264_video(
    video_path: Path,
    output_path: Optional[Path] = None,
    *,
    crf: int = 23,
    preset: str = "veryfast",
    return_original_on_failure: bool = False,
    validate_reencode: bool = False,
) -> Path:
    """
    Guarantee an H.264 video stream without changing resolution.

    Sources whose first video stream is already h264 are returned untouched;
    anything else (e.g. iPhone HEVC) is re-encoded with libx264 at the original
    size. This is the compression-opt-out companion of
    compress_video_to_max_height: disabling compression only skips downscaling,
    never the H.264 output guarantee.
    """
    source_path = video_path.expanduser().resolve()
    destination = output_path or source_path.with_name(
        f"{source_path.stem}_h264.mp4")
    return compress_video_to_max_height(
        video_path=source_path,
        output_path=destination,
        max_height=NO_DOWNSCALE_MAX_HEIGHT,
        crf=crf,
        preset=preset,
        return_original_on_failure=return_original_on_failure,
        validate_reencode=validate_reencode,
    )


def compress_video_to_1080p(
    video_path: Path,
    output_path: Optional[Path] = None,
    *,
    crf: int = 23,
    preset: str = "veryfast",
) -> Path:
    """
    Backward-compatible helper for explicit 1080p compression.
    """
    return compress_video_to_max_height(
        video_path=video_path,
        output_path=output_path,
        max_height=1080,
        crf=crf,
        preset=preset,
    )


def compress_video_to_720p(
    video_path: Path,
    output_path: Optional[Path] = None,
    *,
    crf: int = 23,
    preset: str = "veryfast",
) -> Path:
    """
    Backward-compatible helper for explicit 720p compression.
    """
    return compress_video_to_max_height(
        video_path=video_path,
        output_path=output_path,
        max_height=720,
        crf=crf,
        preset=preset,
    )
