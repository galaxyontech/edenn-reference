from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from EdennCode.Scripts.run_enhanced_studio_full_matrix import (  # noqa: E402
    CASES,
    DEFAULT_VIDEO_URL,
    INPUT_SCHEMA,
    run_case,
)


DEFAULT_URL = (
    "https://japan-prod-v2.api.example.invalid"
    "/api/v1/jobs/video"
)
DEFAULT_OUTPUT_DIR = REPO_ROOT / "EdennCode" / "Deployment" / "remote_video_generation_results"
VALID_MODELSPECS = ("all", "edenn_enhanced", "edenn_studio")
VALID_MODES = ("all", "simple", "verbose")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _has_value(body: dict[str, Any], field: str) -> bool:
    value = body.get(field)
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value)
    return value is not None


def _validate_result(case: dict[str, str], result: dict[str, Any]) -> list[str]:
    if not result.get("http_ok"):
        return []

    body = result.get("response")
    if not isinstance(body, dict):
        return ["response is not a JSON object"]

    errors: list[str] = []
    expected_modelspec = case["modelspec"]
    include_vocals = _as_bool(case.get("include_vocals"))

    if body.get("status") != "completed":
        errors.append(f"expected status='completed', got {body.get('status')!r}")
    if body.get("modelspec") != expected_modelspec:
        errors.append(
            f"expected modelspec={expected_modelspec!r}, got {body.get('modelspec')!r}"
        )
    if bool(body.get("include_vocals")) != include_vocals:
        errors.append(
            f"expected include_vocals={include_vocals!r}, got {body.get('include_vocals')!r}"
        )

    for field in ("video_url", "audio_url", "thumbnail_url"):
        if not _has_value(body, field):
            errors.append(f"missing {field}")

    if include_vocals and expected_modelspec in {"edenn_enhanced", "edenn_studio"}:
        for field in (
            "complete_audio_url",
            "primary_full_lyrics",
            "primary_full_lyrics_timestamps",
            "primary_full_word_level_lyrics_timestamps",
        ):
            if not _has_value(body, field):
                errors.append(f"missing {field}")

        matching = body.get("matching")
        if isinstance(matching, dict) and matching.get("used_track") != "primary":
            errors.append(
                f"expected matching.used_track='primary', got {matching.get('used_track')!r}"
            )

    if _as_bool(case.get("verbose_instruction")):
        music_prompt = body.get("music_prompt")
        if not isinstance(music_prompt, dict):
            errors.append("missing music_prompt")
        else:
            if not _has_value(music_prompt, "style_prompt"):
                errors.append("missing music_prompt.style_prompt")
            if case.get("lyrics_prompt") and not _has_value(music_prompt, "lyrics_prompt"):
                errors.append("missing music_prompt.lyrics_prompt")

    return errors


def _select_cases(args: argparse.Namespace) -> list[dict[str, str]]:
    selected = list(CASES)

    if args.modelspec != "all":
        selected = [case for case in selected if case["modelspec"] == args.modelspec]
    if args.mode == "simple":
        selected = [case for case in selected if not _as_bool(case.get("verbose_instruction"))]
    elif args.mode == "verbose":
        selected = [case for case in selected if _as_bool(case.get("verbose_instruction"))]
    if args.filter:
        selected = [case for case in selected if args.filter in case["case_id"]]
    if args.case_id:
        wanted = set(args.case_id)
        selected = [case for case in selected if case["case_id"] in wanted]
    if args.limit is not None:
        selected = selected[: max(0, args.limit)]

    return selected


