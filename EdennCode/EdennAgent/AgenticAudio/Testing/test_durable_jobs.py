"""The studio's job store, against a real database.

Every property here is about what Postgres does when two processes touch one
row — a conditional update that must produce exactly one winner, a guard that
must refuse to move a finished job, a sweep that must distinguish a render
somebody is still doing from one nobody is. No fake can answer those, so these
run against a throwaway database and skip without one. See ``Testing/README.md``.

    docker run -d --name edenn-test-pg -e POSTGRES_PASSWORD=test \\
      -e POSTGRES_DB=edenn_test -p 55432:5432 postgres:16-alpine
    EDENN_TEST_PG_DSN=postgresql://postgres:test@127.0.0.1:55432/edenn_test pytest ...

These tests share one database and :meth:`fail_interrupted` is deliberately
unscoped — in production it must settle every abandoned row, not a chosen few.
So each test creates the rows it asserts on and asserts on nothing else. Run
them serially.
"""

from __future__ import annotations

import os
import threading
import uuid
from typing import Any
from urllib.parse import urlparse

import pytest

from EdennCode.Deployment.async_pipeline_v2.models import JobStatus

DSN = os.getenv("EDENN_TEST_PG_DSN", "").strip()

pytestmark = pytest.mark.skipif(
    not DSN, reason="set EDENN_TEST_PG_DSN to a throwaway Postgres to run these"
)


def _client_factory():
    from EdennCode.Deployment.postgres_wrapper import (
        PostgresClient,
        PostgresConnectionConfig,
    )

    parsed = urlparse(DSN)

    def make() -> Any:
        return PostgresClient(
            PostgresConnectionConfig(
                host=parsed.hostname or "127.0.0.1",
                port=parsed.port or 5432,
                database=(parsed.path or "/postgres").lstrip("/"),
                user=parsed.username or "postgres",
                password=parsed.password,
                # A throwaway container has no TLS, and the default is "require".
                sslmode="disable",
                connect_timeout=10,
            )
        )

    return make


@pytest.fixture()
def repo():
    from EdennCode.EdennAgent.AgenticAudio.persistence.jobs import (
        DurableJobRepository,
    )

    r = DurableJobRepository(client_factory=_client_factory())
    r.ensure_schema()
    return r


def _job_id() -> str:
    return f"job_{uuid.uuid4().hex[:16]}"


def _make(repo, *, status: str = JobStatus.QUEUED, job_type: str = "video_music"):
    # session_id deliberately absent: job rows REFERENCE the sessions table
    # with ON DELETE CASCADE, so an invented session id here is an FK
    # violation on insert — which is exactly what the first run of this suite
    # against a real database tripped over, in the setup of nearly every test.
    # The cascade itself is exercised by the test that creates a real session.
    return repo.create_job(
        job_id=_job_id(),
        job_type=job_type,
        request_json={"user_prompt": "warm strings"},
        status=status,
    )


def test_a_job_for_a_session_that_does_not_exist_is_refused(repo) -> None:
    """The foreign key IS the retention promise, so it must actually bite."""
    with pytest.raises(Exception):
        repo.create_job(
            job_id=_job_id(),
            job_type="video_music",
            request_json={},
            session_id=f"sess_{uuid.uuid4().hex[:10]}",
        )


def test_deleting_a_session_takes_its_jobs_and_their_children_with_it(repo) -> None:
    """Retention deletes the session row and promises everything went with it.

    A job row carries the prompt the user wrote, the analysis of their footage
    and the URL of what was made from it — a partial delete here is the exact
    failure the session cascade already exists to prevent.
    """
    from EdennCode.EdennAgent.AgenticAudio.persistence.repositories import (
        AgenticAudioRepository,
    )

    sessions = AgenticAudioRepository(client_factory=_client_factory())
    sessions.ensure_schema()
    session = sessions.create_session(
        source_video_artifact_id="artifact_cascade_test",
        phase="observing",
        state_json={},
    )
    job = repo.create_job(
        job_id=_job_id(),
        job_type="video_music",
        request_json={"user_prompt": "the whole record of the work"},
        session_id=session.session_id,
    )
    repo.add_artifact(job_id=job.job_id, artifact_type="music_audio", url="/dev/media/x.mp3")
    repo.add_event(job_id=job.job_id, event_type="queued")

    assert sessions.delete_session(session.session_id) is True

    assert repo.get_job(job.job_id) is None
    assert repo.list_artifacts(job.job_id) == []
    assert repo.list_events(job.job_id) == []


