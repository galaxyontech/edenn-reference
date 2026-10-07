"""Tests for the performance-test fake provider (no real generation)."""
from __future__ import annotations

import asyncio
import shutil
import types
import wave
from pathlib import Path

import pytest

from EdennCode.Deployment.async_pipeline_v2.stages import video_music_split as vms


def test_fake_provider_flags(monkeypatch):
    monkeypatch.delenv("ASYNC_V2_FAKE_PROVIDER", raising=False)
    assert vms._fake_provider_enabled() is False
    monkeypatch.setenv("ASYNC_V2_FAKE_PROVIDER", "1")
    assert vms._fake_provider_enabled() is True
    monkeypatch.setenv("ASYNC_V2_FAKE_PROVIDER_DELAY_MS", "1500")
    assert vms._fake_provider_delay_s() == 1.5
    monkeypatch.setenv("ASYNC_V2_FAKE_PROVIDER_DELAY_MS", "bad")
    assert vms._fake_provider_delay_s() == 0.0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
def test_fake_candidate_produces_valid_30s_audio(tmp_path, monkeypatch):
    monkeypatch.setenv("ASYNC_V2_FAKE_PROVIDER", "1")
    monkeypatch.setenv("ASYNC_V2_FAKE_PROVIDER_DELAY_MS", "0")
    stage = object.__new__(vms.VideoMusicProviderCandidateGenerationStage)
    stage_input = types.SimpleNamespace(
        job_id="perf_test_job",
        analysis=types.SimpleNamespace(
            video_metadata=types.SimpleNamespace(temp_folder=str(tmp_path))),
    )
    out = asyncio.run(stage._fake_candidate(stage_input))
    assert out.candidate_audio_path.exists()
    assert out.generation_api_call_count == 0
    assert out.complete_audio_path is None
    with wave.open(str(out.candidate_audio_path)) as w:
        dur = w.getnframes() / w.getframerate()
    assert abs(dur - 30.0) < 1.0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
def test_fake_candidate_reuses_existing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ASYNC_V2_FAKE_PROVIDER", "1")
    monkeypatch.setenv("ASYNC_V2_FAKE_PROVIDER_DELAY_MS", "0")
    stage = object.__new__(vms.VideoMusicProviderCandidateGenerationStage)
    si = types.SimpleNamespace(
        job_id="reuse_job",
        analysis=types.SimpleNamespace(
            video_metadata=types.SimpleNamespace(temp_folder=str(tmp_path))),
    )
    first = asyncio.run(stage._fake_candidate(si))
    mtime1 = first.candidate_audio_path.stat().st_mtime_ns
    second = asyncio.run(stage._fake_candidate(si))
    # same path, not regenerated
    assert second.candidate_audio_path == first.candidate_audio_path
    assert second.candidate_audio_path.stat().st_mtime_ns == mtime1
