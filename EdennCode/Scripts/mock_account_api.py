#!/usr/bin/env python3
"""Standalone mock of the Edenn account/billing query endpoints.

Zero dependencies (stdlib only) — hand this file to any client/frontend team:

    python3 EdennCode/Scripts/mock_account_api.py            # listens on :8899
    python3 EdennCode/Scripts/mock_account_api.py 9000       # custom port

Mocked surface (behavior mirrors the real API):
  GET /api/v1/account/balance
  GET /api/v1/account/usage?from=&to=&limit=&key_prefix=

Rules reproduced from production:
  - Authorization: Bearer sk-... required -> else 401 {"detail","code":"api_key_required"}
  - key_prefix is the key's first 12 chars (e.g. sk-e8vQoI3B7); exact match
  - unknown key_prefix is NOT an error: empty rows + zero totals
  - totals + per-key by_key breakdown are computed from the filtered rows
  - from/to are ISO-8601 string bounds on timestamp_utc (inclusive/exclusive)
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

ACCOUNT = {
    "account_id": "910000000000",
    "registered_name": "示例传媒科技有限公司",
    "balance_usd": 985.5,
    "is_active": True,
    "updated_at": "2026-07-23T02:10:00+00:00",
}

# Two keys under the SAME account (shared wallet), three finished jobs.
USAGE_ROWS = [
    {
        "job_id": "job_ef93d8322b7b45e9a89fe31027cf8b24",
        "endpoint": "/api/v2/jobs/video-music",
        "status": "completed",
        "key_prefix": "sk-e8vQoI3B7",
        "model_spec": "edenn_basic",
        "music_provider": "edenn_basic",
        "timestamp_utc": "2026-07-22T06:06:17.569941+00:00",
        "latency_ms": -1,
        "prompt_tokens": 16394,
        "completion_tokens": 1462,
        "total_tokens": 17856,
        "token_cost_usd": 0.150996,
        "generation_cost_usd": 0.065,
        "total_cost_usd": 0.215996,
        "video_duration_s": 18.566667,
        "billing_mode": "per_request",
        "billed_units": 1,
        "unit_price_usd": 10.0,
        "billed_amount_usd": 10.0,
    },
    {
        "job_id": "job_0b8f3816b8d34df9822f15713998259c",
        "endpoint": "/api/v2/jobs/multi-image-music",
        "status": "completed",
        "key_prefix": "sk-XbgqH8R8r",
        "model_spec": "edenn_basic",
        "music_provider": "edenn_basic",
        "timestamp_utc": "2026-07-21T09:30:41.120000+00:00",
        "latency_ms": 143000,
        "prompt_tokens": 3200,
        "completion_tokens": 703,
        "total_tokens": 3903,
        "token_cost_usd": 0.041236,
        "generation_cost_usd": 0.065,
        "total_cost_usd": 0.45,
        "video_duration_s": 9.0,
        "billing_mode": "per_second",
        "billed_units": 9,
        "unit_price_usd": 0.5,
        "billed_amount_usd": 4.5,
    },
    {
        "job_id": "job_58d3b39e15c5426bbc597c47f205fd86",
        "endpoint": "/api/v2/jobs/video-music",
        "status": "failed",  # failed jobs are recorded but never billed
        "key_prefix": "sk-XbgqH8R8r",
        "model_spec": "edenn_enhanced",
        "music_provider": "edenn_enhanced",
        "timestamp_utc": "2026-07-20T15:02:03.000000+00:00",
        "latency_ms": -1,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "token_cost_usd": 0.0,
        "generation_cost_usd": 0.0,
        "total_cost_usd": 0.0,
        "video_duration_s": None,
        "billing_mode": "",
        "billed_units": 0,
        "unit_price_usd": 0.0,
        "billed_amount_usd": 0.0,
    },
]


def _totals(rows: list[dict]) -> dict:
    by_key: dict[str, dict] = {}
    for r in rows:
        label = r.get("key_prefix") or "unattributed"
        b = by_key.setdefault(label, {
            "jobs": 0, "total_tokens": 0,
            "total_cost_usd": 0.0, "total_billed_usd": 0.0,
        })
        b["jobs"] += 1
        b["total_tokens"] += r["total_tokens"]
        b["total_cost_usd"] = round(b["total_cost_usd"] + r["total_cost_usd"], 6)
        b["total_billed_usd"] = round(
            b["total_billed_usd"] + r["billed_amount_usd"], 6)
    return {
        "jobs": len(rows),
        "total_tokens": sum(r["total_tokens"] for r in rows),
        "total_cost_usd": round(sum(r["total_cost_usd"] for r in rows), 6),
        "total_billed_usd": round(
            sum(r["billed_amount_usd"] for r in rows), 6),
        "by_key": by_key,
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer_ok(self) -> bool:
        auth = self.headers.get("Authorization", "")
        parts = auth.split(None, 1)
        return len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].startswith("sk-")

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        url = urlparse(self.path)
        if url.path not in ("/api/v1/account/balance", "/api/v1/account/usage"):
            self._send(404, {"detail": "Not Found"})
            return
        if not self._bearer_ok():
            self._send(401, {
                "detail": "A valid API key is required for account endpoints. "
                          "Provide an Authorization: Bearer sk-... header.",
                "code": "api_key_required",
            })
            return
        if url.path == "/api/v1/account/balance":
            self._send(200, ACCOUNT)
            return
        q = parse_qs(url.query)
        rows = list(USAGE_ROWS)
        if q.get("from"):
            rows = [r for r in rows if r["timestamp_utc"] >= q["from"][0]]
        if q.get("to"):
            rows = [r for r in rows if r["timestamp_utc"] < q["to"][0]]
        if q.get("key_prefix"):
            rows = [r for r in rows if r["key_prefix"] == q["key_prefix"][0]]
        rows.sort(key=lambda r: r["timestamp_utc"], reverse=True)
        rows = rows[: int(q.get("limit", ["200"])[0])]
        self._send(200, {"rows": rows, "totals": _totals(rows)})

    def log_message(self, fmt, *args):  # keep demo output clean
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    print(f"mock account API on http://localhost:{port}  (Ctrl-C to stop)")
    print("  GET /api/v1/account/balance")
    print("  GET /api/v1/account/usage?from=&to=&limit=&key_prefix=")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
