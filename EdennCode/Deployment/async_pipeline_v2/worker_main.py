from __future__ import annotations

import argparse
import asyncio
import faulthandler
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional, Type
from uuid import uuid4

from EdennCode.Deployment.async_pipeline_v2.queue.postgres_queue import PostgresTaskQueue
from EdennCode.Deployment.async_pipeline_v2.queue_names import (
    async_v2_queue_namespace,
    namespaced_queue_name,
)
from EdennCode.Deployment.async_pipeline_v2.repositories import AsyncPipelineV2Repository
from EdennCode.Deployment.async_pipeline_v2.cache_service import CacheService
from EdennCode.Deployment.postgres_wrapper import (
    PostgresConnectionConfig,
    PostgresConnectionPool,
)
from EdennCode.Deployment.async_pipeline_v2.workers.monolith_worker import (
    VideoMusicMonolithWorker,
    run_worker_loop,
)
from EdennCode.Deployment.async_pipeline_v2.workers.multi_image_worker import (
    MultiImageMonolithWorker,
    MultiImagePlanCache,
)
from EdennCode.Deployment.multi_image_workflows import MultiImageGenerationOrchestrator
from EdennCode.Deployment.provider_music_callbacks import (
    resolve_provider_music_callback_store,
)
from EdennCode.Deployment.async_pipeline_v2.workers.split_stage_workers import (
    AnalysisAndPlanningWorker,
    BasicMusicGenerationWorker,
    EnhancedMusicGenerationWorker,
    ProviderCandidateGenerationWorker,
    SelectionRankingRemixFinalizeWorker,
    StudioMusicGenerationWorker,
    VideoPreprocessWorker,
    VideoMusicSplitStageRuntime,
    run_worker_loop as run_split_worker_loop,
)
from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Deployment.storage import create_storage_service
from EdennCode.Deployment.workflows import VideoGenerationOrchestrator
from EdennCode.env import load_env


SplitWorkerClass = Type[
    VideoPreprocessWorker
    | AnalysisAndPlanningWorker
    | ProviderCandidateGenerationWorker
    | BasicMusicGenerationWorker
    | EnhancedMusicGenerationWorker
    | StudioMusicGenerationWorker
    | SelectionRankingRemixFinalizeWorker
]


@dataclass(frozen=True)
class WorkerTopology:
    """Deployment-level worker role for Azure Container Apps.

    Individual queue roles remain useful for local debugging and precise
    canaries. Composite roles are the production deployment contract. The
    no-redownload topology keeps all video-byte stages on `worker-media` and
    all provider queues on `worker-provider`; legacy high/normal roles remain
    available during canary transition. Keeping this mapping in code prevents
    docs, tests, and container commands from drifting apart.
    """

    role: str
    cpu_class: str
    description: str
    worker_classes: tuple[SplitWorkerClass, ...]
    recommended_cpu: float
    recommended_memory: str
    min_replicas: int
    max_replicas: int
    poll_interval_seconds: float = 2.0

    @property
    def queue_names(self) -> tuple[str, ...]:
        return tuple(worker_class.queue_name for worker_class in self.worker_classes)

    @property
    def command_args(self) -> tuple[str, ...]:
        return (
            "python",
            "-m",
            "EdennCode.Deployment.async_pipeline_v2.worker_main",
            "--role",
            self.role,
            "--lease-seconds",
            "7200",
            "--poll-interval-seconds",
            str(self.poll_interval_seconds),
        )


SPLIT_ROLE_CLASSES: dict[str, SplitWorkerClass] = {
    "worker-video-preprocess": VideoPreprocessWorker,
    "video-preprocess": VideoPreprocessWorker,
    "worker-analysis": AnalysisAndPlanningWorker,
    "analysis-and-planning": AnalysisAndPlanningWorker,
    "worker-provider-candidates": ProviderCandidateGenerationWorker,
    "provider-candidate-generation": ProviderCandidateGenerationWorker,
    "worker-basic": BasicMusicGenerationWorker,
    "music-basic": BasicMusicGenerationWorker,
    "worker-enhanced": EnhancedMusicGenerationWorker,
    "music-enhanced": EnhancedMusicGenerationWorker,
    "worker-studio": StudioMusicGenerationWorker,
    "music-studio": StudioMusicGenerationWorker,
    "worker-remix-finalize": SelectionRankingRemixFinalizeWorker,
    "selection-ranking-remix-finalize": SelectionRankingRemixFinalizeWorker,
}


