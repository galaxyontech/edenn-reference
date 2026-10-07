from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from EdennCode.Scripts.run_japan_video_api_10x import (
    DEFAULT_OUTPUT_DIR,
    SAMPLES,
    VALID_MODELSPEC_FILTERS,
    build_input_schema,
    run_suite,
    select_request_variants,
)


DEFAULT_URL = (
    "https://japan-prod-v2.api.example.invalid"
    "/api/v1/jobs/video"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run 10 Japan prod-v2 video API samples.")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--timeout-s", type=float, default=900.0)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--count", type=int, default=len(SAMPLES), help="Number of samples to run.")
    parser.add_argument("--concurrency", type=int, default=2, help="Number of concurrent API calls.")
    parser.add_argument(
        "--modelspec",
        choices=VALID_MODELSPEC_FILTERS,
        default="all",
        help="Limit request variants to one modelspec.",
    )
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="Print the exact samples and request variants without running the API calls.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    selected_samples = SAMPLES[: max(0, args.count)]
    selected_variants = select_request_variants(args.modelspec)
    selected_concurrency = max(1, min(args.concurrency, len(selected_samples) or 1))
    input_schema = build_input_schema(
        args.url,
        selected_samples,
        selected_variants,
        concurrency=selected_concurrency,
    )
    print("Input schema:")
    print(json.dumps(input_schema, indent=2, ensure_ascii=False), flush=True)
    if args.schema_only:
        return

    results = run_suite(
        args.url,
        args.timeout_s,
        samples=selected_samples,
        request_variants=selected_variants,
        concurrency=selected_concurrency,
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    model_label = args.modelspec if args.modelspec != "all" else "mixed"
    output_path = args.output_dir / (
        f"japan_prod_v2_video_api_{model_label}_{len(selected_samples)}x_c{selected_concurrency}_{stamp}.json"
    )
    output_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(results["summary"], indent=2))
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
