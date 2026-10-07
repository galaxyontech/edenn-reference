"""Throughput benchmark: v2 durable pipeline vs v1 in-process.

Demonstrates v2's architectural win that single-job latency cannot show. Submits
N jobs and processes them:
  - v2: N workers lease from one queue and run process_one() CONCURRENTLY.
  - v1: the in-process orchestrator runs the N jobs SERIALLY (v1's per-replica
    semaphore defaults to 1).
The v2 speedup = v1_total / v2_total. It is bounded by real provider concurrency,
so this uses edenn_basic: the fastest/cheapest local tier and the one most likely
to admit concurrent requests. If the provider serializes, the report shows a ~1x
speedup and says so — the win then requires a multi-key provider tier.
Caching is OFF.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from EdennCode.Deployment.api_multi_image_generation import _build_multi_image_response
from EdennCode.Deployment.async_pipeline_v2.artifact_service import ArtifactStagingService
from EdennCode.Deployment.async_pipeline_v2.models import JobStatus, TaskEnvelope, TaskStatus
from EdennCode.Deployment.async_pipeline_v2.queue.postgres_queue import PostgresTaskQueue
from EdennCode.Deployment.async_pipeline_v2.repositories import AsyncPipelineV2Repository
from EdennCode.Deployment.async_pipeline_v2.workers.multi_image_worker import (
    MultiImageMonolithWorker,
)
from EdennCode.Deployment.multi_image_workflows import MultiImageGenerationOrchestrator
from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Deployment.storage import create_storage_service
from EdennCode.env import load_env
from EdennCode.TestSuites.helpers.paths import MULTI_IMAGE_DESIGN_IMAGES_DIR


def _pg() -> bool:
    load_env()
    return bool(os.getenv("DATABASE_URL") or (os.getenv("PGHOST") and os.getenv("PGDATABASE")))


def _storage() -> bool:
    load_env()
    return bool(os.getenv("AZURE_STORAGE_CONNECTION_STRING") or os.getenv("AZURE_STORAGE_ACCOUNT_URL"))


def _provider() -> bool:
    load_env()
    return all(os.getenv(k) for k in ("AZURE_API_KEY", "AZURE_ENDPOINT", "AZURE_MODEL")) and bool(
        os.getenv("PROVIDER_A_API_KEY")
    )


@pytest.mark.remote_integration
@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_REAL_PROVIDER_INTEGRATION") != "1"
    or os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1"
    or os.getenv("RUN_ASYNC_V2_STORAGE_INTEGRATION") != "1"
    or not _pg()
    or not _storage(),
    reason="Set RUN_ASYNC_V2_REAL_PROVIDER_INTEGRATION=1 + POSTGRES + STORAGE flags.",
)
def test_multi_image_v2_throughput_vs_v1(tmp_path: Path) -> None:
    if not _provider():
        pytest.skip("LLM + basic-tier provider env not configured.")
    n_jobs = max(2, int(os.getenv("BENCHMARK_JOBS", "3")))
    modelspec = "edenn_basic"  # fast/cheap + concurrency-capable for the demo
    settings = DeploymentSettings.from_env()
    storage = create_storage_service(settings)
    repo = AsyncPipelineV2Repository()
    queue = PostgresTaskQueue()
    staging = ArtifactStagingService(
        repository=repo, storage=storage, upload_container=settings.upload_container
    )
    image_files = sorted(p for p in MULTI_IMAGE_DESIGN_IMAGES_DIR.iterdir() if p.is_file())[:2]
    request_json = {
        "modelspec": modelspec,
        "user_prompt": "Create an uplifting instrumental track.",
        "align_to_beats": True, "per_image_duration": 3.0,
    }
    cleanup: list[tuple[str, str]] = []

    try:
        # ---------------- v2: N concurrent workers on one shared queue ----------------
        run_id = uuid.uuid4().hex
        queue_name = f"bench-throughput-{run_id}"
        workers = []
        for i in range(n_jobs):
            job_id = f"bench_tp_{run_id}_{i}"
            repo.create_job(job_id=job_id, job_type="multi_image",
                            request_json=request_json, status=JobStatus.QUEUED, priority=5)
            staged_ids = []
            for idx, img in enumerate(image_files):
                staged = staging.stage_image_bytes(
                    job_id=job_id, data=img.read_bytes(), filename=img.name,
                    destination_dir=tmp_path / f"v2_{i}", index=idx,
                )
                staged_ids.append(staged.artifact.artifact_id)
                if staged.artifact.container and staged.artifact.blob_name:
                    cleanup.append((staged.artifact.container, staged.artifact.blob_name))
            queue.enqueue(TaskEnvelope(
                task_id=f"{job_id}:task", job_id=job_id, queue_name=queue_name,
                task_type="multi_image_monolith",
                payload_json={"image_artifact_ids": staged_ids},
                priority=5, max_attempts=1,
                idempotency_key=f"{job_id}:multi_image_monolith:v1",
            ))
            workers.append(MultiImageMonolithWorker(
                repository=repo, queue=queue, orchestrator=MultiImageGenerationOrchestrator(),
                settings=settings, storage=storage, worker_id=f"tp_worker_{run_id}_{i}",
                queue_name=queue_name, lease_seconds=1200, plan_cache=None,
            ))

        async def _run_all():
            return await asyncio.gather(*[w.process_one() for w in workers])

        t0 = time.perf_counter()
        processed = asyncio.run(_run_all())
        v2_total = round(time.perf_counter() - t0, 2)
        assert all(p is not None and p.status == TaskStatus.COMPLETED for p in processed), processed
        for i in range(n_jobs):
            for a in repo.list_artifacts(f"bench_tp_{run_id}_{i}"):
                if a.container and a.blob_name:
                    cleanup.append((a.container, a.blob_name))

        # ---------------- v1: N jobs SERIAL (in-process, semaphore=1) ----------------
        images_dir = tmp_path / "v1_images"
        images_dir.mkdir(parents=True, exist_ok=True)
        for idx, img in enumerate(image_files):
            (images_dir / f"image_{idx + 1:03d}{img.suffix}").write_bytes(img.read_bytes())
        ctx = SimpleNamespace(storage=storage, settings=settings)
        t0 = time.perf_counter()
        for i in range(n_jobs):
            result = asyncio.run(MultiImageGenerationOrchestrator().run(
                folder_path=images_dir, output_path=tmp_path / f"v1_out_{i}" / "s.mp4",
                user_prompt=request_json["user_prompt"], align_to_beats=True,
                modelspec=modelspec, per_image_duration=3.0,
            ))
            resp = _build_multi_image_response(context=ctx, job_id=f"bench_v1_{run_id}_{i}", result=result)
            for track in resp.full_tracks:
                if track.blob:
                    cleanup.append((settings.audio_container_name, track.blob))
            if resp.video_blob:
                cleanup.append((settings.output_container, resp.video_blob))
        v1_total = round(time.perf_counter() - t0, 2)

        report = {
            "modelspec": modelspec,
            "caching": "off",
            "jobs": n_jobs,
            "v1_total_s_serial": v1_total,
            "v2_total_s_concurrent": v2_total,
            "speedup_x": round(v1_total / v2_total, 2) if v2_total else None,
            "v1_per_job_s": round(v1_total / n_jobs, 2),
            "v2_per_job_s_effective": round(v2_total / n_jobs, 2),
            "note": (
                "v2 processes N jobs concurrently across workers; v1 serializes them. "
                "Speedup is capped by real provider concurrency — a >1x result confirms "
                "the pipeline parallelizes; ~1x means the provider itself serialized."
            ),
        }
        print("\n===== MULTI-IMAGE v2 THROUGHPUT vs v1 (caching OFF) =====")
        print(json.dumps(report, indent=2))
        report_path = os.getenv("BENCHMARK_REPORT_PATH")
        (Path(report_path) if report_path else tmp_path / "throughput_report.json").write_text(
            json.dumps(report, indent=2)
        )
        assert v1_total > 0 and v2_total > 0
    finally:
        seen: set[tuple[str, str]] = set()
        for container, blob_name in cleanup:
            if (container, blob_name) in seen:
                continue
            seen.add((container, blob_name))
            if hasattr(storage, "delete_blob"):
                try:
                    storage.delete_blob(container=container, blob_name=blob_name)
                except Exception:
                    pass
