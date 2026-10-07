from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from EdennCode.Scripts.run_newapp_enhanced_burst import (  # noqa: E402
    _response_duration_s,
    _video_duration_s,
)
from EdennCode.Scripts.run_newapp_three_model_requests import (  # noqa: E402
    DEFAULT_OUTPUT_DIR,
    DEFAULT_URL,
    _compact_response,
)
from EdennCode.TestSuites.helpers.paths import SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH  # noqa: E402

VALID_MODELSPECS = ("edenn_basic", "edenn_enhanced", "edenn_studio")


def _normalize_base_url(value: str) -> str:
    url = value.rstrip("/")
    for suffix in (
        "/api/v1/jobs/async_video_music_gen",
        "/api/v1/jobs/video",
    ):
        if url.endswith(suffix):
            return url[: -len(suffix)]
    return url


def _as_bool_arg(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(
        "Expected a boolean value such as true/false, yes/no, or 1/0."
    )


def _build_async_case(
    index: int,
    *,
    modelspec: str,
    include_vocals: bool,
    video_length_s: float | None,
    user_prompt_override: str | None = None,
) -> dict[str, str]:
    vocal_label = "vocals" if include_vocals else "instrumental"
    if user_prompt_override:
        prompt = user_prompt_override
    elif include_vocals:
        if modelspec == "edenn_studio":
            prompt = "Create a cinematic female vocal anthem with lyrics for this video."
        else:
            prompt = (
                "Create a modern female vocal pop song with clear lyrics for this "
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
        "case_id": f"newapp_{modelspec.removeprefix('edenn_')}_async_{vocal_label}_{index:02d}",
        "modelspec": modelspec,
        "include_vocals": str(include_vocals).lower(),
        "vocal_gender": "female",
        "user_prompt": prompt,
        "preserve_original_audio": "true",
        "music_volume": "0.7",
        "compression_flag": "true",
    }


def _submit_one(
    *,
    index: int,
    total: int,
    submit_url: str,
    modelspec: str,
    include_vocals: bool,
    video_path: Path,
    video_length_s: float | None,
    timeout_s: float,
    retries: int,
    user_prompt_override: str | None,
) -> dict[str, Any]:
    case = _build_async_case(
        index,
        modelspec=modelspec,
        include_vocals=include_vocals,
        video_length_s=video_length_s,
        user_prompt_override=user_prompt_override,
    )
    payload = {key: value for key, value in case.items() if key != "case_id"}
    started = time.monotonic()
    print(f"[{index:02d}/{total}] SUBMIT {case['case_id']}", flush=True)
    body: Any = {}
    ok = False
    status_code = None
    error = None
    attempt_errors: list[str] = []
    for attempt in range(1, retries + 2):
        try:
            with video_path.open("rb") as handle:
                response = requests.post(
                    submit_url,
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
            try:
                body = response.json()
            except ValueError:
                body = response.text
            ok = response.ok and isinstance(body, dict) and bool(body.get("job_id"))
            status_code = response.status_code
            error = None if ok else f"HTTP {response.status_code}"
            break
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            attempt_errors.append(error)
            if attempt <= retries:
                time.sleep(min(2.0, 0.25 * attempt))

    elapsed_s = round(time.monotonic() - started, 3)
    print(
        f"[{index:02d}/{total}] SUBMITTED ok={ok} status={status_code} "
        f"elapsed={elapsed_s}s job={body.get('job_id') if isinstance(body, dict) else None}",
        flush=True,
    )
    return {
        "case_id": case["case_id"],
        "run_index": index,
        "ok": ok,
        "status_code": status_code,
        "elapsed_s": elapsed_s,
        "input": payload | {
            "video_path": str(video_path),
            "video_length_s": video_length_s,
        },
        "accepted": body,
        "error": error,
        "attempt_errors": attempt_errors,
    }


def _poll_job(
    *,
    status_url: str,
    job_id: str,
    poll_s: float,
    timeout_s: float,
) -> dict[str, Any]:
    started = time.monotonic()
    attempts = 0
    last_payload: Any = None
    while True:
        attempts += 1
        try:
            response = requests.get(status_url.format(job_id=job_id), timeout=60)
            try:
                payload: Any = response.json()
            except ValueError:
                payload = response.text
            last_payload = payload
            if response.status_code == 200 and isinstance(payload, dict):
                status = payload.get("status")
                if status in {"completed", "failed"}:
                    result = payload.get("result")
                    return {
                        "ok": status == "completed",
                        "status_code": response.status_code,
                        "status": status,
                        "elapsed_s": round(time.monotonic() - started, 3),
                        "attempts": attempts,
                        "payload": payload,
                        "response_video_duration_s": _response_duration_s(result),
                        "compact_response": _compact_response(result),
                    }
            elif response.status_code == 404:
                return {
                    "ok": False,
                    "status_code": response.status_code,
                    "status": "missing",
                    "elapsed_s": round(time.monotonic() - started, 3),
                    "attempts": attempts,
                    "payload": payload,
                    "response_video_duration_s": None,
                    "compact_response": {},
                }
        except Exception as exc:
            last_payload = f"{type(exc).__name__}: {exc}"

        if time.monotonic() - started >= timeout_s:
            return {
                "ok": False,
                "status_code": None,
                "status": "timeout",
                "elapsed_s": round(time.monotonic() - started, 3),
                "attempts": attempts,
                "payload": last_payload,
                "response_video_duration_s": None,
                "compact_response": {},
            }
        time.sleep(poll_s)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Submit concurrent edenn_enhanced async jobs to the deployed newapp API."
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_URL.replace("/api/v1/jobs/video", ""),
    )
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
    parser.add_argument("--submit-timeout-s", type=float, default=240.0)
    parser.add_argument("--submit-retries", type=int, default=3)
    parser.add_argument(
        "--user-prompt",
        default=None,
        help="Optional prompt to use for every submitted job.",
    )
    parser.add_argument(
        "--resolve-ip",
        default=None,
        help="Optional IPv4 address to use for the API hostname when local Python DNS fails.",
    )
    parser.add_argument(
        "--poll-concurrency",
        type=int,
        default=None,
        help="Maximum number of accepted jobs to poll in parallel. Defaults to --concurrency.",
    )
    parser.add_argument("--poll-timeout-s", type=float, default=2400.0)
    parser.add_argument("--poll-s", type=float, default=15.0)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    base_url = _normalize_base_url(args.base_url)
    submit_url = f"{base_url}/api/v1/jobs/async_video_music_gen"
    status_url = f"{base_url}/api/v1/jobs/async_video_music_gen/{{job_id}}"
    parsed = urlparse(base_url)
    if parsed.hostname:
        if args.resolve_ip:
            original_getaddrinfo = socket.getaddrinfo

            def _pinned_getaddrinfo(
                host: str,
                port: int | str | None,
                family: int = 0,
                type: int = 0,
                proto: int = 0,
                flags: int = 0,
            ):
                if host == parsed.hostname:
                    return original_getaddrinfo(
                        args.resolve_ip,
                        port,
                        family,
                        type,
                        proto,
                        flags,
                    )
                return original_getaddrinfo(host, port, family, type, proto, flags)

            socket.getaddrinfo = _pinned_getaddrinfo
        else:
            socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    video_path = args.video_path.expanduser().resolve()
    if not video_path.is_file():
        raise SystemExit(f"Video file not found: {video_path}")

    count = max(1, args.count)
    concurrency = max(1, min(args.concurrency, count))
    poll_concurrency = max(
        1,
        min(args.poll_concurrency if args.poll_concurrency is not None else concurrency, count),
    )
    video_length_s = _video_duration_s(video_path)
    suite_start = time.monotonic()
    record: dict[str, Any] = {
        "base_url": base_url,
        "submit_url": submit_url,
        "status_url": status_url,
        "video_path": str(video_path),
        "video_length_s": video_length_s,
        "video_size_bytes": video_path.stat().st_size,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "case_count": count,
        "concurrency": concurrency,
        "poll_concurrency": poll_concurrency,
        "resolve_ip": args.resolve_ip,
        "submit_retries": args.submit_retries,
        "modelspec": args.modelspec,
        "include_vocals": args.include_vocals,
        "submitted": [],
        "final": [],
    }

    print(
        f"Submitting {count} async {args.modelspec} jobs with concurrency={concurrency} "
        f"include_vocals={args.include_vocals} "
        f"to {submit_url}; video_length_s={video_length_s}",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(
                _submit_one,
                index=index,
                total=count,
                submit_url=submit_url,
                modelspec=args.modelspec,
                include_vocals=args.include_vocals,
                video_path=video_path,
                video_length_s=video_length_s,
                timeout_s=args.submit_timeout_s,
                retries=max(0, args.submit_retries),
                user_prompt_override=args.user_prompt,
            ): index
            for index in range(1, count + 1)
        }
        for future in as_completed(futures):
            record["submitted"].append(future.result())

    record["submitted"].sort(key=lambda item: item["run_index"])
    accepted = [
        item
        for item in record["submitted"]
        if item.get("ok") and isinstance(item.get("accepted"), dict)
    ]
    print(f"Accepted {len(accepted)}/{count} jobs; polling final status", flush=True)

    with ThreadPoolExecutor(max_workers=min(poll_concurrency, max(1, len(accepted)))) as executor:
        futures = {}
        for item in accepted:
            accepted_payload = item["accepted"]
            job_id = accepted_payload["job_id"]
            futures[
                executor.submit(
                    _poll_job,
                    status_url=status_url,
                    job_id=job_id,
                    poll_s=args.poll_s,
                    timeout_s=args.poll_timeout_s,
                )
            ] = item

        for future in as_completed(futures):
            item = futures[future]
            job_id = item["accepted"]["job_id"]
            final = future.result()
            final["case_id"] = item["case_id"]
            final["run_index"] = item["run_index"]
            final["job_id"] = job_id
            final["video_length_s"] = video_length_s
            record["final"].append(final)
            print(
                f"[{item['run_index']:02d}/{count}] FINAL status={final['status']} "
                f"ok={final['ok']} elapsed={final['elapsed_s']}s "
                f"duration={final['response_video_duration_s']}",
                flush=True,
            )

    record["final"].sort(key=lambda item: item["run_index"])
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    record["summary"] = {
        "total": count,
        "accepted_count": len(accepted),
        "submit_failure_count": count - len(accepted),
        "completed_count": sum(1 for item in record["final"] if item.get("ok")),
        "final_failure_count": sum(1 for item in record["final"] if not item.get("ok")),
        "elapsed_s": round(time.monotonic() - suite_start, 1),
        "video_length_s": video_length_s,
        "cases": [
            {
                "case_id": item.get("case_id"),
                "job_id": item.get("job_id"),
                "ok": item.get("ok"),
                "status": item.get("status"),
                "status_code": item.get("status_code"),
                "elapsed_s": item.get("elapsed_s"),
                "response_video_duration_s": item.get("response_video_duration_s"),
                "compact_response": item.get("compact_response"),
            }
            for item in record["final"]
        ],
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = args.output_dir / (
        f"newapp_{args.modelspec.removeprefix('edenn_')}_async_"
        f"{'vocals' if args.include_vocals else 'instrumental'}_"
        f"burst{count}_c{concurrency}_{stamp}.json"
    )
    output_path.write_text(
        json.dumps(record, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(json.dumps(record["summary"], indent=2, ensure_ascii=False), flush=True)
    print(f"Saved -> {output_path}", flush=True)
    if (
        record["summary"]["submit_failure_count"]
        or record["summary"]["final_failure_count"]
        or record["summary"]["completed_count"] != count
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
