"""Live matrix for the presence-keyed lyrics_prompt contract (v2, 2026-08-12).

Exercises the deployed v2 API end to end after the verbose-collapse change:

Video-music (POST /api/v2/jobs/video-music, multipart upload):
  vm_lyrics_enhanced   user_prompt + lyrics_prompt on edenn_enhanced -> vocal track
  vm_lyrics_studio     same on edenn_studio (content-filter canary for the
                       reworded dual template)
  vm_lyrics_only_enh   lyrics_prompt with NO user_prompt (empty style slot)
  vm_plain_basic       user_prompt-only regression on each tier
  vm_plain_enhanced
  vm_plain_studio
  vm_lyrics_basic_400  lyrics_prompt + edenn_basic -> 400 error 10009 (free)
  vm_lyrics_nospec_400 lyrics_prompt + omitted modelspec (silent basic
                       default) -> 400 error 10009 (free)
  vm_legacy_verbose    old client shape: verbose_instruction=true +
                       music_style_prompt + lyrics_prompt -> unchanged behavior

Multi-image (POST /api/v2/jobs/multi-image-music, 3 images):
  mi_lyrics_enhanced   user_prompt + user_lyrics_prompt (sanitizer in path)
  mi_lyrics_basic      same on edenn_basic (multi-image DOES support lyric
                       direction on basic -- the documented asymmetry)

Each paid case polls to completion and records the terminal job document.
The lyric-direction cases ask for the word "sunset" so delivery can be
verified in the transcript. Results are written as JSON for the report.

Usage:
  python EdennCode/Scripts/run_lyrics_contract_live_matrix.py \
      --base-url https://<fqdn> --api-key sk-... \
      [--out EdennCode/Scripts/matrix_results/lyrics_contract_<ts>.json] [--only case,...]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from EdennCode.TestSuites.helpers.paths import (  # noqa: E402
    MULTI_IMAGE_DESIGN_IMAGES_DIR,
    SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH,
)

POLL_INTERVAL_S = 15
POLL_TIMEOUT_S = 30 * 60
LYRIC_TOKEN = "sunset"

VM_EP = "/api/v2/jobs/video-music"
MI_EP = "/api/v2/jobs/multi-image-music"

USER_PROMPT = "Warm acoustic folk with female vocals in English, gentle and hopeful."
LYRICS_DIRECTION = (
    "Sing about coming home at dusk; mention the word sunset in the chorus."
)
MI_USER_PROMPT = "Upbeat English city pop with female vocals."

VM_CASES = {
    "vm_lyrics_enhanced": {
        "kind": "vm", "expect": "completed",
        "data": {"modelspec": "edenn_enhanced", "user_prompt": USER_PROMPT,
                 "lyrics_prompt": LYRICS_DIRECTION},
    },
    "vm_lyrics_studio": {
        "kind": "vm", "expect": "completed",
        "data": {"modelspec": "edenn_studio", "user_prompt": USER_PROMPT,
                 "lyrics_prompt": LYRICS_DIRECTION},
    },
    "vm_lyrics_only_enh": {
        "kind": "vm", "expect": "completed",
        "data": {"modelspec": "edenn_enhanced", "lyrics_prompt": LYRICS_DIRECTION},
    },
    "vm_plain_basic": {
        "kind": "vm", "expect": "completed",
        "data": {"modelspec": "edenn_basic",
                 "user_prompt": "Calm instrumental piano, no vocals."},
    },
    "vm_plain_enhanced": {
        "kind": "vm", "expect": "completed",
        "data": {"modelspec": "edenn_enhanced", "user_prompt": USER_PROMPT},
    },
    "vm_plain_studio": {
        "kind": "vm", "expect": "completed",
        "data": {"modelspec": "edenn_studio", "user_prompt": USER_PROMPT},
    },
    "vm_lyrics_basic_400": {
        "kind": "vm", "expect": "rejected_10009",
        "data": {"modelspec": "edenn_basic", "user_prompt": USER_PROMPT,
                 "lyrics_prompt": LYRICS_DIRECTION},
    },
    "vm_lyrics_nospec_400": {
        "kind": "vm", "expect": "rejected_10009",
        "data": {"user_prompt": USER_PROMPT, "lyrics_prompt": LYRICS_DIRECTION},
    },
    "vm_legacy_verbose": {
        "kind": "vm", "expect": "completed",
        "data": {"modelspec": "edenn_enhanced", "verbose_instruction": "true",
                 "music_style_prompt": USER_PROMPT,
                 "lyrics_prompt": LYRICS_DIRECTION},
    },
    "mi_lyrics_enhanced": {
        "kind": "mi", "expect": "completed",
        "data": {"modelspec": "edenn_enhanced", "user_prompt": MI_USER_PROMPT,
                 "lyrics_prompt": LYRICS_DIRECTION,
                 "per_image_duration": "5"},
    },
    "mi_lyrics_basic": {
        # Exercises the deprecated alias so old-client acceptance stays covered.
        "kind": "mi", "expect": "completed",
        "data": {"modelspec": "edenn_basic", "user_prompt": MI_USER_PROMPT,
                 "user_lyrics_prompt": LYRICS_DIRECTION,
                 "per_image_duration": "5"},
    },
}


def _submit(session: requests.Session, base_url: str, name: str, case: dict) -> dict:
    if case["kind"] == "vm":
        video = SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH
        with video.open("rb") as fh:
            resp = session.post(
                base_url + VM_EP, data=case["data"],
                files={"video": (video.name, fh, "video/mp4")}, timeout=300)
    else:
        images = sorted(
            p for p in MULTI_IMAGE_DESIGN_IMAGES_DIR.iterdir() if p.is_file())[:3]
        files = [("images", (p.name, p.read_bytes(), "image/jpeg")) for p in images]
        resp = session.post(
            base_url + MI_EP, data=case["data"], files=files, timeout=300)

    record: dict = {
        "case": name, "expect": case["expect"], "request": case["data"],
        "submit_status": resp.status_code, "submitted_at": time.time(),
    }
    try:
        record["submit_body"] = resp.json()
    except ValueError:
        record["submit_body"] = resp.text[:2000]
    return record


def _poll(session: requests.Session, base_url: str, record: dict) -> dict:
    job_id = record.get("job_id") or record["submit_body"].get("job_id")
    record["job_id"] = job_id
    deadline = time.time() + POLL_TIMEOUT_S
    doc: dict = {}
    while time.time() < deadline:
        # Transient network failures (connection resets, gateway blips) must
        # never kill a paid matrix run — retry on the next tick.
        try:
            resp = session.get(f"{base_url}/api/v2/jobs/{job_id}", timeout=60)
            doc = resp.json()
        except (requests.RequestException, ValueError) as exc:
            doc = {"_poll_error": str(exc)[:300]}
            time.sleep(POLL_INTERVAL_S)
            continue
        status = doc.get("status")
        if status in {"completed", "failed", "cancelled", "dead_lettered"}:
            break
        time.sleep(POLL_INTERVAL_S)
    record["final_status"] = doc.get("status")
    record["job_document"] = doc
    blob = json.dumps(doc, ensure_ascii=False).lower()
    record["lyric_token_delivered"] = LYRIC_TOKEN in blob
    record["finished_at"] = time.time()
    return record


def _evaluate(record: dict) -> dict:
    expect = record["expect"]
    if expect == "rejected_10009":
        body = record.get("submit_body") or {}
        detail = body.get("detail") if isinstance(body, dict) else {}
        ok = (
            record["submit_status"] == 400
            and isinstance(detail, dict)
            and detail.get("error_code") == 10009
        )
        record["passed"] = ok
    else:
        record["passed"] = (
            record["submit_status"] == 200
            and record.get("final_status") == "completed"
        )
    return record


def _save(records: list, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(records, indent=2, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--only", default=None,
                        help="Comma-separated case names to run.")
    parser.add_argument("--resume", default=None,
                        help="Path of a previous results JSON: skip submits, "
                             "keep polling its non-terminal jobs.")
    args = parser.parse_args()

    session = requests.Session()
    if args.api_key:
        session.headers["Authorization"] = f"Bearer {args.api_key}"
    base_url = args.base_url.rstrip("/")

    if args.resume:
        out_path = Path(args.resume)
        records = json.loads(out_path.read_text())
    else:
        out_path = Path(args.out) if args.out else (
            REPO_ROOT / "EdennCode" / "Scripts" / "matrix_results" /
            f"lyrics_contract_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json")
        selected = {
            name: case for name, case in VM_CASES.items()
            if not args.only or name in set(args.only.split(","))
        }
        records = []
        for name, case in selected.items():
            record = _submit(session, base_url, name, case)
            record["job_id"] = (record.get("submit_body") or {}).get("job_id") \
                if isinstance(record.get("submit_body"), dict) else None
            records.append(record)
            # Persist after every submit so a crashed run never loses job ids.
            _save(records, out_path)
            print(f"[submit] {name} -> {record['submit_status']} "
                  f"job={record.get('job_id')}", flush=True)

    for record in records:
        if record.get("final_status") in {"completed", "failed", "cancelled",
                                          "dead_lettered"}:
            continue
        if record["expect"] != "rejected_10009" and record["submit_status"] == 200:
            print(f"[poll] {record['case']} job={record.get('job_id')}", flush=True)
            _poll(session, base_url, record)
        _evaluate(record)
        _save(records, out_path)
        print(f"[done] {record['case']}: passed={record.get('passed')} "
              f"status={record.get('final_status', record['submit_status'])}",
              flush=True)

    for record in records:
        _evaluate(record)
    _save(records, out_path)
    print(f"[saved] {out_path}")

    failures = [r["case"] for r in records if not r.get("passed")]
    print(f"[summary] {len(records) - len(failures)}/{len(records)} passed; "
          f"failures: {failures or 'none'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
