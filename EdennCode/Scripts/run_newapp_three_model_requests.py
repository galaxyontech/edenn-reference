from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from EdennCode.Scripts.run_enhanced_studio_full_matrix import (  # noqa: E402
    DEFAULT_VIDEO_URL,
    _build_output_schema,
    _json_body,
    run_case,
)
from EdennCode.TestSuites.helpers.paths import SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH  # noqa: E402


DEFAULT_URL = (
    "https://staging-app.worker.example.invalid"
    "/api/v1/jobs/video"
)
DEFAULT_OUTPUT_DIR = (
    REPO_ROOT / "EdennCode" / "Deployment" / "remote_video_generation_results"
)


CASES: list[dict[str, str]] = [
    {
        "case_id": "newapp_basic_vocals",
        "modelspec": "edenn_basic",
        "include_vocals": "true",
        "vocal_gender": "female",
        "user_prompt": "Create a modern female vocal pop song with clear lyrics for this video.",
        "preserve_original_audio": "true",
        "music_volume": "0.7",
        "compression_flag": "true",
    },
    {
        "case_id": "newapp_enhanced_vocals",
        "modelspec": "edenn_enhanced",
        "include_vocals": "true",
        "vocal_gender": "female",
        "user_prompt": "Create a modern female vocal pop song with clear lyrics for this video.",
        "preserve_original_audio": "true",
        "music_volume": "0.7",
        "compression_flag": "true",
    },
    {
        "case_id": "newapp_studio_vocals",
        "modelspec": "edenn_studio",
        "include_vocals": "true",
        "vocal_gender": "female",
        "user_prompt": "Create a cinematic female vocal anthem with lyrics for this video.",
        "preserve_original_audio": "true",
        "music_volume": "0.7",
        "compression_flag": "true",
    },
]


