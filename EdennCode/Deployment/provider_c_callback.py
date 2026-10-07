import logging
from pathlib import Path
from typing import Any, Dict, List

from EdennCode.Deployment.music_callback_api_deployment.provider_c_callback_service import (
    CallbackEvent,
    ProviderCCallbackService,
    TaskInfo,
    TrackInfo,
)

logger = logging.getLogger(__name__)

# Stays anchored at the repo root (the shim's historical home) so the
# webhook/download/lyrics dirs land exactly where they always have.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SERVICE = ProviderCCallbackService(base_dir=PROJECT_ROOT, logger=logger)
WEBHOOK_LOG_DIR = _SERVICE.webhook_log_dir
DOWNLOAD_DIR = _SERVICE.download_dir
LYRICS_TS_DIR = _SERVICE.lyrics_ts_dir


def parse_callback_payload(payload: dict) -> CallbackEvent:
    return _SERVICE.parse_callback_payload(payload)


def handle_callback_event(
    event: CallbackEvent,
    *,
    download: bool = True,
    fetch_ts_lyrics: bool = True,
    download_retries: int = 3,
    retry_backoff_s: float = 1.5,
    timeout_s: float = 120.0,
) -> Dict[str, Any]:
    return _SERVICE.handle_callback_event(
        event,
        download=download,
        fetch_ts_lyrics=fetch_ts_lyrics,
        download_retries=download_retries,
        retry_backoff_s=retry_backoff_s,
        timeout_s=timeout_s,
    )


def run_post_analysis_hook(task_id: str, audio_paths: List[str]) -> None:
    _SERVICE.run_post_analysis_hook(task_id, audio_paths)


__all__ = [
    "CallbackEvent",
    "DOWNLOAD_DIR",
    "LYRICS_TS_DIR",
    "PROJECT_ROOT",
    "TaskInfo",
    "TrackInfo",
    "WEBHOOK_LOG_DIR",
    "handle_callback_event",
    "parse_callback_payload",
    "run_post_analysis_hook",
]
