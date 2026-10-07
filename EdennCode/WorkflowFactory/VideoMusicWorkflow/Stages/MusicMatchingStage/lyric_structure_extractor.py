from __future__ import annotations

import re
from typing import Dict, List, Sequence, Tuple

from .data_models import MusicEvent
from .word_ts import WordTS


class LyricStructureExtractor:
    """
    Converts WordTS into lyric structure events to improve alignment:
      - section_start: tokens containing [Verse], [Chorus], etc.
      - line_start: tokens that begin a new line (based on newline markers)
    Also provides lyric coverage stats for gating "must involve lyrics".
    """

    SECTION_RE = re.compile(
        r"\[(verse|chorus|pre[- ]?chorus|bridge|outro)(?:\s+\d+)?\]",
        re.IGNORECASE,
    )

    def extract_events(self, word_ts: Sequence[WordTS]) -> Tuple[List[MusicEvent], List[float]]:
        words = sorted(word_ts, key=lambda w: w.startS)

        events: List[MusicEvent] = []
        line_starts: List[float] = []

        prev_text = ""
        for w in words:
            txt = w.text or ""
            t = float(w.startS)

            # section markers
            if self.SECTION_RE.search(txt):
                events.append(MusicEvent(t=t, weight=1.6, kind="section_start"))
                line_starts.append(t)

            # line start heuristics
            prev_has_nl = ("\n" in prev_text)
            cur_has_marker_or_nl = (txt.lstrip().startswith("[") or txt.lstrip().startswith("\n"))
            if prev_has_nl or cur_has_marker_or_nl:
                events.append(MusicEvent(t=t, weight=1.2, kind="line_start"))
                line_starts.append(t)

            prev_text = txt

        # De-dup near-identical events
        events.sort(key=lambda e: e.t)
        dedup: List[MusicEvent] = []
        for e in events:
            if dedup and abs(e.t - dedup[-1].t) < 0.08 and e.kind == dedup[-1].kind:
                continue
            dedup.append(e)

        line_starts = sorted(set(round(t, 3) for t in line_starts))
        return dedup, line_starts

    def lyric_presence(self, word_ts: Sequence[WordTS], win_start: float, win_end: float) -> Dict[str, float]:
        """
        Returns:
          - coverage_s: total overlapped time by words in window
          - coverage_ratio: coverage_s / window_duration
          - word_count: number of tokens overlapped
        """
        win_start = float(win_start)
        win_end = float(win_end)
        dur = max(1e-6, win_end - win_start)

        coverage = 0.0
        count = 0
        for w in word_ts:
            a = max(win_start, float(w.startS))
            b = min(win_end, float(w.endS))
            if b > a:
                coverage += (b - a)
                count += 1

        return {
            "coverage_s": float(coverage),
            "coverage_ratio": float(coverage / dur),
            "word_count": float(count),
        }