WORKER_TOPOLOGIES: dict[str, WorkerTopology] = {
    "worker-media": WorkerTopology(
        role="worker-media",
        cpu_class="media",
        description=(
            "All split stages that need readable video bytes: preprocess, "
            "analysis/planning, and final remix/render."
        ),
        worker_classes=(
            VideoPreprocessWorker,
            AnalysisAndPlanningWorker,
            SelectionRankingRemixFinalizeWorker,
        ),
        recommended_cpu=2.0,
        recommended_memory="4Gi",
        # Keep one media replica warm so cross-stage handoffs (preprocess ->
        # analysis -> finalize) do not pay a heavy-image cold start, and so the
        # finalize hop after the long provider wait lands on a live worker.
        min_replicas=1,
        max_replicas=4,
        poll_interval_seconds=0.5,
    ),
    "worker-provider": WorkerTopology(
        role="worker-provider",
        cpu_class="provider",
        description="Provider candidate generation queues. This role must not read video bytes.",
        worker_classes=(
            BasicMusicGenerationWorker,
            EnhancedMusicGenerationWorker,
            StudioMusicGenerationWorker,
        ),
        recommended_cpu=1.0,
        recommended_memory="2Gi",
        # Keep one provider replica warm so a burst is leased immediately and
        # drained concurrently (see ProviderCandidateGenerationWorker
        # max_concurrency) instead of waiting on scale-from-zero. KEDA still
        # scales out to max_replicas under sustained load.
        min_replicas=1,
        max_replicas=8,
        poll_interval_seconds=0.5,
    ),
    "worker-high-cpu": WorkerTopology(
        role="worker-high-cpu",
        cpu_class="high-cpu",
        description=(
            "Legacy split role for video preprocess and final remix/render. "
            "Prefer worker-media for the no-redownload topology."
        ),
        worker_classes=(VideoPreprocessWorker, SelectionRankingRemixFinalizeWorker),
        recommended_cpu=4.0,
        recommended_memory="8Gi",
        min_replicas=0,
        max_replicas=4,
    ),
    "worker-normal-cpu": WorkerTopology(
        role="worker-normal-cpu",
        cpu_class="normal-cpu",
        description=(
            "Legacy split role for analysis and provider polling. Prefer "
            "worker-media plus worker-provider for the no-redownload topology."
        ),
        worker_classes=(
            AnalysisAndPlanningWorker,
            BasicMusicGenerationWorker,
            EnhancedMusicGenerationWorker,
            StudioMusicGenerationWorker,
        ),
        recommended_cpu=2.0,
        recommended_memory="4Gi",
        min_replicas=0,
        max_replicas=8,
    ),
}


# Roles that run the multi-image monolith worker (own queue + orchestrator).
MULTI_IMAGE_ROLES = {"worker-multi-image", "multi-image-monolith"}
# Narration. Its own role because nothing ran it: the agentic tool enqueued
# voiceover tasks that no deployed process consumed, so they sat in the queue
# forever while the API reported them queued.
VOICEOVER_ROLES = {"worker-voiceover", "voiceover-monolith"}
# Restyling an existing track. Its own role for exactly the same reason as
# narration above, and it went unnoticed for longer: the agentic tool enqueues
# audio_creative_edit tasks, a worker class exists to run them, and no role ran
# it — so on the fleet they queued forever while the API reported them queued.
# The standalone never showed the problem because it completes jobs in-process.
CREATIVE_EDIT_ROLES = {"worker-creative-edit", "creative-edit-monolith"}
# Sound effects. The third and last of these, and the worst of the three: the
# agentic tool enqueued video_sfx tasks, and there was no worker class and no
# role — nothing on the fleet ran them at all. The standalone hid it completely
# because it completes jobs in-process.
SFX_ROLES = {"worker-sfx", "sfx-monolith"}


def supported_worker_roles() -> set[str]:
    """Return every role accepted by the async v2 worker entrypoint."""

    return {
        "worker-pipeline",
        "video-music-monolith",
        *MULTI_IMAGE_ROLES,
        *VOICEOVER_ROLES,
        *CREATIVE_EDIT_ROLES,
        *SFX_ROLES,
        *SPLIT_ROLE_CLASSES,
        *WORKER_TOPOLOGIES,
    }


