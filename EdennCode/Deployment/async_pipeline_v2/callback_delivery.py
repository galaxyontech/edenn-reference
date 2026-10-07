"""Best-effort delivery of a terminal v2 job status to a client callback URL.

The v2 API accepts and SSRF-validates ``callback_url`` at submit and persists it
into the job's ``request_json``, but nothing ever delivered it (finding #18): the
webhook was accepted and silently dropped. This module delivers the final status
once, best-effort, at the worker's terminal transition, mirroring the v1 webhook
contract (``api_video_generation._fire_callback``):
``{job_id, status, result, error, created_at}``.

Delivery runs AFTER the job is already committed terminal (completed / failed),
is bounded by a timeout, and swallows every error, so it can neither fail nor
meaningfully delay a job. It is at-most-once (a crash between commit and POST
drops it), so clients must still treat the status endpoint as the source of
truth — the same contract v1 has.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from EdennCode.Deployment.async_pipeline_v2.result_paths import (
    strip_cost_metadata,
    strip_result_envelope_duplicates,
)

# Match v1's callback timeout so a client's receiver sees one consistent contract.
_CALLBACK_TIMEOUT_S = 30.0


async def deliver_terminal_callback(
    *,
    url: str,
    job_id: Optional[str],
    status: str,
    result_json: Optional[dict[str, Any]],
    error_json: Optional[dict[str, Any]],
    created_at: Optional[str],
    logger: logging.Logger,
) -> None:
    """POST the terminal status to ``url`` once, best-effort (never raises)."""
    if isinstance(result_json, dict):
        # Same egress rules as the status endpoint: envelope fields are not
        # repeated inside the result block, and cost accounting is dropped
        # entirely. (Both apply to video-music, which is the job type that
        # uses callbacks.)
        result_json = strip_result_envelope_duplicates(dict(result_json))
        strip_cost_metadata(result_json)
    payload = {
        "job_id": job_id,
        "status": status,
        "result": result_json,
        "error": error_json,
        "created_at": created_at,
    }
    try:
        async with httpx.AsyncClient(timeout=_CALLBACK_TIMEOUT_S) as client:
            response = await client.post(url, json=payload)
        if response.status_code >= 400:
            logger.warning(
                "v2 job %s callback returned HTTP %s for %s",
                job_id, response.status_code, url,
            )
    except Exception as exc:
        logger.warning(
            "v2 job %s callback delivery failed for %s: %s",
            job_id, url, exc, exc_info=True,
        )


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


async def deliver_job_callback(
    job: Any,
    *,
    status: str,
    result_json: Optional[dict[str, Any]],
    error_json: Optional[dict[str, Any]],
    logger: logging.Logger,
) -> None:
    """Deliver the terminal status to ``job.request_json['callback_url']`` if set.

    A no-op when the job is unavailable or has no callback_url. The URL was
    SSRF-validated at acceptance, so it is safe to POST here.
    """
    if job is None:
        return
    request = getattr(job, "request_json", None)
    url = request.get("callback_url") if isinstance(request, dict) else None
    if not url:
        return
    await deliver_terminal_callback(
        url=url,
        job_id=getattr(job, "job_id", None),
        status=status,
        result_json=result_json,
        error_json=error_json,
        created_at=_iso(getattr(job, "created_at", None)),
        logger=logger,
    )


__all__ = ["deliver_terminal_callback", "deliver_job_callback"]
