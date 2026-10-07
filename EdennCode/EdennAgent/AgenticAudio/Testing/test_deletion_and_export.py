"""Leaving with your data, and taking it down.

Account deletion deliberately kept the sessions a person owns, because deleting
an account and destroying work a team still depends on are different acts. This
is the other half: deleting a session on purpose, and being able to leave with
what is in it first.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

from EdennCode.EdennAgent.AgenticAudio.Testing.test_agentic_audio_api import (
    _AUDIO,
    _client_with_memory_collab,
    _create_session,
)

OWNER = {"Authorization": "Bearer tok_owner"}
GUEST = {"Authorization": "Bearer tok_guest"}


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "tok_owner:owner,tok_guest:guest")
    monkeypatch.setenv("AGENTIC_AUDIO_SHARE_SECRET", "test-share-secret")
    return _client_with_memory_collab(tmp_path)


def _shared(client, source) -> str:
    sid = _create_session(client, source, headers=OWNER)["session_id"]
    grant = client.post(
        f"{_AUDIO}/sessions/{sid}/collab/links", json={"role": "comment"}, headers=OWNER
    ).json()["grant"]
    client.post(f"{_AUDIO}/sessions/{sid}/collab/join", json={"grant": grant}, headers=GUEST)
    return sid


# ---------------------------------------------------------------------------#
# deleting a session                                                          #
# ---------------------------------------------------------------------------#


def test_the_owner_can_delete_their_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, source = _client(tmp_path, monkeypatch)
    sid = _create_session(client, source, headers=OWNER)["session_id"]

    gone = client.delete(f"{_AUDIO}/sessions/{sid}?confirm={sid}", headers=OWNER)
    assert gone.status_code == 200, gone.text
    assert gone.json()["deleted"] is True
    assert client.get(f"{_AUDIO}/sessions/{sid}", headers=OWNER).status_code == 404


def test_deletion_requires_the_session_id_as_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DELETE that fires on a mistyped URL destroys work nobody can get back."""
    client, source = _client(tmp_path, monkeypatch)
    sid = _create_session(client, source, headers=OWNER)["session_id"]

    assert client.delete(f"{_AUDIO}/sessions/{sid}", headers=OWNER).status_code == 400
    assert client.delete(
        f"{_AUDIO}/sessions/{sid}?confirm=wrong", headers=OWNER
    ).status_code == 400
    assert client.get(f"{_AUDIO}/sessions/{sid}", headers=OWNER).status_code == 200


def test_a_collaborator_cannot_delete_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Being able to comment on something is not being able to erase it."""
    client, source = _client(tmp_path, monkeypatch)
    sid = _shared(client, source)

    denied = client.delete(f"{_AUDIO}/sessions/{sid}?confirm={sid}", headers=GUEST)
    assert denied.status_code == 403, denied.text
    assert client.get(f"{_AUDIO}/sessions/{sid}", headers=OWNER).status_code == 200


def test_deleting_takes_the_conversation_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial delete leaves a person believing their footage is gone while
    the transcript describing it is still there."""
    client, source = _client(tmp_path, monkeypatch)
    sid = _shared(client, source)
    client.post(
        f"{_AUDIO}/sessions/{sid}/collab/threads",
        json={"body": "the drop lands late", "anchor_node_id": "__source__"},
        headers=GUEST,
    )

    client.delete(f"{_AUDIO}/sessions/{sid}?confirm={sid}", headers=OWNER)

    assert client.get(f"{_AUDIO}/sessions/{sid}", headers=OWNER).status_code == 404
    assert client.get(f"{_AUDIO}/sessions/{sid}/collab", headers=OWNER).status_code == 404


