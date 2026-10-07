"""
Pipeline latency test suite.

Runs the full VideoMusicWorkflowE2E pipeline across music providers and prompt
variants, timing every stage individually.  Results are saved as JSON and
printed as a summary table.

Usage:
    python -m tests.latency.test_pipeline_latency
    python -m tests.latency.test_pipeline_latency --runs 5
    python -m tests.latency.test_pipeline_latency --provider edenn_basic --prompt short --runs 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from statistics import mean
from typing import Any

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
from EdennCode.env import load_env

load_env()

from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Deployment.storage import AzureBlobStorageService
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow import (
    VideoMusicWorkflowE2E,
    VideoMusicWorkflowE2EInput,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicGenerationStage.music_generation_stage import (
    MusicGenertionModelEnum,
)

from EdennCode.TestSuites.latency.prompts import PROMPT_VARIANTS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_VIDEO = Path(__file__).resolve().parents[3] / "test_videos" / "longmorn_beauty_film_60_16x9_1080p.mp4"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

PROVIDER_MODEL_MAP: dict[str, str] = {
    "edenn_basic": MusicGenertionModelEnum.EDENN_BASIC,
    "edenn_enhanced": MusicGenertionModelEnum.EDENN_ENHANCED,
    "edenn_studio": MusicGenertionModelEnum.EDENN_STUDIO,
}

PROVIDER_KEY_CHECK: dict[str, list[str]] = {
    "edenn_basic": ["PROVIDER_A_API_KEY"],
    "edenn_enhanced": ["PROVIDER_B_API_KEY", "PROVIDER_B_API_KEY_1", "EDENN_ENHANCED_PROVIDER_B_API_KEY"],
    "edenn_studio": ["PROVIDER_C_API_KEY", "PROVIDER_C_API_KEY_1"],
}

STAGE_ORDER = [
    "user_intent",
    "preprocess",
    "scene_segmentation",
    "video_understanding",
    "prompt_orchestration",
    "music_generation",
    "video_remix",
]

logger = logging.getLogger("latency-suite")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


# ---------------------------------------------------------------------------
# Stage timer
# ---------------------------------------------------------------------------
class StageTimer:
    """Context manager that records wall-clock duration into *collector*."""

    def __init__(self, name: str, collector: dict[str, dict[str, float]]):
        self.name = name
        self.collector = collector

    def __enter__(self) -> "StageTimer":
        self.start = time.monotonic()
        return self

    def __exit__(self, *exc: object) -> None:
        self.collector[self.name] = {
            "duration_s": round(time.monotonic() - self.start, 3),
        }


# ---------------------------------------------------------------------------
# Instrumentation — wraps real stage methods with timing
# ---------------------------------------------------------------------------
def _wrap_async(original, stage_name: str, collector: dict):
    """Return an async wrapper that times *original* into *collector*."""

    @wraps(original)
    async def wrapper(*args, **kwargs):
        with StageTimer(stage_name, collector):
            return await original(*args, **kwargs)

    return wrapper


def instrument_workflow(
    workflow: VideoMusicWorkflowE2E,
    collector: dict[str, dict[str, float]],
) -> None:
    """Monkey-patch *workflow* instance so every stage records its latency."""

    workflow.user_intent_understanding_stage.preprocess = _wrap_async(
        workflow.user_intent_understanding_stage.preprocess, "user_intent", collector
    )
    workflow.video_asset_preprocess.run = _wrap_async(
        workflow.video_asset_preprocess.run, "preprocess", collector
    )
    workflow.scene_segmentation_stage.run = _wrap_async(
        workflow.scene_segmentation_stage.run, "scene_segmentation", collector
    )
    workflow.video_understanding_stage.run = _wrap_async(
        workflow.video_understanding_stage.run, "video_understanding", collector
    )
    workflow.music_prompt_orchestration_stage.run = _wrap_async(
        workflow.music_prompt_orchestration_stage.run, "prompt_orchestration", collector
    )
    workflow.music_generation_stage.run = _wrap_async(
        workflow.music_generation_stage.run, "music_generation", collector
    )
    workflow.post_generation_remix.run = _wrap_async(
        workflow.post_generation_remix.run, "video_remix", collector
    )


# ---------------------------------------------------------------------------
# Provider availability check
# ---------------------------------------------------------------------------
def available_providers(requested: list[str] | None) -> list[str]:
    """Return providers that have at least one API key configured."""
    providers = requested or list(PROVIDER_KEY_CHECK)
    available = []
    for p in providers:
        env_vars = PROVIDER_KEY_CHECK[p]
        if any(os.getenv(v, "").strip() for v in env_vars):
            available.append(p)
        else:
            logger.warning("Skipping %s — no API key found (checked: %s)", p, ", ".join(env_vars))
    return available


# ---------------------------------------------------------------------------
# Single run
# ---------------------------------------------------------------------------
async def run_single(
    provider: str,
    prompt_variant: str,
    prompt_text: str,
    video_path: Path,
    run_number: int,
    slot: int = 0,
) -> dict[str, Any]:
    """Execute one pipeline run and return the result dict."""

    collector: dict[str, dict[str, float]] = {}
    slot_tag = f"_slot{slot}" if slot else ""
    run_id = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%S')}_{provider}_{prompt_variant}_run{run_number}{slot_tag}"

    logger.info("=== Run %s ===", run_id)

    overall_start = time.monotonic()

    try:
        settings = DeploymentSettings.from_env()
        storage = AzureBlobStorageService(settings)
        workflow = VideoMusicWorkflowE2E(
            storage_service=storage,
            llm_image_container=settings.llm_image_container,
            llm_image_sas_ttl_minutes=settings.llm_image_sas_ttl_minutes,
            llm_image_cleanup_delay_seconds=settings.llm_image_cleanup_delay_seconds,
        )
        instrument_workflow(workflow, collector)

        workflow_input = VideoMusicWorkflowE2EInput(
            video_path=str(video_path),
            user_prompt=prompt_text,
            music_model_spec=PROVIDER_MODEL_MAP[provider],
        )
        await workflow.generate(workflow_input)

        total_s = round(time.monotonic() - overall_start, 3)

        return {
            "run_id": run_id,
            "provider": provider,
            "prompt_variant": prompt_variant,
            "prompt_text": prompt_text,
            "video_file": video_path.name,
            "run_number": run_number,
            "stages": collector,
            "total_s": total_s,
            "success": True,
            "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as exc:
        total_s = round(time.monotonic() - overall_start, 3)
        logger.error("Run %s FAILED: %s", run_id, exc)
        return {
            "run_id": run_id,
            "provider": provider,
            "prompt_variant": prompt_variant,
            "prompt_text": prompt_text,
            "video_file": video_path.name,
            "run_number": run_number,
            "stages": collector,
            "total_s": total_s,
            "success": False,
            "error": f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def aggregate_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Build min/avg/max summary grouped by (provider, prompt_variant)."""

    groups: dict[tuple[str, str], list[dict]] = {}
    for r in results:
        key = (r["provider"], r["prompt_variant"])
        groups.setdefault(key, []).append(r)

    summary: dict[str, Any] = {"cases": []}

    for (provider, prompt_variant), runs in groups.items():
        successful = [r for r in runs if r["success"]]
        case: dict[str, Any] = {
            "provider": provider,
            "prompt_variant": prompt_variant,
            "total_runs": len(runs),
            "successful_runs": len(successful),
            "stages": {},
            "total_s": {},
        }

        if successful:
            # Per-stage stats
            for stage in STAGE_ORDER:
                durations = [
                    r["stages"][stage]["duration_s"]
                    for r in successful
                    if stage in r["stages"]
                ]
                if durations:
                    case["stages"][stage] = {
                        "min": round(min(durations), 3),
                        "avg": round(mean(durations), 3),
                        "max": round(max(durations), 3),
                    }

            # Total stats
            totals = [r["total_s"] for r in successful]
            case["total_s"] = {
                "min": round(min(totals), 3),
                "avg": round(mean(totals), 3),
                "max": round(max(totals), 3),
            }

        summary["cases"].append(case)

    return summary


