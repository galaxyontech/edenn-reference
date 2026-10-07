from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


DEFAULT_URL = (
    "https://staging-app.worker.example.invalid"
    "/api/v1/jobs/video"
)
DEFAULT_VIDEO_URL = (
    "https://video.xingbao.chat/works/1773368504897428956/"
    "62a807d3bc7a4d32bd33cc0ad8de1126.mp4"
)
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "EdennCode" / "Scripts" / "matrix_results"

# ---------------------------------------------------------------------------
# Input schema definition (used both as documentation and for schema capture)
# ---------------------------------------------------------------------------
INPUT_SCHEMA = {
    "description": "Form fields accepted by POST /api/v1/jobs/video",
    "fields": {
        "video_url":              {"type": "string",  "required": True,  "note": "Source video URL"},
        "modelspec":              {"type": "string",  "required": True,  "note": "edenn_enhanced | edenn_studio"},
        "include_vocals":         {"type": "boolean", "required": False, "note": "Generate with vocals (true/false)"},
        "vocal_gender":           {"type": "string",  "required": False, "note": "male | female"},
        "user_prompt":            {"type": "string",  "required": False, "note": "Simple free-text music prompt (simple mode)"},
        "verbose_instruction":    {"type": "boolean", "required": False, "note": "Enable verbose instruction mode"},
        "music_style_prompt":     {"type": "string",  "required": False, "note": "Detailed style description (verbose mode)"},
        "lyrics_prompt":          {"type": "string",  "required": False, "note": "Lyrics generation instruction (verbose mode)"},
        "preserve_original_audio":{"type": "boolean", "required": False, "note": "Mix original audio into output"},
        "music_volume":           {"type": "float",   "required": False, "note": "Music volume 0.0–1.0"},
        "compression_flag":       {"type": "boolean", "required": False, "note": "Apply video compression"},
    },
}