def split_worker_classes_for_role(role: str) -> tuple[SplitWorkerClass, ...]:
    """Return split worker classes for an individual or composite role."""

    topology = WORKER_TOPOLOGIES.get(role)
    if topology is not None:
        return topology.worker_classes
    worker_class = SPLIT_ROLE_CLASSES.get(role)
    if worker_class is None:
        return ()
    return (worker_class,)


def build_split_workers(
    *,
    role: str,
    repository: AsyncPipelineV2Repository,
    queue: PostgresTaskQueue,
    runtime: VideoMusicSplitStageRuntime,
    settings: DeploymentSettings,
    storage: object,
    worker_id: str | None,
    lease_seconds: int,
    cache: object | None = None,
) -> list[object]:
    """Build all split worker instances for a worker role.

    Composite roles run multiple queue consumers in one process. Each worker
    gets a stable role/stage-prefixed lease owner so logs and leased tasks show
    which container role handled the queue. An optional content-addressed cache is
    shared across all workers in the process.
    """

    workers: list[object] = []
    for worker_class in split_worker_classes_for_role(role):
        resolved_worker_id = (
            f"{worker_id}-{worker_class.queue_name}"
            if worker_id
            else f"{role}-{worker_class.queue_name}-{uuid4().hex}"
        )
        workers.append(
            worker_class(
                repository=repository,
                queue=queue,
                runtime=runtime,
                settings=settings,
                storage=storage,
                worker_id=resolved_worker_id,
                lease_seconds=lease_seconds,
                cache=cache,
            )
        )
    return workers


def _env_flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _build_cache(*, client_factory) -> Optional[CacheService]:
    """Build the shared content-addressed cache when any cache layer is enabled.

    The cache is a pure optimization, gated by env flags so it can be rolled out and
    rolled back without code changes. Returns None when disabled, which keeps every
    stage on its original recompute path.
    """

    if not (_env_flag("ASYNC_V2_COMPRESS_CACHE") or _env_flag("ASYNC_V2_UNDERSTANDING_CACHE")):
        return None
    ttl_raw = os.getenv("ASYNC_V2_CACHE_TTL_DAYS")
    try:
        ttl_days = int(ttl_raw) if ttl_raw else 14
    except ValueError:
        ttl_days = 14
    return CacheService(client_factory=client_factory, default_ttl_days=ttl_days)


def _build_multi_image_plan_cache(*, client_factory) -> Optional[MultiImagePlanCache]:
    """Build the inline plan cache for multi-image jobs when enabled.

    Gated by ASYNC_V2_MULTI_IMAGE_PLAN_CACHE so it can be rolled out/back without
    code changes (and turned OFF for latency eval). Safe by construction: it stores
    only the plan JSON, never a SAS URL.
    """

    if not _env_flag("ASYNC_V2_MULTI_IMAGE_PLAN_CACHE"):
        return None
    ttl_raw = os.getenv("ASYNC_V2_CACHE_TTL_DAYS")
    try:
        ttl_days = int(ttl_raw) if ttl_raw else 14
    except ValueError:
        ttl_days = 14
    return MultiImagePlanCache(
        CacheService(client_factory=client_factory, default_ttl_days=ttl_days),
        ttl_days=ttl_days,
    )


def _provider_concurrency_override() -> Optional[int]:
    """Read ASYNC_V2_PROVIDER_CONCURRENCY for tuning provider fan-out per replica.

    Provider workers default to their class `max_concurrency`. Ops can override
    it without a code change to balance provider-queue drain against per-replica
    Postgres connections and provider rate limits. Invalid or non-positive values
    are ignored so a bad env var never silently stops the worker.
    """

    raw = os.getenv("ASYNC_V2_PROVIDER_CONCURRENCY")
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        logging.getLogger(__name__).warning(
            "Ignoring invalid ASYNC_V2_PROVIDER_CONCURRENCY=%r (expected a positive int).",
            raw,
        )
        return None
    if value < 1:
        return None
    return value


