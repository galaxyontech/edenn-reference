"""v1-vs-v2 benchmark with a FAKE, stable-latency music provider (no real APIs, no cost).

Removes the two confounds from the real-provider benchmarks: (1) provider latency
variance, and (2) provider concurrency=1. The fake "generation" is a fixed async
sleep returning a dummy audio file, so it PARALLELIZES — letting the throughput
comparison show v2's real win. The narrative planning (LLM) is also faked. Everything else is
real (image preprocess, ffmpeg slideshow assembly, response assembly). Modelspec
is edenn_enhanced (fake model).

Offline + free, but slow (stable sleeps), so gated:
    RUN_MULTI_IMAGE_FAKE_BENCHMARK=1 FAKE_MUSIC_LATENCY_S=12 BENCHMARK_JOBS=4 \
    pytest .../test_multi_image_fake_provider_benchmark.py -s -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import patch

import pytest

from EdennCode.Deployment.api_multi_image_generation import _build_multi_image_response
from EdennCode.Deployment.async_pipeline_v2.models import AsyncV2Artifact, AsyncV2Task, JobStatus, TaskStatus
from EdennCode.Deployment.async_pipeline_v2.workers.multi_image_worker import MultiImageMonolithWorker
from EdennCode.Deployment.multi_image_workflows import MultiImageGenerationOrchestrator
from EdennCode.MusicGenerationCore.models import (
    MusicGenerationResult, MusicVariant, ProviderJobRef, SectionTiming,
)
from EdennCode.Util.MediaUtils.ffmpeg_utils import resolve_ffmpeg_binary
from EdennCode.TestSuites.helpers.paths import MULTI_IMAGE_DESIGN_IMAGES_DIR

E2E_MOD = "EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.MultiImageE2EGenerationStage.multi_image_generation_e2e_stage"

FAKE_PLAN = {
    "video_title": "Fake Reel", "music_title": "Stable Horizon", "video_description": "A fake reel.",
    "image_order": [], "storyline_summary": "A steady arc.", "overall_mood": "uplifting",
    "target_bpm": 120, "primary_instruments": ["synth"], "music_prompt_summary": "fake instrumental",
    "image_beats": [], "music_sections": [],
}


class _FakePlanClient:
    async def complete_messages(self, messages, json_schema=None):
        return (dict(FAKE_PLAN), {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})


class _FakeMusicService:
    """Stable-latency dummy music: sleep a fixed time (parallelizable), return dummy audio."""

    def __init__(self, dummy_wav: Path, latency_s: float) -> None:
        self.dummy_wav = dummy_wav
        self.latency_s = latency_s

    async def generate(self, request):
        await asyncio.sleep(self.latency_s)  # stable "provider" time — overlaps across jobs
        total = request.section_plan.total_duration_s or sum(
            s.target_duration_s for s in request.section_plan.sections
        ) or 6.0
        out_dir = Path(request.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / "fake_music.wav"
        shutil.copy(self.dummy_wav, out)
        timeline, t = [], 0.0
        for s in request.section_plan.sections:
            timeline.append(SectionTiming(section_id=s.section_id, expected_start_s=t,
                                          expected_end_s=t + s.target_duration_s))
            t += s.target_duration_s
        variant = MusicVariant(variant_id="fake", audio_path=out, duration_s=float(total),
                               lyrics_timestamps=[], section_timeline=timeline)
        return MusicGenerationResult(used_modelspec=request.modelspec, primary=variant,
                                     alternates=[], job_ref=ProviderJobRef(provider="fake"),
                                     prompt_manifest={}, prompt_summary="fake instrumental",
                                     vocal_id_used=None)


class _DisabledStorage:
    enabled = False


class _FakeRepo:
    def __init__(self):
        self.jobs, self.artifacts, self.job_status = {}, {}, {}

    def get_job(self, jid): return self.jobs.get(jid)
    def get_artifact(self, aid): return self.artifacts.get(aid)

    def add_artifact(self, *, artifact_id, job_id, artifact_type, role, container,
                     blob_name, url, content_type, local_path, metadata_json):
        a = AsyncV2Artifact(artifact_id=artifact_id, job_id=job_id, artifact_type=artifact_type,
                            role=role, container=container, blob_name=blob_name, url=url,
                            content_type=content_type, local_path=local_path, metadata_json=metadata_json or {})
        self.artifacts[artifact_id] = a
        return a

    def start_stage_run(self, **k): return SimpleNamespace(stage_run_id="sr")
    def update_stage_run(self, *a, **k): pass
    def update_job_status(self, jid, *, status, **k): self.job_status[jid] = {"status": status, **k}
    def add_event(self, **k): pass


class _FakeQueue:
    def __init__(self, task): self._task, self._leased, self.completed = task, False, False
    def lease(self, **k):
        if self._leased: return None
        self._leased = True; return self._task
    def heartbeat(self, **k): pass
    def complete(self, *, task_id, worker_id):
        self.completed = True
        return SimpleNamespace(task_id=task_id, status=TaskStatus.COMPLETED)
    def fail(self, **k): return SimpleNamespace(status="dead_lettered")


def _make_dummy_wav(path: Path, seconds: int = 30) -> None:
    subprocess.run(
        [resolve_ffmpeg_binary(), "-y", "-f", "lavfi", "-i",
         f"anullsrc=r=44100:cl=stereo", "-t", str(seconds), str(path)],
        check=True, capture_output=True,
    )


def _seed_image_artifacts(repo: _FakeRepo, job_id: str, dest: Path, image_files) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    ids = []
    for i, img in enumerate(image_files):
        p = dest / f"src_{i}{img.suffix}"
        p.write_bytes(img.read_bytes())
        aid = f"{job_id}:image:{i}"
        repo.artifacts[aid] = AsyncV2Artifact(
            artifact_id=aid, job_id=job_id, artifact_type="source_image", role=f"image_{i}",
            container=None, blob_name=None, url=None, content_type="image/jpeg",
            local_path=str(p), metadata_json={})
        ids.append(aid)
    return ids


async def _v2_process(worker):
    return await worker.process_one()


@pytest.mark.skipif(os.getenv("RUN_MULTI_IMAGE_FAKE_BENCHMARK") != "1",
                    reason="Set RUN_MULTI_IMAGE_FAKE_BENCHMARK=1 to run this (slow, offline) benchmark.")
def test_multi_image_fake_provider_v1_vs_v2() -> None:
    latency = float(os.getenv("FAKE_MUSIC_LATENCY_S", "12"))
    n_jobs = max(2, int(os.getenv("BENCHMARK_JOBS", "4")))
    modelspec = "edenn_enhanced"
    image_files = sorted(p for p in MULTI_IMAGE_DESIGN_IMAGES_DIR.iterdir() if p.is_file())[:2]
    settings = SimpleNamespace(workdir=None, audio_container_name="audio", output_container="video")

    with TemporaryDirectory(prefix="mi-fake-bench-") as tmp:
        tmp_path = Path(tmp)
        dummy = tmp_path / "dummy.wav"
        _make_dummy_wav(dummy, seconds=30)
        fake_music = _FakeMusicService(dummy, latency)

        def _run_v1(idx: int) -> float:
            images_dir = tmp_path / f"v1_{idx}_img"
            images_dir.mkdir(parents=True, exist_ok=True)
            for j, img in enumerate(image_files):
                (images_dir / f"i{j}{img.suffix}").write_bytes(img.read_bytes())
            ctx = SimpleNamespace(storage=_DisabledStorage(), settings=settings)
            t0 = time.perf_counter()
            result = asyncio.run(MultiImageGenerationOrchestrator().run(
                folder_path=images_dir, output_path=tmp_path / f"v1_{idx}_out" / "s.mp4",
                user_prompt="", align_to_beats=False, modelspec=modelspec, per_image_duration=3.0,
            ))
            _build_multi_image_response(context=ctx, job_id=f"v1_{idx}", result=result)
            return round(time.perf_counter() - t0, 2)

        def _make_v2_worker(idx: int):
            repo = _FakeRepo()
            job_id = f"v2_{idx}"
            ids = _seed_image_artifacts(repo, job_id, tmp_path / f"v2_{idx}_img", image_files)
            repo.jobs[job_id] = SimpleNamespace(job_id=job_id, request_json={
                "modelspec": modelspec, "align_to_beats": False, "per_image_duration": 3.0, "user_prompt": ""})
            task = AsyncV2Task(task_id=f"{job_id}:t", job_id=job_id, queue_name="q",
                               task_type="multi_image_monolith", status=TaskStatus.LEASED,
                               payload_json={"image_artifact_ids": ids}, attempt=1, max_attempts=1)
            return MultiImageMonolithWorker(
                repository=repo, queue=_FakeQueue(task),
                orchestrator=MultiImageGenerationOrchestrator(), settings=settings,
                storage=_DisabledStorage(), worker_id=f"w{idx}", plan_cache=None), repo, job_id

        with patch(f"{E2E_MOD}.build_azure_client", return_value=_FakePlanClient()), \
             patch(f"{E2E_MOD}.build_music_generation_service", return_value=fake_music):
            # ---- single-job latency (stable) ----
            v1_single = _run_v1(0)
            w, repo, jid = _make_v2_worker(0)
            t0 = time.perf_counter(); asyncio.run(_v2_process(w)); v2_single = round(time.perf_counter() - t0, 2)
            assert repo.job_status[jid]["status"] == JobStatus.COMPLETED

            # ---- throughput: v1 serial vs v2 concurrent ----
            t0 = time.perf_counter()
            for i in range(1, n_jobs + 1):
                _run_v1(i)
            v1_serial = round(time.perf_counter() - t0, 2)

            v2_workers = [_make_v2_worker(100 + i)[0] for i in range(n_jobs)]

            async def _all():
                return await asyncio.gather(*[_v2_process(w) for w in v2_workers])
            t0 = time.perf_counter()
            processed = asyncio.run(_all())
            v2_concurrent = round(time.perf_counter() - t0, 2)
            assert all(p is not None and p.status == TaskStatus.COMPLETED for p in processed)

    report = {
        "provider": "FAKE (stable latency)", "modelspec": modelspec,
        "fake_music_latency_s": latency, "jobs": n_jobs, "images": len(image_files),
        "single_job_latency_s": {"v1": v1_single, "v2": v2_single,
                                 "v2_overhead_s": round(v2_single - v1_single, 2)},
        "throughput_s": {"v1_serial": v1_serial, "v2_concurrent": v2_concurrent,
                         "speedup_x": round(v1_serial / v2_concurrent, 2) if v2_concurrent else None,
                         "v1_per_job": round(v1_serial / n_jobs, 2),
                         "v2_per_job_effective": round(v2_concurrent / n_jobs, 2)},
    }
    print("\n===== MULTI-IMAGE FAKE-PROVIDER BENCHMARK (v1 vs v2) =====")
    print(json.dumps(report, indent=2))
    out = os.getenv("BENCHMARK_REPORT_PATH")
    if out:
        Path(out).write_text(json.dumps(report, indent=2))
    assert v1_single > 0 and v2_single > 0 and v1_serial > 0 and v2_concurrent > 0