# ---------------------------------------------------------------------------
# Console table
# ---------------------------------------------------------------------------
def _fmt_stats(stats: dict | None) -> str:
    if not stats:
        return "FAIL"
    return f"{stats['min']:.1f}/{stats['avg']:.1f}/{stats['max']:.1f}"


def print_summary_table(summary: dict[str, Any]) -> None:
    """Print a formatted table of min/avg/max per stage."""

    header_labels = ["Provider", "Prompt", "Runs"] + [s.replace("_", " ").title() for s in STAGE_ORDER] + ["Total"]
    col_widths = [16, 8, 5] + [14] * (len(STAGE_ORDER) + 1)

    header = "".join(label.ljust(w) for label, w in zip(header_labels, col_widths))
    separator = "".join("─" * (w - 1) + " " for w in col_widths)

    print()
    print("Pipeline Latency Summary (min/avg/max seconds)")
    print(header)
    print(separator)

    for case in summary["cases"]:
        row_parts = [
            case["provider"].ljust(col_widths[0]),
            case["prompt_variant"].ljust(col_widths[1]),
            f"{case['successful_runs']}/{case['total_runs']}".ljust(col_widths[2]),
        ]
        for stage in STAGE_ORDER:
            row_parts.append(_fmt_stats(case["stages"].get(stage)).ljust(col_widths[3]))
        row_parts.append(_fmt_stats(case.get("total_s") if case["successful_runs"] else None).ljust(col_widths[-1]))
        print("".join(row_parts))

    print()


