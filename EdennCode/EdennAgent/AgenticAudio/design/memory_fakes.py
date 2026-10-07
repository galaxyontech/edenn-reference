"""In-memory repositories, queue, and provider fakes for the AgenticAudio devserver.

Extracted from Testing/test_agentic_audio_api.py so the standalone devserver can
run WITHOUT the test suite in the image (tests are excluded from the container
build, and the test module imports pytest). The test suite imports these back
from here, so there is exactly one implementation.
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from EdennCode.EdennAgent.AgenticAudio.models import (
    AgenticAudioChoice,
    AgenticAudioMessage,
    AgenticAudioSession,
    AgenticAudioSessionSnapshot,
    AgenticAudioToolCall,
    AgenticSessionStatus,
)
from EdennCode.EdennAgent.AgenticAudio.persistence.jobs import (
    TERMINAL_JOB_STATUSES as _TERMINAL_JOB_STATUSES,
)
from EdennCode.Deployment.async_pipeline_v2.models import (
    AsyncV2Artifact,
    AsyncV2Job,
    AsyncV2JobEvent,
    AsyncV2StageRun,
    AsyncV2Task,
    JobStatus,
    StageStatus,
    TaskEnvelope,
    TaskStatus,
    new_id,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _fake_analyze(
    *,
    artifact: Any,
    user_prompt: str = "",
    modelspec: str = "edenn_basic",
) -> dict[str, Any]:
    metadata = dict(getattr(artifact, "metadata_json", None) or {})
    duration = float(metadata.get("duration") or metadata.get("duration_s") or 0.0)
    return {
        "duration_s": duration,
        "width": metadata.get("width"),
        "height": metadata.get("height"),
        "video_title": "Test Video",
        "video_description": "A short test video.",
        "scenes": [],
        "detected_language": "en",
        "detected_category": "VIDEO",
        "detected_include_vocals": False,
        "detected_vocal_gender": "female",
        "sanitized_prompt": user_prompt,
        "music_prompt": {},
        "suggested_modelspec": modelspec,
    }


class _MemoryAgenticRepository:
    def __init__(self) -> None:
        self.sessions: dict[str, AgenticAudioSession] = {}
        self.messages: list[AgenticAudioMessage] = []
        self.tool_calls: list[AgenticAudioToolCall] = []
        self.choices: list[AgenticAudioChoice] = []

    def create_session(self, **kwargs: Any) -> AgenticAudioSession:
        session = AgenticAudioSession(
            session_id=kwargs.get("session_id") or new_id("agent_session"),
            source_video_artifact_id=kwargs["source_video_artifact_id"],
            creator_user_id=kwargs.get("creator_user_id"),
            status=AgenticSessionStatus.ACTIVE,
            phase=kwargs["phase"],
            state_json=kwargs.get("state_json") or {},
            created_at=_now(),
            updated_at=_now(),
        )
        self.sessions[session.session_id] = session
        return session

    def get_session(self, session_id: str) -> AgenticAudioSession | None:
        return self.sessions.get(session_id)

    def ping(self) -> None:
        """An in-memory store is reachable for as long as the process is."""

    def sessions_sharing_source(self, artifact_id: str, *, excluding: str = "") -> int:
        return sum(
            1
            for sid, session in self.sessions.items()
            if sid != excluding
            and getattr(session, "source_video_artifact_id", None) == artifact_id
        )

    def list_sessions(self, *, creator_user_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        rows = [
            s for s in self.sessions.values()
            if creator_user_id is None or s.creator_user_id == creator_user_id
        ]
        rows.sort(key=lambda s: (s.updated_at or s.created_at or _now()), reverse=True)
        out = []
        for s in rows[: int(limit)]:
            st = s.state_json or {}
            obs = st.get("observation") or {}
            fin = st.get("final_artifact") or {}
            out.append(
                {
                    "session_id": s.session_id,
                    "status": s.status,
                    "phase": s.phase,
                    "creator_user_id": s.creator_user_id,
                    "source_video_artifact_id": s.source_video_artifact_id,
                    "updated_at": s.updated_at.isoformat() if s.updated_at else None,
                    # Display extras for the entrance Sessions/Gallery views —
                    # mirrors the SQL repository's payload.
                    "title": obs.get("video_title"),
                    "final_media_url": fin.get("video_url") or fin.get("audio_url"),
                }
            )
        return out

    def delete_session(self, session_id: str) -> bool:
        """Remove a session and everything that hangs off it.

        The Postgres schema does this with ON DELETE CASCADE; in memory the
        cascade has to be written out, and forgetting one of these lists is
        exactly how a "deleted" session leaves its transcript behind.
        """
        existed = self.sessions.pop(session_id, None) is not None
        for attr in ("messages", "tool_calls", "choices"):
            store = getattr(self, attr, None)
            if isinstance(store, dict):
                store.pop(session_id, None)
            elif isinstance(store, list):
                setattr(
                    self,
                    attr,
                    [r for r in store if getattr(r, "session_id", None) != session_id],
                )
        return existed

    def mutate_session_state(self, session_id: str, mutate: Any) -> Any:
        """Read-modify-write, mirroring the durable repository's primitive.

        The fake needs it because the tools now use it on the write path, not
        only on the poll: a take is written into the session before its job row
        exists, and a fake without this would exercise an ordering production
        does not have.
        """

        session = self.sessions[session_id]
        updated = mutate(dict(session.state_json or {}))
        if updated is None:
            return session
        return self.update_session(session_id, state_json=updated)

    def update_session(self, session_id: str, **kwargs: Any) -> AgenticAudioSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        updated = replace(
            session,
            phase=kwargs.get("phase", session.phase),
            status=kwargs.get("status", session.status),
            selected_candidate_id=kwargs.get(
                "selected_candidate_id",
                session.selected_candidate_id,
            ),
            linked_job_ids=kwargs.get("linked_job_ids", session.linked_job_ids),
            state_json=kwargs.get("state_json", session.state_json),
            updated_at=_now(),
            finished_at=_now() if kwargs.get("finished") else session.finished_at,
        )
        self.sessions[session_id] = updated
        return updated

    def append_message(self, **kwargs: Any) -> AgenticAudioMessage:
        message = AgenticAudioMessage(
            message_id=kwargs.get("message_id") or new_id("agent_msg"),
            session_id=kwargs["session_id"],
            role=kwargs["role"],
            content=kwargs["content"],
            payload_json=kwargs.get("payload_json") or {},
            created_at=_now(),
        )
        self.messages.append(message)
        return message

    def list_messages(self, session_id: str) -> list[AgenticAudioMessage]:
        return [message for message in self.messages if message.session_id == session_id]

    def record_tool_call(self, **kwargs: Any) -> AgenticAudioToolCall:
        tool_call = AgenticAudioToolCall(
            tool_call_id=kwargs.get("tool_call_id") or new_id("agent_tool"),
            session_id=kwargs["session_id"],
            tool_name=kwargs["tool_name"],
            status=kwargs["status"],
            input_json=kwargs.get("input_json") or {},
            output_json=kwargs.get("output_json"),
            error_json=kwargs.get("error_json"),
            linked_job_id=kwargs.get("linked_job_id"),
            linked_artifact_ids=kwargs.get("linked_artifact_ids") or [],
            created_at=_now(),
            updated_at=_now(),
            finished_at=_now() if kwargs.get("finished") else None,
        )
        self.tool_calls.append(tool_call)
        return tool_call

    def list_tool_calls(self, session_id: str) -> list[AgenticAudioToolCall]:
        return [tool_call for tool_call in self.tool_calls if tool_call.session_id == session_id]

    def record_choice(self, **kwargs: Any) -> AgenticAudioChoice:
        choice = AgenticAudioChoice(
            choice_id=kwargs.get("choice_id") or new_id("agent_choice"),
            session_id=kwargs["session_id"],
            choice_type=kwargs["choice_type"],
            target_id=kwargs["target_id"],
            payload_json=kwargs.get("payload_json") or {},
            created_at=_now(),
        )
        self.choices.append(choice)
        return choice

    def list_choices(self, session_id: str) -> list[AgenticAudioChoice]:
        return [choice for choice in self.choices if choice.session_id == session_id]

    def build_snapshot(self, session_id: str) -> AgenticAudioSessionSnapshot:
        session = self.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        return AgenticAudioSessionSnapshot(
            session_id=session.session_id,
            source_video_artifact_id=session.source_video_artifact_id,
            status=session.status,
            phase=session.phase,
            creator_user_id=session.creator_user_id,
            selected_candidate_id=session.selected_candidate_id,
            linked_job_ids=session.linked_job_ids,
            state=session.state_json,
            messages=[
                {
                    "message_id": message.message_id,
                    "role": message.role,
                    "content": message.content,
                    "payload": message.payload_json,
                    "created_at": message.created_at.isoformat() if message.created_at else None,
                }
                for message in self.list_messages(session_id)
            ],
            tool_calls=[
                {
                    "tool_call_id": tool_call.tool_call_id,
                    "tool_name": tool_call.tool_name,
                    "status": tool_call.status,
                    "input": tool_call.input_json,
                    "output": tool_call.output_json,
                    "error": tool_call.error_json,
                    "linked_job_id": tool_call.linked_job_id,
                    "linked_artifact_ids": tool_call.linked_artifact_ids,
                    "created_at": tool_call.created_at.isoformat()
                    if tool_call.created_at
                    else None,
                    "finished_at": tool_call.finished_at.isoformat()
                    if tool_call.finished_at
                    else None,
                }
                for tool_call in self.list_tool_calls(session_id)
            ],
            choices=[
                {
                    "choice_id": choice.choice_id,
                    "choice_type": choice.choice_type,
                    "target_id": choice.target_id,
                    "payload": choice.payload_json,
                    "created_at": choice.created_at.isoformat() if choice.created_at else None,
                }
                for choice in self.list_choices(session_id)
            ],
        )


class _MemoryAsyncRepository:
    def __init__(self) -> None:
        self.jobs: dict[str, AsyncV2Job] = {}
        self._runners: dict[str, str] = {}
        self.artifacts: dict[str, AsyncV2Artifact] = {}
        self.events: list[AsyncV2JobEvent] = []
        self.stage_runs: dict[str, AsyncV2StageRun] = {}

    @contextmanager
    def transaction(self):
        yield self

    def create_job(self, **kwargs: Any) -> AsyncV2Job:
        job = AsyncV2Job(
            job_id=kwargs.get("job_id") or new_id("job"),
            session_id=kwargs.get("session_id"),
            creator_user_id=kwargs.get("creator_user_id"),
            actor_user_id=(
                kwargs.get("actor_user_id") or kwargs.get("creator_user_id")
            ),
            job_type=kwargs["job_type"],
            status=kwargs.get("status") or JobStatus.QUEUED,
            request_json=kwargs["request_json"],
            priority=kwargs.get("priority") or 0,
            created_at=_now(),
            updated_at=_now(),
        )
        self.jobs[job.job_id] = job
        return job

    def get_job(self, job_id: str) -> AsyncV2Job | None:
        return self.jobs.get(job_id)

    def update_job_status(self, job_id: str, **kwargs: Any) -> AsyncV2Job:
        job = self.jobs[job_id]
        updated = replace(
            job,
            status=kwargs.get("status", job.status),
            current_stage=kwargs.get("current_stage", job.current_stage),
            progress_percent=kwargs.get("progress_percent", job.progress_percent),
            result_json=kwargs.get("result_json", job.result_json),
            error_json=kwargs.get("error_json", job.error_json),
            updated_at=_now(),
            finished_at=_now() if kwargs.get("finished") else job.finished_at,
        )
        self.jobs[job_id] = updated
        return updated

    def update_job_status_checked(self, job_id: str, **kwargs: Any) -> tuple[AsyncV2Job, bool]:
        job = self.jobs[job_id]
        if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED):
            return job, False
        return self.update_job_status(job_id, **kwargs), True

    # ---- the completer's half -------------------------------------------
    #
    # The dev server's in-process completer talks to whichever repository it
    # was handed, so these exist here with the same names and the same meanings
    # they have on the durable store. Without them there would be two
    # completers — one for laptops and one for the deployment — and the one
    # nobody runs locally is the one that breaks.

    def list_open_jobs(
        self, *, limit: int = 200, exclude_job_types: Sequence[str] = ()
    ) -> list[AsyncV2Job]:
        excluded = set(exclude_job_types)
        open_jobs = [
            job
            for job in self.jobs.values()
            if job.status not in _TERMINAL_JOB_STATUSES
            and job.job_type not in excluded
        ]
        return open_jobs[: int(limit)]

    def claim_job(
        self,
        job_id: str,
        *,
        runner_id: str,
        on_claim: Any = None,
        client: Any = None,
    ) -> AsyncV2Job | None:
        """Queued → processing, once. Returns None if it was not queued.

        ``on_claim`` mirrors the durable store's hook INCLUDING its fail-closed
        behaviour: a hook that raises leaves the job queued. Without that, the
        browser suite and every in-memory test would exercise a claim that
        meters differently from the one production runs.
        """
        job = self.jobs.get(job_id)
        if job is None or job.status != JobStatus.QUEUED:
            return None
        claimed = replace(job, status=JobStatus.PROCESSING, updated_at=_now())
        if on_claim is not None:
            on_claim(claimed, client=client)
        self.jobs[job_id] = claimed
        self._runners[job_id] = runner_id
        return claimed

    def touch_heartbeat(self, job_ids: Sequence[str], *, runner_id: str) -> int:
        """A no-op that counts, because nothing outlives this process."""
        return sum(1 for job_id in job_ids if self._runners.get(job_id) == runner_id)

    def fail_interrupted(self, *, runner_id: str, **_ignored: Any) -> dict[str, list[str]]:
        """Nothing to reconcile: an in-memory store starts empty every time."""
        del runner_id
        return {"interrupted": [], "abandoned": []}

    def add_artifact(self, **kwargs: Any) -> AsyncV2Artifact:
        artifact = AsyncV2Artifact(
            artifact_id=kwargs.get("artifact_id") or new_id("artifact"),
            job_id=kwargs["job_id"],
            artifact_type=kwargs["artifact_type"],
            role=kwargs.get("role"),
            container=kwargs.get("container"),
            blob_name=kwargs.get("blob_name"),
            url=kwargs.get("url"),
            content_type=kwargs.get("content_type"),
            local_path=kwargs.get("local_path"),
            metadata_json=kwargs.get("metadata_json") or {},
            payload_json=kwargs.get("payload_json"),
            created_at=_now(),
        )
        self.artifacts[artifact.artifact_id] = artifact
        return artifact

    def session_media_refs(self, session_id: str) -> list[tuple[str, str]]:
        """Mirror of the durable store's query, so tests exercise the real shape."""

        job_ids = {
            job.job_id
            for job in self.jobs.values()
            if getattr(job, "session_id", None) == session_id
        }
        refs: list[tuple[str, str]] = []
        for artifact in self.artifacts.values():
            if artifact.job_id not in job_ids:
                continue
            if not (artifact.container and artifact.blob_name):
                continue
            ref = (str(artifact.container), str(artifact.blob_name))
            if ref not in refs:
                refs.append(ref)
        return refs

    def get_artifact(self, artifact_id: str) -> AsyncV2Artifact | None:
        return self.artifacts.get(artifact_id)

    def list_artifacts(self, job_id: str) -> list[AsyncV2Artifact]:
        return [artifact for artifact in self.artifacts.values() if artifact.job_id == job_id]

    def add_event(self, **kwargs: Any) -> AsyncV2JobEvent:
        event = AsyncV2JobEvent(
            event_id=kwargs.get("event_id") or new_id("event"),
            job_id=kwargs["job_id"],
            event_type=kwargs["event_type"],
            stage_name=kwargs.get("stage_name"),
            message=kwargs.get("message"),
            payload_json=kwargs.get("payload_json") or {},
            created_at=_now(),
        )
        self.events.append(event)
        return event

    def list_events(self, job_id: str, *, limit: int | None = None) -> list[AsyncV2JobEvent]:
        events = [event for event in self.events if event.job_id == job_id]
        return events if limit is None else events[:limit]

    def start_stage_run(self, **kwargs: Any) -> AsyncV2StageRun:
        stage_run = AsyncV2StageRun(
            stage_run_id=kwargs.get("stage_run_id") or new_id("stage_run"),
            job_id=kwargs["job_id"],
            stage_name=kwargs["stage_name"],
            status=StageStatus.STARTED,
            task_id=kwargs.get("task_id"),
            attempt=kwargs.get("attempt") or 1,
            input_json=kwargs.get("input_json") or {},
            started_at=_now(),
            heartbeat_at=_now(),
        )
        self.stage_runs[stage_run.stage_run_id] = stage_run
        return stage_run

    def update_stage_run(self, stage_run_id: str, **kwargs: Any) -> AsyncV2StageRun:
        stage_run = self.stage_runs[stage_run_id]
        updated = replace(
            stage_run,
            status=kwargs.get("status", stage_run.status),
            output_json=kwargs.get("output_json", stage_run.output_json),
            error_json=kwargs.get("error_json", stage_run.error_json),
            heartbeat_at=_now() if kwargs.get("heartbeat") else stage_run.heartbeat_at,
            finished_at=_now() if kwargs.get("finished") else stage_run.finished_at,
        )
        self.stage_runs[stage_run_id] = updated
        return updated

    def build_status_view(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        artifacts = self.list_artifacts(job_id)
        stages = [stage for stage in self.stage_runs.values() if stage.job_id == job_id]
        return {
            "job_id": job.job_id,
            "job_type": job.job_type,
            "status": job.status,
            "current_stage": job.current_stage,
            "progress_percent": job.progress_percent,
            "priority": job.priority,
            "request": job.request_json,
            "result": job.result_json,
            "error": job.error_json,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "stages": [
                {
                    "stage_run_id": stage.stage_run_id,
                    "stage_name": stage.stage_name,
                    "status": stage.status,
                    "task_id": stage.task_id,
                    "attempt": stage.attempt,
                    "input": stage.input_json,
                    "output": stage.output_json,
                    "error": stage.error_json,
                    "started_at": stage.started_at.isoformat()
                    if stage.started_at
                    else None,
                    "finished_at": stage.finished_at.isoformat()
                    if stage.finished_at
                    else None,
                }
                for stage in stages
            ],
            "artifacts": [
                {
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "role": artifact.role,
                    "container": artifact.container,
                    "blob_name": artifact.blob_name,
                    "url": artifact.url,
                    "content_type": artifact.content_type,
                    "metadata": artifact.metadata_json,
                    "payload_available": artifact.payload_json is not None,
                    "created_at": artifact.created_at.isoformat()
                    if artifact.created_at
                    else None,
                }
                for artifact in artifacts
            ],
        }


class _MemoryQueue:
    def __init__(self) -> None:
        self.envelopes: list[TaskEnvelope] = []
        self.tasks: dict[str, AsyncV2Task] = {}

    def enqueue(self, envelope: TaskEnvelope) -> str:
        self.envelopes.append(envelope)
        self.tasks[envelope.task_id] = AsyncV2Task(
            task_id=envelope.task_id,
            job_id=envelope.job_id,
            queue_name=envelope.queue_name,
            task_type=envelope.task_type,
            status=TaskStatus.QUEUED,
            payload_json=envelope.payload_json,
            priority=envelope.priority,
            max_attempts=envelope.max_attempts,
            idempotency_key=envelope.idempotency_key,
            not_before=envelope.not_before,
            created_at=_now(),
            updated_at=_now(),
        )
        return envelope.task_id

    def lease(
        self,
        *,
        queue_name: str,
        worker_id: str,
        lease_seconds: int,
    ) -> AsyncV2Task | None:
        del lease_seconds
        candidates = [
            task
            for task in self.tasks.values()
            if task.queue_name == queue_name and task.status == TaskStatus.QUEUED
        ]
        if not candidates:
            return None
        task = sorted(candidates, key=lambda item: (-item.priority, item.created_at or _now()))[0]
        leased = replace(
            task,
            status=TaskStatus.LEASED,
            lease_owner=worker_id,
            attempt=task.attempt + 1,
            updated_at=_now(),
        )
        self.tasks[leased.task_id] = leased
        return leased

    def heartbeat(
        self,
        *,
        task_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> AsyncV2Task:
        del lease_seconds
        task = self.tasks[task_id]
        if task.lease_owner != worker_id or task.status != TaskStatus.LEASED:
            raise KeyError(task_id)
        updated = replace(task, updated_at=_now())
        self.tasks[task_id] = updated
        return updated

    def complete(self, *, task_id: str, worker_id: str, **_: Any) -> AsyncV2Task:
        task = self.tasks[task_id]
        if task.lease_owner != worker_id or task.status != TaskStatus.LEASED:
            raise KeyError(task_id)
        completed = replace(
            task,
            status=TaskStatus.COMPLETED,
            lease_owner=None,
            lease_until=None,
            updated_at=_now(),
            finished_at=_now(),
        )
        self.tasks[task_id] = completed
        return completed

    def fail(
        self,
        *,
        task_id: str,
        worker_id: str,
        error: dict[str, Any],
        retry: bool,
        backoff_seconds: int = 0,
    ) -> AsyncV2Task:
        del backoff_seconds
        task = self.tasks[task_id]
        if task.lease_owner != worker_id or task.status != TaskStatus.LEASED:
            raise KeyError(task_id)
        status = TaskStatus.QUEUED if retry and task.attempt < task.max_attempts else TaskStatus.FAILED
        failed = replace(
            task,
            status=status,
            lease_owner=None,
            lease_until=None,
            last_error_json=error,
            updated_at=_now(),
            finished_at=None if status == TaskStatus.QUEUED else _now(),
        )
        self.tasks[task_id] = failed
        return failed

    def cancel_job_tasks(self, *, job_id: str) -> int:
        canceled = 0
        for task in list(self.tasks.values()):
            if task.job_id != job_id or task.status not in {TaskStatus.QUEUED, TaskStatus.LEASED}:
                continue
            self.tasks[task.task_id] = replace(
                task,
                status=TaskStatus.CANCELED,
                lease_owner=None,
                lease_until=None,
                updated_at=_now(),
                finished_at=_now(),
            )
            canceled += 1
        return canceled


def _seed_source_video(repo: _MemoryAsyncRepository) -> AsyncV2Artifact:
    repo.create_job(
        job_id="asset_job_source",
        job_type="asset_staging",
        request_json={"source": "upload"},
        status=JobStatus.COMPLETED,
    )
    return repo.add_artifact(
        artifact_id="artifact_source_video",
        job_id="asset_job_source",
        artifact_type="source_video",
        role="input",
        container="user-uploads",
        blob_name="source/source.mp4",
        url="https://cdn.test/source.mp4",
        content_type="video/mp4",
        metadata_json={
            "duration": 12.5,
            "width": 360,
            "height": 360,
            "source_kind": "upload",
        },
    )


async def _fake_remix(
    *,
    candidate: dict[str, Any],
    source_video_artifact_id: str,
    music_volume: float,
    preserve_original_audio: bool,
    music_envelope: Any = None,
) -> dict[str, Any]:
    del source_video_artifact_id
    return {
        "status": "completed",
        "remixed_video_url": (
            f"https://storage.test/remix/{candidate.get('candidate_id')}.mp4?sig=fake"
        ),
        "music_volume": music_volume,
        "preserve_original_audio": preserve_original_audio,
    }
