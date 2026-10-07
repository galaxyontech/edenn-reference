from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from EdennCode.Scripts.run_enhanced_studio_full_matrix import (  # noqa: E402
    CASES as FULL_USE_CASES,
    DEFAULT_VIDEO_URL,
    INPUT_SCHEMA as BASE_INPUT_SCHEMA,
    run_case as _run_case,
)


DEFAULT_URL = (
    "https://staging-app.worker.example.invalid"
    "/api/v1/jobs/video"
)
DEFAULT_OUTPUT_DIR = (
    REPO_ROOT / "EdennCode" / "Deployment" / "remote_video_generation_results"
)
DEFAULT_EXPECTED_STORAGE_HOST = "secondary.storage.example.invalid"
VALID_MODES = ("all", "simple", "verbose")
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
DEFAULT_EXAMPLE_CASE_IDS = (
    "enhanced_simple_en_female",
)
INPUT_SCHEMA = json.loads(json.dumps(BASE_INPUT_SCHEMA))
INPUT_SCHEMA["fields"]["modelspec"]["note"] = "edenn_enhanced"


def _clip(value: Any, max_len: int = 220) -> str:
    text = str(value)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


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
    if isinstance(value, dict):
        return bool(value)
    return value is not None


def _host(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urlparse(value)
    return parsed.netloc or None


def _expected_response_modelspec(case: dict[str, str]) -> str:
    # Instrumental enhanced/studio requests intentionally route through the
    # edenn_basic generation path. The current full use-case matrix is vocal,
    # but keeping this here makes future case additions validate correctly.
    if not _as_bool(case.get("include_vocals")):
        return "edenn_basic"
    return case["modelspec"]


def _validate_storage_host(
    *,
    body: dict[str, Any],
    fields: tuple[str, ...],
    expected_storage_host: str | None,
) -> list[str]:
    if not expected_storage_host:
        return []

    errors: list[str] = []
    for field in fields:
        value = body.get(field)
        if value is None:
            continue
        actual_host = _host(value)
        if actual_host != expected_storage_host:
            errors.append(
                f"expected {field} host {expected_storage_host!r}, got {actual_host!r}"
            )
    return errors


def _validate_result(
    case: dict[str, str],
    result: dict[str, Any],
    *,
    expected_storage_host: str | None,
) -> list[str]:
    if not result.get("http_ok"):
        return []

    body = result.get("response")
    if not isinstance(body, dict):
        return ["response is not a JSON object"]

    errors: list[str] = []
    include_vocals = _as_bool(case.get("include_vocals"))
    expected_modelspec = _expected_response_modelspec(case)

    if body.get("status") != "completed":
        errors.append(f"expected status='completed', got {body.get('status')!r}")
    if body.get("modelspec") != expected_modelspec:
        errors.append(
            f"expected modelspec={expected_modelspec!r}, got {body.get('modelspec')!r}"
        )
    if bool(body.get("include_vocals")) != include_vocals:
        errors.append(
            f"expected include_vocals={include_vocals!r}, "
            f"got {body.get('include_vocals')!r}"
        )

    for field in ("upload_url", "video_url", "audio_url", "thumbnail_url"):
        if not _has_value(body, field):
            errors.append(f"missing {field}")

    if include_vocals and case["modelspec"] in {"edenn_enhanced", "edenn_studio"}:
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
                "expected matching.used_track='primary', "
                f"got {matching.get('used_track')!r}"
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

    errors.extend(
        _validate_storage_host(
            body=body,
            fields=(
                "upload_url",
                "video_url",
                "audio_url",
                "thumbnail_url",
                "complete_audio_url",
                "secondary_complete_audio_url",
            ),
            expected_storage_host=expected_storage_host,
        )
    )
    return errors


def _is_retryable(result: dict[str, Any]) -> bool:
    status_code = result.get("status_code")
    if isinstance(status_code, int) and status_code in RETRYABLE_STATUS_CODES:
        return True
    return bool(result.get("error")) and status_code is None


def _attempt_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": bool(result.get("ok")),
        "status_code": result.get("status_code"),
        "elapsed_s": result.get("elapsed_s"),
        "error": result.get("error"),
        "summary": result.get("summary") or result.get("response_summary") or {},
    }


