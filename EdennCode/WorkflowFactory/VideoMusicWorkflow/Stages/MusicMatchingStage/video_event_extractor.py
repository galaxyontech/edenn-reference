from __future__ import annotations

import numpy as np
from typing import List, Tuple

from .data_models import VideoEvent
from .media_tools import MediaTools


class VideoEventExtractor:
    """
    Extracts events from a video timeline.

    With detect_cuts=True (default), histogram correlation is used alongside raw
    pixel diff so that hard scene cuts are detected as high-weight anchors
    (kind="cut") rather than being conflated with smooth panning motion.
    """

    def __init__(
        self,
        *,
        max_events: int = 6,
        fps_sample: int = 6,
        min_gap_s: float = 0.35,
        detect_cuts: bool = True,
        cut_corr_threshold: float = 0.70,
    ) -> None:
        self.max_events = max_events
        self.fps_sample = fps_sample
        self.min_gap_s = min_gap_s
        self.detect_cuts = detect_cuts
        self.cut_corr_threshold = cut_corr_threshold

    def extract(self, video_path: str) -> List[VideoEvent]:
        duration_s = MediaTools.duration_seconds(video_path)

        try:
            import cv2  # type: ignore

            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                raise RuntimeError("OpenCV couldn't open video")

            fps = cap.get(cv2.CAP_PROP_FPS)
            if not fps or fps <= 1e-6:
                fps = 30.0
            step = max(1, int(round(fps / self.fps_sample)))

            prev_gray = None
            prev_hist = None
            # (time, effective_score, is_cut)
            diffs: List[Tuple[float, float, bool]] = []
            frame_idx = 0

            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if frame_idx % step != 0:
                    frame_idx += 1
                    continue

                t = frame_idx / fps
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

                if prev_gray is not None:
                    motion_score = float(cv2.absdiff(gray, prev_gray).mean())

                    is_cut = False
                    if self.detect_cuts:
                        hist = cv2.calcHist([gray], [0], None, [64], [0, 256])
                        cv2.normalize(hist, hist, alpha=1.0, beta=0.0, norm_type=cv2.NORM_L1)
                        if prev_hist is not None:
                            corr = float(cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL))
                            is_cut = corr < self.cut_corr_threshold
                        prev_hist = hist

                    # Cuts are boosted so they rank above gradual-motion frames.
                    effective_score = motion_score * (3.0 if is_cut else 1.0)
                    diffs.append((t, effective_score, is_cut))
                elif self.detect_cuts:
                    hist = cv2.calcHist([gray], [0], None, [64], [0, 256])
                    cv2.normalize(hist, hist, alpha=1.0, beta=0.0, norm_type=cv2.NORM_L1)
                    prev_hist = hist

                prev_gray = gray
                frame_idx += 1

            cap.release()

            if not diffs:
                raise RuntimeError("No frame diffs computed")

            diffs.sort(key=lambda x: x[1], reverse=True)
            chosen: List[VideoEvent] = []
            for t, effective_s, is_cut in diffs:
                if t < 0.25 or t > duration_s - 0.15:
                    continue
                if any(abs(t - e.t) < self.min_gap_s for e in chosen):
                    continue
                raw_score = effective_s / (3.0 if is_cut else 1.0)
                w = 1.0 + min(2.0, raw_score / 20.0)
                if is_cut:
                    w = min(3.5, w * 1.5)
                kind = "cut" if is_cut else "motion"
                chosen.append(VideoEvent(t=t, weight=w, kind=kind))
                if len(chosen) >= self.max_events:
                    break

            if not chosen:
                raise RuntimeError("No usable motion events")

            chosen.sort(key=lambda e: e.t)
            return self._normalize_weights(chosen)

        except Exception:
            anchors = [0.25 * duration_s, 0.50 * duration_s, 0.75 * duration_s]
            fallback = [
                VideoEvent(t=a, weight=1.0 + i * 0.25, kind="fallback")
                for i, a in enumerate(anchors)
                if 0.25 < a < duration_s - 0.1
            ]
            return self._normalize_weights(fallback)

    @staticmethod
    def _normalize_weights(events: List[VideoEvent]) -> List[VideoEvent]:
        if not events:
            return events
        w = np.array([e.weight for e in events], dtype=float)
        w = (w - w.min()) / (w.max() - w.min() + 1e-6)
        out = [VideoEvent(t=e.t, weight=1.0 + 1.5 * float(wi), kind=e.kind) for e, wi in zip(events, w)]
        out.sort(key=lambda e: e.t)
        return out
