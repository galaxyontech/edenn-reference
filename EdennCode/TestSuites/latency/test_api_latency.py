"""
API latency test suite.

Sends concurrent HTTP requests to the deployed Edenn API, timing each
end-to-end (upload + processing + response).

Usage:
    python -m tests.latency.test_api_latency
    python -m tests.latency.test_api_latency --provider edenn_basic --prompt short --runs 1
    python -m tests.latency.test_api_latency --runs 1 --concurrency 3
    python -m tests.latency.test_api_latency --base-url http://localhost:8080
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

import httpx

from EdennCode.TestSuites.latency.prompts import PROMPT_VARIANTS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_BASE_URL = "https://prod-app.api.example.invalid"
DEFAULT_VIDEO = Path(__file__).resolve().parents[3] / "test_videos" / "longmorn_beauty_film_60_16x9_1080p.mp4"
RESULTS_DIR = Path(__file__).resolve().parent / "results" / "api"

PROFILES = ["edenn_basic", "edenn_enhanced", "edenn_studio"]

# Prompts that imply vocals
VOCAL_PROMPTS = {"short_vocal"}

REQUEST_TIMEOUT = 600  # 10 minutes

logger = logging.getLogger("api-latency-suite")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


# ---------------------------------------------------------------------------
# Single request
# ---------------------------------------------------------------------------
async def run_single(
    client: httpx.AsyncClient,
    base_url: str,
    video_bytes: bytes,
    video_filename: str,
    provider: str,
    prompt_variant: str,
    prompt_text: str,
    run_number: int,
    slot: int = 0,
) -> dict[str, Any]:
    """Send one API request and return the result dict."""

    include_vocals = prompt_variant in VOCAL_PROMPTS
    slot_tag = f"_slot{slot}" if slot else ""
    run_id = (
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%S')}"
        f"_{provider}_{prompt_variant}_run{run_number}{slot_tag}"
    )

    logger.info(">>> %s", run_id)
    start = time.monotonic()

    try:
        resp = await client.post(
            f"{base_url}/api/v1/jobs/video",
            files={"video": (video_filename, video_bytes, "video/mp4")},
            data={
                "user_prompt": prompt_text,
                "modelspec": provider,
                "include_vocals": str(include_vocals).lower(),
                "vocal_gender": "female",
            },
            timeout=REQUEST_TIMEOUT,
        )
        latency = round(time.monotonic() - start, 3)

        try:
            body = resp.json()
        except Exception:
            body = {}

        error_detail = None
        error_code = None
        if resp.status_code != 200:
            error_detail = body if isinstance(body, dict) else {"raw": str(body)[:500]}
            detail = body.get("detail") if isinstance(body, dict) else None
            if isinstance(detail, dict):
                error_code = detail.get("error_code")

        success = resp.status_code == 200
        logger.info(
            "<<< %s  HTTP %d  %.1fs%s",
            run_id, resp.status_code, latency,
            "" if success else f"  error_code={error_code}",
        )

        return {
            "run_id": run_id,
            "provider": provider,
            "prompt_variant": prompt_variant,
            "prompt_text": prompt_text,
            "include_vocals": include_vocals,
            "run_number": run_number,
            "slot": slot,
            "http_status": resp.status_code,
            "latency_s": latency,
            "success": success,
            "error": error_detail,
            "response_status": body.get("status") if isinstance(body, dict) else None,
            "response_error_code": error_code,
            "response_job_id": body.get("job_id") if isinstance(body, dict) else None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as exc:
        latency = round(time.monotonic() - start, 3)
        logger.error("<<< %s FAILED: %s", run_id, exc)
        return {
            "run_id": run_id,
            "provider": provider,
            "prompt_variant": prompt_variant,
            "prompt_text": prompt_text,
            "include_vocals": include_vocals,
            "run_number": run_number,
            "slot": slot,
            "http_status": 0,
            "latency_s": latency,
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
            "response_status": None,
            "response_error_code": None,
            "response_job_id": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def aggregate_results(results: list[dict[str, Any]]) -> dict[str, Any]:
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
            "latency_s": {},
            "errors": len(runs) - len(successful),
        }
        if successful:
            latencies = [r["latency_s"] for r in successful]
            case["latency_s"] = {
                "min": round(min(latencies), 1),
                "avg": round(mean(latencies), 1),
                "max": round(max(latencies), 1),
            }
        summary["cases"].append(case)

    return summary


# ---------------------------------------------------------------------------
# Console table
# ---------------------------------------------------------------------------
def print_summary_table(summary: dict[str, Any]) -> None:
    col_widths = [16, 12, 6, 20, 10, 8]
    headers = ["Provider", "Prompt", "Runs", "Latency (min/avg/max)", "HTTP 200", "Errors"]

    header_line = "".join(h.ljust(w) for h, w in zip(headers, col_widths))
    separator = "".join("─" * (w - 1) + " " for w in col_widths)

    print()
    print("API Latency Summary (seconds)")
    print(header_line)
    print(separator)

    for case in summary["cases"]:
        lat = case["latency_s"]
        lat_str = f"{lat['min']}/{lat['avg']}/{lat['max']}" if lat else "FAIL"
        row = [
            case["provider"].ljust(col_widths[0]),
            case["prompt_variant"].ljust(col_widths[1]),
            f"{case['successful_runs']}/{case['total_runs']}".ljust(col_widths[2]),
            lat_str.ljust(col_widths[3]),
            str(case["successful_runs"]).ljust(col_widths[4]),
            str(case["errors"]).ljust(col_widths[5]),
        ]
        print("".join(row))

    print()


# ---------------------------------------------------------------------------
# Suite runner
# ---------------------------------------------------------------------------
async def run_api_suite(
    base_url: str,
    video_path: Path,
    providers: list[str],
    prompt_keys: list[str],
    runs: int,
    concurrency: int,
) -> list[dict[str, Any]]:

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    all_results: list[dict[str, Any]] = []

    video_bytes = video_path.read_bytes()
    video_filename = video_path.name

    total = len(providers) * len(prompt_keys) * runs * max(concurrency, 1)
    logger.info(
        "Starting API latency suite: %d provider(s) x %d prompt(s) x %d run(s) x %d concurrent = %d total",
        len(providers), len(prompt_keys), runs, concurrency, total,
    )
    logger.info("Base URL: %s", base_url)
    logger.info("Video: %s (%.1f MB)", video_path.name, len(video_bytes) / 1024 / 1024)

    async with httpx.AsyncClient() as client:
        for provider in providers:
            for prompt_key in prompt_keys:
                prompt_text = PROMPT_VARIANTS[prompt_key]
                for run_num in range(1, runs + 1):
                    if concurrency <= 1:
                        results = [
                            await run_single(
                                client, base_url, video_bytes, video_filename,
                                provider, prompt_key, prompt_text, run_num,
                            )
                        ]
                    else:
                        logger.info(
                            "Launching %d concurrent: %s / %s / run %d",
                            concurrency, provider, prompt_key, run_num,
                        )
                        tasks = [
                            run_single(
                                client, base_url, video_bytes, video_filename,
                                provider, prompt_key, prompt_text, run_num, slot=s,
                            )
                            for s in range(1, concurrency + 1)
                        ]
                        results = list(await asyncio.gather(*tasks))

                    for result in results:
                        all_results.append(result)
                        result_path = RESULTS_DIR / f"{result['run_id']}.json"
                        result_path.write_text(json.dumps(result, indent=2, default=str))

    summary = aggregate_results(all_results)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    summary_path = RESULTS_DIR / f"{ts}_api_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str))
    logger.info("Summary saved to %s", summary_path)

    print_summary_table(summary)
    return all_results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="API latency test suite")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="API base URL")
    parser.add_argument("--runs", type=int, default=1, help="Runs per case (default: 1)")
    parser.add_argument("--concurrency", type=int, default=1, help="Concurrent requests per case (default: 1)")
    parser.add_argument(
        "--provider", choices=PROFILES, action="append", dest="providers",
        help="Profile to test (repeatable; default: all)",
    )
    parser.add_argument(
        "--prompt", choices=list(PROMPT_VARIANTS), action="append", dest="prompts",
        help="Prompt variant (repeatable; default: all)",
    )
    parser.add_argument("--video", type=Path, default=DEFAULT_VIDEO, help="Video file path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    if not args.video.exists():
        logger.error("Video not found: %s", args.video)
        sys.exit(1)

    providers = args.providers or PROFILES
    prompts = args.prompts or list(PROMPT_VARIANTS)

    asyncio.run(run_api_suite(
        base_url=args.base_url,
        video_path=args.video,
        providers=providers,
        prompt_keys=prompts,
        runs=args.runs,
        concurrency=args.concurrency,
    ))


if __name__ == "__main__":
    main()
