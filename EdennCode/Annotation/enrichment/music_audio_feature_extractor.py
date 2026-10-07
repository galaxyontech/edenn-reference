"""
MusicAudioFeatureExtractor — librosa-based audio feature computation.

All extraction runs in a thread-pool executor so it does not block the asyncio
event loop.  The extractor is intentionally stateless: instantiate once and call
:meth:`extract` for each file.

Features computed
-----------------
* ``bpm_actual``          — beat tracking (librosa.beat.beat_track)
* ``rms_energy_db``       — RMS loudness in dBFS
* ``spectral_brightness`` — spectral centroid / Nyquist  (0–1)
* ``acousticness``        — harmonic-to-total energy ratio via HPSS (0–1)
* ``danceability``        — mean onset strength / peak onset strength (0–1)
* ``vocal_energy_ratio``  — fraction of STFT energy in the 300 Hz–3 kHz band (0–1)
* ``duration_s``          — measured signal duration
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# Vocal frequency band boundaries (Hz)
_VOCAL_LOW_HZ = 300
_VOCAL_HIGH_HZ = 3_000

# Librosa sample rate for analysis (downsample for speed; beats/timbre stable at 22050)
_ANALYSIS_SR = 22_050


@dataclass
class RawAudioFeatures:
    """Plain-data holder returned by the synchronous extraction path."""
    bpm_actual: float
    rms_energy_db: float
    spectral_brightness: float
    acousticness: float
    danceability: float
    vocal_energy_ratio: float
    duration_s: float
    extraction_latency_s: float


def _extract_sync(file_path: str) -> RawAudioFeatures:
    """
    Synchronous librosa extraction — runs in a thread-pool executor.
    Raises on file-not-found or unsupported format.
    """
    import librosa  # late import so module loads without librosa in test envs

    t0 = time.perf_counter()

    y, sr = librosa.load(file_path, sr=_ANALYSIS_SR, mono=True)
    duration_s = float(librosa.get_duration(y=y, sr=sr))

    # ── BPM ───────────────────────────────────────────────────────────────────
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    bpm_actual = round(float(np.atleast_1d(tempo)[0]), 1)

    # ── RMS energy → dBFS ────────────────────────────────────────────────────
    rms_frames = librosa.feature.rms(y=y)[0]
    rms_mean = float(np.mean(rms_frames))
    rms_energy_db = round(float(librosa.amplitude_to_db(np.array([rms_mean]))[0]), 2)

    # ── Spectral brightness (centroid / Nyquist) ──────────────────────────────
    centroid = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
    spectral_brightness = round(float(np.mean(centroid)) / (sr / 2), 4)
    spectral_brightness = max(0.0, min(1.0, spectral_brightness))

    # ── Acousticness via HPSS (harmonic / total signal energy) ───────────────
    y_harm, _ = librosa.effects.hpss(y)
    harm_energy = float(np.mean(np.abs(y_harm)))
    total_signal_energy = float(np.mean(np.abs(y))) + 1e-8
    acousticness = round(harm_energy / total_signal_energy, 4)
    acousticness = max(0.0, min(1.0, acousticness))

    # ── Danceability (mean / peak onset strength) ─────────────────────────────
    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    peak = float(np.max(onset_env)) + 1e-8
    danceability = round(float(np.mean(onset_env)) / peak, 4)
    danceability = max(0.0, min(1.0, danceability))

    # ── Vocal energy ratio (300 Hz–3 kHz band) ───────────────────────────────
    stft = np.abs(librosa.stft(y))
    freqs = librosa.fft_frequencies(sr=sr)
    vocal_mask = (freqs >= _VOCAL_LOW_HZ) & (freqs <= _VOCAL_HIGH_HZ)
    vocal_energy = float(np.mean(stft[vocal_mask, :])) if vocal_mask.any() else 0.0
    total_stft_energy = float(np.mean(stft)) + 1e-8
    vocal_energy_ratio = round(vocal_energy / total_stft_energy, 4)
    vocal_energy_ratio = max(0.0, min(1.0, vocal_energy_ratio))

    elapsed = time.perf_counter() - t0
    return RawAudioFeatures(
        bpm_actual=bpm_actual,
        rms_energy_db=rms_energy_db,
        spectral_brightness=spectral_brightness,
        acousticness=acousticness,
        danceability=danceability,
        vocal_energy_ratio=vocal_energy_ratio,
        duration_s=round(duration_s, 2),
        extraction_latency_s=round(elapsed, 3),
    )


class MusicAudioFeatureExtractor:
    """
    Async façade over :func:`_extract_sync` that offloads CPU-bound librosa
    computation to a thread-pool executor.

    Parameters
    ----------
    executor:
        Optional :class:`concurrent.futures.Executor`.  ``None`` uses the
        default asyncio thread pool.
    """

    def __init__(self, executor=None) -> None:
        self._executor = executor

    async def extract(self, file_path: str) -> RawAudioFeatures:
        """
        Compute audio features for *file_path* without blocking the event loop.

        Parameters
        ----------
        file_path:
            Absolute path to the music file (MP3, WAV, M4A, FLAC).

        Returns
        -------
        RawAudioFeatures
            All computed feature values.

        Raises
        ------
        FileNotFoundError
            If *file_path* does not exist.
        Exception
            Any librosa / soundfile error is re-raised so the processor can
            record it on the event.
        """
        if not Path(file_path).exists():
            raise FileNotFoundError(f"Music file not found: {file_path!r}")

        loop = asyncio.get_running_loop()
        logger.info(
            "MusicAudioFeatureExtractor: analysing %s",
            Path(file_path).name,
        )
        features = await loop.run_in_executor(
            self._executor,
            _extract_sync,
            file_path,
        )
        logger.info(
            "MusicAudioFeatureExtractor: done bpm=%.1f rms=%.1fdB "
            "acoustic=%.2f dance=%.2f latency=%.2fs",
            features.bpm_actual,
            features.rms_energy_db,
            features.acousticness,
            features.danceability,
            features.extraction_latency_s,
        )
        return features
