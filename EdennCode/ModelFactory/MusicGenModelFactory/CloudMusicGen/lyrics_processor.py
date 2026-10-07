from __future__ import annotations

from dataclasses import dataclass
from typing import List, Dict, Any, Optional
import re


@dataclass
class WordTS:
    text: str
    startS: float
    endS: float
    i: Optional[int] = None


class LyricsProcessor:
    """
    Utility class for converting raw ProviderC alignedWords payloads into
    a structured list of WordTS objects and higher-level lyric sections.
    """

    SECTION_PATTERN = re.compile(
        r"^\s*(verse|chorus|pre-chorus|bridge|intro|outro|hook)\s*(\d+)?\s*$",
        re.IGNORECASE,
    )

    _SECTION_TITLE = {
        "verse": "Verse",
        "chorus": "Chorus",
        "pre-chorus": "Pre-Chorus",
        "bridge": "Bridge",
        "intro": "Intro",
        "outro": "Outro",
        "hook": "Hook",
    }

    @staticmethod
    def _sorted_words(words: List[WordTS]) -> List[WordTS]:
        if any(w.i is not None for w in words):
            return sorted(words, key=lambda w: (w.startS, w.i if w.i is not None else float("inf")))
        return list(words)

    @classmethod
    def _normalize_section_label(cls, raw: str) -> Optional[str]:
        match = cls.SECTION_PATTERN.match(raw)
        if not match:
            return None
        base, num = match.groups()
        label = cls._SECTION_TITLE[base.lower()]
        if num:
            label = f"{label} {num.strip()}"
        return label

    # ---------- Public API ----------

    @classmethod
    def from_aligned_words(cls, words: List[Dict[str, Any]]) -> List[WordTS]:
        """
        Convert ProviderC alignedWords JSON into sorted WordTS list.
        """
        out = [
            WordTS(
                text=w.get("word") or w.get("text") or "",
                startS=float(w["startS"]),
                endS=float(w["endS"]),
            )
            for w in words
        ]
        out.sort(key=lambda w: w.startS)
        return out

    @classmethod
    def parse_sections(cls, wordts: List[WordTS]) -> List[Dict[str, Any]]:
        """
        Parse timestamped tokens into labeled sections with start/end and aggregated text.
        Returns: list of {"label", "startS", "endS", "text"}
        """
        words = cls._sorted_words(wordts)
        sections: List[Dict[str, Any]] = []

        current_label: Optional[str] = None
        current_start: Optional[float] = None
        current_text_parts: List[str] = []
        current_end: Optional[float] = None

        buffer = ""
        buffer_start: Optional[float] = None

        def close_section(end_time: float) -> None:
            nonlocal current_label, current_start, current_text_parts, current_end
            if current_label is None or current_start is None:
                return
            sections.append(
                {
                    "label": current_label,
                    "startS": current_start,
                    "endS": end_time,
                    "text": "".join(current_text_parts).strip(),
                }
            )
            current_label = None
            current_start = None
            current_text_parts = []
            current_end = None

        for tok in words:
            literal_buf = []
            for ch in tok.text:
                if buffer:
                    buffer += ch
                    if ch == "]":
                        inner = buffer[1:-1]
                        label = cls._normalize_section_label(inner)
                        if label:
                            # close previous section at marker start
                            marker_start = buffer_start if buffer_start is not None else tok.startS
                            close_section(marker_start)
                            # start new section
                            current_label = label
                            current_start = marker_start
                            current_text_parts = []
                            current_end = marker_start
                        else:
                            literal_buf.append(buffer)
                        buffer = ""
                        buffer_start = None
                else:
                    if ch == "[":
                        buffer = "["
                        buffer_start = tok.startS
                    else:
                        literal_buf.append(ch)

            # If buffer is still open (marker split across tokens), wait for next token
            if buffer:
                # do not append partial marker to text yet
                pass
            elif literal_buf and current_label is not None:
                current_text_parts.append("".join(literal_buf))
                current_end = tok.endS
            elif current_label is not None:
                # no literal text but still advance end to cover timing
                current_end = tok.endS

        # Unclosed marker: treat as literal text
        if buffer and current_label is not None:
            current_text_parts.append(buffer)
            current_end = current_end
            buffer = ""

        # Close final section
        if current_label is not None and current_start is not None:
            final_end = current_end if current_end is not None else (words[-1].endS if words else 0.0)
            close_section(final_end)

        return sections

    @classmethod
    def section_length_map(cls, wordts: List[WordTS]) -> Dict[str, Dict[str, float]]:
        """
        Compute lengths per occurrence and aggregated lengths per label.
        Returns {"lengths_by_occurrence": {...}, "lengths_aggregated": {...}}
        """
        sections = cls.parse_sections(wordts)
        by_occurrence: Dict[str, float] = {}
        aggregated: Dict[str, float] = {}
        counts: Dict[str, int] = {}

        for sec in sections:
            label = sec["label"]
            duration = max(0.0, sec["endS"] - sec["startS"])
            counts[label] = counts.get(label, 0) + 1
            by_occurrence[f"{label}#{counts[label]}"] = duration
            aggregated[label] = aggregated.get(label, 0.0) + duration

        return {"lengths_by_occurrence": by_occurrence, "lengths_aggregated": aggregated}