def _build_schema(
    *,
    url: str,
    video_url: str,
    selected_cases: list[dict[str, str]],
    concurrency: int,
) -> dict[str, Any]:
    return {
        "url": url,
        "video_url": video_url,
        "case_count": len(selected_cases),
        "concurrency": concurrency,
        "input_schema": INPUT_SCHEMA,
        "cases": selected_cases,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run enhanced/studio video API matrix against Japan prod-v2."
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--video-url", default=DEFAULT_VIDEO_URL)
    parser.add_argument("--timeout-s", type=float, default=1200.0)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--modelspec",
        choices=VALID_MODELSPECS,
        default="all",
        help="Run all cases or only one public modelspec.",
    )
    parser.add_argument(
        "--mode",
        choices=VALID_MODES,
        default="all",
        help="Run all, simple-prompt, or verbose-prompt cases.",
    )
    parser.add_argument(
        "--filter",
        default=None,
        help="Only run cases whose case_id contains this substring.",
    )
    parser.add_argument(
        "--case-id",
        action="append",
        help="Run one exact case_id. May be passed more than once.",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="Print selected cases and exit without sending requests.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    selected_cases = _select_cases(args)
    if not selected_cases:
        raise SystemExit("No cases selected.")

    concurrency = max(1, min(args.concurrency, len(selected_cases)))
    schema = _build_schema(
        url=args.url,
        video_url=args.video_url,
        selected_cases=selected_cases,
        concurrency=concurrency,
    )
    print("Input schema:")
    print(json.dumps(schema, indent=2, ensure_ascii=False), flush=True)
    if args.schema_only:
        return

    record: dict[str, Any] = {
        "url": args.url,
        "video_url": args.video_url,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "case_count": len(selected_cases),
        "concurrency": concurrency,
        "input_schema": INPUT_SCHEMA,
        "cases": selected_cases,
        "success": [],
        "failure": [],
    }

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(
                run_case,
                endpoint=args.url,
                video_url=args.video_url,
                form=form,
                timeout_s=args.timeout_s,
            ): (index, form)
            for index, form in enumerate(selected_cases, start=1)
        }

        total = len(futures)
        for future in as_completed(futures):
            index, form = futures[future]
            case_id = form["case_id"]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "case_id": case_id,
                    "http_ok": False,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "input": {
                        key: value
                        for key, value in form.items()
                        if key != "case_id"
                    }
                    | {"video_url": args.video_url},
                    "summary": {},
                    "output_schema": {},
                    "response": {},
                }

            result["run_index"] = index
            result["http_ok"] = bool(result.get("ok"))
            result["contract_errors"] = _validate_result(form, result)
            result["contract_ok"] = not result["contract_errors"]
            result["ok"] = result["http_ok"] and result["contract_ok"]

            bucket = "success" if result["ok"] else "failure"
            record[bucket].append(result)

            status = result.get("status_code", "ERROR")
            elapsed = result.get("elapsed_s", "?")
            model = result.get("summary", {}).get("modelspec", "?")
            errors = "; ".join(result["contract_errors"][:2])
            suffix = f"  errors={errors}" if errors else ""
            print(
                f"[{index}/{total}] {bucket.upper():7} {case_id} "
                f"HTTP {status} {elapsed}s model={model}{suffix}",
                flush=True,
            )

    record["success"].sort(key=lambda item: item["run_index"])
    record["failure"].sort(key=lambda item: item["run_index"])
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    record["summary"] = {
        "total": len(selected_cases),
        "success_count": len(record["success"]),
        "failure_count": len(record["failure"]),
        "http_success_count": sum(
            1 for item in record["success"] + record["failure"] if item.get("http_ok")
        ),
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    model_label = args.modelspec if args.modelspec != "all" else "mixed"
    mode_label = args.mode if args.mode != "all" else "allmodes"
    output_path = args.output_dir / (
        "japan_prod_v2_enhanced_studio_matrix_"
        f"{model_label}_{mode_label}_{len(selected_cases)}x_c{concurrency}_{stamp}.json"
    )
    output_path.write_text(
        json.dumps(record, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(json.dumps(record["summary"], indent=2), flush=True)
    print(f"Wrote {output_path}", flush=True)


if __name__ == "__main__":
    main()
