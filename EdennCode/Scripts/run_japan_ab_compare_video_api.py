from __future__ import annotations

"""A/B endpoint comparison: same edenn_enhanced vocal-prompt requests sent to
two endpoints sequentially, one after the other, so outputs can be diffed."""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from EdennCode.Scripts.run_japan_video_api_10x import (
    DEFAULT_OUTPUT_DIR,
    REQUEST_VARIANTS,
    SAMPLES,
    REPO_ROOT,
    run_once,
)

ENDPOINT_A = (
    "https://japan-dev-test.api.example.invalid"
    "/api/v1/jobs/video"
)
ENDPOINT_B = (
    "https://japan-prod-v2.api.example.invalid"
    "/api/v1/jobs/video"
)

# Only edenn_enhanced variants that include vocals
ENHANCED_VOCAL_VARIANTS = [
    v for v in REQUEST_VARIANTS
    if v["modelspec"] == "edenn_enhanced" and v.get("include_vocals") == "true"
]


def _run_ab_pair(
    *,
    endpoint_a: str,
    endpoint_b: str,
    timeout_s: float,
    index: int,
    total: int,
    sample: str,
    form: dict[str, str],
    variant_index: int,
) -> dict:
    video_path = (REPO_ROOT / sample).resolve()
    record_base = {"run": index, "sample": sample, "variant": variant_index, "request": form}

    if not video_path.exists():
        return {
            **record_base,
            "ok": False,
            "error": f"Video file not found: {video_path}",
            "endpoint_a": None,
            "endpoint_b": None,
        }

    results = {}
    for label, url in (("endpoint_a", endpoint_a), ("endpoint_b", endpoint_b)):
        try:
            results[label] = run_once(url, video_path, form, timeout_s)
            status = results[label]["status_code"]
            elapsed = results[label]["elapsed_s"]
            print(
                f"[{index}/{total}] variant={variant_index} {label} HTTP {status} in {elapsed}s",
                flush=True,
            )
        except Exception as exc:
            results[label] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            print(
                f"[{index}/{total}] variant={variant_index} {label} FAILED: {exc}",
                flush=True,
            )

    both_ok = bool(results.get("endpoint_a", {}).get("ok")) and bool(
        results.get("endpoint_b", {}).get("ok")
    )
    return {**record_base, "ok": both_ok, **results}


def run_ab_suite(
    endpoint_a: str,
    endpoint_b: str,
    timeout_s: float,
    *,
    samples: list[str],
    variants: list[dict[str, str]],
    concurrency: int = 1,
) -> dict:
    total = len(samples) * len(variants)
    results: dict = {
        "endpoint_a": endpoint_a,
        "endpoint_b": endpoint_b,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "variants": variants,
        "sample_count": len(samples),
        "variant_count": len(variants),
        "total_pairs": total,
        "success": [],
        "failure": [],
    }

    tasks = [
        (idx, sample, variant_idx, variant)
        for idx, sample in enumerate(samples, start=1)
        for variant_idx, variant in enumerate(variants, start=1)
    ]

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        futures = {
            executor.submit(
                _run_ab_pair,
                endpoint_a=endpoint_a,
                endpoint_b=endpoint_b,
                timeout_s=timeout_s,
                index=idx,
                total=len(samples),
                sample=sample,
                form=dict(variant),
                variant_index=variant_idx,
            ): (idx, sample, variant_idx)
            for idx, sample, variant_idx, variant in tasks
        }
        for future in as_completed(futures):
            record = future.result()
            bucket = "success" if record.get("ok") else "failure"
            results[bucket].append(record)

    results["success"].sort(key=lambda r: (r["run"], r["variant"]))
    results["failure"].sort(key=lambda r: (r["run"], r["variant"]))
    results["finished_at"] = datetime.now(timezone.utc).isoformat()
    results["summary"] = {
        "success_count": len(results["success"]),
        "failure_count": len(results["failure"]),
    }
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="A/B compare two endpoints using identical edenn_enhanced vocal-prompt requests."
    )
    parser.add_argument("--endpoint-a", default=ENDPOINT_A, help="First endpoint URL.")
    parser.add_argument("--endpoint-b", default=ENDPOINT_B, help="Second endpoint URL.")
    parser.add_argument("--timeout-s", type=float, default=900.0)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--count", type=int, default=3, help="Number of samples to run.")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Number of sample pairs to run concurrently (each pair is still sequential A→B).",
    )
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="Print selected samples and variants without running API calls.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    selected_samples = SAMPLES[: max(0, args.count)]
    variants = ENHANCED_VOCAL_VARIANTS

    if not variants:
        print("No edenn_enhanced vocal variants found.", flush=True)
        return

    print("Endpoints:")
    print(f"  A: {args.endpoint_a}")
    print(f"  B: {args.endpoint_b}")
    print(f"Samples : {len(selected_samples)}")
    print(f"Variants: {len(variants)}")
    for i, v in enumerate(variants, 1):
        print(f"  variant {i}: modelspec={v['modelspec']} gender={v['vocal_gender']}")
    print(f"Total A/B pairs: {len(selected_samples) * len(variants)}", flush=True)

    if args.schema_only:
        print(json.dumps({"samples": selected_samples, "variants": variants}, indent=2, ensure_ascii=False))
        return

    results = run_ab_suite(
        args.endpoint_a,
        args.endpoint_b,
        args.timeout_s,
        samples=selected_samples,
        variants=variants,
        concurrency=args.concurrency,
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = args.output_dir / (
        f"japan_ab_compare_enhanced_vocals_{len(selected_samples)}x_{stamp}.json"
    )
    output_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(results["summary"], indent=2))
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
