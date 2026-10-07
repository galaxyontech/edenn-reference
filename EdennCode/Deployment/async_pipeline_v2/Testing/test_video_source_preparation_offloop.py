"""Tests for the source-video preparation hardening (PR#7).

- #56: the compressed-source ``artifact.created`` event no longer carries a
  signed SAS ``url`` (a stale credential that the /events endpoint replays to
  clients); only ``blob_name`` remains for internal debugging.
- #35: the CPU-heavy ffmpeg compress runs off the event loop (asyncio.to_thread),
  so the worker's lease heartbeat is not starved during compression.

Both are exercised through the real ``prepare_for_workflow`` with the ffmpeg call
and video-probe stubbed (no real re-encode needed to prove the wiring).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from EdennCode.Deployment.async_pipeline_v2 import video_source_preparation as vsp
from EdennCode.Deployment.async_pipeline_v2.models import AsyncV2Artifact


class _FakeStorage:
    enabled = True

    def upload_path(self, *, container, path, blob_name, content_type):
        return blob_name

    def generate_sas_url(self, *, container, blob_name):
        return f"https://sas.example/{container}/{blob_name}?sig=SECRET-CREDENTIAL"


class _FakeRepo:
    def __init__(self):
        self.events = []

    def get_artifact(self, _artifact_id):
        return None

    def add_artifact(self, **kw):
        return AsyncV2Artifact(
            artifact_id=kw["artifact_id"], job_id=kw["job_id"],
            artifact_type=kw["artifact_type"], role=kw.get("role"),
            container=kw.get("container"), blob_name=kw.get("blob_name"),
            url=kw.get("url"),
        )

    def add_event(self, **kw):
        self.events.append(kw)


def _service(tmp_path: Path, repo: _FakeRepo) -> vsp.VideoSourcePreparationService:
    settings = SimpleNamespace(workdir=str(tmp_path), upload_container="user-uploads")
    return vsp.VideoSourcePreparationService(
        repository=repo, settings=settings, storage=_FakeStorage())


def _source_artifact(src: Path) -> AsyncV2Artifact:
    return AsyncV2Artifact(
        artifact_id="a-src", job_id="job-1", artifact_type="source_video",
        role="input", local_path=str(src), content_type="video/mp4", metadata_json={})


def _install_stubs(monkeypatch, *, sleep_s: float = 0.0):
    def _fake_compress(*, video_path, output_path, **_):
        if sleep_s:
            import time
            time.sleep(sleep_s)  # simulate a slow, blocking re-encode
        Path(output_path).write_bytes(b"compressed-bytes")
        return Path(output_path)

    monkeypatch.setattr(vsp, "compress_video_to_max_height", _fake_compress)
    monkeypatch.setattr(vsp, "_inspect_public_video_metadata", lambda *a, **k: {})


def test_compressed_event_has_no_signed_url(monkeypatch, tmp_path):
    _install_stubs(monkeypatch)
    src = tmp_path / "src.mp4"
    src.write_bytes(b"source-bytes")
    repo = _FakeRepo()
    service = _service(tmp_path, repo)

    prepared = asyncio.run(service.prepare_for_workflow(
        job_id="job-1",
        request={"compression_flag": True, "compression_max_height": 720},
        source_artifact=_source_artifact(src),
    ))
    assert prepared.compression_info["applied"] is True

    created = [e for e in repo.events if e.get("event_type") == "artifact.created"]
    assert len(created) == 1
    payload = created[0]["payload_json"]
    assert "blob_name" in payload
    assert "url" not in payload
    # And the signed credential must not leak through the event at all.
    assert "SECRET-CREDENTIAL" not in str(payload)


def test_compress_runs_off_the_event_loop(monkeypatch, tmp_path):
    # A blocking 0.2s compress must not freeze the loop: a concurrent ticker
    # keeps advancing because the compress runs in a worker thread.
    _install_stubs(monkeypatch, sleep_s=0.2)
    src = tmp_path / "src.mp4"
    src.write_bytes(b"source-bytes")
    service = _service(tmp_path, _FakeRepo())

    async def _run() -> int:
        state = {"ticks": 0, "stop": False}

        async def ticker():
            while not state["stop"]:
                state["ticks"] += 1
                await asyncio.sleep(0.01)

        task = asyncio.create_task(ticker())
        await service.prepare_for_workflow(
            job_id="job-1",
            request={"compression_flag": True, "compression_max_height": 720},
            source_artifact=_source_artifact(src),
        )
        state["stop"] = True
        task.cancel()
        return state["ticks"]

    ticks = asyncio.run(asyncio.wait_for(_run(), timeout=5.0))
    # ~0.2s / 0.01s ≈ 20 ticks if the loop stayed free; a blocking compress -> ~0.
    assert ticks >= 5, f"event loop appears blocked during compress (ticks={ticks})"
