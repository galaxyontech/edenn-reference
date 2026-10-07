"""E2E latency benchmark for the v2 multi-image durable path.

Times the full durable processing (plan -> music gen -> assembly -> upload) for a
NON-basic tier (edenn_studio by default; edenn_enhanced if that tier's key is
present), with caching OFF, and prints a latency report + stage breakdown from the
job's stage-run timestamps. Gated behind the same flags as the real-provider e2e.

Run:
    RUN_ASYNC_V2_REAL_PROVIDER_INTEGRATION=1 RUN_ASYNC_V2_POSTGRES_INTEGRATION=1 \
    RUN_ASYNC_V2_STORAGE_INTEGRATION=1 \
    BENCHMARK_MODELSPEC=edenn_studio BENCHMARK_RUNS=2 \
    pytest .../test_multi_image_v2_latency_benchmark.py -s
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime
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


def _postgres_env_available() -> bool:
    load_env()
    return bool(os.getenv("DATABASE_URL") or (os.getenv("PGHOST") and os.getenv("PGDATABASE")))


def _storage_env_available() -> bool:
    load_env()
    return bool(
        os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        or os.getenv("AZURE_STORAGE_ACCOUNT_URL")
        or (os.getenv("COS_SECRET_ID") and os.getenv("COS_SECRET_KEY"))
    )


def _provider_key_for(modelspec: str) -> bool:
    load_env()
    if not all(os.getenv(k) for k in ("AZURE_API_KEY", "AZURE_ENDPOINT", "AZURE_MODEL")):
        return False
    if modelspec == "edenn_studio":
        return bool(os.getenv("PROVIDER_C_API_KEY"))
    if modelspec == "edenn_enhanced":
        return any(os.getenv(k) for k in ("PROVIDER_B_API_KEY", "PROVIDER_B_API_KEY_1", "EDENN_ENHANCED_PROVIDER_B_API_KEY"))
    if modelspec == "edenn_basic":
        return bool(os.getenv("PROVIDER_A_API_KEY"))
    return False


def _parse_iso(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _stage_breakdown(status_view: dict) -> dict:
    out: dict[str, float] = {}
    for stage in status_view.get("stages") or []:
        started = _parse_iso(stage.get("started_at"))
        finished = _parse_iso(stage.get("finished_at"))
        if started and finished and finished >= started:
            out[str(stage.get("stage_name"))] = round(finished - started, 2)
    return out


def _measure_v1(*, modelspec, image_files, workdir, storage, settings, cleanup) -> float:
    """Measure the v1 in-process path (orchestrator.run + build response, no queue/DB).

    Uses the SAME orchestrator + providers + blob upload as v2, so the delta vs v2
    isolates the durable-pipeline overhead (queue/DB/staging) from the generation
    cost that both share.
    """
    from EdennCode.Deployment.multi_image_workflows import MultiImageGenerationOrchestrator as _Orch

    job_id = f"bench_v1_{uuid.uuid4().hex}"
    images_dir = workdir / "v1_images"
    images_dir.mkdir(parents=True, exist_ok=True)
    for idx, img in enumerate(image_files):
        (images_dir / f"image_{idx + 1:03d}{img.suffix}").write_bytes(img.read_bytes())
    output_path = workdir / "v1_out" / "story.mp4"
    ctx = SimpleNamespace(storage=storage, settings=settings)

    t0 = time.perf_counter()
    result = asyncio.run(_Orch().run(
        folder_path=images_dir, output_path=output_path,
        user_prompt="Create an uplifting cinematic instrumental track.",
        align_to_beats=True, modelspec=modelspec, per_image_duration=3.0,
    ))
    response = _build_multi_image_response(context=ctx, job_id=job_id, result=result)
    elapsed = round(time.perf_counter() - t0, 2)

    for track in response.full_tracks:
        if track.blob:
            cleanup.append((settings.audio_container_name, track.blob))
    if response.video_blob:
        cleanup.append((settings.output_container, response.video_blob))
    return elapsed


@pytest.mark.remote_integration
@pytest.mark.skipif(
    os.getenv("RUN_ASYNC_V2_REAL_PROVIDER_INTEGRATION") != "1"
    or os.getenv("RUN_ASYNC_V2_POSTGRES_INTEGRATION") != "1"
    or os.getenv("RUN_ASYNC_V2_STORAGE_INTEGRATION") != "1"
    or not _postgres_env_available()
    or not _storage_env_available(),
    reason="Set RUN_ASYNC_V2_REAL_PROVIDER_INTEGRATION=1 + POSTGRES + STORAGE flags.",
)
def test_multi_image_v2_latency_benchmark(tmp_path: Path) -> None:
    # NON-basic by default, per the eval spec. Caching is OFF (plan_cache=None).
    modelspec = os.getenv("BENCHMARK_MODELSPEC", "edenn_studio").strip()
    assert modelspec != "edenn_basic", "benchmark must not use the basic tier"
    if not _provider_key_for(modelspec):
        pytest.skip(f"Provider env not configured for {modelspec}.")
    runs = max(1, int(os.getenv("BENCHMARK_RUNS", "2")))
    image_count = max(2, int(os.getenv("BENCHMARK_IMAGE_COUNT", "2")))

    settings = DeploymentSettings.from_env()
    storage = create_storage_service(settings)
    repo = AsyncPipelineV2Repository()
    queue = PostgresTaskQueue()
    staging = ArtifactStagingService(
        repository=repo, storage=storage, upload_container=settings.upload_container
    )
    image_files = sorted(p for p in MULTI_IMAGE_DESIGN_IMAGES_DIR.iterdir() if p.is_file())[:image_count]

    results: list[dict] = []
    cleanup: list[tuple[str, str]] = []
    try:
        for run_idx in range(runs):
            suffix = uuid.uuid4().hex
            job_id = f"bench_multi_image_{modelspec}_{suffix}"
            queue_name = f"bench-multi-image-{suffix}"
            worker = MultiImageMonolithWorker(
                repository=repo, queue=queue,
                orchestrator=MultiImageGenerationOrchestrator(),
                settings=settings, storage=storage,
                worker_id=f"bench_{suffix}", queue_name=queue_name,
                lease_seconds=1200, plan_cache=None,  # caching OFF
            )
            repo.create_job(
                job_id=job_id, job_type="multi_image",
                request_json={
                    "modelspec": modelspec,
                    "user_prompt": "Create an uplifting cinematic instrumental track.",
                    "align_to_beats": True, "per_image_duration": 3.0,
                },
                status=JobStatus.QUEUED, priority=5,
            )
            staged_ids = []
            for idx, img in enumerate(image_files):
                staged = staging.stage_image_bytes(
                    job_id=job_id, data=img.read_bytes(), filename=img.name,
                    destination_dir=tmp_path / f"images_{run_idx}", index=idx,
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

            t0 = time.perf_counter()
            processed = asyncio.run(worker.process_one())
            v2_elapsed = round(time.perf_counter() - t0, 2)
            status_view = repo.build_status_view(job_id)
            assert processed is not None and processed.status == TaskStatus.COMPLETED, status_view
            for a in repo.list_artifacts(job_id):
                if a.container and a.blob_name:
                    cleanup.append((a.container, a.blob_name))

            # v1 baseline (same images/modelspec/providers, in-process, no queue/DB).
            v1_elapsed = _measure_v1(
                modelspec=modelspec, image_files=image_files,
                workdir=tmp_path / f"v1_{run_idx}", storage=storage,
                settings=settings, cleanup=cleanup,
            )
            results.append({
                "run": run_idx + 1,
                "modelspec": modelspec,
                "image_count": len(image_files),
                "v1_e2e_latency_s": v1_elapsed,
                "v2_e2e_latency_s": v2_elapsed,
                "v2_overhead_s": round(v2_elapsed - v1_elapsed, 2),
                "stage_breakdown_s": _stage_breakdown(status_view),
                "video_duration_s": (status_view.get("result") or {}).get("video_metadata", {}).get("duration"),
            })

        v1s = [r["v1_e2e_latency_s"] for r in results]
        v2s = [r["v2_e2e_latency_s"] for r in results]
        v1_mean = round(sum(v1s) / len(v1s), 2)
        v2_mean = round(sum(v2s) / len(v2s), 2)
        report = {
            "modelspec": modelspec,
            "caching": "off",
            "runs": runs,
            "image_count": len(image_files),
            "v1_e2e_latency_s": {"min": min(v1s), "max": max(v1s), "mean": v1_mean},
            "v2_e2e_latency_s": {"min": min(v2s), "max": max(v2s), "mean": v2_mean},
            "v2_vs_v1": {
                "mean_delta_s": round(v2_mean - v1_mean, 2),
                "mean_ratio": round(v2_mean / v1_mean, 3) if v1_mean else None,
                "note": (
                    "Both paths call the same orchestrator + providers, so single-job "
                    "wall-clock is provider-bound and expected at parity; v2's added "
                    "delta is the durable queue/DB/staging overhead. v2's wins are "
                    "non-blocking submit, throughput (worker fleet), durability "
                    "(retry/dead-letter/lease-reaper), and warm-cache planning."
                ),
            },
            "per_run": results,
        }
        out_path = tmp_path / "multi_image_v2_latency_report.json"
        out_path.write_text(json.dumps(report, indent=2))
        print("\n===== MULTI-IMAGE v1 vs v2 LATENCY BENCHMARK (caching OFF) =====")
        print(json.dumps(report, indent=2))
        assert v2_mean > 0 and v1_mean > 0
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
