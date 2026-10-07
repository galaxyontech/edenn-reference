from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Dict

from .provider_c_callback import handle_callback_event, parse_callback_payload

BASE_DIR = Path(__file__).resolve().parent
QUEUE_DIR = BASE_DIR / "webhook_queue"
RESULTS_DIR = BASE_DIR / "webhook_results"


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    _ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _process_queue_file(queue_file: Path) -> Dict[str, Any]:
    payload = _load_json(queue_file)
    if "payload" in payload and isinstance(payload["payload"], dict):
        payload = payload["payload"]

    event = parse_callback_payload(payload)
    summary = handle_callback_event(
        event,
        download=True,
        fetch_ts_lyrics=True,
        download_retries=3,
        retry_backoff_s=1.5,
        timeout_s=120.0,
    )

    _ensure_dir(RESULTS_DIR)
    result_path = RESULTS_DIR / f"{event.task_id}.json"
    _write_json(result_path, summary)
    return summary


def drain(loop: bool, sleep_s: float) -> None:
    _ensure_dir(QUEUE_DIR)
    _ensure_dir(RESULTS_DIR)

    while True:
        queue_files = sorted(QUEUE_DIR.glob("*.json"))
        if not queue_files:
            if loop:
                time.sleep(sleep_s)
                continue
            break

        for queue_file in queue_files:
            try:
                summary = _process_queue_file(queue_file)
                if not summary.get("errors"):
                    queue_file.unlink(missing_ok=True)
            except Exception as exc:
                error_summary = {"error": str(exc), "queue_file": str(queue_file)}
                _write_json(RESULTS_DIR / f"error_{queue_file.stem}.json", error_summary)

        if not loop:
            break


def main() -> None:
    parser = argparse.ArgumentParser(description="Drain ProviderC webhook queue.")
    sub = parser.add_subparsers(dest="command", required=True)

    drain_parser = sub.add_parser("drain", help="Drain the webhook queue.")
    drain_parser.add_argument("--loop", action="store_true", help="Run continuously.")
    drain_parser.add_argument("--sleep", type=float, default=1.0, help="Loop sleep seconds.")

    args = parser.parse_args()
    if args.command == "drain":
        drain(loop=args.loop, sleep_s=args.sleep)


if __name__ == "__main__":
    main()
