from __future__ import annotations

from dataclasses import dataclass

import librosa
import numpy as np


@dataclass
class MusicAnalysisResult:
    onset_times_s: np.ndarray
    beat_times_s: np.ndarray
    rms_curve: np.ndarray
    sr: int
    tempo_bpm: float
    hop_length: int


class MusicAnalyzer:
    """Computes onset times, beat grid, tempo, and RMS energy curve."""

    def __init__(self, *, hop_length: int = 512, use_beats: bool = True) -> None:
        self.hop_length = hop_length
        self.use_beats = use_beats

    def analyze(self, music_path: str) -> MusicAnalysisResult:
        y, sr = librosa.load(music_path, sr=None, mono=True)

        onset_frames = librosa.onset.onset_detect(
            y=y, sr=sr, hop_length=self.hop_length, units="frames"
        )
        onset_times = librosa.frames_to_time(
            onset_frames, sr=sr, hop_length=self.hop_length
        ).astype(float)

        if self.use_beats and len(y) > 0:
            try:
                tempo_arr, beat_frames = librosa.beat.beat_track(
                    y=y, sr=sr, hop_length=self.hop_length
                )
                tempo_bpm = float(np.atleast_1d(tempo_arr)[0])
                beat_times = librosa.frames_to_time(
                    beat_frames, sr=sr, hop_length=self.hop_length
                ).astype(float)
                if tempo_bpm < 40.0 or tempo_bpm > 250.0 or len(beat_times) == 0:
                    raise ValueError("implausible tempo")
            except Exception:
                tempo_bpm = 120.0
                beat_times = np.array([], dtype=float)
        else:
            tempo_bpm = 120.0
            beat_times = np.array([], dtype=float)

        rms = librosa.feature.rms(y=y, hop_length=self.hop_length)[0].astype(float)
        return MusicAnalysisResult(
            onset_times_s=onset_times,
            beat_times_s=beat_times,
            rms_curve=rms,
            sr=sr,
            tempo_bpm=tempo_bpm,
            hop_length=self.hop_length,
        )

    def rms_slice(self, rms_curve: np.ndarray, sr: int, start_s: float, end_s: float) -> np.ndarray:
        a_f = int(max(0, round(start_s * sr / self.hop_length)))
        b_f = int(max(a_f + 1, round(end_s * sr / self.hop_length)))
        b_f = min(b_f, len(rms_curve))
        return rms_curve[a_f:b_f]
