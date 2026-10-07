"""Whose video is this.

Creating a session checked only that the source artifact EXISTED. Artifact ids
travel in URLs and API responses, so knowing one was enough to start a session
over somebody else's uploaded footage — have the agent analyse it, and watch it
back in the console. The upload did not record an owner either, so there was
nothing to check against even if anyone had thought to.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from EdennCode.EdennAgent.AgenticAudio.Testing.test_agentic_audio_api import (
    _AUDIO,
    _bootstrap_decisions,
    _client_with_decisions,
    _seed_source_video,
)

ALICE = {"Authorization": "Bearer tok_alice"}
BOB = {"Authorization": "Bearer tok_bob"}


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "tok_alice:alice,tok_bob:bob")
    return _client_with_decisions(tmp_path, _bootstrap_decisions() * 3)


def _owned_artifact(async_repo, owner: str):
    """A source video staged by a named owner, the way an upload stages one."""
    artifact = _seed_source_video(async_repo)
    job = async_repo.get_job(artifact.job_id)
    if job is not None:
        async_repo.jobs[artifact.job_id] = type(job)(
            **{**job.__dict__, "creator_user_id": owner}
        )
    return artifact


def test_a_stranger_cannot_start_a_session_over_someone_elses_footage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, async_repo, _, _ = _client(tmp_path, monkeypatch)
    mine = _owned_artifact(async_repo, "alice")

    stolen = client.post(
        f"{_AUDIO}/sessions",
        json={"source_video_artifact_id": mine.artifact_id},
        headers=BOB,
    )
    assert stolen.status_code == 404, stolen.text


def test_the_refusal_does_not_confirm_that_the_id_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """404 rather than 403: telling a caller "that exists, but it is not yours"
    is itself a disclosure, and lets an id be probed for validity."""
    client, _, async_repo, _, _ = _client(tmp_path, monkeypatch)
    mine = _owned_artifact(async_repo, "alice")

    real_but_theirs = client.post(
        f"{_AUDIO}/sessions",
        json={"source_video_artifact_id": mine.artifact_id},
        headers=BOB,
    )
    invented = client.post(
        f"{_AUDIO}/sessions",
        json={"source_video_artifact_id": "artifact_does_not_exist"},
        headers=BOB,
    )
    assert real_but_theirs.status_code == invented.status_code == 404
    assert real_but_theirs.json()["detail"] == invented.json()["detail"].replace(
        "artifact_does_not_exist", mine.artifact_id
    )


def test_the_owner_can_start_a_session_over_their_own_footage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, async_repo, _, _ = _client(tmp_path, monkeypatch)
    mine = _owned_artifact(async_repo, "alice")

    started = client.post(
        f"{_AUDIO}/sessions",
        json={"source_video_artifact_id": mine.artifact_id},
        headers=ALICE,
    )
    assert started.status_code == 200, started.text


def test_an_artifact_with_no_recorded_owner_is_still_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deliberately permissive for unknown ownership.

    Artifacts staged before uploads recorded an owner have none. Refusing them
    would lock people out of their own existing sessions to close a hole that a
    KNOWN owner already closes.
    """
    client, _, async_repo, _, source = _client(tmp_path, monkeypatch)
    started = client.post(
        f"{_AUDIO}/sessions",
        json={"source_video_artifact_id": source.artifact_id},
        headers=BOB,
    )
    assert started.status_code == 200, started.text


def test_with_auth_off_nothing_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no principal to compare against, so the check cannot apply — a
    local run must not start refusing its own fixtures."""
    monkeypatch.delenv("AGENTIC_AUDIO_REQUIRE_AUTH", raising=False)
    client, _, async_repo, _, _ = _client_with_decisions(tmp_path, _bootstrap_decisions())
    owned = _owned_artifact(async_repo, "somebody")
    started = client.post(
        f"{_AUDIO}/sessions", json={"source_video_artifact_id": owned.artifact_id}
    )
    assert started.status_code == 200, started.text