# ---------------------------------------------------------------------------
# Test cases — all with include_vocals=true (no instrumental)
# Dimensions covered: modelspec × instruction_mode × language × vocal_gender
#                     × preserve_original_audio × music_volume
# ---------------------------------------------------------------------------
CASES: list[dict[str, str]] = [
    # ── edenn_enhanced · simple mode ────────────────────────────────────────
    {
        "case_id": "enhanced_simple_en_female",
        "modelspec": "edenn_enhanced",
        "include_vocals": "true",
        "vocal_gender": "female",
        "user_prompt": "Bright upbeat English female vocal pop for a lifestyle commercial, warm synths, catchy hook.",
        "preserve_original_audio": "true",
        "music_volume": "0.7",
        "compression_flag": "true",
    },
    {
        "case_id": "enhanced_simple_en_male",
        "modelspec": "edenn_enhanced",
        "include_vocals": "true",
        "vocal_gender": "male",
        "user_prompt": "Confident English male vocal indie-pop, modern production, driving rhythm, premium brand feel.",
        "preserve_original_audio": "true",
        "music_volume": "0.7",
        "compression_flag": "true",
    },
    {
        "case_id": "enhanced_simple_cn_female",
        "modelspec": "edenn_enhanced",
        "include_vocals": "true",
        "vocal_gender": "female",
        "user_prompt": "明亮温暖的中文女声流行广告歌曲，适合生活方式短视频，节奏稳定，情绪积极。",
        "preserve_original_audio": "true",
        "music_volume": "0.7",
        "compression_flag": "true",
    },
    {
        "case_id": "enhanced_simple_cn_male",
        "modelspec": "edenn_enhanced",
        "include_vocals": "true",
        "vocal_gender": "male",
        "user_prompt": "充满活力的中文男声流行歌曲，节奏感强，旋律朗朗上口，适合品牌广告。",
        "preserve_original_audio": "true",
        "music_volume": "0.7",
        "compression_flag": "true",
    },
    {
        "case_id": "enhanced_simple_jp_female",
        "modelspec": "edenn_enhanced",
        "include_vocals": "true",
        "vocal_gender": "female",
        "user_prompt": "明るく爽やかな日本語女性ボーカルポップ、ライフスタイル広告向け、感情豊かでキャッチーなサビ。",
        "preserve_original_audio": "true",
        "music_volume": "0.7",
        "compression_flag": "true",
    },
    # ── edenn_enhanced · verbose mode ───────────────────────────────────────
    {
        "case_id": "enhanced_verbose_en_female",
        "modelspec": "edenn_enhanced",
        "include_vocals": "true",
        "vocal_gender": "female",
        "verbose_instruction": "true",
        "music_style_prompt": "English female vocal pop, polished and bright, clean drums, warm synth pads, premium lifestyle advertising sound.",
        "lyrics_prompt": "Write concise English lyrics about everyday confidence, trust, and forward motion with a memorable chorus.",
        "preserve_original_audio": "true",
        "music_volume": "0.7",
        "compression_flag": "true",
    },
    {
        "case_id": "enhanced_verbose_en_male",
        "modelspec": "edenn_enhanced",
        "include_vocals": "true",
        "vocal_gender": "male",
        "verbose_instruction": "true",
        "music_style_prompt": "Mandarin male vocal pop for a short-form commercial, warm synths, confident rhythm, memorable hook, bright premium feel.",
        "lyrics_prompt": "Write concise Mandarin lyrics about momentum, trust, and everyday confidence. Keep the chorus easy to remember.",
        "preserve_original_audio": "true",
        "music_volume": "0.7",
        "compression_flag": "true",
    },
    {
        "case_id": "enhanced_verbose_cn_female",
        "modelspec": "edenn_enhanced",
        "include_vocals": "true",
        "vocal_gender": "female",
        "verbose_instruction": "true",
        "music_style_prompt": "中文女声流行，明亮温柔，合成器铺底，副歌朗朗上口，适合品牌广告。",
        "lyrics_prompt": "写一段简洁的中文歌词，主题是自信、向前、美好生活，副歌容易记忆。",
        "preserve_original_audio": "true",
        "music_volume": "0.7",
        "compression_flag": "true",
    },
    {
        "case_id": "enhanced_verbose_jp_male",
        "modelspec": "edenn_enhanced",
        "include_vocals": "true",
        "vocal_gender": "male",
        "verbose_instruction": "true",
        "music_style_prompt": "Japanese male pop, energetic and modern, punchy drums, electric guitar accents, commercial advertising sound.",
        "lyrics_prompt": "Generate Japanese lyrics for a male singer with themes of ambition, progress, and daily confidence. Memorable chorus.",
        "preserve_original_audio": "true",
        "music_volume": "0.7",
        "compression_flag": "true",
    },
    # ── edenn_enhanced · volume / preserve_audio variants ───────────────────
    {
        "case_id": "enhanced_low_volume_no_preserve",
        "modelspec": "edenn_enhanced",
        "include_vocals": "true",
        "vocal_gender": "female",
        "user_prompt": "Soft dreamy English female vocal for a calm lifestyle brand, gentle melody, warm atmosphere.",
        "preserve_original_audio": "false",
        "music_volume": "0.5",
        "compression_flag": "true",
    },
    {
        "case_id": "enhanced_full_volume_no_compress",
        "modelspec": "edenn_enhanced",
        "include_vocals": "true",
        "vocal_gender": "male",
        "user_prompt": "High-energy English male vocal electronic track for a sports brand ad, powerful beat, bold hook.",
        "preserve_original_audio": "true",
        "music_volume": "1.0",
        "compression_flag": "false",
    },
    # ── edenn_studio · simple mode ───────────────────────────────────────────
    {
        "case_id": "studio_simple_en_female",
        "modelspec": "edenn_studio",
        "include_vocals": "true",
        "vocal_gender": "female",
        "user_prompt": "Bright English vocal pop for a lifestyle commercial, optimistic hook, modern clean production.",
        "preserve_original_audio": "true",
        "music_volume": "0.7",
        "compression_flag": "true",
    },
    {
        "case_id": "studio_simple_en_male",
        "modelspec": "edenn_studio",
        "include_vocals": "true",
        "vocal_gender": "male",
        "user_prompt": "Smooth English male R&B vocal, sophisticated feel, premium brand, slow groove, aspirational mood.",
        "preserve_original_audio": "true",
        "music_volume": "0.7",
        "compression_flag": "true",
    },
    {
        "case_id": "studio_simple_cn_female",
        "modelspec": "edenn_studio",
        "include_vocals": "true",
        "vocal_gender": "female",
        "user_prompt": "清新甜美的中文女声流行歌曲，适合美妆品牌广告，旋律轻快，情感真挚。",
        "preserve_original_audio": "true",
        "music_volume": "0.7",
        "compression_flag": "true",
    },
    {
        "case_id": "studio_simple_cn_male",
        "modelspec": "edenn_studio",
        "include_vocals": "true",
        "vocal_gender": "male",
        "user_prompt": "深情大气的中文男声流行，适合高端品牌广告，编曲层次丰富，情绪递进自然。",
        "preserve_original_audio": "true",
        "music_volume": "0.7",
        "compression_flag": "true",
    },
    {
        "case_id": "studio_simple_jp_female",
        "modelspec": "edenn_studio",
        "include_vocals": "true",
        "vocal_gender": "female",
        "user_prompt": "明るく爽やかな日本語女性ボーカルポップ、ライフスタイル広告向け、感情豊かでキャッチーなサビ。",
        "preserve_original_audio": "true",
        "music_volume": "0.7",
        "compression_flag": "true",
    },
    # ── edenn_studio · verbose mode ──────────────────────────────────────────
    {
        "case_id": "studio_verbose_en_female",
        "modelspec": "edenn_studio",
        "include_vocals": "true",
        "vocal_gender": "female",
        "verbose_instruction": "true",
        "music_style_prompt": "English female indie-pop, warm acoustic guitar, subtle strings, bright and hopeful, premium commercial feel.",
        "lyrics_prompt": "Write English lyrics about new beginnings, everyday joy, and moving forward. Short and memorable chorus.",
        "preserve_original_audio": "true",
        "music_volume": "0.7",
        "compression_flag": "true",
    },
    {
        "case_id": "studio_verbose_cn_male",
        "modelspec": "edenn_studio",
        "include_vocals": "true",
        "vocal_gender": "male",
        "verbose_instruction": "true",
        "music_style_prompt": "Chinese male pop music, warm, catchy, medium-fast tempo, clear melody, suitable for short-form advertising.",
        "lyrics_prompt": "Generate Mandarin lyrics for a male singer. The theme is positive confidence, with an easy-to-remember chorus.",
        "preserve_original_audio": "true",
        "music_volume": "0.7",
        "compression_flag": "true",
    },
    {
        "case_id": "studio_verbose_jp_female",
        "modelspec": "edenn_studio",
        "include_vocals": "true",
        "vocal_gender": "female",
        "verbose_instruction": "true",
        "music_style_prompt": "Japanese female pop, bright and polished, emotional but upbeat, modern commercial sound.",
        "lyrics_prompt": "Generate Japanese lyrics with an optimistic lifestyle theme and a memorable hook.",
        "preserve_original_audio": "true",
        "music_volume": "0.7",
        "compression_flag": "true",
    },
    {
        "case_id": "studio_verbose_en_male",
        "modelspec": "edenn_studio",
        "include_vocals": "true",
        "vocal_gender": "male",
        "verbose_instruction": "true",
        "music_style_prompt": "English male soul-pop, rich warm voice, lush orchestral production, aspirational advertising tone.",
        "lyrics_prompt": "Write English lyrics about ambition, self-belief, and reaching your best self. Memorable and uplifting chorus.",
        "preserve_original_audio": "true",
        "music_volume": "0.7",
        "compression_flag": "true",
    },
    # ── edenn_studio · volume / preserve_audio variants ─────────────────────
    {
        "case_id": "studio_low_volume_no_preserve",
        "modelspec": "edenn_studio",
        "include_vocals": "true",
        "vocal_gender": "female",
        "user_prompt": "Gentle English female vocal acoustic ballad, intimate and warm, soft commercial lifestyle feel.",
        "preserve_original_audio": "false",
        "music_volume": "0.5",
        "compression_flag": "true",
    },
    {
        "case_id": "studio_full_volume_no_compress",
        "modelspec": "edenn_studio",
        "include_vocals": "true",
        "vocal_gender": "male",
        "user_prompt": "Powerful English male vocal anthem, full orchestral build, emotional peak, premium cinematic brand feel.",
        "preserve_original_audio": "true",
        "music_volume": "1.0",
        "compression_flag": "false",
    },
]


