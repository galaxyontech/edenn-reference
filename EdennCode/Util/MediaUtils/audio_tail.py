"""Remove a provider's trailing tag from a generated track.

Every paid provider signs the end of what it returns. Nothing we hand a
customer may carry that signature, so any path that downloads a full track owes
it a chop before the track is cut into, muxed, or watermarked — watermarking in
particular is one-way, since our clip is concatenated onto the end and seals
anything left behind mid-file.

The music pipelines each grew their own copy of this with a *conditional* chop
that stands down rather than shorten a track below what a caller asked for. This
one has no such condition on purpose: it is for delivery boundaries, where a
short track is a coverage problem someone downstream pads for, and a tagged
track is not recoverable at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .ffmpeg_utils import get_video_duration, resolve_ffmpeg_binary


PROVIDER_TAIL_TRIM_S = 6.0
MIN_TRIMMED_AUDIO_DURATION_S = 0.25


def tail_trim_marker(tail_trim_s: float = PROVIDER_TAIL_TRIM_S) -> str:
    label = ("%g" % tail_trim_s).replace(".", "p")
    return f"_trimmed_tail{label}s"


def is_tail_trimmed(
    audio_path: Path,
    *,
    tail_trim_s: float = PROVIDER_TAIL_TRIM_S,
) -> bool:
    """Was the tail already chopped off *this* file?

    Only a trailing marker counts. Extensions name their output
    ``{stem}_extend{n}``, so a track chopped before extension carries the marker
    mid-stem while its real tail is whatever the provider just appended.
    """

    return Path(audio_path).stem.endswith(tail_trim_marker(tail_trim_s))


def _duration_s(path: Path, *, required: bool) -> float:
    """Probe a duration, refusing to guess.

    A chop that silently no-ops on a file it cannot measure is worse than no
    chop at all: it reports success and ships the tag.
    """

    try:
        duration_s = get_video_duration(path)
    except Exception as exc:
        if not required:
            return 0.0
        raise RuntimeError(
            f"Cannot strip the provider tail from {path}: its duration is "
            "unreadable, and delivering it unchopped would ship the provider's tag."
        ) from exc
    if duration_s <= 0 and required:
        raise RuntimeError(
            f"Cannot strip the provider tail from {path}: probed duration is "
            f"{duration_s}, and delivering it unchopped would ship the provider's tag."
        )
    return duration_s


def _audio_codec_args(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    if suffix == ".wav":
        return ["-c:a", "pcm_s16le"]
    if suffix == ".mp3":
        return ["-c:a", "libmp3lame", "-b:a", "192k"]
    return ["-c:a", "aac", "-b:a", "192k"]


def strip_provider_tail(
    audio_path: Path,
    *,
    tail_trim_s: float = PROVIDER_TAIL_TRIM_S,
) -> tuple[Path, float]:
    """Chop the provider's tail off a track. Returns (path, duration_s).

    Returns the input untouched when it was already chopped, when it is too
    short to survive the chop, or when the chop would leave nothing — never
    silently, since a duration we cannot read raises rather than handing back a
    file we have not actually cleaned.
    """

    audio_path = Path(audio_path)
    if is_tail_trimmed(audio_path, tail_trim_s=tail_trim_s):
        return audio_path, _duration_s(audio_path, required=False)

    duration_s = _duration_s(audio_path, required=True)
    trimmed_duration_s = duration_s - float(tail_trim_s)
    if trimmed_duration_s <= MIN_TRIMMED_AUDIO_DURATION_S:
        return audio_path, duration_s

    suffix = audio_path.suffix or ".mp3"
    output_path = audio_path.with_name(
        f"{audio_path.stem}{tail_trim_marker(tail_trim_s)}{suffix}"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            resolve_ffmpeg_binary(), "-y",
            "-i", str(audio_path),
            "-vn",
            "-t", f"{trimmed_duration_s:.3f}",
            *_audio_codec_args(output_path),
            str(output_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return output_path, _duration_s(output_path, required=False) or trimmed_duration_s
