"""TikTok Business API adapter — the first real channel (BACKEND_GAPS §3 P2).

Complete code paths against the Business API v1.3 surface; activates when the
access token + advertiser id are present (APP_ID is audit-only). Until then
`is_configured()` is False and callers fall back to the sandbox adapter, so the
loop stays exercisable.

Required env (add to the deployment env to go live):
    TIKTOK_ADS_ACCESS_TOKEN   long-lived access token for the ad account
    TIKTOK_ADS_ADVERTISER_ID  advertiser (ad account) id
    TIKTOK_ADS_APP_ID         the registered app id (audit trail only)

Flow per the Business API: `publish` uploads the rendered video to the
advertiser's creative library and returns its video_id. Reporting is by AD, not
by uploaded video — so `pull_metrics(channel_ref, ...)` treats channel_ref as an
AD id and filters `/report/integrated/get/` by `ad_ids`, accepting only the row
whose ad_id matches exactly (never a stray account-wide row). The upload ->
adgroup/ad-creation step that turns a video into a delivering ad is left to the
platform's campaign tooling; until an ad exists for the uploaded video,
pull_metrics correctly returns None. This whole path is UNVERIFIED against the
live API (no credentials in any env yet); the sandbox adapter is what runs today.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .base import ChannelAdapter, DailyMetrics, PublishResult

logger = logging.getLogger(__name__)

API = "https://business-api.tiktok.com/open_api/v1.3"


class TikTokAdsAdapter(ChannelAdapter):
    name = "tiktok"

    def __init__(self) -> None:
        self.token = os.getenv("TIKTOK_ADS_ACCESS_TOKEN", "").strip()
        self.advertiser_id = os.getenv("TIKTOK_ADS_ADVERTISER_ID", "").strip()

    def is_configured(self) -> bool:
        return bool(self.token and self.advertiser_id)

    def _call(self, method: str, path: str, payload: Optional[dict[str, Any]] = None,
              query: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        url = f"{API}{path}"
        if query:
            from urllib.parse import urlencode
            url += "?" + urlencode({k: json.dumps(v) if isinstance(v, (list, dict)) else v
                                    for k, v in query.items()})
        req = urllib.request.Request(
            url, method=method,
            data=json.dumps(payload).encode() if payload else None,
            headers={"Access-Token": self.token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read())
        if body.get("code") not in (0, "0"):
            raise RuntimeError(f"channel error {body.get('code')}: {body.get('message')}")
        return body.get("data") or {}

    async def publish(self, video_path: Path, *, title: str, campaign: str,
                      meta: Optional[dict[str, Any]] = None) -> PublishResult:
        if not self.is_configured():
            raise RuntimeError(
                "TikTok adapter not configured — set TIKTOK_ADS_ACCESS_TOKEN and "
                "TIKTOK_ADS_ADVERTISER_ID (see ads/tiktok.py header)")
        import asyncio

        def _upload() -> dict[str, Any]:
            # multipart upload: /file/video/ad/upload/
            boundary = "----edennads"
            payload = video_path.read_bytes()
            sig = hashlib.md5(payload).hexdigest()
            parts = []
            for k, v in [("advertiser_id", self.advertiser_id),
                         ("upload_type", "UPLOAD_BY_FILE"),
                         ("video_signature", sig),
                         ("file_name", video_path.name)]:
                parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                             f'name="{k}"\r\n\r\n{v}\r\n'.encode())
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                         f'name="video_file"; filename="{video_path.name}"\r\n'
                         f"Content-Type: video/mp4\r\n\r\n".encode())
            body = b"".join(parts) + payload + f"\r\n--{boundary}--\r\n".encode()
            req = urllib.request.Request(
                f"{API}/file/video/ad/upload/", data=body, method="POST",
                headers={"Access-Token": self.token,
                         "Content-Type": f"multipart/form-data; boundary={boundary}"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                out = json.loads(resp.read())
            if out.get("code") not in (0, "0"):
                raise RuntimeError(f"upload error {out.get('code')}: {out.get('message')}")
            items = out.get("data") or []
            return items[0] if isinstance(items, list) and items else out.get("data") or {}

        data = await asyncio.to_thread(_upload)
        video_id = str(data.get("video_id") or data.get("id") or "")
        logger.info("tiktok upload ok: %s (%s)", video_id, title)
        return PublishResult(channel=self.name, channel_ref=video_id, raw=data)

    async def pull_metrics(self, channel_ref: str, date: str) -> Optional[DailyMetrics]:
        import asyncio

        def _report() -> Optional[dict[str, Any]]:
            data = self._call(
                "GET", "/report/integrated/get/",
                query={"advertiser_id": self.advertiser_id,
                       "report_type": "BASIC", "data_level": "AUCTION_AD",
                       "dimensions": ["ad_id", "stat_time_day"],
                       "metrics": ["impressions", "clicks", "conversion", "spend"],
                       "start_date": date, "end_date": date,
                       "filtering": [{"field_name": "ad_ids",
                                      "filter_type": "IN",
                                      "filter_value": json.dumps([channel_ref])}],
                       "page_size": 10})
            rows = data.get("list") or []
            # Fail safe: accept only the row that is exactly this ad, never a
            # stray account-wide row, so metrics can't be mis-attributed.
            for r in rows:
                if str((r.get("dimensions") or {}).get("ad_id")) == str(channel_ref):
                    return r
            return None

        row = await asyncio.to_thread(_report)
        if not row:
            return None
        m = row.get("metrics") or {}
        return DailyMetrics(
            channel=self.name, channel_ref=channel_ref, date=date,
            impressions=int(float(m.get("impressions") or 0)),
            clicks=int(float(m.get("clicks") or 0)),
            conversions=int(float(m.get("conversion") or 0)),
            spend=float(m.get("spend") or 0.0),
        )
