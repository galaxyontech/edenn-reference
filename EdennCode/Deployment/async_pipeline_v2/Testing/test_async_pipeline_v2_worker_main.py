from __future__ import annotations

import asyncio
from types import SimpleNamespace

from EdennCode.Deployment.async_pipeline_v2.worker_main import (
    WORKER_TOPOLOGIES,
    _apply_provider_concurrency_override,
    _provider_concurrency_override,
    build_split_workers,
    split_worker_classes_for_role,
    supported_worker_roles,
)
from EdennCode.Deployment.async_pipeline_v2.workers.split_stage_workers import (
    AnalysisAndPlanningWorker,
    BasicMusicGenerationWorker,
    EnhancedMusicGenerationWorker,
    ProviderCandidateGenerationWorker,
    SelectionRankingRemixFinalizeWorker,
    StudioMusicGenerationWorker,
    VideoPreprocessWorker,
    run_worker_loop,
)


def test_supported_worker_roles_include_composite_and_individual_roles() -> None:
    roles = supported_worker_roles()

    assert "worker-pipeline" in roles
    assert "video-music-monolith" in roles
    assert "worker-media" in roles
    assert "worker-provider" in roles
    assert "worker-high-cpu" in roles
    assert "worker-normal-cpu" in roles
    assert "worker-video-preprocess" in roles
    assert "worker-analysis" in roles
    assert "worker-enhanced" in roles
    assert "worker-studio" in roles


def test_media_topology_owns_all_video_byte_split_queues() -> None:
    topology = WORKER_TOPOLOGIES["worker-media"]

    assert topology.cpu_class == "media"
    assert topology.worker_classes == (
        VideoPreprocessWorker,
        AnalysisAndPlanningWorker,
        SelectionRankingRemixFinalizeWorker,
    )
    assert topology.queue_names == (
        "video-preprocess",
        "analysis-and-planning",
        "selection-ranking-remix-finalize",
    )
    assert topology.recommended_cpu == 2.0
    assert topology.recommended_memory == "4Gi"
    assert topology.poll_interval_seconds == 0.5
    # Keep a warm replica so cross-stage handoffs skip scale-from-zero cold starts.
    assert topology.min_replicas == 1
    assert "--role" in topology.command_args
    assert "worker-media" in topology.command_args


def test_provider_topology_owns_all_music_generation_queues() -> None:
    topology = WORKER_TOPOLOGIES["worker-provider"]

    assert topology.cpu_class == "provider"
    assert topology.worker_classes == (
        BasicMusicGenerationWorker,
        EnhancedMusicGenerationWorker,
        StudioMusicGenerationWorker,
    )
    assert topology.queue_names == (
        "music-basic",
        "music-enhanced",
        "music-studio",
    )
    assert topology.recommended_cpu == 1.0
    assert topology.recommended_memory == "2Gi"
    assert topology.poll_interval_seconds == 0.5
    # Keep a warm replica so bursts are leased immediately, not after scale-up.
    assert topology.min_replicas == 1
    assert "--role" in topology.command_args
    assert "worker-provider" in topology.command_args


def test_high_cpu_topology_owns_only_video_heavy_split_queues() -> None:
    topology = WORKER_TOPOLOGIES["worker-high-cpu"]

    assert topology.cpu_class == "high-cpu"
    assert topology.worker_classes == (
        VideoPreprocessWorker,
        SelectionRankingRemixFinalizeWorker,
    )
    assert topology.queue_names == (
        "video-preprocess",
        "selection-ranking-remix-finalize",
    )
    assert topology.recommended_cpu == 4.0
    assert topology.recommended_memory == "8Gi"
    assert "--role" in topology.command_args
    assert "worker-high-cpu" in topology.command_args


def test_normal_cpu_topology_owns_planning_and_provider_queues() -> None:
    topology = WORKER_TOPOLOGIES["worker-normal-cpu"]

    assert topology.cpu_class == "normal-cpu"
    assert topology.worker_classes == (
        AnalysisAndPlanningWorker,
        BasicMusicGenerationWorker,
        EnhancedMusicGenerationWorker,
        StudioMusicGenerationWorker,
    )
    assert topology.queue_names == (
        "analysis-and-planning",
        "music-basic",
        "music-enhanced",
        "music-studio",
    )
    assert topology.recommended_cpu == 2.0
    assert topology.recommended_memory == "4Gi"
    assert "--role" in topology.command_args
    assert "worker-normal-cpu" in topology.command_args


def test_individual_split_role_aliases_resolve_to_single_worker_class() -> None:
    assert split_worker_classes_for_role("video-preprocess") == (VideoPreprocessWorker,)
    assert split_worker_classes_for_role("worker-remix-finalize") == (
        SelectionRankingRemixFinalizeWorker,
    )
    assert split_worker_classes_for_role("music-enhanced") == (
        EnhancedMusicGenerationWorker,
    )
    assert split_worker_classes_for_role("unsupported") == ()


