from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from EdennCode.Scripts.run_newapp_three_model_requests import (  # noqa: E402
    DEFAULT_OUTPUT_DIR,
    DEFAULT_URL,
    _compact_response,
    _run_upload_case,
)
from EdennCode.TestSuites.helpers.paths import SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH  # noqa: E402

VALID_MODELSPECS = ("edenn_basic", "edenn_enhanced", "edenn_studio")


def _video_duration_s(video_path: Path) -> float | None:
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nk=1:nw=1",
                str(video_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return round(float(completed.stdout.strip()), 6)
    except Exception:
        return None


def _response_duration_s(body: Any) -> float | None:
    if not isinstance(body, dict):
        return None
    metadata = body.get("video_metadata")
    if not isinstance(metadata, dict):
        return None
    duration = metadata.get("duration")
    try:
        return round(float(duration), 6)
    except (TypeError, ValueError):
        return None


def _as_bool_arg(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(
        "Expected a boolean value such as true/false, yes/no, or 1/0."
    )


def _build_case(
    index: int,
    *,
    modelspec: str,
    include_vocals: bool,
    vocal_gender: str,
    video_length_s: float | None,
    user_prompt_override: str | None = None,
) -> dict[str, str]:
    vocal_label = "vocals" if include_vocals else "instrumental"
    cleaned_vocal_gender = vocal_gender.strip().lower() or "female"
    if user_prompt_override:
        prompt = user_prompt_override
    elif include_vocals:
        if modelspec == "edenn_studio":
            prompt = f"Create a cinematic {cleaned_vocal_gender} vocal anthem with lyrics for this video."
        else:
            prompt = (
                f"Create a modern {cleaned_vocal_gender} vocal pop song with clear lyrics for this "
                "short lifestyle video. Keep the hook memorable and the energy upbeat."
            )
    else:
        prompt = (
            "Create a modern instrumental soundtrack for this short lifestyle video. "
            "Keep the energy upbeat and avoid lyrics or vocals."
        )
    if video_length_s is not None:
        prompt += f" The source video is about {video_length_s:.2f} seconds long."
    return {
        "case_id": f"newapp_{modelspec.removeprefix('edenn_')}_sync_{vocal_label}_{index:02d}",
        "modelspec": modelspec,
        "include_vocals": str(include_vocals).lower(),
        "vocal_gender": cleaned_vocal_gender,
        "user_prompt": prompt,
        "preserve_original_audio": "true",
        "music_volume": "0.7",
        "compression_flag": "true",
    }


def _run_one(
    *,
    index: int,
    total: int,
    endpoint: str,
    modelspec: str,
    include_vocals: bool,
    vocal_gender: str,
    video_path: Path,
    video_length_s: float | None,
    timeout_s: float,
    user_prompt_override: str | None,
) -> dict[str, Any]:
    case = _build_case(
        index,
        modelspec=modelspec,
        include_vocals=include_vocals,
        vocal_gender=vocal_gender,
        video_length_s=video_length_s,
        user_prompt_override=user_prompt_override,
    )
    print(
        f"[{index:02d}/{total}] START {case['case_id']} "
        f"model={modelspec} video_length_s={video_length_s}",
        flush=True,
    )
    result = _run_upload_case(
        endpoint=endpoint,
        video_path=video_path,
        form=case,
        timeout_s=timeout_s,
    )
    result["run_index"] = index
    result["video_length_s"] = video_length_s
    result["response_video_duration_s"] = _response_duration_s(result.get("response"))
    result["compact_response"] = _compact_response(result.get("response"))
    result["compact_response"]["response_video_duration_s"] = result[
        "response_video_duration_s"
    ]
    result["input"]["video_length_s"] = video_length_s
    summary = result.get("summary") or {}
    bucket = "SUCCESS" if result.get("ok") else "FAILURE"
    print(
        f"[{index:02d}/{total}] {bucket} {case['case_id']} "
        f"HTTP {result.get('status_code')} elapsed={result.get('elapsed_s')}s "
        f"api_status={summary.get('status')} model={summary.get('modelspec')} "
        f"job={summary.get('job_id')} "
        f"video_duration={result['response_video_duration_s']} "
        f"video={bool(summary.get('has_video_url'))} "
        f"audio={bool(summary.get('has_audio_url'))} "
        f"complete_audio={bool(summary.get('has_complete_audio_url'))}",
        flush=True,
    )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send concurrent requests to the newapp sync video API."
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument(
        "--video-path",
        type=Path,
        default=SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH,
    )
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument(
        "--modelspec",
        choices=VALID_MODELSPECS,
        default="edenn_enhanced",
    )
    parser.add_argument(
        "--include-vocals",
        type=_as_bool_arg,
        default=True,
    )
    parser.add_argument("--vocal-gender", default="female")
    parser.add_argument(
        "--user-prompt",
        default=None,
        help="Optional prompt to use for every request.",
    )
    parser.add_argument("--timeout-s", type=float, default=1800.0)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    video_path = args.video_path.expanduser().resolve()
    if not video_path.is_file():
        raise SystemExit(f"Video file not found: {video_path}")

    count = max(1, args.count)
    concurrency = max(1, min(args.concurrency, count))
    video_length_s = _video_duration_s(video_path)
    started_at = datetime.now(timezone.utc).isoformat()
    suite_start = time.monotonic()

    record: dict[str, Any] = {
        "url": args.url,
        "video_path": str(video_path),
        "video_length_s": video_length_s,
        "video_size_bytes": video_path.stat().st_size,
        "started_at": started_at,
        "case_count": count,
        "concurrency": concurrency,
        "modelspec": args.modelspec,
        "include_vocals": args.include_vocals,
        "vocal_gender": args.vocal_gender,
        "success": [],
        "failure": [],
    }

    print(
        f"Sending {count} {args.modelspec} sync requests with concurrency={concurrency} "
        f"include_vocals={args.include_vocals} vocal_gender={args.vocal_gender} "
        f"to {args.url}; video_length_s={video_length_s}",
        flush=True,
    )

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(
                _run_one,
                index=index,
                total=count,
                endpoint=args.url,
                modelspec=args.modelspec,
                include_vocals=args.include_vocals,
                vocal_gender=args.vocal_gender,
                video_path=video_path,
                video_length_s=video_length_s,
                timeout_s=args.timeout_s,
                user_prompt_override=args.user_prompt,
            ): index
            for index in range(1, count + 1)
        }
        completed = 0
        success_count = 0
        failure_count = 0
        for future in as_completed(futures):
            index = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "case_id": f"newapp_{args.modelspec.removeprefix('edenn_')}_sync_{'vocals' if args.include_vocals else 'instrumental'}_{index:02d}",
                    "run_index": index,
                    "ok": False,
                    "status_code": None,
                    "elapsed_s": None,
                    "video_length_s": video_length_s,
                    "response_video_duration_s": None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "summary": {},
                    "response": {},
                    "compact_response": {},
                }
            bucket = "success" if result.get("ok") else "failure"
            record[bucket].append(result)
            completed += 1
            success_count += 1 if result.get("ok") else 0
            failure_count += 0 if result.get("ok") else 1
            print(
                f"[progress {completed:02d}/{count}] {bucket.upper()} "
                f"run={index:02d} success={success_count} failure={failure_count} "
                f"suite_elapsed={time.monotonic() - suite_start:.1f}s",
                flush=True,
            )

    record["success"].sort(key=lambda item: item["run_index"])
    record["failure"].sort(key=lambda item: item["run_index"])
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    all_results = record["success"] + record["failure"]
    record["summary"] = {
        "total": count,
        "success_count": len(record["success"]),
        "failure_count": len(record["failure"]),
        "elapsed_s": round(time.monotonic() - suite_start, 1),
        "video_length_s": video_length_s,
        "cases": [
            {
                "case_id": item.get("case_id"),
                "ok": item.get("ok"),
                "status_code": item.get("status_code"),
                "elapsed_s": item.get("elapsed_s"),
                "video_length_s": item.get("video_length_s"),
                "response_video_duration_s": item.get("response_video_duration_s"),
                "compact_response": item.get("compact_response"),
                "error": item.get("error"),
            }
            for item in sorted(all_results, key=lambda value: value["run_index"])
        ],
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = args.output_dir / (
        f"newapp_{args.modelspec.removeprefix('edenn_')}_sync_"
        f"{'vocals' if args.include_vocals else 'instrumental'}_"
        f"burst{count}_c{concurrency}_{stamp}.json"
    )
    output_path.write_text(
        json.dumps(record, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(json.dumps(record["summary"], indent=2, ensure_ascii=False), flush=True)
    print(f"Saved -> {output_path}", flush=True)
    if record["failure"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