def _build_connection_pool(role: str) -> PostgresConnectionPool:
    """Create the shared Postgres connection pool for a worker process.

    All queue consumers in one replica share a single pool so short repository
    and queue operations reuse warm connections instead of opening a fresh TLS
    connection each time. The pool is sized to comfortably exceed the number of
    queue loops and their provider concurrency, since each consumer plus its
    heartbeat may briefly hold a connection. Borrows do not overlap within one
    event loop, so this ceiling is conservative.
    """

    queue_loops = max(1, len(split_worker_classes_for_role(role)) or 1)
    provider_concurrency = _provider_concurrency_override() or (
        ProviderCandidateGenerationWorker.max_concurrency
        if any(
            issubclass(cls, ProviderCandidateGenerationWorker)
            for cls in split_worker_classes_for_role(role)
        )
        else 1
    )
    # Each consumer loop plus a heartbeat, with provider loops fanning out.
    max_conn = max(8, queue_loops * 2 + provider_concurrency)
    return PostgresConnectionPool(
        PostgresConnectionConfig.from_env(), minconn=1, maxconn=max_conn
    )


def _apply_provider_concurrency_override(workers: list[object]) -> None:
    """Apply the env concurrency override to provider workers only.

    Media stages stay at their CPU-bound default of 1; only provider candidate
    generation workers are I/O-bound enough to fan out within a replica.
    """

    override = _provider_concurrency_override()
    if override is None:
        return
    for worker in workers:
        if isinstance(worker, ProviderCandidateGenerationWorker):
            worker.max_concurrency = override