def _case_mode(case: dict[str, str]) -> str:
    return "verbose" if _as_bool(case.get("verbose_instruction")) else "simple"


def _case_brief(case: dict[str, str]) -> str:
    return (
        f"model={case.get('modelspec', '?')} "
        f"mode={_case_mode(case)} "
        f"vocals={case.get('include_vocals', '?')} "
        f"gender={case.get('vocal_gender', '-')} "
        f"preserve={case.get('preserve_original_audio', '-')} "
        f"volume={case.get('music_volume', '-')} "
        f"compress={case.get('compression_flag', '-')}"
    )


def _media_flags(summary: dict[str, Any]) -> str:
    flags = []
    for label, field in (
        ("upload", "has_upload_url"),
        ("video", "has_video_url"),
        ("audio", "has_audio_url"),
        ("complete", "has_complete_audio_url"),
        ("secondary", "has_secondary_complete_audio_url"),
        ("thumb", "has_thumbnail_url"),
    ):
        if summary.get(field):
            flags.append(label)
    return ",".join(flags) if flags else "-"


def _safe_key_label(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    lowered = text.lower()
    if any(token in lowered for token in ("sk-", "bearer ", "api_key", "apikey")):
        return "[redacted-secret]"
    if len(text) > 24:
        return f"{text[:8]}...{text[-4:]}"
    return text


def _provider_b_key_used(body_or_summary: Any) -> str:
    if not isinstance(body_or_summary, dict):
        return "not_exposed"

    candidate_paths: tuple[tuple[str, ...], ...] = (
        ("provider_b_key_used",),
        ("provider_b_key_label",),
        ("provider_b_key_fingerprint",),
        ("provider_key_used",),
        ("provider_key_label",),
        ("provider_key_fingerprint",),
        ("music_generation", "provider_b_key_used"),
        ("music_generation", "provider_b_key_label"),
        ("music_generation", "provider_b_key_fingerprint"),
        ("metadata", "provider_b_key_used"),
        ("metadata", "provider_b_key_label"),
        ("metadata", "provider_b_key_fingerprint"),
    )
    for path in candidate_paths:
        value: Any = body_or_summary
        for part in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(part)
        label = _safe_key_label(value)
        if label:
            return label
    return "not_exposed"


def _attempt_line(
    *,
    run_index: int,
    total: int,
    case_id: str,
    attempt_idx: int,
    max_attempts: int,
    result: dict[str, Any],
) -> str:
    summary = result.get("summary") or result.get("response_summary") or {}
    if not isinstance(summary, dict):
        summary = {}

    status_code = result.get("status_code", "ERROR")
    elapsed = result.get("elapsed_s", "?")
    api_status = summary.get("status") or "-"
    model = summary.get("modelspec") or "-"
    job_id = summary.get("job_id") or "-"
    version = summary.get("version") or "-"
    storage_host = summary.get("storage_host") or "-"
    token_usage = summary.get("token_usage")
    tokens = f" tokens={token_usage}" if token_usage is not None else ""
    error = result.get("error")
    error_suffix = f" error={_clip(error)}" if error else ""
    body = result.get("response")
    provider_b_key_used = _provider_b_key_used(body)

    return (
        f"[run {run_index:02d}/{total}] ATTEMPT {attempt_idx}/{max_attempts} "
        f"{case_id} HTTP {status_code} {elapsed}s "
        f"api_status={api_status} model={model} job={job_id} "
        f"media={_media_flags(summary)} storage={storage_host} "
        f"provider_b_key={provider_b_key_used} version={version}{tokens}{error_suffix}"
    )


def run_case_with_retries(
    *,
    run_index: int,
    total: int,
    endpoint: str,
    video_url: str,
    form: dict[str, str],
    timeout_s: float,
    retry_count: int,
    retry_base_delay_s: float,
    retry_max_delay_s: float,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    max_attempts = max(1, retry_count + 1)
    result: dict[str, Any] = {}
    case_id = form["case_id"]

    print(
        f"[run {run_index:02d}/{total}] START {case_id} {_case_brief(form)}",
        flush=True,
    )

    for attempt_idx in range(1, max_attempts + 1):
        print(
            f"[run {run_index:02d}/{total}] SEND attempt {attempt_idx}/{max_attempts} "
            f"{case_id}",
            flush=True,
        )
        result = _run_case(
            endpoint=endpoint,
            video_url=video_url,
            form=form,
            timeout_s=timeout_s,
        )
        result["attempt_number"] = attempt_idx
        attempts.append(_attempt_summary(result))
        print(
            _attempt_line(
                run_index=run_index,
                total=total,
                case_id=case_id,
                attempt_idx=attempt_idx,
                max_attempts=max_attempts,
                result=result,
            ),
            flush=True,
        )

        if result.get("ok") or not _is_retryable(result) or attempt_idx >= max_attempts:
            break

        delay_s = min(retry_max_delay_s, retry_base_delay_s * (2 ** (attempt_idx - 1)))
        print(
            f"[run {run_index:02d}/{total}] RETRY {case_id} attempt {attempt_idx}/{max_attempts} "
            f"HTTP {result.get('status_code')} - sleeping {delay_s:.1f}s",
            flush=True,
        )
        time.sleep(delay_s)

    result["attempt_count"] = len(attempts)
    result["provider_b_key_used"] = _provider_b_key_used(result.get("response"))
    if len(attempts) > 1:
        result["attempts"] = attempts
    return result


def _select_cases(args: argparse.Namespace) -> list[dict[str, str]]:
    selected = [
        case for case in FULL_USE_CASES if case["modelspec"] == "edenn_enhanced"
    ]

    if not args.full:
        default_ids = set(DEFAULT_EXAMPLE_CASE_IDS)
        selected = [case for case in selected if case["case_id"] in default_ids]
    if args.mode == "simple":
        selected = [
            case for case in selected if not _as_bool(case.get("verbose_instruction"))
        ]
    elif args.mode == "verbose":
        selected = [
            case for case in selected if _as_bool(case.get("verbose_instruction"))
        ]
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
    expected_storage_host: str | None,
    retry_count: int,
) -> dict[str, Any]:
    return {
        "url": url,
        "video_url": video_url,
        "case_count": len(selected_cases),
        "concurrency": concurrency,
        "expected_storage_host": expected_storage_host,
        "retry_count": retry_count,
        "input_schema": INPUT_SCHEMA,
        "cases": selected_cases,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one edenn_enhanced video URL use-case request against the "
            "newapp endpoint. Pass --full for every enhanced sample."
        )
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--video-url", default=DEFAULT_VIDEO_URL)
    parser.add_argument("--timeout-s", type=float, default=1200.0)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
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
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run every enhanced sample instead of the reduced default set.",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        metavar="N",
        help="Repeat the selected case suite N times (default: 1).",
    )
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--retry-count", type=int, default=0)
    parser.add_argument("--retry-base-delay-s", type=float, default=20.0)
    parser.add_argument("--retry-max-delay-s", type=float, default=120.0)
    parser.add_argument(
        "--expected-storage-host",
        default=DEFAULT_EXPECTED_STORAGE_HOST,
        help="Expected host for returned blob URLs. Pass '' to disable.",
    )
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

    runs = max(1, args.runs)
    if runs > 1:
        selected_cases = selected_cases * runs

    expected_storage_host = args.expected_storage_host.strip() or None
    concurrency = max(1, min(args.concurrency, len(selected_cases)))
    schema = _build_schema(
        url=args.url,
        video_url=args.video_url,
        selected_cases=selected_cases,
        concurrency=concurrency,
        expected_storage_host=expected_storage_host,
        retry_count=max(0, args.retry_count),
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
        "expected_storage_host": expected_storage_host,
        "retry_count": max(0, args.retry_count),
        "input_schema": INPUT_SCHEMA,
        "cases": selected_cases,
        "success": [],
        "failure": [],
    }

    suite_start = time.monotonic()
    print(
        f"Starting {len(selected_cases)} cases with concurrency={concurrency}, "
        f"retry_count={max(0, args.retry_count)}",
        flush=True,
    )

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        total = len(selected_cases)
        futures = {
            executor.submit(
                run_case_with_retries,
                run_index=index,
                total=total,
                endpoint=args.url,
                video_url=args.video_url,
                form=form,
                timeout_s=args.timeout_s,
                retry_count=max(0, args.retry_count),
                retry_base_delay_s=max(0.0, args.retry_base_delay_s),
                retry_max_delay_s=max(0.0, args.retry_max_delay_s),
            ): (index, form)
            for index, form in enumerate(selected_cases, start=1)
        }

        completed_count = 0
        success_count = 0
        failure_count = 0
        http_success_count = 0
        contract_success_count = 0

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
                        key: value for key, value in form.items() if key != "case_id"
                    }
                    | {"video_url": args.video_url},
                    "summary": {},
                    "output_schema": {},
                    "response": {},
                    "attempt_count": 0,
                    "provider_b_key_used": "not_exposed",
                }

            result["run_index"] = index
            result["http_ok"] = bool(result.get("ok"))
            result["contract_errors"] = _validate_result(
                form,
                result,
                expected_storage_host=expected_storage_host,
            )
            result["contract_ok"] = not result["contract_errors"]
            result["ok"] = result["http_ok"] and result["contract_ok"]

            bucket = "success" if result["ok"] else "failure"
            record[bucket].append(result)
            completed_count += 1
            success_count += 1 if result["ok"] else 0
            failure_count += 0 if result["ok"] else 1
            http_success_count += 1 if result.get("http_ok") else 0
            contract_success_count += 1 if result.get("contract_ok") else 0

            status = result.get("status_code", "ERROR")
            elapsed = result.get("elapsed_s", "?")
            summary = result.get("summary") or {}
            if not isinstance(summary, dict):
                summary = {}
            model = summary.get("modelspec", "?")
            job_id = summary.get("job_id", "-")
            provider_b_key_used = result.get("provider_b_key_used") or _provider_b_key_used(
                result.get("response")
            )
            attempts = result.get("attempt_count", 1)
            errors = "; ".join(result["contract_errors"][:2])
            suffix = f" errors={errors}" if errors else ""
            print(
                f"[progress {completed_count:02d}/{total}] {bucket.upper():7} "
                f"run={index:02d} {case_id} HTTP {status} {elapsed}s "
                f"attempts={attempts} model={model} job={job_id} "
                f"media={_media_flags(summary)} "
                f"provider_b_key={provider_b_key_used} "
                f"success={success_count} failure={failure_count} "
                f"http_ok={http_success_count} contract_ok={contract_success_count} "
                f"suite_elapsed={time.monotonic() - suite_start:.1f}s{suffix}",
                flush=True,
            )

    record["success"].sort(key=lambda item: item["run_index"])
    record["failure"].sort(key=lambda item: item["run_index"])
    record["finished_at"] = datetime.now(timezone.utc).isoformat()

    merged_output_schema: dict[str, set[str]] = {}
    for item in record["success"]:
        for field, typ in item.get("output_schema", {}).items():
            merged_output_schema.setdefault(field, set()).add(typ)
    record["output_schema"] = {
        field: sorted(types) for field, types in merged_output_schema.items()
    }

    elapsed_s = round(
        (
            datetime.fromisoformat(record["finished_at"])
            - datetime.fromisoformat(record["started_at"])
        ).total_seconds(),
        1,
    )
    all_results = record["success"] + record["failure"]
    record["summary"] = {
        "total": len(selected_cases),
        "success_count": len(record["success"]),
        "failure_count": len(record["failure"]),
        "http_success_count": sum(1 for item in all_results if item.get("http_ok")),
        "contract_success_count": sum(
            1 for item in all_results if item.get("contract_ok")
        ),
        "retried_case_count": sum(
            1 for item in all_results if int(item.get("attempt_count") or 0) > 1
        ),
        "elapsed_s": elapsed_s,
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    mode_label = args.mode if args.mode != "all" else "allmodes"
    runs_label = f"_r{runs}" if runs > 1 else ""
    output_path = args.output_dir / (
        "newapp_video_url_enhanced_use_case_"
        f"{mode_label}_{len(selected_cases)}x{runs_label}_c{concurrency}_{stamp}.json"
    )
    output_path.write_text(
        json.dumps(record, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(json.dumps(record["summary"], indent=2), flush=True)
    print(f"Wrote {output_path}", flush=True)


if __name__ == "__main__":
    main()
