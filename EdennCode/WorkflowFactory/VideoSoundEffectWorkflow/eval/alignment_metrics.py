"""
Offline alignment metrics for video→SFX projects.

Two complementary measurements:

1. **Audio↔visual alignment** (no labels needed): for every rendered event,
   the perceived audio onset (energy rise of the placed clip on the timeline)
   is compared against the visual motion onset near the event window. This
   scores what a viewer actually experiences — does the sound land on the
   visual moment?
2. **Event-detection quality vs. human labels**: predicted events against a
   hand-labeled event list — greedy temporal-IoU matching → precision/recall/F1
   at IoU thresholds plus mean TempIoU and onset error on matched pairs.

Everything here is numpy + ffmpeg; no model calls, so it runs on any saved
project (ours or a competitor's output muxed into the same shape).
"""

from __future__ import annotations

import json
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.Stages.TimingRefinementStage.timing_refinement_stage import (
    compute_motion_energy,
    find_motion_onset,
    is_discrete_event,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.VideoSoundEffectDataModel.datamodel import (
    SfxProject,
    SoundFXEvent,
)

DEFAULT_TOLERANCES_S = (0.1, 0.25, 0.5)


# --------------------------------------------------------------------- audio


def _read_mono(path: Path) -> Tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        sample_width = wf.getsampwidth()
        if sample_width != 2:
            raise ValueError(
                f"unsupported WAV sample width {sample_width * 8}-bit "
                f"(expected 16-bit PCM): {path}"
            )
        channels = wf.getnchannels()
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
    if channels > 1:
        frames = len(samples) // channels
        samples = samples[: frames * channels].reshape(frames, channels).mean(axis=1)
    return samples, rate


def audio_onset_time(
    wav_path: Path,
    *,
    hop_ms: float = 5.0,
    threshold_ratio: float = 0.25,
) -> Optional[float]:
    """
    Perceived onset of a clip: first time its short-window RMS envelope crosses
    ``threshold_ratio`` × the envelope peak. Robust for one-shot SFX clips.
    """
    samples, rate = _read_mono(Path(wav_path))
    if samples.size == 0:
        return None
    hop = max(1, int(rate * hop_ms / 1000.0))
    frame_count = samples.size // hop
    if frame_count == 0:
        return None
    trimmed = samples[: frame_count * hop].reshape(frame_count, hop)
    rms = np.sqrt((trimmed**2).mean(axis=1))
    peak = float(rms.max())
    if peak <= 1e-6:
        return None
    threshold = peak * threshold_ratio
    idx = int(np.argmax(rms >= threshold))
    return idx * hop / rate


# ------------------------------------------------- audio↔visual alignment


@dataclass
class EventAlignmentResult:
    event_id: str
    event_type: str
    placed_audio_onset_s: Optional[float]
    visual_onset_s: Optional[float]
    error_s: Optional[float]
    llm_start_s: float = 0.0
    refined_start_s: Optional[float] = None
    note: str = ""


@dataclass
class AlignmentReport:
    per_event: List[EventAlignmentResult] = field(default_factory=list)
    measured_events: int = 0
    mean_abs_error_s: Optional[float] = None
    median_abs_error_s: Optional[float] = None
    p90_abs_error_s: Optional[float] = None
    within_tolerance: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "per_event": [asdict(r) for r in self.per_event],
            "measured_events": self.measured_events,
            "mean_abs_error_s": self.mean_abs_error_s,
            "median_abs_error_s": self.median_abs_error_s,
            "p90_abs_error_s": self.p90_abs_error_s,
            "within_tolerance": self.within_tolerance,
        }


