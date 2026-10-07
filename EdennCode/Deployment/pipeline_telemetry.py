"""Best-effort persistence of v1 pipeline run + per-stage timing.

The v1 (synchronous) video-music workflow already emits a structured ``pipeline_timing``
log line per stage but persists nothing, so it had no queryable telemetry comparable to
v2's ``async_v2_stage_runs``. This recorder writes the same information to the existing
``pipeline_runs`` / ``pipeline_stages`` tables.

Every database operation is wrapped so a telemetry failure can never break a generation
job: on error we log and continue (and disable further writes for the run if the initial
insert fails).
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from EdennCode.Deployment.postgres_wrapper import PostgresClient

logger = logging.getLogger(__name__)

# Stable namespace so a non-UUID job id maps to a deterministic run_id.
_RUN_NS = uuid.UUID("6f1d6c2e-1a3b-5c7d-9e0f-1a2b3c4d5e6f")

_PROVIDER_BY_MODELSPEC = {
    "edenn_basic": "provider_a",
    "edenn_enhanced": "provider_b",
    "edenn_studio": "provider_c",
}


def pipeline_telemetry_enabled() -> bool:
    """Default ON; set PIPELINE_TELEMETRY_ENABLED=0/false to disable."""
    raw = os.getenv("PIPELINE_TELEMETRY_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _as_uuid(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return str(uuid.uuid5(_RUN_NS, str(value)))


def provider_for_modelspec(modelspec: Optional[str]) -> Optional[str]:
    if not modelspec:
        return None
    return _PROVIDER_BY_MODELSPEC.get(str(modelspec).strip().lower())


class PipelineRunRecorder:
    """Records one v1 pipeline run and its stages to Postgres (best-effort)."""

    def __init__(
        self,
        *,
        job_id: str,
        modelspec: Optional[str] = None,
        workflow_type: str = "video_music",
        workflow_version: str = "v1",
        request_id: Optional[str] = None,
        endpoint: str = "/api/v1/jobs/video",
        user_prompt: Optional[str] = None,
        client_factory: Optional[Callable[[], PostgresClient]] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        self.job_id = str(job_id)
        self.run_id = _as_uuid(job_id)
        # request_id parents the run (FK requests.request_id). Derive it from the
        # job id so it is stable per job; the recorder creates the requests row too.
        self.request_id = _as_uuid(request_id) if request_id else _as_uuid(f"req:{job_id}")
        self.modelspec = modelspec
        self.workflow_type = workflow_type
        self.workflow_version = workflow_version
        self.endpoint = endpoint
        self.user_prompt = user_prompt
        self._cf = client_factory or PostgresClient.from_env
        self._stage_index = 0
        self._enabled = True
        self._metadata = {"job_id": self.job_id, **(metadata or {})}

    def _exec(self, sql: str, params: list) -> None:
        with self._cf() as client:
            client.run_sql(sql, params=params)

    def start(self) -> None:
        try:
            # pipeline_runs.request_id has an FK to requests(request_id); create the
            # parent request row first (the API does not record requests itself).
            self._exec(
                """
                INSERT INTO requests (request_id, endpoint, user_prompt, status, metadata)
                VALUES (%s, %s, %s, 'received', %s::jsonb)
                ON CONFLICT (request_id) DO NOTHING
                """,
                [self.request_id, self.endpoint, self.user_prompt, json.dumps(self._metadata)],
            )
            self._exec(
                """
                INSERT INTO pipeline_runs
                    (run_id, request_id, workflow_type, workflow_version, modelspec, status, metadata)
                VALUES (%s, %s, %s, %s, %s, 'running', %s::jsonb)
                ON CONFLICT (run_id) DO NOTHING
                """,
                [
                    self.run_id, self.request_id, self.workflow_type,
                    self.workflow_version, self.modelspec, json.dumps(self._metadata),
                ],
            )
            logger.info("pipeline telemetry: started run %s (job %s)", self.run_id, self.job_id)
        except Exception:
            self._enabled = False
            logger.warning("pipeline telemetry: start failed for job %s", self.job_id, exc_info=True)

    def record_stage(
        self,
        stage_name: str,
        duration_s: Optional[float],
        *,
        ts_start: Optional[float] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
    ) -> None:
        # "total" is the run summary, not a stage; finish() captures it.
        if not self._enabled or stage_name == "total":
            return
        try:
            # duration_ms is a generated column (finished_at - started_at); set the
            # timestamps and let Postgres compute it.
            if ts_start is not None:
                started = datetime.fromtimestamp(ts_start, tz=timezone.utc)
            elif duration_s is not None:
                started = datetime.now(timezone.utc) - timedelta(seconds=duration_s)
            else:
                started = datetime.now(timezone.utc)
            finished = (
                started + timedelta(seconds=duration_s)
                if duration_s is not None else None
            )
            self._exec(
                """
                INSERT INTO pipeline_stages
                    (run_id, stage_name, stage_index, started_at, finished_at,
                     status, provider, model, input_tokens, output_tokens)
                VALUES (%s, %s, %s, %s, %s, 'succeeded', %s, %s, %s, %s)
                """,
                [
                    self.run_id, stage_name, self._stage_index, started, finished,
                    provider, model, input_tokens, output_tokens,
                ],
            )
            self._stage_index += 1
        except Exception:
            logger.warning(
                "pipeline telemetry: record_stage(%s) failed for job %s",
                stage_name, self.job_id, exc_info=True,
            )

    def finish(
        self,
        *,
        status: str = "completed",
        duration_s: Optional[float] = None,
        total_input_tokens: Optional[int] = None,
        total_output_tokens: Optional[int] = None,
        music_provider: Optional[str] = None,
        video_summary: Optional[str] = None,
        music_prompt: Optional[Any] = None,
        failed_at_stage: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        if not self._enabled:
            return
        try:
            # duration_ms / latency_ms are generated columns; finished_at / responded_at
            # drive them. duration_s is accepted for API symmetry but not written.
            _ = duration_s
            # CHECK constraints allow only succeeded/failed (+ running/received).
            status = "failed" if str(status).lower() in {
                "failed", "error", "cancelled", "canceled"} else "succeeded"
            mp = None
            if music_prompt is not None:
                try:
                    mp = json.dumps(music_prompt, default=str)
                except Exception:
                    mp = None
            vs = str(video_summary)[:8000] if video_summary is not None else None
            self._exec(
                """
                UPDATE pipeline_runs SET
                    finished_at = now(),
                    updated_at = now(),
                    status = %s,
                    total_input_tokens = COALESCE(%s, total_input_tokens),
                    total_output_tokens = COALESCE(%s, total_output_tokens),
                    music_provider = COALESCE(%s, music_provider),
                    video_summary = COALESCE(%s, video_summary),
                    music_prompt = COALESCE(%s::jsonb, music_prompt),
                    failed_at_stage = %s,
                    error_code = %s,
                    error_message = %s
                WHERE run_id = %s
                """,
                [
                    status, total_input_tokens, total_output_tokens,
                    music_provider, vs, mp, failed_at_stage, error_code,
                    (str(error_message)[:2000] if error_message else None), self.run_id,
                ],
            )
            # requests has a CHECK that a 'succeeded' row carries an output_url, which
            # this layer doesn't have (the API uploads after the workflow returns). So
            # only mark failures here; pipeline_runs.status carries the success signal.
            self._exec(
                """
                UPDATE requests SET responded_at = now(), updated_at = now(),
                    status = CASE WHEN %s = 'failed' THEN 'failed' ELSE status END,
                    error_code = %s, error_message = %s
                WHERE request_id = %s
                """,
                [
                    status, error_code,
                    (str(error_message)[:2000] if error_message else None), self.request_id,
                ],
            )
        except Exception:
            logger.warning("pipeline telemetry: finish failed for job %s", self.job_id, exc_info=True)
