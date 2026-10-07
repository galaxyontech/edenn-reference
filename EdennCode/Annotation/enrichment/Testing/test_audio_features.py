"""
Tests for MusicAudioFeatureExtractor and EnrichmentProcessor.process_audio_features.

librosa is mocked throughout so the test suite runs without audio files or the
librosa / soundfile native dependencies.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, patch as mock_patch

from EdennCode.Annotation.enrichment.music_audio_feature_extractor import (
    MusicAudioFeatureExtractor,
    RawAudioFeatures,
    _extract_sync,
)
from EdennCode.Annotation.enrichment.music_audio_features_event import MusicAudioFeaturesEvent
from EdennCode.Annotation.enrichment.enrichment_processor import EnrichmentProcessor
from EdennCode.Annotation.enrichment.enrichment_processor_result import EnrichmentResult
from EdennCode.Annotation.store.in_memory_store import InMemoryAnnotationStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_raw_features(**overrides) -> RawAudioFeatures:
    defaults = dict(
        bpm_actual=120.0,
        rms_energy_db=-18.5,
        spectral_brightness=0.35,
        acousticness=0.72,
        danceability=0.61,
        vocal_energy_ratio=0.44,
        duration_s=30.0,
        extraction_latency_s=0.5,
    )
    defaults.update(overrides)
    return RawAudioFeatures(**defaults)


def _make_processor(store=None, audio_extractor=None) -> EnrichmentProcessor:
    mock_extractor = MagicMock()
    mock_extractor.model_name = "chat-advanced"
    store = store or InMemoryAnnotationStore()
    return EnrichmentProcessor(
        store=store,
        extractor=mock_extractor,
        audio_extractor=audio_extractor,
    )


# ---------------------------------------------------------------------------
# _extract_sync (unit — mocks librosa)
# ---------------------------------------------------------------------------

def _make_librosa_mock():
    """Build a MagicMock that mimics the librosa API used by _extract_sync."""
    import numpy as np
    import sys

    sr = 22050
    y = np.zeros(sr * 5, dtype=np.float32)

    mock_lib = MagicMock()
    mock_lib.load.return_value = (y, sr)
    mock_lib.get_duration.return_value = 5.0
    mock_lib.beat.beat_track.return_value = (np.array([120.0]), None)
    mock_lib.feature.rms.return_value = np.array([[0.05] * 100])
    mock_lib.amplitude_to_db.return_value = np.array([-26.0])
    mock_lib.feature.spectral_centroid.return_value = np.array([[3000.0] * 100])
    mock_lib.effects.hpss.return_value = (y * 0.8, y * 0.2)
    mock_lib.onset.onset_strength.return_value = np.array([0.4, 0.8, 0.6, 1.0, 0.5])
    stft = np.ones((1025, 100), dtype=np.float32)
    mock_lib.stft.return_value = stft
    mock_lib.fft_frequencies.return_value = np.linspace(0, sr / 2, 1025)
    return mock_lib


class TestExtractSync(unittest.TestCase):

    def test_extract_sync_happy_path(self):
        import sys
        mock_lib = _make_librosa_mock()
        with patch.dict("sys.modules", {"librosa": mock_lib}):
            result = _extract_sync("/fake/path.mp3")

        self.assertIsInstance(result, RawAudioFeatures)
        self.assertEqual(result.bpm_actual, 120.0)
        self.assertEqual(result.rms_energy_db, -26.0)
        self.assertEqual(result.duration_s, 5.0)
        self.assertGreaterEqual(result.extraction_latency_s, 0.0)
        self.assertGreaterEqual(result.spectral_brightness, 0.0)
        self.assertLessEqual(result.spectral_brightness, 1.0)
        self.assertGreaterEqual(result.acousticness, 0.0)
        self.assertLessEqual(result.acousticness, 1.0)
        self.assertGreaterEqual(result.danceability, 0.0)
        self.assertLessEqual(result.danceability, 1.0)
        self.assertGreaterEqual(result.vocal_energy_ratio, 0.0)
        self.assertLessEqual(result.vocal_energy_ratio, 1.0)


# ---------------------------------------------------------------------------
# MusicAudioFeatureExtractor (async)
# ---------------------------------------------------------------------------

class TestMusicAudioFeatureExtractor(unittest.IsolatedAsyncioTestCase):

    async def test_raises_file_not_found(self):
        extractor = MusicAudioFeatureExtractor()
        with self.assertRaises(FileNotFoundError):
            await extractor.extract("/nonexistent/file.mp3")

    async def test_runs_extract_sync_in_executor(self):
        expected = _make_raw_features()
        extractor = MusicAudioFeatureExtractor()

        with patch(
            "EdennCode.Annotation.enrichment.music_audio_feature_extractor._extract_sync",
            return_value=expected,
        ), patch("pathlib.Path.exists", return_value=True):
            result = await extractor.extract("/fake/track.mp3")

        self.assertEqual(result.bpm_actual, 120.0)
        self.assertEqual(result.rms_energy_db, -18.5)
        self.assertEqual(result.acousticness, 0.72)

    async def test_propagates_librosa_error(self):
        extractor = MusicAudioFeatureExtractor()

        with patch(
            "EdennCode.Annotation.enrichment.music_audio_feature_extractor._extract_sync",
            side_effect=RuntimeError("librosa decode error"),
        ), patch("pathlib.Path.exists", return_value=True):
            with self.assertRaises(RuntimeError):
                await extractor.extract("/fake/corrupt.mp3")


# ---------------------------------------------------------------------------
# EnrichmentProcessor.process_audio_features
# ---------------------------------------------------------------------------

class TestProcessAudioFeatures(unittest.IsolatedAsyncioTestCase):

    def _make_mock_audio_extractor(self, features=None, error=None):
        mock = MagicMock(spec=MusicAudioFeatureExtractor)
        if error:
            mock.extract = AsyncMock(side_effect=error)
        else:
            mock.extract = AsyncMock(return_value=features or _make_raw_features())
        return mock

    async def test_enriched_result_written_to_store(self):
        store = InMemoryAnnotationStore()
        audio_ext = self._make_mock_audio_extractor()
        processor = _make_processor(store=store, audio_extractor=audio_ext)

        with patch("pathlib.Path.exists", return_value=True):
            result = await processor.process_audio_features(
                "job-001", "/fake/track.mp3",
                provider_name="provider_b", model_spec="edenn_basic",
            )

        self.assertEqual(result, EnrichmentResult.ENRICHED)
        events = await store.get_by_job_and_type("job-001", "music_audio_features")
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertIsInstance(ev, MusicAudioFeaturesEvent)
        self.assertEqual(ev.bpm_actual, 120.0)
        self.assertEqual(ev.provider_name, "provider_b")
        self.assertEqual(ev.model_spec, "edenn_basic")
        self.assertEqual(ev.music_filename, "track.mp3")
        self.assertFalse(ev.failed)

    async def test_idempotency_skip_on_existing_event(self):
        store = InMemoryAnnotationStore()
        audio_ext = self._make_mock_audio_extractor()
        processor = _make_processor(store=store, audio_extractor=audio_ext)

        with patch("pathlib.Path.exists", return_value=True):
            await processor.process_audio_features("job-002", "/fake/track.mp3")
            result = await processor.process_audio_features("job-002", "/fake/track.mp3")

        self.assertEqual(result, EnrichmentResult.SKIPPED_ALREADY_ENRICHED)
        audio_ext.extract.assert_called_once()

    async def test_failed_extraction_writes_failed_event(self):
        store = InMemoryAnnotationStore()
        audio_ext = self._make_mock_audio_extractor(error=RuntimeError("decode error"))
        processor = _make_processor(store=store, audio_extractor=audio_ext)

        with patch("pathlib.Path.exists", return_value=True):
            result = await processor.process_audio_features("job-003", "/fake/bad.mp3")

        self.assertEqual(result, EnrichmentResult.FAILED)
        events = await store.get_by_job_and_type("job-003", "music_audio_features")
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].failed)
        self.assertIn("decode error", events[0].error_message)

    async def test_event_fields_match_raw_features(self):
        raw = _make_raw_features(
            bpm_actual=95.5,
            rms_energy_db=-24.1,
            spectral_brightness=0.28,
            acousticness=0.88,
            danceability=0.55,
            vocal_energy_ratio=0.31,
            duration_s=45.0,
            extraction_latency_s=1.2,
        )
        store = InMemoryAnnotationStore()
        audio_ext = self._make_mock_audio_extractor(features=raw)
        processor = _make_processor(store=store, audio_extractor=audio_ext)

        with patch("pathlib.Path.exists", return_value=True):
            await processor.process_audio_features("job-004", "/fake/song.mp3")

        ev: MusicAudioFeaturesEvent = (await store.get_by_job_and_type("job-004", "music_audio_features"))[0]
        self.assertEqual(ev.bpm_actual, 95.5)
        self.assertEqual(ev.rms_energy_db, -24.1)
        self.assertEqual(ev.spectral_brightness, 0.28)
        self.assertEqual(ev.acousticness, 0.88)
        self.assertEqual(ev.danceability, 0.55)
        self.assertEqual(ev.vocal_energy_ratio, 0.31)
        self.assertEqual(ev.duration_s, 45.0)
        self.assertEqual(ev.extraction_latency_s, 1.2)


# ---------------------------------------------------------------------------
# MusicAudioFeaturesEvent serialisation
# ---------------------------------------------------------------------------

class TestMusicAudioFeaturesEventSerialization(unittest.TestCase):

    def test_to_dict_roundtrip(self):
        ev = MusicAudioFeaturesEvent(
            job_id="job-x",
            music_filename="demo.mp3",
            provider_name="provider_a",
            model_spec="edenn_studio",
            bpm_actual=128.0,
            rms_energy_db=-12.3,
            spectral_brightness=0.45,
            acousticness=0.6,
            danceability=0.75,
            vocal_energy_ratio=0.52,
            duration_s=60.0,
            extraction_latency_s=0.8,
        )
        d = ev.to_dict()
        self.assertEqual(d["bpm_actual"], 128.0)
        self.assertEqual(d["provider_name"], "provider_a")
        self.assertEqual(d["event_type"], "music_audio_features")
        self.assertFalse(d["failed"])
        self.assertIsNone(d["error_message"])

    def test_failed_event_fields(self):
        ev = MusicAudioFeaturesEvent(
            job_id="job-fail",
            failed=True,
            error_message="FileNotFoundError: /missing.mp3",
        )
        d = ev.to_dict()
        self.assertTrue(d["failed"])
        self.assertIn("FileNotFoundError", d["error_message"])


if __name__ == "__main__":
    unittest.main()
