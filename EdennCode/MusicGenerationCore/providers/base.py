from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from EdennCode.Util.MediaUtils import resolve_ffmpeg_binary

from ..audio import coerce_timestamped_words, measure_audio_duration
from ..models import MusicGenerationRequest, MusicGenerationResult, TimestampedWord

# A provider that closes its tracks with its own audible tag has that tail
# chopped off before delivery, regardless of watermark. Which providers do that
# is DECLARED per strategy (appends_provider_tail) and verified against real
# provider audio — a chop applied to a provider that ends its tracks musically
# just deletes six seconds of the customer's music.
FULL_TRACK_TAIL_TRIM_S = 6.0
MIN_TRIMMED_AUDIO_DURATION_S = 0.25


class MusicGenerationStrategy(ABC):
    # Subclasses set these in __init__; declared here so the shared trim helper
    # below has a sane default even if a subclass forgets.
    extension_tolerance_s: float = 0.5

    # Does this provider append its own trailing tag to a finished track? The
    # service strips the tail from every delivered variant of a strategy that
    # says yes. Declared rather than inferred: a strategy that does not tag its
    # output must not lose six seconds of music to a defensive chop, and a new
    # provider that does tag must state it here instead of being discovered in
    # a customer's deliverable.
    appends_provider_tail: bool = False

    @abstractmethod
    async def generate(self, request: MusicGenerationRequest) -> MusicGenerationResult:
        raise NotImplementedError

    @staticmethod
    def _trimmed_tail_path(path: Path, *, tail_trim_s: float) -> Path:
        suffix = path.suffix or ".mp3"
        label = ("%g" % tail_trim_s).replace(".", "p")
        return path.with_name(f"{path.stem}_trimmed_tail{label}s{suffix}")

    @staticmethod
    def _ffmpeg_audio_codec_args(path: Path) -> list[str]:
        suffix = path.suffix.lower()
        if suffix == ".wav":
            return ["-c:a", "pcm_s16le"]
        if suffix == ".mp3":
            return ["-c:a", "libmp3lame", "-b:a", "192k"]
        if suffix in {".m4a", ".mp4", ".aac"}:
            return ["-c:a", "aac", "-b:a", "192k"]
        return ["-c:a", "aac", "-b:a", "192k"]

    def _trim_full_track_tail(
        self,
        audio_path: Path,
        *,
        minimum_required_duration_s: float = 0.0,
        tail_trim_s: float = FULL_TRACK_TAIL_TRIM_S,
    ) -> tuple[Path, float]:
        """Chop ``tail_trim_s`` seconds off the end of a full track.

        A no-op for a provider that does not tag its output: there is nothing to
        remove there, and cutting anyway deletes six seconds of the customer's
        music. Gated here rather than at the call sites so a provider that starts
        tagging is re-enabled by flipping its declaration.

        For a tagging provider the chop is unconditional (only the sliver floor
        below applies): a chopped track that no longer covers its target is the
        extension loop's problem, not a reason to keep the provider's tail.
        """

        if not self.appends_provider_tail:
            return audio_path, measure_audio_duration(audio_path, fallback_s=0.0)
        duration_s = measure_audio_duration(audio_path, fallback_s=0.0)
        if duration_s <= 0 or tail_trim_s <= 0:
            return audio_path, duration_s

        trimmed_duration_s = duration_s - float(tail_trim_s)
        if (
            trimmed_duration_s + self.extension_tolerance_s
            < float(minimum_required_duration_s)
            or trimmed_duration_s <= MIN_TRIMMED_AUDIO_DURATION_S
        ):
            return audio_path, duration_s

        output_path = self._trimmed_tail_path(audio_path, tail_trim_s=tail_trim_s)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            resolve_ffmpeg_binary(),
            "-y",
            "-i",
            str(audio_path),
            "-vn",
            "-t",
            f"{trimmed_duration_s:.3f}",
            *self._ffmpeg_audio_codec_args(output_path),
            str(output_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return output_path, measure_audio_duration(
            output_path,
            fallback_s=trimmed_duration_s,
        )

    @staticmethod
    def _clip_words_to_duration(
        words: list[object],
        duration_s: float,
    ) -> list[TimestampedWord]:
        if duration_s <= 0:
            return coerce_timestamped_words(words)
        clipped: list[TimestampedWord] = []
        duration_s = max(0.0, float(duration_s))
        for word in coerce_timestamped_words(words):
            if word.startS >= duration_s:
                continue
            clipped_end_s = min(word.endS, duration_s)
            if clipped_end_s <= word.startS:
                continue
            clipped.append(
                TimestampedWord(
                    text=word.text,
                    startS=word.startS,
                    endS=clipped_end_s,
                    i=word.i,
                )
            )
        return clipped
