"""The studio's own users: who exists, what they are called, and leaving.

Deliberately the studio's own table rather than the platform's accounts — the
studio ships as a separate app, and the two share a CREDENTIAL, not a schema.

There is no sign-up of our own to hang a registration step on, so the table
fills itself from authentication: the first time a verified principal makes a
request, they exist.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from EdennCode.EdennAgent.AgenticAudio.Testing.test_agentic_audio_api import (
    _AUDIO,
    _client_with_memory_collab,
    _create_session,
)

ALICE = {"Authorization": "Bearer tok_alice"}
BOB = {"Authorization": "Bearer tok_bob"}


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "tok_alice:alice,tok_bob:bob")
    monkeypatch.setenv("AGENTIC_AUDIO_SHARE_SECRET", "test-share-secret")
    return _client_with_memory_collab(tmp_path)


# ---------------------------------------------------------------------------#
# existing                                                                    #
# ---------------------------------------------------------------------------#


def test_authenticating_is_what_creates_the_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No separate registration step, because there is no sign-up of our own."""
    client, source = _client(tmp_path, monkeypatch)
    me = client.get(f"{_AUDIO}/me", headers=ALICE)
    assert me.status_code == 200, me.text
    assert me.json()["authenticated"] is True
    assert me.json()["user_id"] == "alice"


def test_an_unauthenticated_caller_is_told_so_rather_than_being_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The console asks "who am I" before it can offer a sign-in, so this one
    endpoint answers instead of 401-ing."""
    client, source = _client(tmp_path, monkeypatch)
    me = client.get(f"{_AUDIO}/me")
    assert me.status_code == 200, me.text
    assert me.json()["authenticated"] is False


def test_the_auth_source_is_recorded_so_legacy_accounts_can_be_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration needs to find every account still resting on a static token."""
    client, source = _client(tmp_path, monkeypatch)
    assert client.get(f"{_AUDIO}/me", headers=ALICE).json()["auth_source"] == "legacy_token"


# ---------------------------------------------------------------------------#
# what people are called                                                      #
# ---------------------------------------------------------------------------#


def test_a_person_can_set_the_name_collaborators_see(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, source = _client(tmp_path, monkeypatch)
    put = client.put(f"{_AUDIO}/me", json={"display_name": "Ada Lovelace"}, headers=ALICE)
    assert put.status_code == 200, put.text
    assert client.get(f"{_AUDIO}/me", headers=ALICE).json()["display_name"] == "Ada Lovelace"


def test_the_name_survives_the_next_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every authenticated request touches the user row. If that overwrote the
    display name, a chosen name would last exactly one request."""
    client, source = _client(tmp_path, monkeypatch)
    client.put(f"{_AUDIO}/me", json={"display_name": "Ada"}, headers=ALICE)
    client.get(f"{_AUDIO}/sessions", headers=ALICE)
    client.get(f"{_AUDIO}/sessions", headers=ALICE)
    assert client.get(f"{_AUDIO}/me", headers=ALICE).json()["display_name"] == "Ada"


def test_an_owner_comments_under_their_name_not_a_raw_principal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner is the session's CREATOR, not a participant row, so the name
    chain had nothing to offer but the principal — and a uid is what
    collaborators saw next to the owner's comments."""
    client, source = _client(tmp_path, monkeypatch)
    client.put(f"{_AUDIO}/me", json={"display_name": "Ada Lovelace"}, headers=ALICE)
    sid = _create_session(client, source, headers=ALICE)["session_id"]

    made = client.post(
        f"{_AUDIO}/sessions/{sid}/collab/threads",
        json={"body": "bring the drop forward", "anchor_node_id": "__source__"},
        headers=ALICE,
    )
    assert made.status_code == 200, made.text
    comment = made.json()["thread"]["comments"][0]
    assert comment["author_id"] == "alice"
    assert comment["author_name"] == "Ada Lovelace"


def test_a_name_cannot_be_set_for_somebody_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The endpoint takes no user id at all — it always writes the caller's."""
    client, source = _client(tmp_path, monkeypatch)
    client.put(f"{_AUDIO}/me", json={"display_name": "Ada"}, headers=ALICE)
    client.put(f"{_AUDIO}/me", json={"display_name": "Mallory"}, headers=BOB)
    assert client.get(f"{_AUDIO}/me", headers=ALICE).json()["display_name"] == "Ada"


def test_setting_a_profile_needs_a_signed_in_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, source = _client(tmp_path, monkeypatch)
    assert client.put(f"{_AUDIO}/me", json={"display_name": "X"}).status_code == 401


def test_an_absurd_name_is_refused_rather_than_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, source = _client(tmp_path, monkeypatch)
    r = client.put(f"{_AUDIO}/me", json={"display_name": "x" * 500}, headers=ALICE)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------#
# leaving                                                                     #
# ---------------------------------------------------------------------------#


def test_deleting_an_account_needs_an_explicit_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DELETE that fires on a mistyped URL is not a thing anyone can undo."""
    client, source = _client(tmp_path, monkeypatch)
    assert client.delete(f"{_AUDIO}/me", headers=ALICE).status_code == 400
    assert client.delete(f"{_AUDIO}/me?confirm=notme", headers=ALICE).status_code == 400


def test_deleting_an_account_removes_the_person_and_their_memberships(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, source = _client(tmp_path, monkeypatch)
    sid = _create_session(client, source, headers=ALICE)["session_id"]
    grant = client.post(
        f"{_AUDIO}/sessions/{sid}/collab/links", json={"role": "comment"}, headers=ALICE
    ).json()["grant"]
    client.post(f"{_AUDIO}/sessions/{sid}/collab/join", json={"grant": grant}, headers=BOB)
    assert client.get(f"{_AUDIO}/sessions/{sid}", headers=BOB).status_code == 200

    gone = client.delete(f"{_AUDIO}/me?confirm=bob", headers=BOB)
    assert gone.status_code == 200, gone.text
    assert gone.json()["deleted"] is True
    # The membership went with the account.
    assert client.get(f"{_AUDIO}/sessions/{sid}", headers=BOB).status_code == 403


def test_deleting_an_account_does_not_destroy_the_work_it_owns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting an account and destroying sessions a team still depends on are
    different acts. The response names what was left so the caller can decide."""
    client, source = _client(tmp_path, monkeypatch)
    sid = _create_session(client, source, headers=ALICE)["session_id"]

    gone = client.delete(f"{_AUDIO}/me?confirm=alice", headers=ALICE)
    assert gone.status_code == 200, gone.text
    assert sid in gone.json()["sessions_retained"]
    assert "kept" in gone.json()["note"]


def test_a_deleted_account_can_come_back_by_signing_in_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The credential is the platform's, not ours — deleting our row cannot and
    should not stop the person authenticating. What must NOT survive is their
    old profile."""
    client, source = _client(tmp_path, monkeypatch)
    client.put(f"{_AUDIO}/me", json={"display_name": "Ada"}, headers=ALICE)
    client.delete(f"{_AUDIO}/me?confirm=alice", headers=ALICE)

    back = client.get(f"{_AUDIO}/me", headers=ALICE)
    assert back.status_code == 200
    assert back.json()["authenticated"] is True
    assert back.json()["display_name"] == "", "the deleted profile came back"