def _build_output_schema(body: dict[str, Any]) -> dict[str, Any]:
    return {
        field: type(value).__name__ if value is not None else "null"
        for field, value in body.items()
    }


def _json_body(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text[:4000]


def run_case(
    *,
    endpoint: str,
    video_url: str,
    form: dict[str, str],
    timeout_s: float,
) -> dict[str, Any]:
    case_id = form["case_id"]
    payload = {k: v for k, v in form.items() if k != "case_id"}
    payload["video_url"] = video_url
    started = time.monotonic()
    try:
        response = requests.post(endpoint, data=payload, timeout=timeout_s)
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
            "job_id":                          body.get("job_id"),
            "status":                          body.get("status"),
            "version":                         body.get("version"),
            "modelspec":                       body.get("modelspec"),
            "include_vocals":                  body.get("include_vocals"),
            "has_upload_url":                  bool(body.get("upload_url")),
            "has_audio_url":                   bool(body.get("audio_url")),
            "has_complete_audio_url":          bool(body.get("complete_audio_url")),
            "has_secondary_complete_audio_url":bool(body.get("secondary_complete_audio_url")),
            "has_video_url":                   bool(body.get("video_url")),
            "has_thumbnail_url":               bool(body.get("thumbnail_url")),
            "storage_host":                    _extract_host(body.get("video_url")),
            "token_usage":                     body.get("token_usage"),
            "job_received_timestamp":          body.get("job_received_timestamp"),
            "job_finished_timestamp":          body.get("job_finished_timestamp"),
        }

    result: dict[str, Any] = {
        "case_id":       case_id,
        "ok":            ok,
        "status_code":   status_code,
        "elapsed_s":     elapsed_s,
        "input":         payload,
        "summary":       summary,
        "output_schema": output_schema,
        "response":      body,
    }
    if error:
        result["error"] = error
    return result