def _compact_response(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    return {
        "job_id": body.get("job_id"),
        "status": body.get("status"),
        "version": body.get("version"),
        "modelspec": body.get("modelspec"),
        "include_vocals": body.get("include_vocals"),
        "vocal_gender": body.get("vocal_gender"),
        "video_url": body.get("video_url"),
        "audio_url": body.get("audio_url"),
        "complete_audio_url": body.get("complete_audio_url"),
        "thumbnail_url": body.get("thumbnail_url"),
        "primary_full_lyrics_present": bool(body.get("primary_full_lyrics")),
        "primary_full_lyrics_timestamps_count": len(
            body.get("primary_full_lyrics_timestamps") or []
        ),
        "primary_full_word_level_lyrics_timestamps_count": len(
            body.get("primary_full_word_level_lyrics_timestamps") or []
        ),
        "lyrics_timestamps_count": len(body.get("lyrics_timestamps") or []),
        "word_level_lyrics_timestamps_count": len(
            body.get("word_level_lyrics_timestamps") or []
        ),
        "matching": body.get("matching"),
        "token_usage": body.get("token_usage"),
        "critical_warning": body.get("critical_warning"),
        "job_received_timestamp": body.get("job_received_timestamp"),
        "job_finished_timestamp": body.get("job_finished_timestamp"),
    }


def _run_upload_case(
    *,
    endpoint: str,
    video_path: Path,
    form: dict[str, str],
    timeout_s: float,
) -> dict[str, Any]:
    case_id = form["case_id"]
    payload = {key: value for key, value in form.items() if key != "case_id"}
    started = time.monotonic()
    try:
        with video_path.open("rb") as handle:
            response = requests.post(
                endpoint,
                data=payload,
                files={
                    "video": (
                        video_path.name,
                        handle,
                        "video/mp4",
                    )
                },
                timeout=timeout_s,
            )
        elapsed_s = round(time.monotonic() - started, 3)
        body = _json_body(response)
        ok = response.ok
        status_code = response.status_code
        error = None
    except Exception as exc:
        elapsed_s = round(time.monotonic() - started, 3)
        body = {}
        ok = False
        status_code = None
        error = f"{type(exc).__name__}: {exc}"

    output_schema: dict[str, Any] = {}
    summary: dict[str, Any] = {}
    if isinstance(body, dict):
        output_schema = _build_output_schema(body)
        summary = {
            "job_id": body.get("job_id"),
            "status": body.get("status"),
            "version": body.get("version"),
            "modelspec": body.get("modelspec"),
            "include_vocals": body.get("include_vocals"),
            "has_upload_url": bool(body.get("upload_url")),
            "has_audio_url": bool(body.get("audio_url")),
            "has_complete_audio_url": bool(body.get("complete_audio_url")),
            "has_secondary_complete_audio_url": bool(body.get("secondary_complete_audio_url")),
            "has_video_url": bool(body.get("video_url")),
            "has_thumbnail_url": bool(body.get("thumbnail_url")),
            "storage_host": None,
            "token_usage": body.get("token_usage"),
            "job_received_timestamp": body.get("job_received_timestamp"),
            "job_finished_timestamp": body.get("job_finished_timestamp"),
        }

    result: dict[str, Any] = {
        "case_id": case_id,
        "ok": ok,
        "status_code": status_code,
        "elapsed_s": elapsed_s,
        "input": payload | {"video_path": str(video_path)},
        "summary": summary,
        "output_schema": output_schema,
        "response": body,
    }
    if error:
        result["error"] = error
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send one newapp video request to basic, enhanced, and studio."
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--video-url", default=DEFAULT_VIDEO_URL)
    parser.add_argument(
        "--video-path",
        type=Path,
        default=SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH,
        help="Local video file to upload. Pass '' to use --video-url instead.",
    )
    parser.add_argument("--timeout-s", type=float, default=1800.0)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    started_at = datetime.now(timezone.utc).isoformat()
    suite_start = time.monotonic()
    record: dict[str, Any] = {
        "url": args.url,
        "video_url": args.video_url,
        "video_path": str(args.video_path) if args.video_path else None,
        "started_at": started_at,
        "case_count": len(CASES),
        "cases": CASES,
        "success": [],
        "failure": [],
    }

    print(
        f"Sending {len(CASES)} sequential requests to {args.url}",
        flush=True,
    )
    for index, case in enumerate(CASES, start=1):
        print(
            f"[{index}/{len(CASES)}] START {case['case_id']} "
            f"model={case['modelspec']} vocals={case['include_vocals']}",
            flush=True,
        )
        if args.video_path:
            result = _run_upload_case(
                endpoint=args.url,
                video_path=args.video_path,
                form=case,
                timeout_s=args.timeout_s,
            )
        else:
            result = run_case(
                endpoint=args.url,
                video_url=args.video_url,
                form=case,
                timeout_s=args.timeout_s,
            )
        result["run_index"] = index
        result["compact_response"] = _compact_response(result.get("response"))
        bucket = "success" if result.get("ok") else "failure"
        record[bucket].append(result)
        summary = result.get("summary") or {}
        print(
            f"[{index}/{len(CASES)}] {bucket.upper()} {case['case_id']} "
            f"HTTP {result.get('status_code')} elapsed={result.get('elapsed_s')}s "
            f"api_status={summary.get('status')} model={summary.get('modelspec')} "
            f"job={summary.get('job_id')} "
            f"video={bool(summary.get('has_video_url'))} "
            f"audio={bool(summary.get('has_audio_url'))} "
            f"complete_audio={bool(summary.get('has_complete_audio_url'))}",
            flush=True,
        )

    record["success"].sort(key=lambda item: item["run_index"])
    record["failure"].sort(key=lambda item: item["run_index"])
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    all_results = record["success"] + record["failure"]
    merged_output_schema: dict[str, set[str]] = {}
    for item in record["success"]:
        response = item.get("response")
        if isinstance(response, dict):
            for field, type_name in _build_output_schema(response).items():
                merged_output_schema.setdefault(field, set()).add(type_name)
    record["output_schema"] = {
        field: sorted(type_names)
        for field, type_names in merged_output_schema.items()
    }
    record["summary"] = {
        "total": len(CASES),
        "success_count": len(record["success"]),
        "failure_count": len(record["failure"]),
        "elapsed_s": round(time.monotonic() - suite_start, 1),
        "cases": [
            {
                "case_id": item.get("case_id"),
                "ok": item.get("ok"),
                "status_code": item.get("status_code"),
                "elapsed_s": item.get("elapsed_s"),
                "compact_response": item.get("compact_response"),
                "error": item.get("error"),
            }
            for item in sorted(all_results, key=lambda value: value["run_index"])
        ],
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = args.output_dir / f"newapp_three_models_vocals_{stamp}.json"
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
