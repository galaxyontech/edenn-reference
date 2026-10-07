from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


DEFAULT_URL = (
    "https://japan-dev-test.api.example.invalid"
    "/api/v1/jobs/video"
)
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "EdennCode" / "Deployment" / "remote_video_generation_results"

SAMPLES = [
    "EdennCode/TestSuites/assets/smoke/videos/Videos2026-04-08_202746_494.mp4",
    "EdennCode/TestSuites/assets/smoke/videos/Videos2026-04-08_104426_347.mp4",
    "EdennCode/TestSuites/assets/smoke/videos/Videos2026-04-07_195857_387.mp4",
    "EdennCode/TestSuites/assets/smoke/videos/sample_clip.mp4",
    "EdennCode/TestSuites/assets/smoke/videos/sample_clip.mp4",
    "EdennCode/TestSuites/assets/smoke/videos/sample_clip.mp4",
    "EdennCode/TestSuites/assets/smoke/videos/sample_clip.mp4",
    "EdennCode/TestSuites/assets/smoke/videos/sample_clip.mp4",
    "EdennCode/TestSuites/assets/smoke/videos/sample_clip.mp4",
    # Repeated on purpose: the run has always sent this clip twice (the second
    # copy used to live under production/ but was byte-identical).
    "EdennCode/TestSuites/assets/smoke/videos/Videos2026-04-08_202746_494.mp4",
]

REQUEST_VARIANTS = [
    {
        "modelspec": "edenn_studio",
        "include_vocals": "true",
        "vocal_gender": "male",
        "verbose_instruction": "true",
        "music_style_prompt": "Chinese male pop music, warm, catchy, medium-fast tempo, clear melody, suitable for short-form advertising.",
        "lyrics_prompt": "Generate Mandarin lyrics for a male singer. The theme is positive confidence, with an easy-to-remember chorus.",
    },
    {
        "modelspec": "edenn_enhanced",
        "include_vocals": "true",
        "vocal_gender": "male",
        "verbose_instruction": "true",
        "music_style_prompt": "Mandarin male vocal pop with warm synths, steady drums, and a confident advertising feel.",
        "lyrics_prompt": "Write concise Mandarin lyrics about momentum, trust, and everyday confidence.",
    },
    {
        "modelspec": "edenn_studio",
        "include_vocals": "true",
        "vocal_gender": "female",
        "verbose_instruction": "true",
        "music_style_prompt": "Japanese female pop, bright and polished, emotional but upbeat, modern commercial sound.",
        "lyrics_prompt": "Generate Japanese lyrics with an optimistic lifestyle theme and a memorable hook.",
    },
    {
        "modelspec": "edenn_enhanced",
        "include_vocals": "true",
        "vocal_gender": "female",
        "verbose_instruction": "true",
        "music_style_prompt": "English female vocal pop, polished and bright, with clean drums, warm synth pads, and a premium lifestyle-ad feel.",
        "lyrics_prompt": "Write concise English lyrics about everyday confidence, trust, and forward motion with a memorable chorus.",
    },
]

VALID_MODELSPEC_FILTERS = ("all", "edenn_enhanced", "edenn_studio")


def _json_body(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text[:2000]


def run_once(url: str, video_path: Path, form: dict[str, str], timeout_s: float) -> dict[str, Any]:
    started = time.monotonic()
    with video_path.open("rb") as video_file:
        response = requests.post(
            url,
            files={"video": (video_path.name, video_file, "video/mp4")},
            data=form,
            timeout=timeout_s,
        )
    elapsed_s = round(time.monotonic() - started, 3)
    body = _json_body(response)
    return {
        "ok": response.ok,
        "status_code": response.status_code,
        "elapsed_s": elapsed_s,
        "video": str(video_path),
        "request": form,
        "response": body,
    }


def select_request_variants(modelspec: str) -> list[dict[str, str]]:
    if modelspec == "all":
        return list(REQUEST_VARIANTS)
    return [variant for variant in REQUEST_VARIANTS if variant["modelspec"] == modelspec]


def build_input_schema(
    url: str,
    samples: list[str],
    request_variants: list[dict[str, str]],
    concurrency: int = 1,
) -> dict[str, Any]:
    return {
        "url": url,
        "method": "POST",
        "content_type": "multipart/form-data",
        "concurrency": concurrency,
        "sample_count": len(samples),
        "samples": samples,
        "request_variant_count": len(request_variants),
        "request_variants": [
            {"variant": index, **variant}
            for index, variant in enumerate(request_variants, start=1)
        ],
    }


def _run_sample(
    *,
    url: str,
    timeout_s: float,
    index: int,
    total: int,
    sample: str,
    form: dict[str, str],
) -> tuple[str, dict[str, Any], str]:
    video_path = (REPO_ROOT / sample).resolve()
    record_base = {"run": index, "sample": sample}

    if not video_path.exists():
        record = {
            **record_base,
            "ok": False,
            "error": f"Video file not found: {video_path}",
            "request": form,
        }
        return "failure", record, f"[{index}/{total}] MISSING: {sample}"

    try:
        result = run_once(url, video_path, form, timeout_s)
        bucket = "success" if result["ok"] else "failure"
        record = {**record_base, **result}
        message = f"[{index}/{total}] HTTP {result['status_code']} in {result['elapsed_s']}s: {sample}"
        return bucket, record, message
    except Exception as exc:
        record = {
            **record_base,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "request": form,
        }
        return "failure", record, f"[{index}/{total}] FAILED: {type(exc).__name__}: {exc}"


def run_suite(
    url: str,
    timeout_s: float,
    *,
    samples: list[str] | None = None,
    request_variants: list[dict[str, str]] | None = None,
    concurrency: int = 1,
) -> dict[str, Any]:
    selected_samples = samples or list(SAMPLES)
    selected_variants = request_variants or list(REQUEST_VARIANTS)
    if not selected_variants:
        raise ValueError("At least one request variant is required.")
    selected_concurrency = max(1, min(int(concurrency), len(selected_samples) or 1))

    results: dict[str, Any] = {
        "url": url,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "input_schema": build_input_schema(
            url,
            selected_samples,
            selected_variants,
            concurrency=selected_concurrency,
        ),
        "success": [],
        "failure": [],
    }

    total = len(selected_samples)
    with ThreadPoolExecutor(max_workers=selected_concurrency) as executor:
        futures = [
            executor.submit(
                _run_sample,
                url=url,
                timeout_s=timeout_s,
                index=index,
                total=total,
                sample=sample,
                form=dict(selected_variants[(index - 1) % len(selected_variants)]),
            )
            for index, sample in enumerate(selected_samples, start=1)
        ]
        for future in as_completed(futures):
            bucket, record, message = future.result()
            results[bucket].append(record)
            print(message, flush=True)

    results["success"].sort(key=lambda record: record["run"])
    results["failure"].sort(key=lambda record: record["run"])

    results["finished_at"] = datetime.now(timezone.utc).isoformat()
    results["summary"] = {
        "success_count": len(results["success"]),
        "failure_count": len(results["failure"]),
    }
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run 10 Japan dev video API samples.")
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
        help="Print the selected samples and request variants without running API calls.",
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
        f"japan_dev_video_api_{model_label}_{len(selected_samples)}x_c{selected_concurrency}_{stamp}.json"
    )
    output_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(results["summary"], indent=2))
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