def _extract_host(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc or None
    except Exception:
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run full enhanced+studio matrix against the newapp endpoint."
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--video-url", default=DEFAULT_VIDEO_URL)
    parser.add_argument("--timeout-s", type=float, default=1200.0)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--concurrency", type=int, default=len(CASES),
                        help="Number of parallel requests (default: all at once)")
    parser.add_argument("--filter", default=None,
                        help="Only run cases whose case_id contains this substring")
    parser.add_argument("--schema-only", action="store_true",
                        help="Print input schema and exit without sending requests")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    selected = CASES
    if args.filter:
        selected = [c for c in CASES if args.filter in c["case_id"]]
    concurrency = max(1, min(args.concurrency, len(selected) or 1))

    print(f"Cases: {len(selected)}  |  Concurrency: {concurrency}", flush=True)
    print(json.dumps(INPUT_SCHEMA, indent=2, ensure_ascii=False), flush=True)

    if args.schema_only:
        return

    record: dict[str, Any] = {
        "url":          args.url,
        "video_url":    args.video_url,
        "started_at":   datetime.now(timezone.utc).isoformat(),
        "case_count":   len(selected),
        "concurrency":  concurrency,
        "input_schema": INPUT_SCHEMA,
        "cases":        selected,
        "success":      [],
        "failure":      [],
    }

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(
                run_case,
                endpoint=args.url,
                video_url=args.video_url,
                form=form,
                timeout_s=args.timeout_s,
            ): (i, form)
            for i, form in enumerate(selected, start=1)
        }
        total = len(futures)
        for future in as_completed(futures):
            i, form = futures[future]
            case_id = form["case_id"]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "case_id":    case_id,
                    "ok":         False,
                    "error":      f"{type(exc).__name__}: {exc}",
                    "input":      {k: v for k, v in form.items() if k != "case_id"} | {"video_url": args.video_url},
                    "summary":    {},
                    "output_schema": {},
                    "response":   {},
                }
            result["run_index"] = i
            bucket = "success" if result.get("ok") else "failure"
            record[bucket].append(result)
            status  = result.get("status_code", "ERROR")
            elapsed = result.get("elapsed_s", "?")
            host    = result.get("summary", {}).get("storage_host", "?")
            print(
                f"[{i}/{total}] {bucket.upper():7} {case_id}  HTTP {status}  {elapsed}s  storage={host}",
                flush=True,
            )

    record["success"].sort(key=lambda r: r["run_index"])
    record["failure"].sort(key=lambda r: r["run_index"])
    record["finished_at"] = datetime.now(timezone.utc).isoformat()

    # Derive a merged output schema from all successful responses
    merged_output_schema: dict[str, set[str]] = {}
    for r in record["success"]:
        for field, typ in r.get("output_schema", {}).items():
            merged_output_schema.setdefault(field, set()).add(typ)
    record["output_schema"] = {k: sorted(v) for k, v in merged_output_schema.items()}

    record["summary"] = {
        "total":         len(selected),
        "success_count": len(record["success"]),
        "failure_count": len(record["failure"]),
        "elapsed_s":     round(
            (datetime.fromisoformat(record["finished_at"]) -
             datetime.fromisoformat(record["started_at"])).total_seconds(), 1
        ),
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = args.output_dir / f"enhanced_studio_matrix_{len(selected)}x_{stamp}.json"
    output_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(record["summary"], indent=2), flush=True)
    print(f"\nSaved → {output_path}", flush=True)


if __name__ == "__main__":
    main()