# ---- the seed path ------------------------------------------------------


def test_re_creating_a_job_id_does_not_erase_the_render_under_it(repo) -> None:
    """The dev server re-seeds a fixed demo job id on every boot.

    Against a dict that dies with the process, replacing the row is harmless.
    Against a table that does not, the row it replaces may be a finished render
    somebody is looking at — so creating over an existing id returns what is
    there instead of overwriting it.
    """
    job = _make(repo)
    repo.update_job_status(
        job.job_id, status=JobStatus.COMPLETED, result_json={"audio_url": "/dev/media/a.mp3"}
    )

    again = repo.create_job(
        job_id=job.job_id, job_type="video_music", request_json={"user_prompt": "different"}
    )

    assert again.status == JobStatus.COMPLETED
    assert again.result_json == {"audio_url": "/dev/media/a.mp3"}
    assert again.request_json == {"user_prompt": "warm strings"}


def test_re_adding_an_artifact_under_the_same_id_replaces_it(repo) -> None:
    """The opposite rule, for the opposite reason.

    The dev server seeds a source-video stub and then re-adds the same artifact
    id once a real local clip exists for it. Preserving the first write would
    leave the stub in place and every no-upload session on the placeholder path.
    """
    job = _make(repo, job_type="asset_staging")
    repo.add_artifact(
        artifact_id="artifact_seeded",
        job_id=job.job_id,
        artifact_type="source_video",
        url="https://cdn.test/source.mp4",
    )
    repo.add_artifact(
        artifact_id="artifact_seeded",
        job_id=job.job_id,
        artifact_type="source_video",
        url="/dev/media/demo_source.mp4",
        local_path="/tmp/demo_source.mp4",
        metadata_json={"duration": 12.0},
    )

    found = repo.get_artifact("artifact_seeded")
    assert found is not None
    assert found.url == "/dev/media/demo_source.mp4"
    assert found.local_path == "/tmp/demo_source.mp4"
    assert found.metadata_json == {"duration": 12.0}


# ---- claiming -----------------------------------------------------------


def test_a_claim_moves_the_row_out_of_queued(repo) -> None:
    """`queued` has to mean *nothing has been spent yet*.

    Before the claim existed a job stayed `queued` for the whole render, so a
    row found after a restart was indistinguishable from one that had been
    generating for two minutes — and the only safe reading of that was the
    pessimistic one, which means never resuming anything.
    """
    job = _make(repo)
    claimed = repo.claim_job(job.job_id, runner_id="runner_a")

    assert claimed is not None
    assert claimed.status == JobStatus.PROCESSING
    assert repo.get_job(job.job_id).status == JobStatus.PROCESSING


