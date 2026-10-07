"""Taking access back.

Sharing was one-way. A session could be shared but never unshared: there was no
way to remove a collaborator, no way to cancel an invite link, and a live socket
kept streaming because the member role is resolved once at connect and never
re-checked. The only remedy was to abandon the session.
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
OTHER = {"Authorization": "Bearer tok_other"}


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv(
        "AGENTIC_AUDIO_API_KEYS",
        "tok_owner:owner,tok_guest:guest,tok_other:other",
    )
    monkeypatch.setenv("AGENTIC_AUDIO_SHARE_SECRET", "test-share-secret")
    # In-memory collab store: the default repository connects to Postgres from
    # environment credentials, which in a developer's shell is a real database.
    client, source = _client_with_memory_collab(tmp_path)
    return client, None, None, None, source


def _shared_session(client, source) -> tuple[str, str]:
    """A session with `guest` joined at comment level. Returns (sid, grant)."""
    sid = _create_session(client, source, headers=OWNER)["session_id"]
    minted = client.post(
        f"{_AUDIO}/sessions/{sid}/collab/links", json={"role": "comment"}, headers=OWNER
    )
    assert minted.status_code == 200, minted.text
    grant = minted.json()["grant"]
    joined = client.post(
        f"{_AUDIO}/sessions/{sid}/collab/join", json={"grant": grant}, headers=GUEST
    )
    assert joined.status_code == 200, joined.text
    return sid, grant


# ---------------------------------------------------------------------------#
# removing a collaborator                                                     #
# ---------------------------------------------------------------------------#


def test_the_owner_can_remove_a_collaborator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _, _, source = _client(tmp_path, monkeypatch)
    sid, _ = _shared_session(client, source)

    assert client.get(f"{_AUDIO}/sessions/{sid}", headers=GUEST).status_code == 200

    removed = client.delete(f"{_AUDIO}/sessions/{sid}/collab/participants/guest", headers=OWNER)
    assert removed.status_code == 200, removed.text
    assert removed.json()["removed"] is True

    assert client.get(f"{_AUDIO}/sessions/{sid}", headers=GUEST).status_code == 403


def test_a_collaborator_cannot_remove_anyone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _, _, source = _client(tmp_path, monkeypatch)
    sid, _ = _shared_session(client, source)
    r = client.delete(f"{_AUDIO}/sessions/{sid}/collab/participants/guest", headers=GUEST)
    assert r.status_code == 403, r.text


def test_a_stranger_cannot_remove_anyone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _, _, source = _client(tmp_path, monkeypatch)
    sid, _ = _shared_session(client, source)
    r = client.delete(f"{_AUDIO}/sessions/{sid}/collab/participants/guest", headers=OTHER)
    assert r.status_code in (403, 404), r.text


def test_the_owner_cannot_remove_themselves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise the session has no owner and nobody can ever share it again."""
    client, _, _, _, source = _client(tmp_path, monkeypatch)
    sid, _ = _shared_session(client, source)
    r = client.delete(f"{_AUDIO}/sessions/{sid}/collab/participants/owner", headers=OWNER)
    assert r.status_code == 400, r.text


def test_removing_a_collaborator_closes_their_live_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The role is resolved at connect and never re-checked, so without this a
    removed collaborator keeps streaming the session until they reload."""
    client, _, _, _, source = _client(tmp_path, monkeypatch)
    sid, _ = _shared_session(client, source)

    with client.websocket_connect(f"{_AUDIO}/sessions/{sid}/ws?token=tok_guest") as ws:
        assert ws.receive_json()["event_type"] == "session.opened"
        removed = client.delete(
            f"{_AUDIO}/sessions/{sid}/collab/participants/guest", headers=OWNER
        )
        assert removed.status_code == 200, removed.text
        assert removed.json()["disconnected"] >= 1
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_removal_does_not_delete_what_they_said(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A thread with the replies deleted out of it is a conversation nobody can
    follow. Removal is about future access."""
    client, _, _, _, source = _client(tmp_path, monkeypatch)
    sid, _ = _shared_session(client, source)
    made = client.post(
        f"{_AUDIO}/sessions/{sid}/collab/threads",
        json={"body": "the drop lands late", "anchor_node_id": "__source__"},
        headers=GUEST,
    )
    assert made.status_code == 200, made.text

    client.delete(f"{_AUDIO}/sessions/{sid}/collab/participants/guest", headers=OWNER)

    threads = client.get(f"{_AUDIO}/sessions/{sid}/collab", headers=OWNER).json()
    body = str(threads)
    assert "the drop lands late" in body


