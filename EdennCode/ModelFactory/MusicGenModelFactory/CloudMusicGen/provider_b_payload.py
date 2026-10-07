"""Pure payload helpers for the upstream music API.

Stateless functions that shape outbound request fields and that extract URLs
and lyric timestamps from upstream JSON payloads, plus decoding of error
envelopes from HTTP responses. Kept apart from the provider so the
orchestration file stays focused on cycle, key, and credit logic.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.lyrics_processor import WordTS


_SECTION_LABEL = re.compile(r"^\s*\[[^\]]+\]\s*$")


# --- Outbound prompt shaping ------------------------------------------------

#: Hard ceiling the upstream API enforces on a generation prompt. A request
#: above it is rejected outright (HTTP 400) and the generation never starts, so
#: the cap has to be applied before the payload leaves this layer.
MAX_PROMPT_CHARS = 1024

# A period only ends a sentence when whitespace follows it, so decimals
# ("128.5 BPM") and timecodes stay intact.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")

# Scene enrichment is emitted as one sentence of ";"-joined entries, so clauses
# are the grain to trim at once whole sentences no longer fit.
_CLAUSE_BOUNDARY = re.compile(r"(?<=;)\s+")

# Trailing joiners left dangling by a mid-sentence cut.
_DANGLING_SEPARATORS = " \t\n,;:\u2014\u2013-"


def _truncate_on_word_boundary(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` characters without splitting a word."""
    head = text[:limit]
    if not text[limit:limit + 1].isspace():
        # The cut landed inside a word — fall back to the last whitespace.
        trimmed = head.rstrip()
        boundary = max(trimmed.rfind(" "), trimmed.rfind("\n"), trimmed.rfind("\t"))
        if boundary > 0:
            head = trimmed[:boundary]
    return head.rstrip(_DANGLING_SEPARATORS)


def _pack_leading_clauses(sentence: str, budget: int) -> str:
    """Keep as many leading ";"-separated clauses of ``sentence`` as fit.

    Returns "" when the sentence has no internal clause boundary, so a single
    indivisible sentence is left out whole rather than cut short.
    """
    if budget <= 0:
        return ""
    clauses = [c.strip() for c in _CLAUSE_BOUNDARY.split(sentence) if c.strip()]
    if len(clauses) <= 1:
        return ""
    kept: List[str] = []
    used = 0
    for clause in clauses:
        cost = len(clause) + (1 if kept else 0)
        if used + cost > budget:
            break
        kept.append(clause)
        used += cost
    if not kept:
        return ""
    packed = " ".join(kept).rstrip(_DANGLING_SEPARATORS)
    if packed and packed[-1] not in ".!?" and len(packed) < budget:
        packed += "."
    return packed


def clamp_prompt(prompt: str, *, limit: int = MAX_PROMPT_CHARS) -> str:
    """Clamp a generation prompt to the provider's hard character limit.

    Prompts arriving here are assembled upstream: the creative direction comes
    first and scene/arc enrichment is appended after it. Dropping whole
    trailing sentences therefore sheds the least load-bearing material and
    leaves the musical direction intact. Only when the first sentence alone
    already exceeds the limit do we cut on a word boundary — never mid-word.
    """
    text = (prompt or "").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text

    kept: List[str] = []
    used = 0
    overflow = ""
    for sentence in _SENTENCE_BOUNDARY.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        cost = len(sentence) + (1 if kept else 0)  # +1 for the joining space
        if used + cost > limit:
            overflow = sentence
            break
        kept.append(sentence)
        used += cost

    # The first sentence that no longer fits whole is the enrichment tail. Keep
    # as many of its leading clauses as the leftover budget allows instead of
    # discarding the whole sentence and the budget with it.
    if overflow:
        partial = _pack_leading_clauses(overflow, limit - used - (1 if kept else 0))
        if partial:
            kept.append(partial)

    if kept:
        return " ".join(kept)
    return _truncate_on_word_boundary(text, limit)


@dataclass
class ProviderBTimestampedLyrics:
    line_level: List[WordTS]
    word_level: List[WordTS]


