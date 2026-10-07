from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

import librosa

from .models import MusicSection, SectionPlan, SectionTiming, TimestampedWord


def measure_audio_duration(path: Path, *, fallback_s: float) -> float:
    try:
        duration = float(librosa.get_duration(path=str(path)))
        if duration > 0:
            return duration
    except Exception:
        pass
    return max(0.0, float(fallback_s))


def build_section_timeline(
    section_plan: SectionPlan,
    *,
    actual_total_duration_s: float | None = None,
) -> list[SectionTiming]:
    expected_total = max(0.001, float(section_plan.total_duration_s))
    actual_total = float(actual_total_duration_s or expected_total)
    scale = actual_total / expected_total if expected_total > 0 else 1.0

    timeline: list[SectionTiming] = []
    expected_cursor = 0.0
    actual_cursor = 0.0
    for idx, section in enumerate(section_plan.sections):
        expected_length = max(0.0, float(section.target_duration_s))
        actual_length = expected_length * scale
        expected_end = expected_cursor + expected_length
        actual_end = actual_cursor + actual_length
        if idx == len(section_plan.sections) - 1:
            actual_end = actual_total
        timeline.append(
            SectionTiming(
                section_id=section.section_id,
                expected_start_s=expected_cursor,
                expected_end_s=expected_end,
                actual_start_s=actual_cursor,
                actual_end_s=actual_end,
                confidence=1.0,
            )
        )
        expected_cursor = expected_end
        actual_cursor = actual_end
    return timeline


_SECTION_HEADER_RE = re.compile(r"\[([^\]]+)\]")
_BRACKETS_RE = re.compile(r"[\[\]]")


def strip_section_header_words(
    words: list[TimestampedWord],
    *,
    full_lyrics: Optional[str] = None,
) -> list[TimestampedWord]:
    """Remove section-header tokens from a timestamped-word timeline.

    Providers align the compiled lyric sheet — headers included — so structure
    tags leak into the word timeline as sung "words". Worse, the tokenizer splits
    multi-word headers and loses brackets: ``[Peak & Resolve]`` arrives as
    ``'[Peak'``, ``'&'``, ``'Resolve'``, which no single-token ``[...]`` regex can
    catch. A run therefore only *starts* with a bracketed token; the rest is
    identified by matching the header phrases parsed from ``full_lyrics`` (where
    the tags survive intact). Without lyrics text, only bracketed tokens
    themselves are dropped. Lyric words are never removed on text alone — a run
    must begin with bracket evidence.
    """
    if not words:
        return words
    header_phrases: list[list[str]] = []
    for header in _SECTION_HEADER_RE.findall(full_lyrics or ""):
        phrase = [part.lower() for part in header.split()]
        if phrase:
            header_phrases.append(phrase)
    # Longest first, so "[Warm Build]" wins over a hypothetical "[Warm]".
    header_phrases.sort(key=len, reverse=True)

    def clean(text: str) -> str:
        return _BRACKETS_RE.sub("", text or "").strip().lower()

    # First pass — a COMPLETE tag glued into one token ("[Verse]\nSnowflakes"):
    # substitute the tag away and keep the lyric remainder; drop tokens that were
    # nothing but the tag. Split fragments (no closing bracket) survive to the
    # run-matching below.
    substituted: list[TimestampedWord] = []
    for word in words:
        text = word.text or ""
        if "[" in text and "]" in text:
            residue = _SECTION_HEADER_RE.sub("", text).strip()
            if not residue:
                continue
            word = TimestampedWord(
                text=residue, startS=word.startS, endS=word.endS, i=word.i
            )
        substituted.append(word)
    words = substituted

    out: list[TimestampedWord] = []
    index = 0
    while index < len(words):
        raw = words[index].text or ""
        if "[" in raw or "]" in raw:
            matched = 1  # at minimum, drop the bracketed token itself
            for phrase in header_phrases:
                if index + len(phrase) > len(words) or clean(raw) != phrase[0]:
                    continue
                if all(
                    clean(words[index + offset].text) == phrase[offset]
                    for offset in range(len(phrase))
                ):
                    matched = len(phrase)
                    break
            index += matched
            continue
        out.append(words[index])
        index += 1
    return out


def coerce_timestamped_words(words: Iterable[object] | None) -> list[TimestampedWord]:
    normalized: list[TimestampedWord] = []
    if not words:
        return normalized
    for idx, item in enumerate(words):
        try:
            normalized.append(
                TimestampedWord(
                    text=str(getattr(item, "text", "")),
                    startS=float(getattr(item, "startS")),
                    endS=float(getattr(item, "endS")),
                    i=getattr(item, "i", idx),
                )
            )
        except (TypeError, ValueError):
            continue
    return normalized
