"""The studio's render jobs, in the database that already holds its sessions.

Until this module existed the standalone deployment kept its jobs in a Python
dict. Sessions were durable and the work they pointed at was not, so a restart
— which on a min-replicas-0 footprint is every scale-from-zero, not an incident
— left each session holding a ``linked_job_id`` that existed nowhere. The UI
renders that as a spinner with nothing behind it, on a render the customer has
already paid for.

Three things this store has to get right, and each is a decision rather than a
detail:

**It is not the fleet's table.** The rows are the same dataclasses the queued
worker fleet uses, but the tables are the studio's own. Both can be pointed at
one database, and the studio's completer claims any unfinished job it can see —
so a shared table would mean a studio replica picking up a fleet job and
finishing it in-process with its own result. An un-namespaced queue has already
caused exactly that here. Separate table names make it impossible instead of
unlikely.

**A claim is a write, not an intention.** :meth:`claim_job` moves a row from
``queued`` to ``processing`` atomically and stamps who took it. That is what
makes ``queued`` mean *nothing has been spent yet* — before this, a job stayed
``queued`` for the whole render, so a row found after a restart was
indistinguishable from one that had been generating for two minutes.

**An interrupted render is never silently re-run.** The process that was
generating may have completed its provider call before it died; the money is
gone either way. :meth:`fail_interrupted` marks such rows failed with something
the user can act on. Only rows that were still genuinely waiting — ``queued``,
and young enough that finishing them is still what the user wants — are left
for the completer to pick up.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence

from EdennCode.Deployment.async_pipeline_v2.models import (
    AsyncV2Artifact,
    AsyncV2Job,
    AsyncV2JobEvent,
    JobStatus,
    new_id,
)
from EdennCode.Deployment.async_pipeline_v2.repositories import (
    AsyncPipelineV2Repository as _Rows,
)
from EdennCode.Deployment.postgres_wrapper import PostgresClient

from .pool import pooled_client


MIGRATION_PATH = Path(__file__).resolve().parent / "migrations" / "005_jobs.sql"

#: A job in one of these states is finished from the session's point of view.
#: Nothing may transition it afterwards, and nothing may dispatch it again — a
#: failed generation is something to retry deliberately, never on a timer.
TERMINAL_JOB_STATUSES: tuple[str, ...] = (
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.CANCELED,
)

#: How long a claimed job may go without a heartbeat before another process is
#: allowed to call it abandoned. Generously longer than the heartbeat interval:
#: the cost of being wrong here is telling a user their render died while it is
#: still running.
DEFAULT_STALE_AFTER_S = 300.0

#: How old a still-``queued`` job may be and still be worth running. A job that
#: has been waiting since yesterday belongs to a session the user has walked
#: away from; starting it on the next boot would be a surprise result and a
#: surprise charge.
DEFAULT_QUEUED_MAX_AGE_S = 3600.0



def _status_literal(statuses: tuple[str, ...]) -> str:
    """``('completed', 'failed', 'canceled')`` as SQL text, not as a parameter.

    The completer's poll has to match the partial index that covers unfinished
    rows, and Postgres cannot prove a parameterised predicate implies the
    index's own — so a bound parameter here quietly turns a twice-a-second
    indexed read into a sequential scan of every job the deployment has ever
    run. These values are a module constant, never input, and the guard below
    is what keeps it that way: anything but a bare lowercase word refuses to
    build rather than being interpolated.
    """
    for status in statuses:
        if not status.isalpha() or not status.islower():
            raise ValueError(f"not a status literal: {status!r}")
    return "(" + ", ".join(f"'{status}'" for status in statuses) + ")"


#: Must stay character-identical to the index predicate in 005_jobs.sql. A test
#: pins the two together, because drifting apart costs nothing at write time and
#: silently loses the index at read time.
OPEN_STATUS_PREDICATE = f"status NOT IN {_status_literal(TERMINAL_JOB_STATUSES)}"

_INTERRUPTED_MESSAGE = (
    "This render was interrupted when the service restarted. It was already "
    "running, so it is not started again automatically — generate it again "
    "when you are ready."
)

_ABANDONED_MESSAGE = (
    "This render was never started before the service restarted, and it has "
    "been waiting too long to start it now. Ask for it again when you are ready."
)


class _Unset:
    """Sentinel: 'this field was not mentioned', distinct from ``None``."""


UNSET: Any = _Unset()


class DurableJobRepository:
    """Postgres-backed job rows with the in-memory repository's exact surface.

    Every method here is a drop-in for the one on the studio's in-memory fake,
    so the tools and the dev server do not know which one they hold. The extra
    methods — :meth:`claim_job`, :meth:`list_open_jobs`, :meth:`touch_heartbeat`
    and :meth:`fail_interrupted` — are what the in-process completer needs once
    the job table outlives the process, and the fake implements them too so
    there is one completer rather than two.
    """

    def __init__(
        self,
        *,
        client_factory: Callable[[], PostgresClient] = pooled_client,
    ) -> None:
        self._client_factory = client_factory
        self._ensure_lock = threading.Lock()
        self._schema_ready = False

    # ---- schema ---------------------------------------------------------

    def ensure_schema(self) -> None:
        """Apply any migration this database has not seen, once, under a lock."""
        if self._schema_ready:
            return
        with self._ensure_lock:
            if self._schema_ready:
                return
            from .migrator import apply_all, discover

            apply_all(self._client_factory, discover(MIGRATION_PATH.parent))
            self._schema_ready = True

    @contextmanager
    def _client_context(
        self, client: Optional[PostgresClient] = None
    ) -> Iterator[PostgresClient]:
        if client is not None:
            yield client
            return
        with self._client_factory() as created:
            yield created

    @contextmanager
    def transaction(self) -> Iterator[PostgresClient]:
        """A Postgres transaction, for the rare multi-row write."""
        with self._client_factory() as client:
            with client.transaction() as tx:
                yield tx

    @staticmethod
    def _one(rows: Any) -> Optional[dict[str, Any]]:
        return rows[0] if isinstance(rows, list) and rows else None

    # ---- jobs -----------------------------------------------------------

    def create_job(
        self,
        *,
        job_id: Optional[str] = None,
        job_type: str,
        request_json: dict[str, Any],
        session_id: Optional[str] = None,
        creator_user_id: Optional[str] = None,
        actor_user_id: Optional[str] = None,
        priority: int = 0,
        status: str = JobStatus.QUEUED,
    ) -> AsyncV2Job:
        """Insert a job, or return the one already under that id.

        Creating over an existing id does **not** overwrite it. The in-memory
        fake replaces the row, which is harmless for a dict that dies with the
        process and destructive for a table that does not: the dev server
        re-seeds a fixed demo job id on every boot, and the seeded row may by
        then be a finished render with a result somebody is looking at.
        """
        self.ensure_schema()
        actual_job_id = job_id or new_id("job")
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                INSERT INTO agentic_audio_jobs (
                    job_id, session_id, creator_user_id, actor_user_id, job_type,
                    status, priority, request_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (job_id) DO NOTHING
                RETURNING *
                """,
                params=[
                    actual_job_id,
                    session_id,
                    creator_user_id,
                    # Who pressed the button, which is not always whose session
                    # it is: a collaborator invited to iterate spends too.
                    actor_user_id or creator_user_id,
                    job_type,
                    status,
                    priority,
                    request_json,
                ],
            )
            job = _Rows._job_from_row(self._one(rows))
            if job is not None:
                return job
            rows = client.run_sql(
                "SELECT * FROM agentic_audio_jobs WHERE job_id = %s LIMIT 1",
                params=[actual_job_id],
            )
        existing = _Rows._job_from_row(self._one(rows))
        if existing is None:
            raise KeyError(actual_job_id)
        return existing

    def get_job(self, job_id: str) -> Optional[AsyncV2Job]:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                "SELECT * FROM agentic_audio_jobs WHERE job_id = %s LIMIT 1",
                params=[job_id],
            )
        return _Rows._job_from_row(self._one(rows))

    def update_job_status(
        self,
        job_id: str,
        *,
        status: str,
        current_stage: Optional[str] = None,
        progress_percent: Optional[float] = None,
        result_json: Any = UNSET,
        error_json: Any = UNSET,
        finished: bool = False,
        client: Optional[PostgresClient] = None,
    ) -> AsyncV2Job:
        job, _applied = self.update_job_status_checked(
            job_id,
            status=status,
            current_stage=current_stage,
            progress_percent=progress_percent,
            result_json=result_json,
            error_json=error_json,
            finished=finished,
            client=client,
        )
        return job

    def update_job_status_checked(
        self,
        job_id: str,
        *,
        status: str,
        current_stage: Optional[str] = None,
        progress_percent: Optional[float] = None,
        result_json: Any = UNSET,
        error_json: Any = UNSET,
        finished: bool = False,
        client: Optional[PostgresClient] = None,
    ) -> tuple[AsyncV2Job, bool]:
        """Write a status unless the job is already terminal.

        Returns ``(job, applied)``. A field left at :data:`UNSET` is not
        mentioned in the write, which is how the in-memory fake behaves and is
        the safe default: a status write that happens not to carry a result must
        not erase the result a previous write recorded.

        A job reaching a terminal status is finished here too — ``finished_at``
        is stamped and the runner stamp is cleared, so the reconcile pass on the
        next boot cannot mistake a completed render for an abandoned one.
        """
        self.ensure_schema()
        terminal = status in TERMINAL_JOB_STATUSES
        with self._client_context(client) as active:
            rows = active.run_sql(
                """
                UPDATE agentic_audio_jobs
                SET status = %s,
                    current_stage = COALESCE(%s, current_stage),
                    progress_percent = COALESCE(%s, progress_percent),
                    result_json = CASE WHEN %s THEN %s ELSE result_json END,
                    error_json = CASE WHEN %s THEN %s ELSE error_json END,
                    runner_id = CASE WHEN %s THEN NULL ELSE runner_id END,
                    updated_at = now(),
                    finished_at = CASE WHEN %s THEN now() ELSE finished_at END
                WHERE job_id = %s
                  AND status NOT IN %s
                RETURNING *
                """,
                params=[
                    status,
                    current_stage,
                    progress_percent,
                    not isinstance(result_json, _Unset),
                    None if isinstance(result_json, _Unset) else result_json,
                    not isinstance(error_json, _Unset),
                    None if isinstance(error_json, _Unset) else error_json,
                    terminal,
                    bool(finished) or terminal,
                    job_id,
                    TERMINAL_JOB_STATUSES,
                ],
            )
            job = _Rows._job_from_row(self._one(rows))
            if job is not None:
                return job, True
            # Nothing updated: the job is already terminal, or it is not there.
            rows = active.run_sql(
                "SELECT * FROM agentic_audio_jobs WHERE job_id = %s LIMIT 1",
                params=[job_id],
            )
        current = _Rows._job_from_row(self._one(rows))
        if current is None:
            raise KeyError(job_id)
        return current, False

    # ---- the in-process completer's half --------------------------------

    def list_open_jobs(
        self,
        *,
        limit: int = 200,
        exclude_job_types: Sequence[str] = (),
    ) -> list[AsyncV2Job]:
        """Jobs that are not finished, oldest first.

        Bounded on purpose. This is polled on a short timer for the life of the
        process, so it reads a partial index over unfinished rows only — the
        finished ones accumulate forever and must never be walked.
        """
        self.ensure_schema()
        clause = ""
        params: list[Any] = []
        if exclude_job_types:
            clause = "AND job_type NOT IN %s"
            params.append(tuple(exclude_job_types))
        params.append(int(limit))
        with self._client_factory() as client:
            rows = client.run_sql(
                f"""
                SELECT * FROM agentic_audio_jobs
                WHERE {OPEN_STATUS_PREDICATE}
                {clause}
                ORDER BY priority DESC, created_at
                LIMIT %s
                """,
                params=params,
            )
        return [
            job
            for row in (rows if isinstance(rows, list) else [])
            if (job := _Rows._job_from_row(row)) is not None
        ]

    def claim_job(
        self,
        job_id: str,
        *,
        runner_id: str,
        on_claim: Optional[Callable[..., None]] = None,
        client: Optional[PostgresClient] = None,
    ) -> Optional[AsyncV2Job]:
        """Take a queued job for this process, or return ``None``.

        The transition ``queued → processing`` is the claim, and it is a single
        conditional update so two replicas racing for the same row produce one
        winner and one ``None``. Everything that spends provider credit happens
        after this returns a row, which is what lets a later reconcile pass tell
        a render that had started from one that had not.

        ``on_claim`` runs INSIDE the claim's transaction, holding the whole row.
        That is what makes anything hung off the claim fail closed: if the hook
        raises, the claim rolls back, the job stays queued, and nothing spends.
        The usage meter is the first such hook — a spend nobody recorded is
        worse than a render that waits for the next poll.
        """
        self.ensure_schema()
        with self._client_context(client) as active:
            with active.transaction() as tx:
                rows = tx.run_sql(
                    """
                    UPDATE agentic_audio_jobs
                    SET status = %s,
                        runner_id = %s,
                        heartbeat_at = now(),
                        updated_at = now()
                    WHERE job_id = %s
                      AND status = %s
                    RETURNING *
                    """,
                    params=[JobStatus.PROCESSING, runner_id, job_id, JobStatus.QUEUED],
                )
                job = _Rows._job_from_row(self._one(rows))
                if job is not None and on_claim is not None:
                    on_claim(job, client=tx)
        return job

    def touch_heartbeat(self, job_ids: Sequence[str], *, runner_id: str) -> int:
        """Say that this process is still working on these jobs.

        Silence is what marks a render abandoned, so a long generation must keep
        speaking. Only rows this process actually claimed are touched.
        """
        # A tuple, not a list: the client adapts any list parameter to JSONB,
        # so `= ANY(%s)` would quietly compare a text column against a JSON
        # document. `IN %s` over a tuple is the form that survives that.
        ids = tuple(str(j) for j in job_ids)
        if not ids:
            return 0
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                UPDATE agentic_audio_jobs
                SET heartbeat_at = now()
                WHERE job_id IN %s
                  AND runner_id = %s
                  AND status NOT IN %s
                RETURNING job_id
                """,
                params=[ids, runner_id, TERMINAL_JOB_STATUSES],
            )
        return len(rows) if isinstance(rows, list) else 0

    def fail_interrupted(
        self,
        *,
        runner_id: str,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
        queued_max_age_s: float = DEFAULT_QUEUED_MAX_AGE_S,
    ) -> dict[str, list[str]]:
        """Tell the truth about work no live process is doing.

        Two different lies get corrected here, and they are not the same lie:

        ``interrupted`` — rows that were claimed and then went quiet. The
        provider call may have completed and been billed, so these are marked
        failed and **never** re-dispatched. Re-running them is how one paid
        render becomes two.

        ``abandoned`` — rows still queued from long enough ago that finishing
        them would surprise rather than serve. Nothing was spent on these; they
        are failed for honesty, not for money.

        A queued row younger than ``queued_max_age_s`` is deliberately left
        alone: it is work the user asked for and nothing has been spent on it,
        so the completer should simply pick it up and run it.

        Rows claimed by *this* process are never touched, and neither is a row
        whose heartbeat is recent — that is a sibling replica mid-render, and
        calling its work dead would be the same lie in the other direction.

        Returns the job ids in each category, so the caller can log what it did
        rather than a count of things nobody can look up.
        """
        self.ensure_schema()
        with self._client_factory() as client:
            interrupted = client.run_sql(
                """
                UPDATE agentic_audio_jobs
                SET status = %s,
                    result_json = %s,
                    runner_id = NULL,
                    updated_at = now(),
                    finished_at = now()
                WHERE status NOT IN %s
                  AND status <> %s
                  AND (runner_id IS NULL OR runner_id <> %s)
                  AND (heartbeat_at IS NULL
                       OR heartbeat_at < now() - make_interval(secs => %s))
                RETURNING job_id
                """,
                params=[
                    JobStatus.FAILED,
                    {"status": "failed", "error": _INTERRUPTED_MESSAGE,
                     "interrupted": True, "placeholder": False},
                    TERMINAL_JOB_STATUSES,
                    JobStatus.QUEUED,
                    runner_id,
                    float(stale_after_s),
                ],
            )
            abandoned = client.run_sql(
                """
                UPDATE agentic_audio_jobs
                SET status = %s,
                    result_json = %s,
                    runner_id = NULL,
                    updated_at = now(),
                    finished_at = now()
                WHERE status = %s
                  AND created_at < now() - make_interval(secs => %s)
                RETURNING job_id
                """,
                params=[
                    JobStatus.FAILED,
                    {"status": "failed", "error": _ABANDONED_MESSAGE,
                     "interrupted": True, "placeholder": False},
                    JobStatus.QUEUED,
                    float(queued_max_age_s),
                ],
            )
        return {
            "interrupted": [str(r["job_id"]) for r in (interrupted if isinstance(interrupted, list) else [])],
            "abandoned": [str(r["job_id"]) for r in (abandoned if isinstance(abandoned, list) else [])],
        }

    # ---- artifacts ------------------------------------------------------

    def session_media_refs(self, session_id: str) -> list[tuple[str, str]]:
        """Every stored object this session's renders produced, as (container, blob).

        Read BEFORE the session row goes: artifacts cascade from jobs and jobs
        cascade from the session, so a caller that deletes first has nothing
        left to tell it what to remove — which is exactly how a retention pass
        ends up reclaiming the database and leaving the customer's footage in a
        container forever.
        """

        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                SELECT a.container, a.blob_name
                FROM agentic_audio_job_artifacts a
                JOIN agentic_audio_jobs j ON j.job_id = a.job_id
                WHERE j.session_id = %s
                  AND a.container IS NOT NULL AND a.container <> ''
                  AND a.blob_name IS NOT NULL AND a.blob_name <> ''
                """,
                params=[session_id],
            )
        seen: list[tuple[str, str]] = []
        for row in rows if isinstance(rows, list) else []:
            ref = (str(row["container"]), str(row["blob_name"]))
            if ref not in seen:
                seen.append(ref)
        return seen

    def add_artifact(
        self,
        *,
        job_id: str,
        artifact_type: str,
        artifact_id: Optional[str] = None,
        role: Optional[str] = None,
        container: Optional[str] = None,
        blob_name: Optional[str] = None,
        url: Optional[str] = None,
        content_type: Optional[str] = None,
        local_path: Optional[str] = None,
        metadata_json: Optional[dict[str, Any]] = None,
        payload_json: Optional[dict[str, Any]] = None,
    ) -> AsyncV2Artifact:
        """Record an artifact, replacing any row already under that id.

        Unlike a job row, re-adding an artifact is a normal thing callers do:
        the dev server seeds a source video and then re-adds the same id once a
        real local file exists for it. The in-memory fake overwrites, so this
        overwrites, or the seeded stub would outlive the real clip.
        """
        self.ensure_schema()
        actual_artifact_id = artifact_id or new_id("artifact")
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                INSERT INTO agentic_audio_job_artifacts (
                    artifact_id, job_id, artifact_type, role, container,
                    blob_name, url, content_type, local_path, metadata_json,
                    payload_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (artifact_id) DO UPDATE SET
                    job_id = EXCLUDED.job_id,
                    artifact_type = EXCLUDED.artifact_type,
                    role = EXCLUDED.role,
                    container = EXCLUDED.container,
                    blob_name = EXCLUDED.blob_name,
                    url = EXCLUDED.url,
                    content_type = EXCLUDED.content_type,
                    local_path = EXCLUDED.local_path,
                    metadata_json = EXCLUDED.metadata_json,
                    payload_json = EXCLUDED.payload_json
                RETURNING *
                """,
                params=[
                    actual_artifact_id,
                    job_id,
                    artifact_type,
                    role,
                    container,
                    blob_name,
                    url,
                    content_type,
                    local_path,
                    metadata_json or {},
                    payload_json,
                ],
            )
        artifact = _Rows._artifact_from_row(self._one(rows))
        if artifact is None:
            raise KeyError(actual_artifact_id)
        return artifact

    def get_artifact(self, artifact_id: str) -> Optional[AsyncV2Artifact]:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                "SELECT * FROM agentic_audio_job_artifacts "
                "WHERE artifact_id = %s LIMIT 1",
                params=[artifact_id],
            )
        return _Rows._artifact_from_row(self._one(rows))

    def list_artifacts(self, job_id: str) -> list[AsyncV2Artifact]:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                SELECT * FROM agentic_audio_job_artifacts
                WHERE job_id = %s
                ORDER BY created_at, artifact_id
                """,
                params=[job_id],
            )
        return [
            artifact
            for row in (rows if isinstance(rows, list) else [])
            if (artifact := _Rows._artifact_from_row(row)) is not None
        ]

    # ---- events ---------------------------------------------------------

    def add_event(
        self,
        *,
        job_id: str,
        event_type: str,
        event_id: Optional[str] = None,
        stage_name: Optional[str] = None,
        message: Optional[str] = None,
        payload_json: Optional[dict[str, Any]] = None,
    ) -> AsyncV2JobEvent:
        self.ensure_schema()
        actual_event_id = event_id or new_id("event")
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                INSERT INTO agentic_audio_job_events (
                    event_id, job_id, event_type, stage_name, message, payload_json
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                params=[
                    actual_event_id,
                    job_id,
                    event_type,
                    stage_name,
                    message,
                    payload_json or {},
                ],
            )
        event = _Rows._event_from_row(self._one(rows))
        if event is None:
            raise KeyError(actual_event_id)
        return event

    def list_events(
        self, job_id: str, *, limit: Optional[int] = None
    ) -> list[AsyncV2JobEvent]:
        self.ensure_schema()
        params: list[Any] = [job_id]
        tail = ""
        if limit is not None:
            tail = "LIMIT %s"
            params.append(int(limit))
        with self._client_factory() as client:
            rows = client.run_sql(
                f"""
                SELECT * FROM agentic_audio_job_events
                WHERE job_id = %s
                ORDER BY created_at, event_id
                {tail}
                """,
                params=params,
            )
        return [
            event
            for row in (rows if isinstance(rows, list) else [])
            if (event := _Rows._event_from_row(row)) is not None
        ]

    # ---- read model -----------------------------------------------------

    def build_status_view(self, job_id: str) -> dict[str, Any]:
        """The job as a client sees it. Shaped exactly like the fleet's view.

        No stage runs: the studio completes a job in one in-process step, so
        the list is empty rather than absent — a caller that iterates it must
        not have to care which deployment answered.
        """
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        artifacts = self.list_artifacts(job_id)
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
            "stages": [],
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


__all__ = [
    "OPEN_STATUS_PREDICATE",
    "DEFAULT_QUEUED_MAX_AGE_S",
    "DEFAULT_STALE_AFTER_S",
    "TERMINAL_JOB_STATUSES",
    "UNSET",
    "DurableJobRepository",
]