# --- URL extraction ---------------------------------------------------------

def find_first_url_by_keys(node: Any, keys: List[str]) -> Optional[str]:
    keyset = {k.lower() for k in keys}
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                lk = k.lower()
                if lk in keyset and isinstance(v, str) and v.startswith("http"):
                    return v
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            for item in cur:
                if isinstance(item, (dict, list)):
                    stack.append(item)
    return None


def find_first_scalar_by_keys(node: Any, keys: List[str]) -> Optional[str]:
    keyset = {k.lower() for k in keys}
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for key, value in cur.items():
                if key.lower() in keyset and value is not None:
                    text = str(value).strip()
                    if text:
                        return text
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(cur, list):
            for item in cur:
                if isinstance(item, (dict, list)):
                    stack.append(item)
    return None


_AUDIO_URL_KEYS = [
    "wav_url",
    "wavUrl",
    "audio_url",
    "audioUrl",
    "mp3_url",
    "mp3Url",
    "stream_url",
    "streamUrl",
    "url",
]


def extract_audio_url(payload: Dict[str, Any]) -> Optional[str]:
    return find_first_url_by_keys(payload, _AUDIO_URL_KEYS)


def extract_choice_audio_url(payload: Dict[str, Any], choice_index: int) -> Optional[str]:
    """Audio URL of one specific take, so audio and lyric alignment can be
    paired deterministically from the same choice object."""
    choices = payload.get("choices") or []
    if not isinstance(choices, list):
        return None
    if choice_index < 0 or choice_index >= len(choices):
        return None
    choice = choices[choice_index]
    if not isinstance(choice, dict):
        return None
    return extract_audio_url(choice)


def extract_audio_urls(payload: Dict[str, Any], *, limit: Optional[int] = None) -> List[str]:
    urls: List[str] = []
    seen: set[str] = set()

    def add_url(candidate: Optional[str]) -> bool:
        if not candidate or candidate in seen:
            return False
        seen.add(candidate)
        urls.append(candidate)
        return limit is not None and len(urls) >= limit

    if add_url(extract_audio_url(payload)):
        return urls

    for list_key in ("audio_urls", "audioUrls", "urls"):
        raw_list = payload.get(list_key)
        if not isinstance(raw_list, list):
            continue
        for item in raw_list:
            candidate = item if isinstance(item, str) else extract_audio_url(item)
            if add_url(candidate):
                return urls

    choices = payload.get("choices") or []
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            if add_url(extract_audio_url(choice)):
                break

    return urls


# --- Lyrics timestamp extraction --------------------------------------------

def lyrics_sections_from_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    sections = payload.get("lyrics_sections") or []
    if sections:
        return sections

    choices = payload.get("choices") or []
    if isinstance(choices, list):
        for choice in choices:
            if isinstance(choice, dict) and choice.get("lyrics_sections"):
                return choice.get("lyrics_sections") or []
    return []


def lyrics_sections_from_choice(
    payload: Dict[str, Any],
    choice_index: int,
) -> List[Dict[str, Any]]:
    choices = payload.get("choices") or []
    if not isinstance(choices, list):
        return []
    if choice_index < 0 or choice_index >= len(choices):
        return []
    choice = choices[choice_index]
    if not isinstance(choice, dict):
        return []
    return choice.get("lyrics_sections") or []


def extract_timestamped_lines_from_sections(
    sections: List[Dict[str, Any]],
) -> List[WordTS]:
    """Extract line-level timestamps from already-selected lyrics_sections."""
    lines: List[WordTS] = []
    for sec in sections:
        for line in sec.get("lines", []) or []:
            text = str(line.get("text") or "").strip()
            if not text or _SECTION_LABEL.match(text):
                continue
            try:
                start_ms = float(line.get("start", 0))
                end_ms = float(line.get("end", 0))
            except (TypeError, ValueError):
                continue
            lines.append(
                WordTS(
                    text=text,
                    startS=start_ms / 1000.0,
                    endS=end_ms / 1000.0,
                    i=len(lines),
                )
            )
    return lines


