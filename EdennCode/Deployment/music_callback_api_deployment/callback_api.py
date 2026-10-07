from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request

from .provider_c_callback import parse_callback_payload

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent
QUEUE_DIR = BASE_DIR / "webhook_queue"


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    _ensure_dir(path.parent)
    path.write_text(_json_dumps(payload), encoding="utf-8")


def _json_dumps(payload: Dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=True, indent=2)


@app.post("/provider_c/callback")
async def provider_c_callback(request: Request) -> Dict[str, Any]:
    secret = request.query_params.get("secret")
    expected = os.getenv("PROVIDER_C_WEBHOOK_SECRET", "dev-secret")
    if secret != expected:
        raise HTTPException(status_code=401, detail="unauthorized")

    payload = await request.json()
    event = parse_callback_payload(payload)

    _ensure_dir(QUEUE_DIR)
    timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    queue_path = QUEUE_DIR / f"{timestamp}_{event.task_id}.json"
    _write_json(queue_path, {"payload": payload})

    return {"ok": True}
