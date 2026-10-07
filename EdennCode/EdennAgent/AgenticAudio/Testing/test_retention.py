"""Deleting what we said we would delete — after 7 days.

The selection half existed; the deleting half was deliberately absent, because
an interval is a product decision and inventing one in code is how a product
ends up quietly keeping people's footage forever. It is set now.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from EdennCode.EdennAgent.AgenticAudio.persistence import retention


class _FakeRepo:
    """Records what it was asked to delete."""

    def __init__(self, eligible: list[str], *, explode_on: str | None = None) -> None:
        self._eligible = list(eligible)
        self.deleted: list[str] = []
        self.asked_days: int | None = None
        self.asked_limit: int | None = None
        self._explode_on = explode_on

    def sessions_older_than(self, days: int, *, limit: int = 500) -> list[str]:
        self.asked_days, self.asked_limit = days, limit
        return self._eligible[:limit]

    def delete_session(self, session_id: str) -> bool:
        if session_id == self._explode_on:
            raise RuntimeError("row is wedged")
        self.deleted.append(session_id)
        return True


# ---------------------------------------------------------------------------#
# the interval                                                                #
# ---------------------------------------------------------------------------#


def test_the_interval_is_seven_days(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTIC_AUDIO_RETENTION_DAYS", raising=False)
    assert retention.retention_days() == 7


def test_the_interval_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIC_AUDIO_RETENTION_DAYS", "30")
    assert retention.retention_days() == 30


@pytest.mark.parametrize("bad", ["0", "-5", "", "soon"])
def test_a_typo_cannot_mean_delete_everything_now(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """Zero or negative would select every session ever created. Nobody should
    be able to reach that by mistyping an environment variable."""
    monkeypatch.setenv("AGENTIC_AUDIO_RETENTION_DAYS", bad)
    assert retention.retention_days() == 7


# ---------------------------------------------------------------------------#
# the sweep                                                                   #
# ---------------------------------------------------------------------------#


def test_it_deletes_nothing_unless_asked() -> None:
    """The first thing anyone should do with a deletion job is watch it not
    delete anything."""
    repo = _FakeRepo(["s1", "s2", "s3"])
    result = retention.sweep(repo)
    assert result.dry_run is True
    assert result.considered == ["s1", "s2", "s3"]
    assert repo.deleted == []


def test_applying_deletes_what_it_listed() -> None:
    repo = _FakeRepo(["s1", "s2"])
    result = retention.sweep(repo, apply=True)
    assert result.dry_run is False
    assert repo.deleted == ["s1", "s2"]
    assert result.deleted == ["s1", "s2"]


def test_it_asks_for_the_configured_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTIC_AUDIO_RETENTION_DAYS", raising=False)
    repo = _FakeRepo([])
    retention.sweep(repo)
    assert repo.asked_days == 7


def test_one_pass_is_bounded() -> None:
    """A job that finds a year of backlog and clears it in one transaction is an
    outage, not a cleanup."""
    repo = _FakeRepo([f"s{i}" for i in range(1000)])
    retention.sweep(repo, limit=10, apply=True)
    assert len(repo.deleted) == 10


def test_one_wedged_row_does_not_stop_the_pass() -> None:
    """Otherwise a single undeletable session freezes retention forever, and the
    backlog grows behind it."""
    repo = _FakeRepo(["s1", "s2", "s3"], explode_on="s2")
    result = retention.sweep(repo, apply=True)
    assert repo.deleted == ["s1", "s3"]
    assert result.failed == ["s2"]


def test_every_deletion_is_audited(caplog: pytest.LogCaptureFixture) -> None:
    """A user asking "where did my session go" deserves an answer."""
    repo = _FakeRepo(["s1"])
    with caplog.at_level(logging.INFO, logger="edenn.agentic_audio.audit"):
        retention.sweep(repo, apply=True)
    lines = [r.getMessage() for r in caplog.records
             if r.name == "edenn.agentic_audio.audit"]
    assert any("session.deleted" in l and "retention" in l for l in lines)


def test_a_dry_run_audits_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """Reporting what would happen is not an event in the session's history."""
    repo = _FakeRepo(["s1"])
    with caplog.at_level(logging.INFO, logger="edenn.agentic_audio.audit"):
        retention.sweep(repo)
    assert not [r for r in caplog.records if r.name == "edenn.agentic_audio.audit"]


def test_the_summary_says_which_mode_it_ran_in() -> None:
    repo = _FakeRepo(["s1", "s2"])
    assert "would delete" in retention.sweep(repo).summary
    assert "deleted" in retention.sweep(_FakeRepo(["s1"]), apply=True).summary


def test_an_empty_sweep_is_not_an_error() -> None:
    result = retention.sweep(_FakeRepo([]), apply=True)
    assert result.considered == [] and result.deleted == []


# ---------------------------------------------------------------------------#
# the half of the promise that was missing: the bytes                         #
# ---------------------------------------------------------------------------#


class _FakeStore:
    """A media store that records what it was asked to delete."""

    def __init__(self, *, can_delete: bool = True, refuse: tuple[str, ...] = ()) -> None:
        self.can_delete = can_delete
        self.refuse = set(refuse)
        self.deleted: list[tuple[str, str]] = []

    def delete_many(self, refs):
        removed, failed = 0, []
        for container, name in refs:
            if name in self.refuse:
                failed.append(name)
                continue
            self.deleted.append((container, name))
            removed += 1
        return removed, failed


