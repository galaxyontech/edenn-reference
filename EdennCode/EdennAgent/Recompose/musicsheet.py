"""MusicSheet: the track's objective structure (knob-independent, cached).

Beat grid + tempo + normalized per-beat energy (proven in the W0 spike),
plus phrase boundaries from smoothed-energy valleys — passages align to
these so semantic focus changes where the music breathes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

import numpy as np

from .domain import MusicPhrase, MusicSheet

ANALYSIS_SR = 22050
MIN_PHRASE_S = 4.0


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_music_sheet(track_path: str, *, window_s: Optional[float] = None) -> MusicSheet:
    import librosa

    y, sr = librosa.load(track_path, sr=ANALYSIS_SR, mono=True)
    duration_s = float(len(y)) / sr
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    rms = librosa.feature.rms(y=y)[0]
    rms_t = librosa.frames_to_time(np.arange(len(rms)), sr=sr)

    t0 = float(beat_times[0]) if len(beat_times) else 0.0
    t_end = min(duration_s, t0 + window_s) if window_s else duration_s
    beats = [float(b) for b in beat_times if t0 <= b <= t_end]
    if len(beats) < 4:
        raise ValueError(f"track has too few beats in window ({len(beats)})")

    def energy_at(t: float) -> float:
        i = int(np.clip(np.searchsorted(rms_t, t), 0, len(rms) - 1))
        return float(rms[i])

    e = np.array([energy_at(b) for b in beats])
    e_norm = (e - e.min()) / (e.max() - e.min() + 1e-9)

    phrases = _detect_phrases(rms, rms_t, t0, t_end, e_norm, beats)
    return MusicSheet(
        track_path=track_path,
        track_sha256=_sha256(Path(track_path)),
        tempo_bpm=round(float(np.atleast_1d(tempo)[0]), 1),
        duration_s=round(duration_s, 3),
        window_start_s=round(t0, 4),
        window_s=round(t_end - t0, 4),
        beats=[round(b, 4) for b in beats],
        beat_energy=[round(float(x), 4) for x in e_norm],
        phrases=phrases,
    )


def provisional_sheet(duration_s: float, tempo_bpm: float = 100.0) -> MusicSheet:
    """A synthetic grid for planning BEFORE music exists (music=generate mode):
    uniform beats at the footage's analyzed tempo, a standard build-peak-release
    energy arc, phrases every ~8s. The visual passages planned on this grid
    shape the generated track's SectionPlan; the final plan is re-made against
    the REAL track's grid afterwards."""

    import numpy as np

    tempo_bpm = max(60.0, min(160.0, tempo_bpm or 100.0))
    period = 60.0 / tempo_bpm
    beats = [round(i * period, 4) for i in range(int(duration_s / period) + 1)]
    t = np.array(beats) / max(duration_s, 1e-6)
    energy = 0.3 + 0.6 * np.sin(np.clip(t, 0, 1) * np.pi) ** 1.5  # build->peak->release
    n_phrases = max(2, int(duration_s // 8))
    edges = [duration_s * i / n_phrases for i in range(n_phrases + 1)]
    phrases = [
        MusicPhrase(index=i, start_s=round(a, 3), end_s=round(b, 3),
                    energy=round(float(np.interp((a + b) / 2 / duration_s, t, energy)), 3))
        for i, (a, b) in enumerate(zip(edges, edges[1:]))
    ]
    return MusicSheet(
        track_path="<provisional>",
        tempo_bpm=tempo_bpm,
        duration_s=duration_s,
        window_start_s=0.0,
        window_s=duration_s,
        beats=beats,
        beat_energy=[round(float(e), 4) for e in energy],
        phrases=phrases,
    )


def truncate_sheet(sheet: MusicSheet, window_s: float) -> MusicSheet:
    """A shorter view of an analyzed sheet — no re-analysis (pure slicing).

    Used by the feasibility probe: analyze the track once, then test many
    window lengths cheaply."""

    t_end = sheet.window_start_s + min(window_s, sheet.window_s)
    keep = [i for i, b in enumerate(sheet.beats) if b <= t_end]
    phrases = []
    for ph in sheet.phrases:
        if ph.start_s >= t_end:
            break
        phrases.append(MusicPhrase(index=ph.index, start_s=ph.start_s,
                                   end_s=min(ph.end_s, t_end), energy=ph.energy))
    return MusicSheet(
        track_path=sheet.track_path,
        track_sha256=sheet.track_sha256,
        tempo_bpm=sheet.tempo_bpm,
        duration_s=sheet.duration_s,
        window_start_s=sheet.window_start_s,
        window_s=round(t_end - sheet.window_start_s, 4),
        beats=[sheet.beats[i] for i in keep],
        beat_energy=[sheet.beat_energy[i] for i in keep],
        phrases=phrases,
    )


def _detect_phrases(
    rms: np.ndarray,
    rms_t: np.ndarray,
    t0: float,
    t_end: float,
    beat_energy: np.ndarray,
    beats: list[float],
) -> list[MusicPhrase]:
    """Phrase boundaries at smoothed-energy valleys ≥ MIN_PHRASE_S apart."""

    mask = (rms_t >= t0) & (rms_t <= t_end)
    e, t = rms[mask], rms_t[mask]
    if len(e) < 8:
        return [MusicPhrase(index=0, start_s=t0, end_s=t_end,
                            energy=round(float(np.mean(beat_energy)), 4))]
    k = max(3, int(len(e) * 0.02) | 1)  # ~2% window, odd
    kernel = np.ones(k) / k
    smooth = np.convolve(e, kernel, mode="same")
    valleys: list[float] = []
    lo_bar = np.quantile(smooth, 0.40)
    for i in range(1, len(smooth) - 1):
        if smooth[i] <= smooth[i - 1] and smooth[i] <= smooth[i + 1] and smooth[i] <= lo_bar:
            ts = float(t[i])
            if ts - t0 < MIN_PHRASE_S or t_end - ts < MIN_PHRASE_S:
                continue
            if valleys and ts - valleys[-1] < MIN_PHRASE_S:
                continue
            valleys.append(ts)

    edges = [t0, *valleys, t_end]
    phrases: list[MusicPhrase] = []
    beats_arr = np.array(beats)
    for idx, (a, b) in enumerate(zip(edges, edges[1:])):
        sel = (beats_arr >= a) & (beats_arr < b)
        energy = float(np.mean(beat_energy[sel])) if sel.any() else 0.0
        phrases.append(MusicPhrase(index=idx, start_s=round(a, 4), end_s=round(b, 4),
                                   energy=round(energy, 4)))
    return phrases