def test_only_one_process_can_claim_a_job(repo) -> None:
    """Two replicas overlap on every rolling deploy. One render, one runner."""
    job = _make(repo)

    results: list[Any] = []
    lock = threading.Lock()

    def claim(name: str) -> None:
        won = repo.claim_job(job.job_id, runner_id=name)
        with lock:
            results.append(won)

    threads = [threading.Thread(target=claim, args=(f"runner_{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, "two processes would have generated the same job"


def test_a_job_that_is_not_queued_cannot_be_claimed(repo) -> None:
    job = _make(repo)
    assert repo.claim_job(job.job_id, runner_id="runner_a") is not None
    assert repo.claim_job(job.job_id, runner_id="runner_b") is None


# ---- status writes ------------------------------------------------------


def test_a_terminal_job_cannot_be_moved(repo) -> None:
    job = _make(repo)
    repo.update_job_status(job.job_id, status=JobStatus.FAILED, result_json={"error": "no"})

    after, applied = repo.update_job_status_checked(
        job.job_id, status=JobStatus.COMPLETED, result_json={"audio_url": "/x.mp3"}
    )

    assert applied is False
    assert after.status == JobStatus.FAILED
    assert after.result_json == {"error": "no"}


def test_a_status_write_carrying_no_result_does_not_erase_the_result(repo) -> None:
    """The in-memory store keeps an unmentioned field; so does this one.

    A status write that happens not to carry a result must not blank the result
    a previous write recorded — that would turn a delivered render into an empty
    one with no error anywhere.
    """
    job = _make(repo)
    repo.update_job_status(
        job.job_id, status=JobStatus.PROCESSING, result_json={"partial": True}
    )

    repo.update_job_status(job.job_id, status=JobStatus.PROCESSING, progress_percent=40.0)

    after = repo.get_job(job.job_id)
    assert after.result_json == {"partial": True}
    assert after.progress_percent == 40.0


def test_finishing_a_job_stamps_it_and_drops_the_runner(repo) -> None:
    """Otherwise the next boot's sweep sees a completed render still claimed by
    a process that is gone, and has to decide what that means."""
    job = _make(repo)
    repo.claim_job(job.job_id, runner_id="runner_a")
    repo.update_job_status(job.job_id, status=JobStatus.COMPLETED, result_json={"ok": 1})

    after = repo.get_job(job.job_id)
    assert after.finished_at is not None

    settled = repo.fail_interrupted(runner_id="runner_b", stale_after_s=0.0)
    assert job.job_id not in settled["interrupted"]


# ---- finding work -------------------------------------------------------


def test_open_jobs_leaves_out_finished_work_and_excluded_types(repo) -> None:
    open_job = _make(repo)
    done_job = _make(repo)
    staging = _make(repo, job_type="asset_staging")
    repo.update_job_status(done_job.job_id, status=JobStatus.COMPLETED, result_json={})

    found = {
        j.job_id
        for j in repo.list_open_jobs(limit=500, exclude_job_types=("asset_staging",))
    }

    assert open_job.job_id in found
    assert done_job.job_id not in found
    assert staging.job_id not in found


def test_the_poll_actually_reaches_the_partial_index(repo) -> None:
    """The one check that catches the predicate drifting from the index.

    Both texts keep working when they disagree — the query returns the right
    rows either way. What changes is that a read the completer does twice a
    second for the life of the deployment starts scanning every job the studio
    has ever run, and nothing anywhere says so.

    Sequential scans are switched off for the plan so that a table with three
    rows in it still has to say which index it *would* use.
    """
    from EdennCode.EdennAgent.AgenticAudio.persistence.jobs import (
        OPEN_STATUS_PREDICATE,
    )

    _make(repo)
    with _client_factory()() as client:
        # Session-scoped, not LOCAL: outside a transaction block SET LOCAL is a
        # no-op with a warning, and the plan would come back unconstrained.
        client.run_sql("SET enable_seqscan = off")
        try:
            rows = client.run_sql(
                f"EXPLAIN SELECT * FROM agentic_audio_jobs "
                f"WHERE {OPEN_STATUS_PREDICATE} "
                f"ORDER BY priority DESC, created_at LIMIT 200"
            )
        finally:
            client.run_sql("RESET enable_seqscan")
    plan = " ".join(str(v) for row in (rows or []) for v in row.values())
    assert "agentic_audio_jobs_open_idx" in plan, plan


def test_open_jobs_is_bounded(repo) -> None:
    """It is polled twice a second for the life of the process."""
    for _ in range(3):
        _make(repo)
    assert len(repo.list_open_jobs(limit=2)) <= 2


# ---- heartbeats ---------------------------------------------------------


def test_a_heartbeat_only_touches_rows_this_runner_claimed(repo) -> None:
    mine = _make(repo)
    theirs = _make(repo)
    repo.claim_job(mine.job_id, runner_id="runner_a")
    repo.claim_job(theirs.job_id, runner_id="runner_b")

    touched = repo.touch_heartbeat([mine.job_id, theirs.job_id], runner_id="runner_a")

    assert touched == 1


def test_a_heartbeat_for_nothing_asks_the_database_nothing(repo) -> None:
    assert repo.touch_heartbeat([], runner_id="runner_a") == 0


# ---- the sweep ----------------------------------------------------------


def test_an_interrupted_render_is_closed_out_and_never_re_run(repo) -> None:
    """A row found in `processing` was mid-render when its container died.

    The provider call it was making may well have completed and been billed.
    Running it again turns one paid render into two and hands the user a second
    take they did not ask for, so this marks it failed instead — which the UI
    already offers a retry on, putting the decision back where it belongs.
    """
    job = _make(repo)
    repo.claim_job(job.job_id, runner_id="runner_dead")

    settled = repo.fail_interrupted(runner_id="runner_new", stale_after_s=0.0)

    assert job.job_id in settled["interrupted"]
    after = repo.get_job(job.job_id)
    assert after.status == JobStatus.FAILED
    assert after.result_json["interrupted"] is True
    assert after.result_json["placeholder"] is False
    assert job.job_id not in {j.job_id for j in repo.list_open_jobs(limit=500)}


def test_a_sibling_replicas_live_render_is_left_alone(repo) -> None:
    """Calling a running render dead is the same lie in the other direction."""
    job = _make(repo)
    repo.claim_job(job.job_id, runner_id="runner_other")

    settled = repo.fail_interrupted(runner_id="runner_new", stale_after_s=300.0)

    assert job.job_id not in settled["interrupted"]
    assert repo.get_job(job.job_id).status == JobStatus.PROCESSING


def test_this_runners_own_work_is_never_swept(repo) -> None:
    job = _make(repo)
    repo.claim_job(job.job_id, runner_id="runner_me")

    settled = repo.fail_interrupted(runner_id="runner_me", stale_after_s=0.0)

    assert job.job_id not in settled["interrupted"]
    assert repo.get_job(job.job_id).status == JobStatus.PROCESSING


def test_a_job_that_never_started_survives_a_restart_and_still_runs(repo) -> None:
    """Nothing was spent on a queued row, and it is what the user asked for."""
    job = _make(repo)

    settled = repo.fail_interrupted(runner_id="runner_new", stale_after_s=0.0)

    assert job.job_id not in settled["interrupted"]
    assert job.job_id not in settled["abandoned"]
    assert repo.get_job(job.job_id).status == JobStatus.QUEUED
    assert job.job_id in {j.job_id for j in repo.list_open_jobs(limit=500)}


def test_a_job_queued_long_enough_ago_is_closed_out_instead(repo) -> None:
    """Starting yesterday's render on today's boot is a surprise result and a
    surprise charge for someone who has walked away."""
    job = _make(repo)

    settled = repo.fail_interrupted(
        runner_id="runner_new", stale_after_s=0.0, queued_max_age_s=0.0
    )

    assert job.job_id in settled["abandoned"]
    assert repo.get_job(job.job_id).status == JobStatus.FAILED


# ---- the rest of the surface -------------------------------------------


def test_artifacts_and_events_round_trip(repo) -> None:
    job = _make(repo)
    repo.add_artifact(
        job_id=job.job_id,
        artifact_type="music_audio",
        role="output",
        url="/dev/media/take.mp3",
        metadata_json={"duration": 20.5},
    )
    repo.add_event(job_id=job.job_id, event_type="queued", message="waiting")

    artifacts = repo.list_artifacts(job.job_id)
    events = repo.list_events(job.job_id)

    assert [a.artifact_type for a in artifacts] == ["music_audio"]
    assert artifacts[0].metadata_json == {"duration": 20.5}
    assert [e.event_type for e in events] == ["queued"]


def test_the_status_view_is_shaped_like_the_fleets(repo) -> None:
    """A caller must not have to know which deployment answered."""
    job = _make(repo)
    repo.add_artifact(job_id=job.job_id, artifact_type="music_audio", url="/dev/media/t.mp3")

    view = repo.build_status_view(job.job_id)

    assert view["job_id"] == job.job_id
    assert view["status"] == JobStatus.QUEUED
    assert view["stages"] == []
    assert [a["artifact_type"] for a in view["artifacts"]] == ["music_audio"]


def test_a_job_that_is_not_there_is_a_key_error(repo) -> None:
    with pytest.raises(KeyError):
        repo.build_status_view("job_nope")
    with pytest.raises(KeyError):
        repo.update_job_status_checked("job_nope", status=JobStatus.COMPLETED)