class _FakeJobs:
    def __init__(self, refs_by_session: dict) -> None:
        self.refs = refs_by_session

    def session_media_refs(self, session_id: str):
        return list(self.refs.get(session_id) or [])


def test_deleting_a_session_removes_the_media_it_produced() -> None:
    """Retention used to reclaim the database and leave the footage in a
    container. A person told their work was deleted, whose work is still stored,
    has been told something untrue — and the bytes bill us until someone looks."""
    from EdennCode.EdennAgent.AgenticAudio.persistence import retention

    store = _FakeStore()
    jobs = _FakeJobs({"s1": [("user-uploads", "source/clip.mp4"),
                             ("generated-media", "take_1.mp3")]})

    removed, failed, unsupported = retention.delete_session_media(
        "s1", job_repository=jobs, media_store=store
    )

    assert (removed, failed, unsupported) == (2, [], False)
    assert ("generated-media", "take_1.mp3") in store.deleted


def test_a_store_that_cannot_delete_says_so_instead_of_reporting_success() -> None:
    """"0 removed" and "there is no delete" look identical in a report and mean
    opposite things. The second one names every object left behind."""
    from EdennCode.EdennAgent.AgenticAudio.persistence import retention

    store = _FakeStore(can_delete=False)
    jobs = _FakeJobs({"s1": [("generated-media", "take_1.mp3")]})

    removed, failed, unsupported = retention.delete_session_media(
        "s1", job_repository=jobs, media_store=store
    )

    assert removed == 0
    assert failed == ["take_1.mp3"]
    assert unsupported is True
    assert store.deleted == []


def test_one_object_that_will_not_go_does_not_strand_the_rest() -> None:
    from EdennCode.EdennAgent.AgenticAudio.persistence import retention

    store = _FakeStore(refuse=("stuck.mp3",))
    jobs = _FakeJobs({"s1": [("generated-media", "stuck.mp3"),
                             ("generated-media", "fine.mp3")]})

    removed, failed, _ = retention.delete_session_media(
        "s1", job_repository=jobs, media_store=store
    )

    assert removed == 1 and failed == ["stuck.mp3"]
    assert ("generated-media", "fine.mp3") in store.deleted


def test_the_objects_go_before_the_row_that_names_them() -> None:
    """The row is the only thing that remembers the object names — artifacts
    cascade from jobs and jobs cascade from the session. Deleting it first
    strands bytes nobody can name again."""
    from EdennCode.EdennAgent.AgenticAudio.persistence import retention

    order: list[str] = []

    class _Repo:
        def sessions_older_than(self, days, limit=200):
            return ["s1"]

        def delete_session(self, session_id):
            order.append("row")
            return True

    class _Store(_FakeStore):
        def delete_many(self, refs):
            order.append("media")
            return super().delete_many(refs)

    result = retention.sweep(
        _Repo(),
        apply=True,
        job_repository=_FakeJobs({"s1": [("generated-media", "t.mp3")]}),
        media_store=_Store(),
    )

    assert order == ["media", "row"]
    assert result.media_deleted == 1


def test_a_sweep_with_no_media_store_behaves_exactly_as_it_always_did() -> None:
    from EdennCode.EdennAgent.AgenticAudio.persistence import retention

    class _Repo:
        def sessions_older_than(self, days, limit=200):
            return ["s1"]

        def delete_session(self, session_id):
            return True

    result = retention.sweep(_Repo(), apply=True)
    assert result.deleted == ["s1"]
    assert result.media_deleted == 0 and result.media_failed == []


def test_the_uploaded_video_goes_too_when_only_this_session_used_it() -> None:
    """The footage is the object the promise is most about, and it is NOT
    reachable from the session's renders: an upload is staged before any session
    exists, so its job row carries no session id."""
    from types import SimpleNamespace

    from EdennCode.EdennAgent.AgenticAudio.persistence import retention

    class _Sessions:
        def get_session(self, session_id):
            return SimpleNamespace(source_video_artifact_id="art_1")

        def sessions_sharing_source(self, artifact_id, *, excluding=""):
            return 0

    class _Jobs(_FakeJobs):
        def get_artifact(self, artifact_id):
            return SimpleNamespace(container="user-uploads", blob_name="source/clip.mp4")

    refs = retention.source_upload_refs(
        "s1", repository=_Sessions(), job_repository=_Jobs({})
    )
    assert refs == [("user-uploads", "source/clip.mp4")]


def test_an_upload_two_sessions_share_is_left_alone() -> None:
    """Deleting it because one session went would reach past what the user asked
    to delete and break somebody else's work."""
    from types import SimpleNamespace

    from EdennCode.EdennAgent.AgenticAudio.persistence import retention

    class _Sessions:
        def get_session(self, session_id):
            return SimpleNamespace(source_video_artifact_id="art_1")

        def sessions_sharing_source(self, artifact_id, *, excluding=""):
            return 1

    class _Jobs(_FakeJobs):
        def get_artifact(self, artifact_id):  # pragma: no cover - must not be reached
            raise AssertionError("the shared upload was looked up for deletion")

    assert retention.source_upload_refs(
        "s1", repository=_Sessions(), job_repository=_Jobs({})
    ) == []