def measure_project_alignment(
    project: SfxProject,
    *,
    visual_search_pad_s: float = 0.75,
    motion_fps: float = 30.0,
    tolerances_s: Sequence[float] = DEFAULT_TOLERANCES_S,
) -> AlignmentReport:
    """
    For each audible event: (event start + clip's internal onset) vs. the
    motion onset found near the event window in the source video.
    """
    report = AlignmentReport()
    video_path = Path(project.video_path)

    for event in project.events:
        result = EventAlignmentResult(
            event_id=event.event_id,
            event_type=event.event_type,
            placed_audio_onset_s=None,
            visual_onset_s=None,
            error_s=None,
            llm_start_s=event.start_time,
            refined_start_s=event.refined_start_time,
        )
        report.per_event.append(result)

        if event.muted or not event.active_audio_path:
            result.note = "muted or missing audio"
            continue
        if not is_discrete_event(event.event_type, event.duration):
            result.note = "continuous event — no measurable onset"
            continue
        try:
            clip_onset = audio_onset_time(Path(event.active_audio_path))
        except (OSError, wave.Error, ValueError) as err:
            # One unreadable clip must not kill the whole report/batch eval.
            result.note = f"audio read failed: {err}"
            continue
        if clip_onset is None:
            result.note = "silent clip"
            continue
        result.placed_audio_onset_s = event.effective_start + clip_onset

        window_start = max(0.0, event.start_time - visual_search_pad_s)
        window_end = min(
            project.video_duration,
            max(event.end_time, event.start_time + visual_search_pad_s),
        )
        try:
            times, energy = compute_motion_energy(
                video_path,
                start_s=window_start,
                end_s=window_end,
                sample_fps=motion_fps,
            )
        except Exception as err:  # video decode issues shouldn't kill the report
            result.note = f"motion analysis failed: {err}"
            continue
        visual_onset = find_motion_onset(times, energy)
        if visual_onset is None:
            result.note = "no distinct visual onset in window"
            continue
        result.visual_onset_s = visual_onset
        result.error_s = round(result.placed_audio_onset_s - visual_onset, 4)

    errors = np.array(
        [abs(r.error_s) for r in report.per_event if r.error_s is not None], dtype=np.float64
    )
    report.measured_events = int(errors.size)
    if errors.size:
        report.mean_abs_error_s = round(float(errors.mean()), 4)
        report.median_abs_error_s = round(float(np.median(errors)), 4)
        report.p90_abs_error_s = round(float(np.percentile(errors, 90)), 4)
        for tol in tolerances_s:
            report.within_tolerance[f"{int(tol * 1000)}ms"] = round(
                float((errors <= tol).mean()), 4
            )
    return report


# ------------------------------------------------- label-based event metrics


@dataclass
class LabeledEvent:
    start_time: float
    end_time: float
    description: str = ""
    event_type: str = ""

    @classmethod
    def from_dict(cls, payload: Dict) -> "LabeledEvent":
        return cls(
            start_time=float(payload["start_time"]),
            end_time=float(payload["end_time"]),
            description=str(payload.get("description", "")),
            event_type=str(payload.get("event_type", "")).upper(),
        )


def load_labels(path: Path) -> List[LabeledEvent]:
    """Labels file: JSON list of {start_time, end_time, description?, event_type?}."""
    payload = json.loads(Path(path).read_text())
    return [LabeledEvent.from_dict(item) for item in payload]


def temporal_iou(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    inter = max(0.0, min(a_end, b_end) - max(a_start, b_start))
    union = max(a_end, b_end) - min(a_start, b_start)
    return inter / union if union > 0 else 0.0


@dataclass
class DetectionReport:
    predicted: int
    labeled: int
    matches: List[Dict] = field(default_factory=list)
    mean_matched_iou: Optional[float] = None
    mean_onset_error_s: Optional[float] = None
    per_threshold: Dict[str, Dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return asdict(self)


def measure_event_detection(
    predicted: Sequence[SoundFXEvent],
    labels: Sequence[LabeledEvent],
    *,
    iou_thresholds: Sequence[float] = (0.1, 0.3, 0.5),
) -> DetectionReport:
    """Greedy IoU matching (highest IoU first, one-to-one)."""
    report = DetectionReport(predicted=len(predicted), labeled=len(labels))
    pairs = [
        (temporal_iou(p.start_time, p.end_time, l.start_time, l.end_time), pi, li)
        for pi, p in enumerate(predicted)
        for li, l in enumerate(labels)
    ]
    pairs.sort(key=lambda x: x[0], reverse=True)
    used_pred: set = set()
    used_label: set = set()
    for iou, pi, li in pairs:
        if iou <= 0.0 or pi in used_pred or li in used_label:
            continue
        used_pred.add(pi)
        used_label.add(li)
        report.matches.append(
            {
                "pred_event_id": predicted[pi].event_id,
                "label_index": li,
                "iou": round(iou, 4),
                "onset_error_s": round(predicted[pi].effective_start - labels[li].start_time, 4),
            }
        )

    if report.matches:
        ious = np.array([m["iou"] for m in report.matches])
        onset_errors = np.array([abs(m["onset_error_s"]) for m in report.matches])
        report.mean_matched_iou = round(float(ious.mean()), 4)
        report.mean_onset_error_s = round(float(onset_errors.mean()), 4)

    for threshold in iou_thresholds:
        tp = sum(1 for m in report.matches if m["iou"] >= threshold)
        precision = tp / len(predicted) if predicted else 0.0
        recall = tp / len(labels) if labels else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        report.per_threshold[f"iou_{threshold}"] = {
            "true_positives": tp,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
        }
    return report