# ---------------------------------------------------------------------------
# Main suite runner
# ---------------------------------------------------------------------------
async def run_latency_suite(
    providers: list[str],
    prompt_keys: list[str],
    video_path: Path,
    runs: int,
    concurrency: int = 1,
) -> list[dict[str, Any]]:
    """Run the full latency test matrix and return all individual results.

    When *concurrency* > 1, each (provider, prompt) case fires that many
    requests simultaneously via ``asyncio.gather``, simulating concurrent load.
    """

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    all_results: list[dict[str, Any]] = []

    total_cases = len(providers) * len(prompt_keys) * runs * concurrency
    logger.info(
        "Starting latency suite: %d provider(s) x %d prompt(s) x %d run(s) x %d concurrent = %d total runs",
        len(providers), len(prompt_keys), runs, concurrency, total_cases,
    )

    for provider in providers:
        for prompt_key in prompt_keys:
            prompt_text = PROMPT_VARIANTS[prompt_key]
            for run_num in range(1, runs + 1):
                if concurrency <= 1:
                    # Sequential — single request
                    results = [await run_single(provider, prompt_key, prompt_text, video_path, run_num)]
                else:
                    # Concurrent — fire N requests at once
                    logger.info(
                        "Launching %d concurrent requests: %s / %s / run %d",
                        concurrency, provider, prompt_key, run_num,
                    )
                    tasks = [
                        run_single(provider, prompt_key, prompt_text, video_path, run_num, slot=slot)
                        for slot in range(1, concurrency + 1)
                    ]
                    results = await asyncio.gather(*tasks)

                for result in results:
                    all_results.append(result)
                    result_path = RESULTS_DIR / f"{result['run_id']}.json"
                    result_path.write_text(json.dumps(result, indent=2, default=str))
                    logger.info("Saved %s (success=%s, total=%.1fs)", result_path.name, result["success"], result["total_s"])

    # Save summary
    summary = aggregate_results(all_results)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    summary_path = RESULTS_DIR / f"{ts}_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str))
    logger.info("Summary saved to %s", summary_path)

    print_summary_table(summary)

    return all_results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pipeline latency test suite")
    parser.add_argument("--runs", type=int, default=3, help="Number of runs per test case (default: 3)")
    parser.add_argument(
        "--provider",
        choices=list(PROVIDER_KEY_CHECK),
        action="append",
        dest="providers",
        help="Provider to test (can specify multiple times; default: all available)",
    )
    parser.add_argument(
        "--prompt",
        choices=list(PROMPT_VARIANTS),
        action="append",
        dest="prompts",
        help="Prompt variant to test (can specify multiple times; default: all)",
    )
    parser.add_argument("--video", type=Path, default=DEFAULT_VIDEO, help="Path to test video")
    parser.add_argument("--concurrency", type=int, default=1, help="Concurrent requests per test case (default: 1)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    if not args.video.exists():
        logger.error("Video not found: %s", args.video)
        sys.exit(1)

    providers = available_providers(args.providers)
    if not providers:
        logger.error("No providers available — check API keys")
        sys.exit(1)

    prompt_keys = args.prompts or list(PROMPT_VARIANTS)

    asyncio.run(run_latency_suite(providers, prompt_keys, args.video, args.runs, args.concurrency))


if __name__ == "__main__":
    main()
