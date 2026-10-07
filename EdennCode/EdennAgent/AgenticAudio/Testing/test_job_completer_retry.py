"""A generation that failed must not be generated again on a timer.

The standalone deployment completes jobs in-process: a loop walks the job table
every half second and dispatches anything that is not finished. It skipped only
COMPLETED — and the in-flight guard is released in a `finally`, so a job that
FAILED was eligible again on the very next tick. It generated again. And again.
Every attempt spends real provider credit, on a job the customer has already
been told did not work, for as long as the container lives.

Nothing reported it, because from the outside it looks like a stuck job rather
than a bill. A failed generation is something to retry deliberately, never
something to retry in a loop.
"""

from __future__ import annotations

from pathlib import Path

from EdennCode.Deployment.async_pipeline_v2.models import JobStatus


def _completer_source() -> str:
    source = Path(
        "EdennCode/EdennAgent/AgenticAudio/design/devserver.py"
    ).read_text()
    head, sep, tail = source.partition("async def job_completer()")
    assert sep, "the completer is gone — this file is reviewing nothing"
    body, sep, _rest = tail.partition("def _refuse_to_serve")
    assert sep, "the slice end marker moved — the checks below would fail open"
    return body


def test_every_terminal_status_is_left_alone() -> None:
    """The bug in one line: the skip listed one terminal status out of three."""
    body = _completer_source()

    assert "JobStatus.COMPLETED" in body
    assert "JobStatus.FAILED" in body, "a failed job was re-dispatched forever"
    assert "JobStatus.CANCELED" in body, "a cancelled job would be re-dispatched too"


def test_the_in_flight_guard_alone_cannot_be_the_protection() -> None:
    """It is released in a finally, so it protects only the moment a job is
    actually running — not the far longer time afterwards. Anything that relies
    on it to prevent a re-run is relying on the wrong thing."""
    source = Path(
        "EdennCode/EdennAgent/AgenticAudio/design/devserver.py"
    ).read_text()

    assert "finally:" in source and "inflight.discard(job.job_id)" in source


def test_the_terminal_set_matches_the_statuses_that_exist() -> None:
    """JobStatus is a plain constants class, so a new terminal status can be
    added without anything noticing. This reads the names off it and fails if
    one that finishes a job is not being skipped."""
    body = _completer_source()
    terminal_names = [
        name for name in vars(JobStatus)
        if name.isupper() and name in {"COMPLETED", "FAILED", "CANCELED"}
    ]
    assert len(terminal_names) == 3, f"the status vocabulary changed: {terminal_names}"

    for name in terminal_names:
        assert f"JobStatus.{name}" in body, f"{name} jobs would be re-dispatched"
