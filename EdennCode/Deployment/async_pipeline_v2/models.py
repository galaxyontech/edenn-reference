from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class JobStatus:
    QUEUED = "queued"
    PROCESSING = "processing"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class TaskStatus:
    QUEUED = "queued"
    LEASED = "leased"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTERED = "dead_lettered"
    CANCELED = "canceled"


class StageStatus:
    STARTED = "started"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass(frozen=True)
class AsyncV2Job:
    job_id: str
    job_type: str
    status: str
    request_json: dict[str, Any]
    session_id: Optional[str] = None
    creator_user_id: Optional[str] = None
    #: Who actually ran it, when that is not the session's owner — a
    #: collaborator invited to iterate spends too. Only the studio's own job
    #: table carries this column; the fleet's rows read back as None.
    actor_user_id: Optional[str] = None
    current_stage: Optional[str] = None
    progress_percent: float = 0.0
    priority: int = 0
    result_json: Optional[dict[str, Any]] = None
    error_json: Optional[dict[str, Any]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


@dataclass(frozen=True)
class AsyncV2Task:
    task_id: str
    job_id: str
    queue_name: str
    task_type: str
    status: str
    payload_json: dict[str, Any]
    priority: int = 0
    attempt: int = 0
    max_attempts: int = 3
    lease_owner: Optional[str] = None
    lease_until: Optional[datetime] = None
    not_before: Optional[datetime] = None
    idempotency_key: Optional[str] = None
    last_error_json: Optional[dict[str, Any]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


@dataclass(frozen=True)
class AsyncV2StageRun:
    stage_run_id: str
    job_id: str
    stage_name: str
    status: str
    task_id: Optional[str] = None
    attempt: int = 1
    input_json: dict[str, Any] = field(default_factory=dict)
    output_json: Optional[dict[str, Any]] = None
    error_json: Optional[dict[str, Any]] = None
    started_at: Optional[datetime] = None
    heartbeat_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


@dataclass(frozen=True)
class AsyncV2Artifact:
    artifact_id: str
    job_id: str
    artifact_type: str
    role: Optional[str] = None
    container: Optional[str] = None
    blob_name: Optional[str] = None
    url: Optional[str] = None
    content_type: Optional[str] = None
    local_path: Optional[str] = None
    metadata_json: dict[str, Any] = field(default_factory=dict)
    payload_json: Optional[dict[str, Any]] = None
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class AsyncV2JobEvent:
    event_id: str
    job_id: str
    event_type: str
    stage_name: Optional[str] = None
    message: Optional[str] = None
    payload_json: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class TaskEnvelope:
    task_id: str
    job_id: str
    queue_name: str
    task_type: str
    payload_json: dict[str, Any]
    priority: int = 0
    max_attempts: int = 3
    idempotency_key: Optional[str] = None
    not_before: Optional[datetime] = None
