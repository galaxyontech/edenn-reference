from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import pytest
import requests

from EdennCode.TestSuites.helpers.integration import should_run_remote_integration


DEFAULT_REMOTE_BASE_URL = (
    "https://staging-app.worker.example.invalid"
)
DEFAULT_REMOTE_VIDEO_URL = (
    "https://thetestdata.com/assets/video/mp4/highquality/4K_UHD_30_FPS.mp4"
)
# 20.6s / 320x568 (portrait) / 2.3MB — must satisfy the source-video input
# guardrails (>15s, <=150s, <=300MB) because these tests exercise guarded paths.
SMOKE_VIDEO = Path(
    "EdennCode/TestSuites/assets/smoke/videos/Videos2026-04-08_104426_347.mp4"
)


def _remote_base_url() -> str:
    return os.getenv("ASYNC_V2_REMOTE_BASE_URL", DEFAULT_REMOTE_BASE_URL).rstrip("/")


def _remote_video_url() -> str:
    return os.getenv("ASYNC_V2_REMOTE_VIDEO_URL", DEFAULT_REMOTE_VIDEO_URL).strip()


def _absolute_url(base_url: str, path_or_url: str) -> str:
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    return urljoin(f"{base_url}/", path_or_url.lstrip("/"))


def _json_or_text(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _require_remote_endpoint_enabled() -> None:
    if not should_run_remote_integration():
        pytest.skip(
            "Remote v2 endpoint tests are skipped locally by default. "
            "Set RUN_REMOTE_INTEGRATION_LOCAL=1 to execute them."
        )


def _poll_job(
    session: requests.Session,
    *,
    base_url: str,
    status_url: str,
    timeout_s: float = 1800.0,
    interval_s: float = 5.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    status_endpoint = _absolute_url(base_url, status_url)
    last_payload: dict[str, Any] | None = None

    while time.monotonic() < deadline:
        response = session.get(status_endpoint, timeout=60)
        assert response.status_code == 200, _json_or_text(response)
        payload = response.json()
        last_payload = payload
        if payload.get("status") in {"completed", "failed", "canceled"}:
            return payload
        time.sleep(interval_s)

    raise AssertionError(
        f"Remote async v2 job did not finish within {timeout_s}s; "
        f"last payload={last_payload}"
    )


def _assert_fetchable_url(
    session: requests.Session,
    url: str,
    *,
    field_name: str,
) -> None:
    response = session.get(url, stream=True, timeout=90)
    try:
        assert 200 <= response.status_code < 300, {
            "field": field_name,
            "url": url,
            "status_code": response.status_code,
            "body": response.text[:500],
        }
        first_chunk = next(response.iter_content(chunk_size=4096), b"")
        assert first_chunk, {"field": field_name, "url": url, "error": "empty body"}
    finally:
        response.close()


def _assert_completed_with_thumbnail(
    session: requests.Session,
    payload: dict[str, Any],
) -> None:
    assert payload.get("status") == "completed", payload
    assert "stages" not in payload
    assert "artifacts" not in payload

    result = payload.get("result") or payload.get("result_json") or {}
    assert isinstance(result, dict), payload

    # Five-block result shape (3e60d89): media URLs live inside the
    # video_metadata / audio_metadata blocks, not flat on result.
    video_metadata = result.get("video_metadata") or {}
    audio_metadata = result.get("audio_metadata") or {}
    for block, field in (
        (video_metadata, "video_url"),
        (video_metadata, "thumbnail_url"),
        (audio_metadata, "audio_url"),
    ):
        value = block.get(field)
        assert isinstance(value, str) and value.strip(), {
            "missing_field": field,
            "job_id": payload.get("job_id"),
            "result": result,
        }

    def _walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                assert "blob" not in key.lower(), payload
                _walk(child)
        elif isinstance(value, list):
            for item in value:
                _walk(item)

    _walk(payload)

    _assert_fetchable_url(session, video_metadata["thumbnail_url"], field_name="thumbnail_url")


@pytest.mark.remote_integration
@pytest.mark.parametrize("input_kind", ["upload", "video_url"])
def test_remote_async_v2_split_video_music_returns_fetchable_thumbnail_url(
    input_kind: str,
) -> None:
    _require_remote_endpoint_enabled()

    base_url = _remote_base_url()
    session = requests.Session()
    data = {
        "mode": "split",
        "modelspec": "edenn_basic",
        "include_vocals": "false",
        "user_prompt": (
            "Create a short upbeat instrumental electronic track for this video."
        ),
        "preserve_original_audio": "true",
        "music_volume": "0.65",
        "compression_flag": "true",
        "compression_max_height": "1280",
        "max_attempts": "1",
    }

    if input_kind == "upload":
        assert SMOKE_VIDEO.exists(), SMOKE_VIDEO
        with SMOKE_VIDEO.open("rb") as video_file:
            response = session.post(
                f"{base_url}/api/v2/jobs/video-music",
                data=data,
                files={"video": ("smoke.mp4", video_file, "video/mp4")},
                timeout=120,
            )
    else:
        data["video_url"] = _remote_video_url()
        response = session.post(
            f"{base_url}/api/v2/jobs/video-music",
            data=data,
            timeout=120,
        )

    assert response.status_code == 200, _json_or_text(response)
    created = response.json()
    assert created.get("job_id"), created
    assert created.get("status_url"), created

    completed = _poll_job(
        session,
        base_url=base_url,
        status_url=created["status_url"],
    )
    _assert_completed_with_thumbnail(session, completed)