def _reap_interval_seconds() -> float:
    raw = os.getenv("ASYNC_V2_REAP_INTERVAL_SECONDS", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return 30.0
    return value if value > 0 else 30.0


def _maintenance_interval_seconds() -> float:
    raw = os.getenv("ASYNC_V2_MAINTENANCE_INTERVAL_SECONDS", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return 3600.0
    return value if value > 0 else 3600.0


def _callback_cleanup_ttl_seconds() -> int:
    raw = os.getenv("PROVIDER_MUSIC_CALLBACK_TTL_SECONDS", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return 6 * 3600
    return value if value > 0 else 6 * 3600


async def _run_maintenance_pass(
    *,
    cache: Any,
    callback_store: Any,
    callback_ttl_seconds: int,
    logger: logging.Logger,
) -> None:
    """Best-effort periodic cleanup: evict expired cache rows and old callback rows.

    Each cleanup is guarded independently so one failing never skips the other or
    disturbs the reaper it runs alongside. Blob cleanup for the cache is a
    separate concern (retention) and is not done here.
    """
    if cache is not None:
        try:
            evicted = await asyncio.to_thread(cache.evict_expired)
            if evicted:
                logger.info("cache maintenance evicted %d expired entries", evicted)
        except Exception:
            logger.exception("cache eviction failed; will retry next pass")
    if callback_store is not None and callback_ttl_seconds > 0:
        try:
            await asyncio.to_thread(
                callback_store.cleanup_expired, ttl_seconds=callback_ttl_seconds
            )
        except Exception:
            logger.exception("callback store cleanup failed; will retry next pass")


async def _run_lease_reaper(
    *,
    queue: PostgresTaskQueue,
    shutdown_event: asyncio.Event,
    queue_name_prefix: Optional[str],
    interval_seconds: float,
    cache: Any = None,
    callback_store: Any = None,
    maintenance_interval_seconds: Optional[float] = None,
    callback_ttl_seconds: Optional[int] = None,
) -> None:
    """Requeue tasks whose lease expired because their owning replica died.

    Without this, a task leased by a replica that crashes, OOMs, redeploys, or is
    terminated by KEDA scale-in stays ``leased`` forever and its job hangs. Runs as
    a sidecar coroutine alongside the consumer loops and exits on shutdown. The DB
    call is offloaded to a thread so it never blocks the worker event loop, and any
    failure is logged and retried rather than killing the worker.

    On a slower cadence it also runs a maintenance pass (cache eviction + callback
    row cleanup) so those tables do not grow without bound. Maintenance is
    independently guarded and can never disturb reaping.
    """
    logger = logging.getLogger(__name__)
    maintenance_interval = (
        maintenance_interval_seconds
        if maintenance_interval_seconds is not None
        else _maintenance_interval_seconds()
    )
    callback_ttl = (
        callback_ttl_seconds
        if callback_ttl_seconds is not None
        else _callback_cleanup_ttl_seconds()
    )
    cycles_per_maintenance = max(1, int(maintenance_interval / max(1.0, interval_seconds)))
    do_maintenance = cache is not None or callback_store is not None
    cycle = 0
    while not shutdown_event.is_set():
        try:
            requeued = await asyncio.to_thread(
                queue.requeue_expired_leases,
                limit=100,
                queue_name_prefix=queue_name_prefix,
            )
            if requeued:
                logger.warning(
                    "lease reaper requeued %d orphaned task(s): %s",
                    len(requeued),
                    [t.task_id for t in requeued],
                )
        except Exception:
            logger.exception("lease reaper pass failed; will retry")
        if do_maintenance and cycle % cycles_per_maintenance == 0:
            await _run_maintenance_pass(
                cache=cache,
                callback_store=callback_store,
                callback_ttl_seconds=callback_ttl,
                logger=logger,
            )
        cycle += 1
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass


def _install_shutdown_handlers(shutdown_event: asyncio.Event) -> None:
    """Set ``shutdown_event`` on SIGTERM/SIGINT so workers stop leasing new work.

    Container Apps sends SIGTERM before terminating a replica (scale-in/deploy);
    draining new leases keeps a doomed replica from grabbing fresh tasks it cannot
    finish, while in-flight tasks finish within the termination grace period or are
    requeued by the reaper after their lease expires.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown_event.set)
        except (NotImplementedError, RuntimeError):
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Async pipeline v2 worker.")
    parser.add_argument("--role", default="worker-pipeline")
    parser.add_argument("--queue", default="video-music-pipeline")
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--lease-seconds", type=int, default=900)
    parser.add_argument("--poll-interval-seconds", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def _start_loop_stall_watchdog(
    loop: asyncio.AbstractEventLoop,
    *,
    stall_seconds: float = 60.0,
) -> threading.Thread:
    """OS-thread watchdog: dump all stacks when the event loop stops responding.

    Every consumer, lease heartbeat and reaper in this process shares one event
    loop, so a single blocking call (e.g. a synchronous read on a half-open DB
    socket) silently freezes everything — the job just sits 'processing' with a
    frozen heartbeat. The watchdog pings the loop from a separate OS thread and,
    when the ping goes unanswered, logs CRITICAL and dumps every thread's stack
    so the exact blocking frame lands in the container logs.
    """
    watchdog_logger = logging.getLogger(f"{__name__}.loop_watchdog")
    pong = threading.Event()

    def _run() -> None:
        while not loop.is_closed():
            pong.clear()
            try:
                loop.call_soon_threadsafe(pong.set)
            except RuntimeError:
                return  # loop shut down
            if not pong.wait(stall_seconds):
                watchdog_logger.critical(
                    "event loop unresponsive for %.0fs — dumping all thread stacks",
                    stall_seconds,
                )
                faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            time.sleep(15.0)

    thread = threading.Thread(target=_run, name="loop-stall-watchdog", daemon=True)
    thread.start()
    return thread


async def main_async() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    load_env()
    args = parse_args()
    if args.role not in supported_worker_roles():
        raise ValueError(f"Unsupported async_pipeline_v2 worker role: {args.role}")
    if not args.once:
        _start_loop_stall_watchdog(asyncio.get_running_loop())

    settings = DeploymentSettings.from_env()

    from EdennCode.Deployment.auth.telemetry import setup_telemetry
    from EdennCode.Deployment.auth.usage_recorder import resolve_usage_recorder
    from EdennCode.Deployment.billing import resolve_billing

    setup_telemetry(
        connection_string=settings.appinsights_connection_string,
        role=f"worker:{args.role}",
        logger=logging.getLogger("EdennCode.Deployment.telemetry"),
    )
    resolve_usage_recorder(settings, logging.getLogger("EdennCode.Deployment.usage"))
    # Debits happen at worker terminal recording, so workers need the engine too.
    resolve_billing(settings, logging.getLogger("EdennCode.Deployment.billing"))

    storage = create_storage_service(settings)
    pool = _build_connection_pool(args.role)
    repository = AsyncPipelineV2Repository(client_factory=pool.client)
    queue = PostgresTaskQueue(client_factory=pool.client)
    # Callback store: reuse this worker's pool so the waiter's per-poll reads
    # borrow a warm connection instead of reconnecting each second, and fail
    # closed (do not boot) if the wait path is on but the store is in-memory.
    callback_store = resolve_provider_music_callback_store(
        client_factory=pool.client,
        logger=logging.getLogger(__name__),
    )

    # Graceful drain + orphaned-lease reaper. Skipped in --once mode (tests/canary).
    shutdown_event = asyncio.Event()
    reaper_enabled = not args.once
    if reaper_enabled:
        _install_shutdown_handlers(shutdown_event)
    namespace = async_v2_queue_namespace(settings)
    queue_name_prefix = f"{namespace}:" if namespace else None
    if split_worker_classes_for_role(args.role):
        runtime = VideoMusicSplitStageRuntime(
            storage_service=storage,
            llm_image_container=settings.llm_image_container,
            llm_image_sas_ttl_minutes=settings.llm_image_sas_ttl_minutes,
            llm_image_cleanup_delay_seconds=settings.llm_image_cleanup_delay_seconds,
        )
        cache = _build_cache(client_factory=pool.client)
        workers = build_split_workers(
            role=args.role,
            repository=repository,
            queue=queue,
            runtime=runtime,
            settings=settings,
            storage=storage,
            worker_id=args.worker_id,
            lease_seconds=args.lease_seconds,
            cache=cache,
        )
        _apply_provider_concurrency_override(workers)
        loops = [
            run_split_worker_loop(
                worker=worker,
                once=args.once,
                poll_interval_seconds=args.poll_interval_seconds,
                concurrency=getattr(worker, "max_concurrency", 1),
                shutdown_event=shutdown_event,
            )
            for worker in workers
        ]
        if reaper_enabled:
            loops.append(
                _run_lease_reaper(
                    queue=queue,
                    shutdown_event=shutdown_event,
                    queue_name_prefix=queue_name_prefix,
                    interval_seconds=_reap_interval_seconds(),
                    cache=cache,
                    callback_store=callback_store,
                )
            )
        await asyncio.gather(*loops)
        return

    if args.role in VOICEOVER_ROLES:
        from EdennCode.Deployment.async_pipeline_v2.workers.voiceover_worker import (
            VoiceoverWorker,
        )

        voiceover_worker = VoiceoverWorker(
            repository=repository,
            queue=queue,
            settings=settings,
            storage=storage,
            worker_id=args.worker_id or f"{args.role}-{uuid4().hex}",
            queue_name=namespaced_queue_name("voiceover-pipeline", settings=settings),
            lease_seconds=args.lease_seconds,
        )
        loops = [
            run_worker_loop(
                worker=voiceover_worker,
                once=args.once,
                poll_interval_seconds=args.poll_interval_seconds,
                shutdown_event=shutdown_event,
            )
        ]
        if reaper_enabled:
            loops.append(
                _run_lease_reaper(
                    queue=queue,
                    shutdown_event=shutdown_event,
                    queue_name_prefix=queue_name_prefix,
                    interval_seconds=_reap_interval_seconds(),
                    callback_store=callback_store,
                )
            )
        await asyncio.gather(*loops)
        return

    if args.role in CREATIVE_EDIT_ROLES:
        from EdennCode.Deployment.async_pipeline_v2.workers.audio_creative_edit_worker import (
            AudioCreativeEditWorker,
        )
        from EdennCode.Deployment.audio_edit_workflows import (
            AudioCreativeEditOrchestrator,
        )

        creative_edit_worker = AudioCreativeEditWorker(
            repository=repository,
            queue=queue,
            orchestrator=AudioCreativeEditOrchestrator(
                storage=storage,
                llm_image_container=getattr(settings, "llm_image_container", None),
                llm_image_sas_ttl_minutes=getattr(
                    settings, "llm_image_sas_ttl_minutes", 60
                ),
                llm_image_cleanup_delay_seconds=getattr(
                    settings, "llm_image_cleanup_delay_seconds", 0
                ),
            ),
            settings=settings,
            storage=storage,
            worker_id=args.worker_id or f"{args.role}-{uuid4().hex}",
            queue_name=namespaced_queue_name(
                "audio-creative-edit-pipeline", settings=settings
            ),
            lease_seconds=args.lease_seconds,
        )
        loops = [
            run_worker_loop(
                worker=creative_edit_worker,
                once=args.once,
                poll_interval_seconds=args.poll_interval_seconds,
                shutdown_event=shutdown_event,
            )
        ]
        if reaper_enabled:
            loops.append(
                _run_lease_reaper(
                    queue=queue,
                    shutdown_event=shutdown_event,
                    queue_name_prefix=queue_name_prefix,
                    interval_seconds=_reap_interval_seconds(),
                    callback_store=callback_store,
                )
            )
        await asyncio.gather(*loops)
        return

    if args.role in SFX_ROLES:
        from EdennCode.Deployment.async_pipeline_v2.workers.video_sfx_worker import (
            VideoSfxWorker,
        )

        sfx_worker = VideoSfxWorker(
            repository=repository,
            queue=queue,
            settings=settings,
            storage=storage,
            worker_id=args.worker_id or f"{args.role}-{uuid4().hex}",
            # Must be the queue the agentic tool actually writes to. It is
            # "sfx-pipeline", not "video-sfx-pipeline": the task type and the
            # queue name do not match for this one, and a role listening on the
            # name the task type suggests would idle forever next to a full
            # queue while looking correct.
            queue_name=namespaced_queue_name("sfx-pipeline", settings=settings),
            lease_seconds=args.lease_seconds,
        )
        loops = [
            run_worker_loop(
                worker=sfx_worker,
                once=args.once,
                poll_interval_seconds=args.poll_interval_seconds,
                shutdown_event=shutdown_event,
            )
        ]
        if reaper_enabled:
            loops.append(
                _run_lease_reaper(
                    queue=queue,
                    shutdown_event=shutdown_event,
                    queue_name_prefix=queue_name_prefix,
                    interval_seconds=_reap_interval_seconds(),
                    callback_store=callback_store,
                )
            )
        await asyncio.gather(*loops)
        return

    if args.role in MULTI_IMAGE_ROLES:
        multi_image_worker = MultiImageMonolithWorker(
            repository=repository,
            queue=queue,
            orchestrator=MultiImageGenerationOrchestrator(),
            settings=settings,
            storage=storage,
            worker_id=args.worker_id or f"{args.role}-{uuid4().hex}",
            queue_name=namespaced_queue_name("multi-image-pipeline", settings=settings),
            lease_seconds=args.lease_seconds,
            plan_cache=_build_multi_image_plan_cache(client_factory=pool.client),
        )
        if reaper_enabled:
            await asyncio.gather(
                run_worker_loop(
                    worker=multi_image_worker,
                    once=args.once,
                    poll_interval_seconds=args.poll_interval_seconds,
                    shutdown_event=shutdown_event,
                ),
                _run_lease_reaper(
                    queue=queue,
                    shutdown_event=shutdown_event,
                    queue_name_prefix=queue_name_prefix,
                    interval_seconds=_reap_interval_seconds(),
                    callback_store=callback_store,
                ),
            )
        else:
            await run_worker_loop(
                worker=multi_image_worker,
                once=args.once,
                poll_interval_seconds=args.poll_interval_seconds,
                shutdown_event=shutdown_event,
            )
        return

    orchestrator = VideoGenerationOrchestrator(
        storage=storage,
        llm_image_container=settings.llm_image_container,
        llm_image_sas_ttl_minutes=settings.llm_image_sas_ttl_minutes,
        llm_image_cleanup_delay_seconds=settings.llm_image_cleanup_delay_seconds,
    )
    worker = VideoMusicMonolithWorker(
        repository=repository,
        queue=queue,
        orchestrator=orchestrator,
        settings=settings,
        storage=storage,
        worker_id=args.worker_id or f"{args.role}-{uuid4().hex}",
        queue_name=namespaced_queue_name(args.queue, settings=settings),
        lease_seconds=args.lease_seconds,
    )
    if reaper_enabled:
        await asyncio.gather(
            run_worker_loop(
                worker=worker,
                once=args.once,
                poll_interval_seconds=args.poll_interval_seconds,
                shutdown_event=shutdown_event,
            ),
            _run_lease_reaper(
                queue=queue,
                shutdown_event=shutdown_event,
                queue_name_prefix=queue_name_prefix,
                interval_seconds=_reap_interval_seconds(),
                callback_store=callback_store,
            ),
        )
    else:
        await run_worker_loop(
            worker=worker,
            once=args.once,
            poll_interval_seconds=args.poll_interval_seconds,
            shutdown_event=shutdown_event,
        )


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()


__all__ = [
    "SPLIT_ROLE_CLASSES",
    "WORKER_TOPOLOGIES",
    "WorkerTopology",
    "build_split_workers",
    "split_worker_classes_for_role",
    "supported_worker_roles",
]