def extract_timestamped_words_from_sections(
    sections: List[Dict[str, Any]],
) -> List[WordTS]:
    """Extract word-level timestamps from already-selected lyrics_sections."""
    words: List[WordTS] = []
    for sec in sections:
        for line in sec.get("lines", []) or []:
            line_words: List[WordTS] = []
            for word in line.get("words", []) or []:
                text = str(word.get("text") or "").strip()
                if not text or _SECTION_LABEL.match(text):
                    continue
                try:
                    start_ms = float(word.get("start", 0))
                    end_ms = float(word.get("end", 0))
                except (TypeError, ValueError):
                    continue
                line_words.append(
                    WordTS(
                        text=text,
                        startS=start_ms / 1000.0,
                        endS=end_ms / 1000.0,
                        i=len(words) + len(line_words),
                    )
                )
            words.extend(line_words)
    return words


def extract_timestamped_lines(payload: Dict[str, Any]) -> List[WordTS]:
    """Extract line-level timestamps from lyrics_sections (convert ms to seconds)."""
    return extract_timestamped_lines_from_sections(
        lyrics_sections_from_payload(payload)
    )


def extract_timestamped_words(payload: Dict[str, Any]) -> List[WordTS]:
    """Extract word-level timestamps, falling back to line-level timestamps."""
    sections = lyrics_sections_from_payload(payload)
    return (
        extract_timestamped_words_from_sections(sections)
        or extract_timestamped_lines_from_sections(sections)
    )


def extract_timestamped_lyrics(payload: Dict[str, Any]) -> ProviderBTimestampedLyrics:
    return ProviderBTimestampedLyrics(
        line_level=extract_timestamped_lines(payload),
        word_level=extract_timestamped_words(payload),
    )


def extract_timestamped_lyrics_for_choice(
    payload: Dict[str, Any],
    choice_index: int,
) -> ProviderBTimestampedLyrics:
    sections = lyrics_sections_from_choice(payload, choice_index)
    line_level = extract_timestamped_lines_from_sections(sections)
    word_level = extract_timestamped_words_from_sections(sections) or list(line_level)
    return ProviderBTimestampedLyrics(
        line_level=line_level,
        word_level=word_level,
    )


# --- HTTP error envelope decoding ------------------------------------------

def http_error_details(resp: httpx.Response) -> Dict[str, Any]:
    try:
        body: Any = resp.json()
    except ValueError:
        text = (resp.text or "").strip()
        return {
            "trace_id": "",
            "provider_code": "",
            "error_message": "",
            "body_preview": text[:1500],
        }

    trace_id = ""
    provider_code: Any = ""
    error_message = ""
    if isinstance(body, dict):
        trace_id = str(body.get("trace_id")
                       or body.get("traceId") or "").strip()
        provider_code = body.get("code") or ""
        error = body.get("error")
        if isinstance(error, dict):
            error_message = str(
                error.get("message")
                or error.get("msg")
                or error.get("detail")
                or ""
            ).strip()
        elif error:
            error_message = str(error).strip()
        if not error_message:
            error_message = str(
                body.get("message")
                or body.get("msg")
                or body.get("detail")
                or ""
            ).strip()

    return {
        "trace_id": trace_id,
        "provider_code": provider_code,
        "error_message": error_message,
        "body_preview": json.dumps(body, ensure_ascii=False, default=str)[:1500],
    }


__all__ = [
    "MAX_PROMPT_CHARS",
    "ProviderBTimestampedLyrics",
    "clamp_prompt",
    "find_first_url_by_keys",
    "find_first_scalar_by_keys",
    "extract_audio_url",
    "extract_audio_urls",
    "lyrics_sections_from_payload",
    "lyrics_sections_from_choice",
    "extract_timestamped_lines_from_sections",
    "extract_timestamped_words_from_sections",
    "extract_timestamped_lines",
    "extract_timestamped_words",
    "extract_timestamped_lyrics",
    "extract_timestamped_lyrics_for_choice",
    "http_error_details",
]
