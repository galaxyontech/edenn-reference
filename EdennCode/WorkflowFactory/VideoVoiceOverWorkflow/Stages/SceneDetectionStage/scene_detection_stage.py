from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from EdennCode.Util.MediaUtils import detect_scene_cuts, get_video_duration
from EdennCode.exceptions import EdennMediaProcessingError


@dataclass(slots=True)
class SceneSegment:
    """Simple timestamp window describing a raw visual scene."""

    start_sec: float
    end_sec: float

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict containing start/end seconds."""
        return {"start_sec": float(self.start_sec), "end_sec": float(self.end_sec)}


@dataclass(slots=True)
class SceneDetectionStageInput:
    video_path: Path
    scene_threshold: Optional[float] = None
    min_scene_length: Optional[float] = None
    scene_detector: Optional[str] = None
    scene_detection_method: Optional[str] = None
    max_scenes: Optional[int] = None


@dataclass(slots=True)
class SceneDetectionStageOutput:
    segments: List[SceneSegment]


class SceneDetectionStage:
    """Low-level visual segmentation mirroring the video-music workflow."""

    def __init__(
        self,
        *,
        scene_threshold: float = 3.0,
        min_scene_length: float = 1.0,
        scene_detector: str = "auto",
        scene_detection_method: str = "adaptive",
        ffprobe_scene_threshold: float = 0.2,
        max_scenes: Optional[int] = 50,
        pyscene_backend: str = "opencv",
    ) -> None:
        """
        Configure default scene detection parameters.

        Inputs:
        - scene_threshold: score threshold for cut detection (used by ffprobe or PyScene).
        - min_scene_length: minimum duration (seconds) per scene.
        - scene_detector: "auto", "ffprobe", or PyScene backend selector.
        - scene_detection_method: PyScene method ("adaptive" or "content").
        - ffprobe_scene_threshold: ffprobe-specific threshold (overridden by scene_threshold when detector=ffprobe).
        - max_scenes: cap on number of segments returned (None = no cap).
        - pyscene_backend: backend for PyScene detection (e.g., "opencv").

        Output:
        - Initializes instance defaults used when inputs are not provided in run().
        """
        self.scene_threshold = scene_threshold
        self.min_scene_length = min_scene_length
        self.scene_detector = scene_detector
        self.scene_detection_method = scene_detection_method
        self.ffprobe_scene_threshold = ffprobe_scene_threshold
        self.max_scenes = max_scenes
        self.pyscene_backend = pyscene_backend

    def run(self, stage_input: SceneDetectionStageInput) -> SceneDetectionStageOutput:
        """
        Detect scene boundaries and return normalized scene segments.

        Inputs:
        - stage_input.video_path: source video file path.
        - stage_input.scene_threshold/min_scene_length/scene_detector/scene_detection_method/max_scenes:
          optional overrides for the defaults configured in __init__.

        Output:
        - SceneDetectionStageOutput containing ordered SceneSegment windows covering the video.
        """
        video_path = stage_input.video_path
        duration = get_video_duration(video_path)

        threshold_value = (
            stage_input.scene_threshold if stage_input.scene_threshold is not None else self.scene_threshold
        )
        min_scene_length = (
            stage_input.min_scene_length if stage_input.min_scene_length is not None else self.min_scene_length
        )
        max_scenes = stage_input.max_scenes if stage_input.max_scenes is not None else self.max_scenes

        detector = (stage_input.scene_detector or self.scene_detector).lower()
        method = (stage_input.scene_detection_method or self.scene_detection_method).lower()

        ffprobe_threshold = self.ffprobe_scene_threshold

        if detector == "ffprobe":
            ffprobe_threshold = threshold_value

        cuts = detect_scene_cuts(
            video_path,
            ffprobe_threshold,
            detector=detector,
            pyscene_method=method,
            pyscene_adaptive_threshold=threshold_value if method == "adaptive" else None,
            pyscene_content_threshold=threshold_value if method == "content" else None,
            min_scene_len_s=min_scene_length,
            backend=self.pyscene_backend,
        )

        if duration > 0:
            cuts.append(duration)

        cuts = sorted(set(cuts))
        filtered = [cuts[0]]
        for ts in cuts[1:]:
            if ts - filtered[-1] >= min_scene_length:
                filtered.append(ts)
        if duration:
            if len(filtered) == 1:
                filtered.append(duration)
            elif filtered[-1] != duration:
                filtered[-1] = duration

        windows: List[Tuple[float, float]] = []
        for start, end in zip(filtered[:-1], filtered[1:]):
            if duration:
                end = min(end, duration)
            if end > start:
                windows.append((start, end))
        if not windows and duration:
            windows.append((0.0, duration))

        if max_scenes and max_scenes > 0 and len(windows) > max_scenes:
            windows = self._downsample_windows(windows, max_scenes)

        segments = [SceneSegment(start_sec=start, end_sec=end) for start, end in windows]

        if not segments:
            if duration <= 0:
                raise EdennMediaProcessingError("Unable to determine fallback duration for video.", component="voiceover_scene_detection", operation="determine_duration")
            segments = [SceneSegment(start_sec=0.0, end_sec=duration)]

        return SceneDetectionStageOutput(segments=segments)

    @staticmethod
    def _downsample_windows(windows: List[Tuple[float, float]], max_scenes: int) -> List[Tuple[float, float]]:
        """
        Reduce a list of scene windows to a fixed count while preserving coverage.

        Inputs:
        - windows: ordered (start, end) tuples in seconds.
        - max_scenes: desired maximum number of windows.

        Output:
        - A subset of windows sized at max_scenes (or fewer if input is smaller).
        """
        if max_scenes <= 0 or len(windows) <= max_scenes:
            return windows

        if max_scenes == 1:
            return [windows[len(windows) // 2]]

        step = (len(windows) - 1) / (max_scenes - 1)
        selected_indices: List[int] = []
        for i in range(max_scenes):
            idx = int(round(i * step))
            if selected_indices:
                idx = max(idx, selected_indices[-1] + 1)
            if idx >= len(windows):
                idx = len(windows) - 1
            selected_indices.append(idx)

        deduped: List[int] = []
        for idx in selected_indices:
            if deduped and idx == deduped[-1]:
                continue
            deduped.append(idx)

        while len(deduped) < max_scenes and deduped[-1] < len(windows) - 1:
            deduped.append(deduped[-1] + 1)

        return [windows[i] for i in deduped[:max_scenes]]