def test_build_high_cpu_workers_creates_distinct_queue_consumers() -> None:
    workers = build_split_workers(
        role="worker-high-cpu",
        repository=SimpleNamespace(),  # type: ignore[arg-type]
        queue=SimpleNamespace(),  # type: ignore[arg-type]
        runtime=SimpleNamespace(),  # type: ignore[arg-type]
        settings=SimpleNamespace(workdir="/tmp"),  # type: ignore[arg-type]
        storage=SimpleNamespace(enabled=True),
        worker_id="highcpu-test",
        lease_seconds=7200,
    )

    assert [worker.queue_name for worker in workers] == [
        "video-preprocess",
        "selection-ranking-remix-finalize",
    ]
    assert [worker.worker_id for worker in workers] == [
        "highcpu-test-video-preprocess",
        "highcpu-test-selection-ranking-remix-finalize",
    ]
    assert all(worker.lease_seconds == 7200 for worker in workers)


def test_build_media_workers_creates_distinct_queue_consumers() -> None:
    workers = build_split_workers(
        role="worker-media",
        repository=SimpleNamespace(),  # type: ignore[arg-type]
        queue=SimpleNamespace(),  # type: ignore[arg-type]
        runtime=SimpleNamespace(),  # type: ignore[arg-type]
        settings=SimpleNamespace(workdir="/tmp"),  # type: ignore[arg-type]
        storage=SimpleNamespace(enabled=True),
        worker_id="media-test",
        lease_seconds=7200,
    )

    assert [worker.queue_name for worker in workers] == [
        "video-preprocess",
        "analysis-and-planning",
        "selection-ranking-remix-finalize",
    ]
    assert [worker.worker_id for worker in workers] == [
        "media-test-video-preprocess",
        "media-test-analysis-and-planning",
        "media-test-selection-ranking-remix-finalize",
    ]
    assert all(worker.lease_seconds == 7200 for worker in workers)


def test_build_normal_cpu_workers_creates_distinct_queue_consumers() -> None:
    workers = build_split_workers(
        role="worker-normal-cpu",
        repository=SimpleNamespace(),  # type: ignore[arg-type]
        queue=SimpleNamespace(),  # type: ignore[arg-type]
        runtime=SimpleNamespace(),  # type: ignore[arg-type]
        settings=SimpleNamespace(workdir="/tmp"),  # type: ignore[arg-type]
        storage=SimpleNamespace(enabled=True),
        worker_id="normalcpu-test",
        lease_seconds=7200,
    )

    assert [worker.queue_name for worker in workers] == [
        "analysis-and-planning",
        "music-basic",
        "music-enhanced",
        "music-studio",
    ]
    assert [worker.worker_id for worker in workers] == [
        "normalcpu-test-analysis-and-planning",
        "normalcpu-test-music-basic",
        "normalcpu-test-music-enhanced",
        "normalcpu-test-music-studio",
    ]
    assert {worker.retry_backoff_seconds for worker in workers} == {20, 30, 45, 60}


def test_build_provider_workers_creates_distinct_music_queue_consumers() -> None:
    workers = build_split_workers(
        role="worker-provider",
        repository=SimpleNamespace(),  # type: ignore[arg-type]
        queue=SimpleNamespace(),  # type: ignore[arg-type]
        runtime=SimpleNamespace(),  # type: ignore[arg-type]
        settings=SimpleNamespace(workdir="/tmp"),  # type: ignore[arg-type]
        storage=SimpleNamespace(enabled=True),
        worker_id="provider-test",
        lease_seconds=7200,
    )

    assert [worker.queue_name for worker in workers] == [
        "music-basic",
        "music-enhanced",
        "music-studio",
    ]
    assert [worker.worker_id for worker in workers] == [
        "provider-test-music-basic",
        "provider-test-music-enhanced",
        "provider-test-music-studio",
    ]
    assert {worker.retry_backoff_seconds for worker in workers} == {20, 45, 60}


def test_build_provider_workers_can_use_namespaced_queues(monkeypatch) -> None:
    monkeypatch.setenv("ASYNC_V2_QUEUE_NAMESPACE", "pytest-worker")

    workers = build_split_workers(
        role="worker-provider",
        repository=SimpleNamespace(),  # type: ignore[arg-type]
        queue=SimpleNamespace(),  # type: ignore[arg-type]
        runtime=SimpleNamespace(),  # type: ignore[arg-type]
        settings=SimpleNamespace(workdir="/tmp"),  # type: ignore[arg-type]
        storage=SimpleNamespace(enabled=True),
        worker_id="provider-test",
        lease_seconds=7200,
    )

    assert [worker.base_queue_name for worker in workers] == [
        "music-basic",
        "music-enhanced",
        "music-studio",
    ]
    assert [worker.queue_name for worker in workers] == [
        "pytest-worker:music-basic",
        "pytest-worker:music-enhanced",
        "pytest-worker:music-studio",
    ]


# --- Per-replica concurrency (provider-queue wait fix) ---------------------


