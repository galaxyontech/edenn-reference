"""
MusicAudioFeaturesEvent — librosa-computed audio features for a generated music file.

Emitted by :class:`~EdennCode.Annotation.enrichment.music_audio_feature_extractor.MusicAudioFeatureExtractor`
and persisted to the ``music_audio_features`` table.  All fields are ground-truth
numeric measurements from the actual audio signal — not LLM-inferred proxies.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from EdennCode.Annotation.core.annotation_event import AnnotationEvent


@dataclass(kw_only=True)
class MusicAudioFeaturesEvent(AnnotationEvent):
    """
    Computed audio feature measurements for one generated music track.

    All continuous features are normalised to ``[0.0, 1.0]`` unless stated
    otherwise, enabling direct numeric comparison across tracks.

    Attributes
    ----------
    event_type:
        Always ``"music_audio_features"``.
    music_filename:
        Base filename of the music file that was analysed.
    complete_music_filename:
        Full path used for analysis (not stored permanently — for audit only).
    provider_name:
        Music generation provider (``"provider_a"``, ``"provider_b"``, ``"provider_c"``).
    model_spec:
        Edenn model tier (``"edenn_basic"``, ``"edenn_enhanced"``, ``"edenn_studio"``).
    bpm_actual:
        Tempo in beats-per-minute from librosa beat tracking.
        Ground truth — preferred over ``tempo_class`` for numeric features.
    rms_energy_db:
        Root-mean-square energy of the signal in dBFS.
        Typically in the range ``[-60, 0]``.  Higher = louder.
    spectral_brightness:
        Spectral centroid frequency normalised to the Nyquist frequency (``[0, 1]``).
        High values indicate bright/trebly timbre; low values indicate bass-heavy.
    acousticness:
        Ratio of harmonic energy to total signal energy via HPSS.
        ``1.0`` = fully acoustic/harmonic; ``0.0`` = fully percussive/electronic.
    danceability:
        Mean onset strength normalised by its peak value.
        Higher values indicate stronger, more regular beat pulses.
    vocal_energy_ratio:
        Fraction of total STFT energy in the 300 Hz–3 kHz vocal band.
        High values correlate with prominent vocals.
    duration_s:
        Measured audio duration in seconds (from librosa, not from metadata).
    extraction_latency_s:
        Wall-clock seconds the librosa pass took.
    failed:
        ``True`` when extraction raised an unhandled exception.
    error_message:
        Description of the failure when ``failed`` is ``True``.
    """

    event_type: str = "music_audio_features"
    music_filename: str = ""
    complete_music_filename: str = ""
    provider_name: str = ""
    model_spec: str = ""

    # Temporal
    bpm_actual: float = 0.0

    # Loudness
    rms_energy_db: float = -60.0

    # Timbre
    spectral_brightness: float = 0.0

    # Acoustic vs electronic
    acousticness: float = 0.5

    # Rhythmic strength
    danceability: float = 0.5

    # Vocal prominence
    vocal_energy_ratio: float = 0.0

    # Duration
    duration_s: float = 0.0

    # Metadata
    extraction_latency_s: float = 0.0
    failed: bool = False
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)