# ---------------------------------------------------------------------------#
# revoking an invite link                                                     #
# ---------------------------------------------------------------------------#


def test_revoking_stops_an_outstanding_link_from_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signed capability cannot be recalled — the holder already has the
    bytes — so revocation changes what the server will accept."""
    client, _, _, _, source = _client(tmp_path, monkeypatch)
    sid = _create_session(client, source, headers=OWNER)["session_id"]
    grant = client.post(
        f"{_AUDIO}/sessions/{sid}/collab/links", json={"role": "comment"}, headers=OWNER
    ).json()["grant"]

    revoked = client.post(f"{_AUDIO}/sessions/{sid}/collab/links/revoke", headers=OWNER)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["epoch"] >= 1

    denied = client.post(
        f"{_AUDIO}/sessions/{sid}/collab/join", json={"grant": grant}, headers=GUEST
    )
    assert denied.status_code == 403, denied.text
    assert "revoked" in denied.json()["detail"].lower()


def test_a_link_minted_after_the_revocation_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _, _, source = _client(tmp_path, monkeypatch)
    sid = _create_session(client, source, headers=OWNER)["session_id"]
    client.post(f"{_AUDIO}/sessions/{sid}/collab/links/revoke", headers=OWNER)

    fresh = client.post(
        f"{_AUDIO}/sessions/{sid}/collab/links", json={"role": "comment"}, headers=OWNER
    ).json()["grant"]
    joined = client.post(
        f"{_AUDIO}/sessions/{sid}/collab/join", json={"grant": fresh}, headers=GUEST
    )
    assert joined.status_code == 200, joined.text


def test_revoking_links_does_not_evict_people_who_already_joined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """They are participants now, not link holders. Revoking the link is not the
    same act as removing a person, and conflating them would surprise an owner
    who only meant to stop a link circulating."""
    client, _, _, _, source = _client(tmp_path, monkeypatch)
    sid, _ = _shared_session(client, source)

    client.post(f"{_AUDIO}/sessions/{sid}/collab/links/revoke", headers=OWNER)
    assert client.get(f"{_AUDIO}/sessions/{sid}", headers=GUEST).status_code == 200


def test_only_the_owner_can_revoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _, _, source = _client(tmp_path, monkeypatch)
    sid, _ = _shared_session(client, source)
    r = client.post(f"{_AUDIO}/sessions/{sid}/collab/links/revoke", headers=GUEST)
    assert r.status_code == 403, r.text


def test_a_grant_from_before_epochs_existed_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Links already in flight when this shipped carry no epoch field. They read
    as generation 0 and keep working until the owner revokes for the first
    time — the alternative is silently breaking every outstanding invite."""
    from EdennCode.EdennAgent.AgenticAudio.api.auth import verify_share_grant
    import base64, hashlib, hmac, json, os, time

    monkeypatch.setenv("AGENTIC_AUDIO_SHARE_SECRET", "test-share-secret")
    body = {"sid": "sess_old", "role": "comment", "exp": int(time.time()) + 600}
    payload = base64.urlsafe_b64encode(
        json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    ).rstrip(b"=")
    sig = hmac.new(b"test-share-secret", payload, hashlib.sha256).hexdigest()[:32]
    legacy = payload.decode() + "." + sig

    assert verify_share_grant(legacy, "sess_old", epoch=0) == "comment"