def test_deleting_disconnects_anyone_watching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nobody should keep streaming a session that no longer exists."""
    client, source = _client(tmp_path, monkeypatch)
    sid = _shared(client, source)

    with client.websocket_connect(f"{_AUDIO}/sessions/{sid}/ws?token=tok_guest") as ws:
        assert ws.receive_json()["event_type"] == "session.opened"
        client.delete(f"{_AUDIO}/sessions/{sid}?confirm={sid}", headers=OWNER)
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_a_deleted_session_leaves_the_owners_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, source = _client(tmp_path, monkeypatch)
    sid = _create_session(client, source, headers=OWNER)["session_id"]
    client.delete(f"{_AUDIO}/sessions/{sid}?confirm={sid}", headers=OWNER)

    listed = client.get(f"{_AUDIO}/sessions", headers=OWNER).json()["sessions"]
    assert sid not in {s["session_id"] for s in listed}


# ---------------------------------------------------------------------------#
# leaving with it                                                             #
# ---------------------------------------------------------------------------#


def test_a_session_can_be_exported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, source = _client(tmp_path, monkeypatch)
    sid = _create_session(client, source, headers=OWNER)["session_id"]

    out = client.get(f"{_AUDIO}/sessions/{sid}/export", headers=OWNER)
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["session"]["session_id"] == sid
    assert "messages" in body["session"]
    assert body["exported_at"]


def test_export_is_not_open_to_strangers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An export is the whole session in one response — the last thing that
    should be reachable by anyone who is not a member."""
    client, source = _client(tmp_path, monkeypatch)
    sid = _create_session(client, source, headers=OWNER)["session_id"]
    assert client.get(f"{_AUDIO}/sessions/{sid}/export", headers=GUEST).status_code == 403


def test_a_collaborator_can_export_a_session_they_are_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, source = _client(tmp_path, monkeypatch)
    sid = _shared(client, source)
    assert client.get(f"{_AUDIO}/sessions/{sid}/export", headers=GUEST).status_code == 200


def test_everything_can_be_exported_in_one_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Give me my data" should be a single act, not a list to walk."""
    client, source = _client(tmp_path, monkeypatch)
    first = _create_session(client, source, headers=OWNER)["session_id"]
    second = _create_session(client, source, headers=OWNER)["session_id"]

    out = client.get(f"{_AUDIO}/me/export", headers=OWNER)
    assert out.status_code == 200, out.text
    ids = {s["session_id"] for s in out.json()["sessions"]}
    assert {first, second} <= ids
    assert out.json()["user"]["user_id"] == "owner"


def test_an_export_only_contains_your_own_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, source = _client(tmp_path, monkeypatch)
    mine = _create_session(client, source, headers=OWNER)["session_id"]

    theirs = client.get(f"{_AUDIO}/me/export", headers=GUEST).json()
    assert mine not in {s["session_id"] for s in theirs["sessions"]}


def test_exporting_needs_a_signed_in_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, source = _client(tmp_path, monkeypatch)
    assert client.get(f"{_AUDIO}/me/export").status_code == 401


def test_deleting_a_session_takes_its_media_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting a session reclaimed the database and left the footage in a
    container. The endpoint now removes the stored objects too, and reports
    what it could not remove rather than implying everything is gone."""
    from EdennCode.EdennAgent.AgenticAudio.persistence import media_store

    class _Store:
        can_delete = True

        def __init__(self) -> None:
            self.deleted: list[tuple[str, str]] = []

        def delete_many(self, refs):
            self.deleted.extend(refs)
            return len(refs), []

    store = _Store()
    media_store.set_active_store(store)
    try:
        client, source = _client(tmp_path, monkeypatch)
        sid = _create_session(client, source, headers=OWNER)["session_id"]

        response = client.delete(
            f"{_AUDIO}/sessions/{sid}?confirm={sid}", headers=OWNER
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["deleted"] is True
        assert body["media_pending"] == 0
        # The seeded source video is the one artifact this session has.
        assert store.deleted, "nothing was asked to be deleted"
        assert all(container for container, _ in store.deleted)
    finally:
        media_store.set_active_store(None)
