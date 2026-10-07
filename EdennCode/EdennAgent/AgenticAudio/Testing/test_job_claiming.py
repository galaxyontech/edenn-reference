"""The dispatch path, running.

Everything about durability rests on one behaviour at runtime: the completer
takes a job before it generates it. ``queued`` means *nothing has been spent*
only because of that write, and every decision a later boot makes — resume this,
close that one out — is read off it.

The rest of the durability story is checked in ``test_durable_jobs.py`` against
a real database, and the statements themselves in
``test_job_durability_contract.py``. This file starts the actual server and
watches one job go through it.
"""

from __future__ import annotations

import importlib
import time
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from EdennCode.Deployment.async_pipeline_v2.models import JobStatus


def _app(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EDENN_DEV_REAL_MUSIC", "0")
    monkeypatch.setenv("EDENN_DEV_REAL_VOICEOVER", "0")
    monkeypatch.delenv("EDENN_DEV_PREGEN", raising=False)
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("AGENTIC_AUDIO_REQUIRE_AUTH", raising=False)
    mod = importlib.import_module("EdennCode.EdennAgent.AgenticAudio.design.devserver")
    return mod, mod.build_app()


def _spy_on_claims(repo: Any) -> list[tuple[str, str | None]]:
    """Record (job_id, resulting status) for every claim the completer makes."""
    seen: list[tuple[str, str | None]] = []
    original = repo.claim_job

    def claim(job_id: str, *, runner_id: str, **kwargs: Any):
        # Forward whatever the completer passes — the claim now carries the
        # usage meter's open hook, and a spy that dropped it would test a claim
        # production does not make.
        claimed = original(job_id, runner_id=runner_id, **kwargs)
        seen.append((job_id, getattr(claimed, "status", None)))
        return claimed

    repo.claim_job = claim
    return seen


def _settle(repo: Any, job_id: str, *, timeout: float = 25.0) -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = repo.get_job(job_id)
        if job is not None and job.status in (
            JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED
        ):
            return job
        time.sleep(0.2)
    return repo.get_job(job_id)


def test_a_job_is_claimed_before_it_is_generated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mod, app = _app(monkeypatch)
    with TestClient(app):
        repo = app.state.async_repository
        claims = _spy_on_claims(repo)
        job = repo.create_job(job_type="video_music", request_json={"user_prompt": "warm"})

        settled = _settle(repo, job.job_id)

        assert [job_id for job_id, _ in claims] == [job.job_id]
        assert claims[0][1] == JobStatus.PROCESSING, (
            "the claim must move the row out of queued before anything spends"
        )
        assert settled.status == JobStatus.COMPLETED
        assert settled.result_json


def test_a_job_is_claimed_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The in-flight guard is released in a `finally`, so it protects only the
    moment a job is running. The claim is what protects everything after."""
    _mod, app = _app(monkeypatch)
    with TestClient(app):
        repo = app.state.async_repository
        claims = _spy_on_claims(repo)
        job = repo.create_job(job_type="video_music", request_json={})

        _settle(repo, job.job_id)
        time.sleep(2.0)  # several more ticks of the completer

        assert [job_id for job_id, _ in claims] == [job.job_id]


def test_staging_work_is_never_dispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    """An upload's staging row is already finished; generating from it would be
    a render nobody asked for."""
    _mod, app = _app(monkeypatch)
    with TestClient(app):
        repo = app.state.async_repository
        claims = _spy_on_claims(repo)
        repo.create_job(
            job_id="asset_job_smoke",
            job_type="asset_staging",
            request_json={},
            status=JobStatus.COMPLETED,
        )
        time.sleep(3.0)

        assert claims == []


def test_a_failed_render_is_not_generated_again_on_a_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every re-dispatch spends provider credit on a job the user has already
    been told did not work."""
    _mod, app = _app(monkeypatch)
    with TestClient(app):
        repo = app.state.async_repository
        claims = _spy_on_claims(repo)
        job = repo.create_job(job_type="video_music", request_json={})
        repo.update_job_status(
            job.job_id, status=JobStatus.FAILED, result_json={"error": "upstream"}
        )
        time.sleep(3.0)

        assert claims == []
        assert repo.get_job(job.job_id).status == JobStatus.FAILED


def test_a_stale_queued_job_is_not_dispatched_even_before_the_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatch loop enforces the same queued-age gate the sweep uses.

    Boot reconcile can fail transiently and the completer starts anyway; if the
    gate lived only in the sweep, a genuinely stale queued row could be claimed
    and rendered in the window before the first in-loop sweep — starting
    yesterday's render on today's boot, a surprise charge. The gate at dispatch
    closes that window.
    """
    from EdennCode.EdennAgent.AgenticAudio.persistence.jobs import (
        DEFAULT_QUEUED_MAX_AGE_S,
    )
    from EdennCode.Deployment.async_pipeline_v2.models import utc_now

    _mod, app = _app(monkeypatch)
    with TestClient(app):
        repo = app.state.async_repository
        claims = _spy_on_claims(repo)
        job = repo.create_job(job_type="video_music", request_json={})
        # Backdate it well past the abandon threshold. The memory job dataclass
        # is frozen, so replace it in the store.
        import dataclasses

        old_created = utc_now() - timedelta(seconds=DEFAULT_QUEUED_MAX_AGE_S + 120)
        repo.jobs[job.job_id] = dataclasses.replace(
            repo.jobs[job.job_id], created_at=old_created
        )
        time.sleep(3.0)

        assert claims == [], "a stale queued row must not be dispatched"
        assert repo.get_job(job.job_id).status == JobStatus.QUEUED


def test_the_generic_completion_handler_fails_paid_jobs_not_completes_them() -> None:
    """A generic exception used to mark ANY job COMPLETED with a placeholder
    tone. Against a durable table that hides a possibly-billed paid render as a
    success forever — a terminal status refuses every later correction. The
    handler must branch on whether the job could have spent: a paid-capable
    job fails (UI offers retry), only a no-spend job degrades to a placeholder.

    The completion handler is a closure inside build_app, so this reads the
    source the way the retry-guard test does — a behavioural test cannot inject
    a fault into a closure the loop reaches internally.
    """
    from pathlib import Path

    source = Path(
        "EdennCode/EdennAgent/AgenticAudio/design/devserver.py"
    ).read_text()
    handler = source.split("async def _complete_job")[1].split(
        "async def job_completer"
    )[0]
    generic = handler.split("except Exception as exc:")[1]

    assert "_job_could_spend(job)" in generic, (
        "the generic handler must decide by whether the job could have spent"
    )
    # The paid branch fails; the no-spend branch keeps the placeholder demo.
    assert "status=JobStatus.FAILED" in generic, "a paid-capable job must fail"
    assert "_placeholder_result(job)" in generic, "a no-spend job still demos"
    # And the spend predicate covers every paid job type.
    spend = source.split("def _job_could_spend")[1].split("async def _complete_job")[0]
    for job_type in ("video_music", "voiceover", "audio_creative_edit", "video_sfx"):
        assert job_type in spend, f"{job_type} is unclassified by _job_could_spend"


def test_the_runner_id_is_published_for_an_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'Which process claimed this row' is the question a stuck job turns on."""
    _mod, app = _app(monkeypatch)
    with TestClient(app):
        assert str(app.state.runner_id).startswith("runner_")
        assert app.state.reconcile_interrupted_jobs() == {
            "interrupted": [], "abandoned": []
        }


def test_a_hook_that_fails_leaves_the_job_queued() -> None:
    """The claim is where everything hung off it gets to fail closed. A spend
    nobody recorded is worse than a render that waits for the next poll — so if
    the meter cannot be written, the job is not claimed and nothing spends."""
    from EdennCode.EdennAgent.AgenticAudio.design.memory_fakes import (
        _MemoryAsyncRepository,
    )
    from EdennCode.Deployment.async_pipeline_v2.models import JobStatus

    repo = _MemoryAsyncRepository()
    job = repo.create_job(job_type="video_music", request_json={}, session_id="s1")

    def _refuses(claimed, *, client=None):
        raise RuntimeError("the meter is unreachable")

    with pytest.raises(RuntimeError):
        repo.claim_job(job.job_id, runner_id="runner_1", on_claim=_refuses)

    assert repo.get_job(job.job_id).status == JobStatus.QUEUED, (
        "the job was claimed even though nothing recorded the spend"
    )


def test_the_hook_sees_the_whole_claimed_row() -> None:
    """It needs the job type, the session, who owns it and who ran it — all of
    which the claim already has in hand from RETURNING *."""
    from EdennCode.EdennAgent.AgenticAudio.design.memory_fakes import (
        _MemoryAsyncRepository,
    )

    repo = _MemoryAsyncRepository()
    created = repo.create_job(
        job_type="voiceover", request_json={"x": 1}, session_id="s1",
        creator_user_id="owner", actor_user_id="helper",
    )
    seen: list[Any] = []

    repo.claim_job(
        created.job_id, runner_id="runner_1",
        on_claim=lambda job, client=None: seen.append(job),
    )

    assert seen and seen[0].job_type == "voiceover"
    assert seen[0].session_id == "s1"
    assert seen[0].actor_user_id == "helper"
