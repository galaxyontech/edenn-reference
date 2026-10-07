"""What the durable job store promises, checked without a database.

The behaviour that needs a real Postgres lives in ``test_durable_jobs.py`` and
skips without one. What is left here is everything that is decided in Python or
in the text of a statement, and it is not a small residue: the two traps this
change is most exposed to are both visible from here.

The first is a **parity** trap, which this branch has hit repeatedly in other
forms: a growth surface added to one implementation and not the other, failing
open. The dev server's completer talks to whichever store it was handed, so a
method the durable store has and the in-memory fake does not is a crash that
only ever happens in the deployment.

The second is a **parameter adaptation** trap. The database client turns any
list parameter into JSONB, so ``status NOT IN %s`` over a list silently compares
a text column against a JSON document. A tuple is the form that survives, and
nothing about the code reads differently — which is exactly why it is pinned
here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Optional

import pytest

from EdennCode.Deployment.async_pipeline_v2.models import JobStatus
from EdennCode.EdennAgent.AgenticAudio.persistence.jobs import (
    TERMINAL_JOB_STATUSES,
    DurableJobRepository,
)


_DEVSERVER = Path("EdennCode/EdennAgent/AgenticAudio/design/devserver.py")
_JOBS_MODULE = Path("EdennCode/EdennAgent/AgenticAudio/persistence/jobs.py")
_MIGRATION = Path(
    "EdennCode/EdennAgent/AgenticAudio/persistence/migrations/005_jobs.sql"
)


# ---- a client that records instead of connecting ------------------------


def _job_row(**over: Any) -> dict[str, Any]:
    row = {
        "job_id": "job_1",
        "session_id": None,
        "creator_user_id": None,
        "job_type": "video_music",
        "status": JobStatus.QUEUED,
        "current_stage": None,
        "progress_percent": 0.0,
        "priority": 0,
        "request_json": {},
        "result_json": None,
        "error_json": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "finished_at": None,
    }
    row.update(over)
    return row


class _Recorder:
    """Every statement and every parameter list, in order."""

    def __init__(self, replies: Optional[list[Any]] = None) -> None:
        self.calls: list[tuple[str, list[Any]]] = []
        self._replies = list(replies or [])

    def __enter__(self) -> "_Recorder":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def run_sql(self, statement: str, *, params: Any = None) -> Any:
        self.calls.append((statement, list(params or [])))
        return self._replies.pop(0) if self._replies else []

    @contextmanager
    def transaction(self) -> Any:
        """The claim runs in one, so the recorder has to offer one.

        Yields itself: the point of these tests is what SQL is sent and with
        what parameters, and a transaction that recorded separately would hide
        exactly the statement the claim now runs inside one.
        """

        yield self

    # convenience for the assertions below
    def statements(self) -> str:
        return "\n".join(stmt for stmt, _ in self.calls)


def _repo(replies: Optional[list[Any]] = None) -> tuple[DurableJobRepository, _Recorder]:
    recorder = _Recorder(replies)
    repo = DurableJobRepository(client_factory=lambda: recorder)
    # The migration runner is exercised in test_migrator.py against a real
    # database; here it would only add noise to the recorded statements.
    repo._schema_ready = True
    return repo, recorder


# ---- the adaptation trap ------------------------------------------------


def _list_params(recorder: _Recorder) -> list[Any]:
    return [p for _stmt, params in recorder.calls for p in params if isinstance(p, list)]


def test_the_terminal_set_is_a_tuple_everywhere_it_reaches_the_database() -> None:
    """A list parameter is adapted to JSONB and the comparison quietly stops
    meaning anything. Nothing about the code would look wrong."""
    assert isinstance(TERMINAL_JOB_STATUSES, tuple)

    repo, recorder = _repo([[_job_row()], [_job_row()], []])
    repo.list_open_jobs(exclude_job_types=("asset_staging",))
    repo.update_job_status("job_1", status=JobStatus.COMPLETED, result_json={})
    repo.touch_heartbeat(["job_1"], runner_id="runner_a")

    assert _list_params(recorder) == [], "a list parameter would be sent as JSONB"
    bound = [
        p for _stmt, params in recorder.calls for p in params
        if isinstance(p, tuple) and p == TERMINAL_JOB_STATUSES
    ]
    assert len(bound) == 2, (
        "the terminal filter reaches the statement as a tuple everywhere it is "
        "bound at all — the poll interpolates it instead, see the index test"
    )


def test_a_heartbeat_sends_its_job_ids_as_a_tuple() -> None:
    repo, recorder = _repo([[{"job_id": "job_1"}]])
    repo.touch_heartbeat(["job_1", "job_2"], runner_id="runner_a")

    assert _list_params(recorder) == []
    ids = [p for _s, params in recorder.calls for p in params
           if isinstance(p, tuple) and p and p[0].startswith("job_")]
    assert ids == [("job_1", "job_2")]


def test_an_empty_heartbeat_asks_the_database_nothing() -> None:
    repo, recorder = _repo()
    assert repo.touch_heartbeat([], runner_id="runner_a") == 0
    assert recorder.calls == []


def test_every_statement_binds_exactly_the_parameters_it_declares() -> None:
    """A ``%s`` with no parameter behind it is a runtime error at the database
    and nothing before it. The recorder cannot see that — it never formats the
    statement — so the arity is counted here, for every method that writes SQL.

    This is the one class of mistake that a store with no local database to run
    against would otherwise reach production with.
    """
    artifact_row = {
        "artifact_id": "a", "job_id": "job_1", "artifact_type": "source_video",
        "metadata_json": {}, "created_at": None,
    }
    event_row = {
        "event_id": "e", "job_id": "job_1", "event_type": "queued",
        "payload_json": {}, "created_at": None,
    }

    def drive(replies: list[Any], call) -> _Recorder:
        repo, recorder = _repo(replies)
        call(repo)
        assert recorder.calls, "the method wrote no SQL at all"
        return recorder

    recorders = [
        drive([[_job_row()]], lambda r: r.create_job(
            job_id="job_1", job_type="video_music", request_json={})),
        drive([[_job_row()]], lambda r: r.get_job("job_1")),
        drive([[_job_row()]], lambda r: r.update_job_status(
            "job_1", status=JobStatus.COMPLETED, result_json={"ok": 1},
            current_stage="mix", progress_percent=100.0, error_json=None)),
        drive([[_job_row()]], lambda r: r.list_open_jobs(
            exclude_job_types=("asset_staging",))),
        drive([[_job_row()]], lambda r: r.list_open_jobs()),
        drive([[_job_row()]], lambda r: r.claim_job("job_1", runner_id="runner_a")),
        drive([[{"job_id": "job_1"}]], lambda r: r.touch_heartbeat(
            ["job_1"], runner_id="runner_a")),
        drive([[], []], lambda r: r.fail_interrupted(runner_id="runner_a")),
        drive([[artifact_row]], lambda r: r.add_artifact(
            job_id="job_1", artifact_type="source_video", artifact_id="a",
            role="input", url="/u", local_path="/p", metadata_json={},
            payload_json=None, container="c", blob_name="b", content_type="video/mp4")),
        drive([[artifact_row]], lambda r: r.get_artifact("a")),
        drive([[artifact_row]], lambda r: r.list_artifacts("job_1")),
        drive([[event_row]], lambda r: r.add_event(job_id="job_1", event_type="queued")),
        drive([[event_row]], lambda r: r.list_events("job_1")),
        drive([[event_row]], lambda r: r.list_events("job_1", limit=10)),
    ]

    for recorder in recorders:
        for statement, params in recorder.calls:
            assert statement.count("%s") == len(params), (
                f"{statement.strip().splitlines()[0]}: "
                f"{statement.count('%s')} placeholders, {len(params)} parameters"
            )


# ---- the guards are in the statement, not in the caller -----------------


def test_a_claim_is_conditional_on_the_job_still_being_queued() -> None:
    """Read-then-write would let two replicas both see `queued` and both run."""
    repo, recorder = _repo([[_job_row(status=JobStatus.PROCESSING)]])
    repo.claim_job("job_1", runner_id="runner_a")

    assert len(recorder.calls) == 1, "a claim must be one statement, not two"
    statement, params = recorder.calls[0]
    assert "UPDATE" in statement
    assert "status = %s" in statement
    assert "AND status = %s" in statement
    assert JobStatus.PROCESSING in params and JobStatus.QUEUED in params
    assert "runner_id = %s" in statement and "heartbeat_at = now()" in statement


def test_a_status_write_refuses_to_move_a_finished_job() -> None:
    repo, recorder = _repo([[_job_row(status=JobStatus.COMPLETED)]])
    repo.update_job_status("job_1", status=JobStatus.COMPLETED, result_json={"ok": 1})

    statement, _params = recorder.calls[0]
    assert "AND status NOT IN %s" in statement


def test_an_unmentioned_result_is_not_written() -> None:
    """The sentinel is the whole point: ``None`` means *clear it*, absence means
    *leave it*, and collapsing the two erases delivered renders."""
    repo, recorder = _repo([[_job_row()]])
    repo.update_job_status("job_1", status=JobStatus.PROCESSING)

    statement, params = recorder.calls[0]
    assert "result_json = CASE WHEN %s THEN %s ELSE result_json END" in statement
    assert False in params, "the 'write the result' flag should be off"


def test_a_cleared_result_is_distinguishable_from_an_absent_one() -> None:
    repo, recorder = _repo([[_job_row()]])
    repo.update_job_status("job_1", status=JobStatus.PROCESSING, result_json=None)

    _statement, params = recorder.calls[0]
    assert True in params, "the 'write the result' flag should be on"


def test_reaching_a_terminal_status_stamps_and_unclaims_the_row() -> None:
    repo, recorder = _repo([[_job_row(status=JobStatus.COMPLETED)]])
    repo.update_job_status("job_1", status=JobStatus.COMPLETED, result_json={"ok": 1})

    statement, _params = recorder.calls[0]
    assert "runner_id = CASE WHEN %s THEN NULL ELSE runner_id END" in statement
    assert "finished_at = CASE WHEN %s THEN now() ELSE finished_at END" in statement


def test_creating_over_an_existing_job_id_preserves_the_row() -> None:
    repo, recorder = _repo([[], [_job_row(status=JobStatus.COMPLETED)]])
    job = repo.create_job(job_id="job_1", job_type="video_music", request_json={})

    assert "ON CONFLICT (job_id) DO NOTHING" in recorder.calls[0][0]
    assert job.status == JobStatus.COMPLETED


def test_adding_an_artifact_over_an_existing_id_replaces_it() -> None:
    repo, recorder = _repo([[{"artifact_id": "a", "job_id": "job_1",
                              "artifact_type": "source_video",
                              "metadata_json": {}, "created_at": None}]])
    repo.add_artifact(artifact_id="a", job_id="job_1", artifact_type="source_video")

    assert "ON CONFLICT (artifact_id) DO UPDATE SET" in recorder.calls[0][0]


def test_the_sweep_never_touches_a_job_that_never_started() -> None:
    """Two categories, two statements, and the first must skip `queued`.

    A queued row has cost nothing and is what the user asked for; folding it in
    with the interrupted renders would fail work that should simply be run.
    """
    repo, recorder = _repo([[], []])
    repo.fail_interrupted(runner_id="runner_new")

    assert len(recorder.calls) == 2
    interrupted, abandoned = recorder.calls[0][0], recorder.calls[1][0]
    assert "AND status <> %s" in interrupted
    assert JobStatus.QUEUED in recorder.calls[0][1]
    assert "heartbeat_at < now() - make_interval" in interrupted
    assert "runner_id IS NULL OR runner_id <> %s" in interrupted
    assert "WHERE status = %s" in abandoned
    assert "created_at < now() - make_interval" in abandoned


def test_the_sweep_marks_failed_and_never_re_queues() -> None:
    """Re-queueing an interrupted render is how one paid generation becomes two."""
    repo, recorder = _repo([[], []])
    repo.fail_interrupted(runner_id="runner_new")

    for _statement, params in recorder.calls:
        assert JobStatus.FAILED in params
        assert JobStatus.PROCESSING not in params
        result = next(p for p in params if isinstance(p, dict))
        assert result["status"] == "failed"
        assert result["interrupted"] is True
        assert result["placeholder"] is False, "a stand-in tone is not a render"


# ---- parity between the two stores --------------------------------------

_COMPLETER_SURFACE = (
    "list_open_jobs",
    "claim_job",
    "touch_heartbeat",
    "fail_interrupted",
)


@pytest.mark.parametrize("name", _COMPLETER_SURFACE)
def test_both_stores_answer_everything_the_completer_asks(name: str) -> None:
    """One completer, two stores. A method on only one of them is a crash that
    happens exclusively in the deployment nobody runs locally."""
    from EdennCode.EdennAgent.AgenticAudio.design.memory_fakes import (
        _MemoryAsyncRepository,
    )

    assert callable(getattr(DurableJobRepository, name, None))
    assert callable(getattr(_MemoryAsyncRepository, name, None))


def test_the_stores_agree_on_what_finished_means() -> None:
    from EdennCode.EdennAgent.AgenticAudio.design import memory_fakes

    assert memory_fakes._TERMINAL_JOB_STATUSES is TERMINAL_JOB_STATUSES
    assert set(TERMINAL_JOB_STATUSES) == {
        JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED
    }


def test_the_in_memory_store_claims_the_same_way() -> None:
    from EdennCode.EdennAgent.AgenticAudio.design.memory_fakes import (
        _MemoryAsyncRepository,
    )

    repo = _MemoryAsyncRepository()
    job = repo.create_job(job_type="video_music", request_json={})

    first = repo.claim_job(job.job_id, runner_id="runner_a")
    second = repo.claim_job(job.job_id, runner_id="runner_b")

    assert first is not None and first.status == JobStatus.PROCESSING
    assert second is None
    assert repo.list_open_jobs(exclude_job_types=("video_music",)) == []


# ---- the store is the studio's own --------------------------------------


def test_the_studio_does_not_borrow_the_fleets_tables() -> None:
    """Both can be pointed at one database, and the studio's completer claims
    any unfinished job it can see. Sharing a table would mean a studio replica
    picking up a fleet job and finishing it in-process — the same
    cross-deployment theft an un-namespaced queue has already caused here."""
    sql_lines = [
        line for line in _MIGRATION.read_text().splitlines()
        if not line.lstrip().startswith("--")
    ]
    # Comments may name the fleet's tables — saying why they are not used is
    # the point. Statements may not.
    for text in ("\n".join(sql_lines), _JOBS_MODULE.read_text()):
        for verb in ("FROM async_v2", "INTO async_v2", "UPDATE async_v2",
                     "TABLE async_v2", "ON async_v2"):
            assert verb not in text, f"the studio reads the fleet's rows: {verb}"
    assert "agentic_audio_jobs" in _MIGRATION.read_text()


def test_deleting_a_session_takes_its_jobs_with_it() -> None:
    """Retention promises the session is gone. A job row carries the prompt the
    user wrote, the analysis of their footage and the URL of what was made from
    it, so a sweep that leaves it behind is a partial delete — the exact failure
    the session cascade already exists to prevent."""
    sql = _MIGRATION.read_text()
    assert (
        "session_id TEXT REFERENCES agentic_audio_sessions(session_id) "
        "ON DELETE CASCADE" in sql
    )
    # And the job's own children go with the job.
    assert sql.count("REFERENCES agentic_audio_jobs(job_id) ON DELETE CASCADE") == 2


def test_the_open_job_index_excludes_finished_rows() -> None:
    """The completer polls this twice a second forever; finished rows
    accumulate for the life of the deployment and must never be walked."""
    sql = _MIGRATION.read_text()
    assert "agentic_audio_jobs_open_idx" in sql
    assert "WHERE status NOT IN ('completed', 'failed', 'canceled')" in sql


def test_the_poll_matches_the_index_predicate_character_for_character() -> None:
    """Postgres cannot prove a parameterised predicate implies a partial index's
    own, so a bound parameter here turns the poll into a sequential scan of
    every job the deployment has ever run — with nothing failing and nothing
    saying so. The two texts drifting apart has exactly the same effect."""
    from EdennCode.EdennAgent.AgenticAudio.persistence.jobs import (
        OPEN_STATUS_PREDICATE,
    )

    assert f"WHERE {OPEN_STATUS_PREDICATE}" in _MIGRATION.read_text()

    repo, recorder = _repo([[_job_row()]])
    repo.list_open_jobs()
    statement, params = recorder.calls[0]
    assert OPEN_STATUS_PREDICATE in statement
    assert TERMINAL_JOB_STATUSES not in params, "the predicate is text, not a parameter"


def test_the_status_literal_refuses_anything_that_is_not_one() -> None:
    """It is interpolated rather than bound, so the guard is the whole safety
    argument. It has to refuse, not sanitise."""
    from EdennCode.EdennAgent.AgenticAudio.persistence.jobs import _status_literal

    assert _status_literal(("completed",)) == "('completed')"
    for bad in ("done'); DROP TABLE agentic_audio_jobs; --", "Completed", "a b", ""):
        with pytest.raises(ValueError):
            _status_literal((bad,))


# ---- the completer's end of the contract --------------------------------


def _completer_source() -> str:
    source = _DEVSERVER.read_text()
    head, sep, tail = source.partition("async def job_completer()")
    assert sep, "the completer is gone — this file is reviewing nothing"
    body, sep, _rest = tail.partition("def _refuse_to_serve")
    assert sep, "the slice end marker moved — the checks below would fail open"
    return body


def test_nothing_is_generated_that_was_not_claimed_first() -> None:
    """The claim is what makes `queued` mean *not started*. Dispatching a job
    the claim did not return would put a row into a render with no record that
    anybody is doing it."""
    body = _completer_source()

    assert "claim_job" in body
    assert "if claimed is None:" in body
    assert "_complete_job(claimed)" in body
    assert "_complete_job(job)" not in body, "dispatching an unclaimed job"


def test_the_completer_only_tries_to_claim_queued_rows() -> None:
    """Otherwise a sibling replica's in-flight render costs a write every tick
    for an answer that cannot change."""
    assert "if job.status != JobStatus.QUEUED:" in _completer_source()


def test_a_claimed_job_keeps_saying_it_is_alive() -> None:
    """Silence is what marks a render abandoned, so noise has to be maintained
    — a generation that outlives the stale window would be swept out from under
    itself on the next boot."""
    body = _completer_source()
    assert "touch_heartbeat" in body

    from EdennCode.EdennAgent.AgenticAudio.persistence import jobs as jobs_module

    assert jobs_module.DEFAULT_STALE_AFTER_S > 60.0


def test_the_database_is_not_polled_on_the_event_loop() -> None:
    """Every repository call here is synchronous. On the loop, a poll twice a
    second is jitter on every request the server is also serving."""
    body = _completer_source()
    for call in ("list_open_jobs", "claim_job", "touch_heartbeat"):
        assert f"async_repo.{call}" in body
        assert "asyncio.to_thread" in body


def test_a_failed_poll_does_not_end_the_completer() -> None:
    """A database blip must not leave the process alive and silently unable to
    finish anything for the rest of its life."""
    body = _completer_source()
    assert "job poll failed" in body
    assert "continue" in body


# ---- the wiring ---------------------------------------------------------


def test_the_durable_store_is_used_when_a_database_is_configured() -> None:
    source = _DEVSERVER.read_text()
    assert "DurableJobRepository" in source
    assert "_with_media_rehydration(DurableJobRepository)" in source
    assert "_with_media_rehydration(_MemoryAsyncRepository)" in source


def test_interrupted_jobs_are_settled_before_the_completer_starts() -> None:
    """The loop must never see a row this pass is about to settle."""
    source = _DEVSERVER.read_text()
    settle = source.index("_reconcile_interrupted_jobs)")
    start = source.index("asyncio.create_task(job_completer())")
    assert settle < start


def test_a_restored_row_still_gets_its_file_back() -> None:
    """Durable rows change which half goes missing. The lookup now succeeds and
    hands back a path on a filesystem that no longer exists, which fails later
    and somewhere else than the error that used to say so."""
    source = _DEVSERVER.read_text()
    rehydration = source.split("def _with_media_rehydration")[1].split(
        "runner_id = new_id"
    )[0]
    assert "_restore_media_file" in rehydration
    assert rehydration.count("_restore_media_file") >= 2, (
        "the hit path needs the file restored too, not just the miss path"
    )