def test_provider_workers_declare_intra_replica_concurrency() -> None:
    # Provider stages are I/O-bound on async provider polling; one replica must
    # be able to hold many in-flight generations rather than serializing them.
    assert ProviderCandidateGenerationWorker.max_concurrency > 1
    assert BasicMusicGenerationWorker.max_concurrency > 1
    assert EnhancedMusicGenerationWorker.max_concurrency > 1
    assert StudioMusicGenerationWorker.max_concurrency > 1

    # CPU-bound media stages get no real asyncio parallelism, so they stay serial.
    assert VideoPreprocessWorker.max_concurrency == 1
    assert AnalysisAndPlanningWorker.max_concurrency == 1
    assert SelectionRankingRemixFinalizeWorker.max_concurrency == 1


def test_apply_provider_concurrency_override_targets_only_provider_workers(monkeypatch) -> None:
    monkeypatch.setenv("ASYNC_V2_PROVIDER_CONCURRENCY", "3")
    workers = build_split_workers(
        role="worker-normal-cpu",  # mixes one media stage with all provider stages
        repository=SimpleNamespace(),  # type: ignore[arg-type]
        queue=SimpleNamespace(),  # type: ignore[arg-type]
        runtime=SimpleNamespace(),  # type: ignore[arg-type]
        settings=SimpleNamespace(workdir="/tmp"),  # type: ignore[arg-type]
        storage=SimpleNamespace(enabled=True),
        worker_id="override-test",
        lease_seconds=7200,
    )

    _apply_provider_concurrency_override(workers)

    by_queue = {worker.queue_name: worker for worker in workers}
    assert by_queue["music-basic"].max_concurrency == 3
    assert by_queue["music-enhanced"].max_concurrency == 3
    assert by_queue["music-studio"].max_concurrency == 3
    # The media stage in the same role must remain serial.
    assert by_queue["analysis-and-planning"].max_concurrency == 1


def test_provider_concurrency_override_ignores_invalid_values(monkeypatch) -> None:
    for raw in ("", "  ", "0", "-2", "abc"):
        monkeypatch.setenv("ASYNC_V2_PROVIDER_CONCURRENCY", raw)
        assert _provider_concurrency_override() is None

    monkeypatch.delenv("ASYNC_V2_PROVIDER_CONCURRENCY", raising=False)
    assert _provider_concurrency_override() is None

    monkeypatch.setenv("ASYNC_V2_PROVIDER_CONCURRENCY", "5")
    assert _provider_concurrency_override() == 5


class _ConcurrencyProbeWorker:
    """Stub worker that records the peak number of simultaneous process_one calls."""

    def __init__(self, *, max_concurrency: int) -> None:
        self.max_concurrency = max_concurrency
        self.active = 0
        self.peak = 0

    async def process_one(self):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.02)
            return SimpleNamespace()  # non-None => keep draining without poll sleep
        finally:
            self.active -= 1


def _peak_concurrency(*, max_concurrency: int, concurrency=None) -> int:
    async def _run() -> int:
        worker = _ConcurrencyProbeWorker(max_concurrency=max_concurrency)
        loop_task = asyncio.create_task(
            run_worker_loop(
                worker=worker,  # type: ignore[arg-type]
                poll_interval_seconds=0.01,
                concurrency=concurrency,
            )
        )
        await asyncio.sleep(0.15)
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        return worker.peak

    return asyncio.run(_run())


def test_run_worker_loop_processes_tasks_concurrently_for_provider_default() -> None:
    # Defaults to the worker's max_concurrency when concurrency is not passed.
    assert _peak_concurrency(max_concurrency=4) == 4


def test_run_worker_loop_concurrency_argument_overrides_worker_default() -> None:
    assert _peak_concurrency(max_concurrency=1, concurrency=3) == 3


def test_run_worker_loop_stays_serial_for_media_default() -> None:
    assert _peak_concurrency(max_concurrency=1) == 1


def test_voiceover_is_a_role_a_deployed_process_can_actually_run():
    """The agentic tool enqueued voiceover tasks that no deployed process
    consumed: they sat in the queue forever while the API reported them queued.
    Rendering the plan correctly is worthless if nothing runs the renderer."""
    from EdennCode.Deployment.async_pipeline_v2.worker_main import (
        supported_worker_roles, VOICEOVER_ROLES,
    )

    roles = supported_worker_roles()
    assert VOICEOVER_ROLES <= roles, "no deployable role consumes voiceover tasks"
    assert "worker-voiceover" in roles

    # It must not have been folded into a music role by accident — narration and
    # music scale on different work and different failure modes.
    from EdennCode.Deployment.async_pipeline_v2.worker_main import SPLIT_ROLE_CLASSES
    assert not (VOICEOVER_ROLES & set(SPLIT_ROLE_CLASSES))


def test_the_voiceover_role_reads_the_queue_the_tool_writes_to():
    """A worker listening on a different queue than the enqueuer writes to is
    indistinguishable from no worker at all."""
    from EdennCode.Deployment.async_pipeline_v2.workers.voiceover_worker import (
        VoiceoverWorker,
    )
    import inspect

    default_queue = inspect.signature(VoiceoverWorker.__init__).parameters[
        "queue_name"].default
    assert default_queue == "voiceover-pipeline"
