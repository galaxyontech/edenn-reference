from __future__ import annotations

import re
from typing import List

from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage import WordTS
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.music_matching_stage import (
    MusicMatchingStage,
)

SECTION_TAG_RE = re.compile(r"\[[^\]]+\]", re.IGNORECASE)


def to_ms_wordts(words: List[WordTS]) -> List[WordTS]:
    scaled: List[WordTS] = []
    for word in words:
        try:
            scaled.append(
                WordTS(
                    text=getattr(word, "text", ""),
                    startS=float(getattr(word, "startS", 0.0)) * 1000.0,
                    endS=float(getattr(word, "endS", 0.0)) * 1000.0,
                    i=getattr(word, "i", None),
                )
            )
        except Exception:
            continue
    return scaled or words


def strip_section_tags(words: List[WordTS]) -> List[WordTS]:
    cleaned: List[WordTS] = []
    for word in words:
        text = SECTION_TAG_RE.sub("", word.text or "")
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        cleaned.append(
            WordTS(
                text=text,
                startS=getattr(word, "startS", 0.0),
                endS=getattr(word, "endS", 0.0),
                i=getattr(word, "i", None),
            )
        )
    return cleaned or words


def contains_cjk(text: str) -> bool:
    for ch in text or "":
        codepoint = ord(ch)
        if (
            0x4E00 <= codepoint <= 0x9FFF
            or 0x3400 <= codepoint <= 0x4DBF
            or 0x3040 <= codepoint <= 0x30FF
            or 0xAC00 <= codepoint <= 0xD7AF
        ):
            return True
    return False


def join_words_for_line(words: List[str], reference_text: str) -> str:
    tokens = [str(word or "").strip() for word in words if str(word or "").strip()]
    if not tokens:
        return ""
    if contains_cjk(reference_text):
        return "".join(tokens)

    no_space_suffixes = {"'s", "'re", "'ve", "'ll", "'d", "'m", "n't"}
    no_space_before = set(".,!?;:%)]}")
    no_space_after = set("([{")

    result = tokens[0]
    for token in tokens[1:]:
        if token in no_space_suffixes:
            result += token
        elif token[:1] in no_space_before:
            result += token
        elif result[-1:] in no_space_after:
            result += token
        else:
            result += f" {token}"
    return result.strip()


def align_line_level_lyrics_to_window(
    line_level_lyrics: List[WordTS],
    word_level_lyrics: List[WordTS],
    window_start_s: float,
    window_duration_s: float,
) -> List[WordTS]:
    if not line_level_lyrics:
        return []
    if not word_level_lyrics:
        return MusicMatchingStage._offset_word_ts(
            line_level_lyrics,
            window_start_s,
            window_duration_s,
        )

    aligned_lines: List[WordTS] = []
    word_index = 0
    total_words = len(word_level_lyrics)

    for line in line_level_lyrics:
        line_start = float(getattr(line, "startS", 0.0))
        line_end = float(getattr(line, "endS", 0.0))

        while (
            word_index < total_words
            and float(getattr(word_level_lyrics[word_index], "endS", 0.0)) <= line_start
        ):
            word_index += 1

        visible_words: List[WordTS] = []
        scan_index = word_index
        while scan_index < total_words:
            word = word_level_lyrics[scan_index]
            word_start = float(getattr(word, "startS", 0.0))
            word_end = float(getattr(word, "endS", 0.0))
            if word_start >= line_end:
                break

            aligned_start = word_start - window_start_s
            aligned_end = word_end - window_start_s
            if aligned_end > 0 and aligned_start < window_duration_s:
                visible_words.append(
                    WordTS(
                        text=getattr(word, "text", ""),
                        startS=max(0.0, aligned_start),
                        endS=min(window_duration_s, aligned_end),
                        i=getattr(word, "i", None),
                    )
                )
            scan_index += 1

        if visible_words:
            line_text = join_words_for_line(
                [word.text for word in visible_words],
                getattr(line, "text", ""),
            ) or getattr(line, "text", "")
            aligned_lines.append(
                WordTS(
                    text=line_text,
                    startS=visible_words[0].startS,
                    endS=visible_words[-1].endS,
                    i=getattr(line, "i", None),
                )
            )
            word_index = scan_index
            continue

        clipped_line = MusicMatchingStage._offset_word_ts(
            [line],
            window_start_s,
            window_duration_s,
        )
        if clipped_line:
            aligned_lines.extend(clipped_line)

    return aligned_lines
