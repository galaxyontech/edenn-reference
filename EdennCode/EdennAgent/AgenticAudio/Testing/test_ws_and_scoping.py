"""The WebSocket handshake, and whose sessions a caller can see.

Two properties that are only visible from outside the process:

* the socket is authenticated and origin-checked BEFORE it is accepted, so an
  unauthenticated or cross-origin peer never holds an open connection;
* the sessions list is scoped by the authenticated principal, never by a query
  parameter the caller supplies.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from EdennCode.EdennAgent.AgenticAudio.Testing.test_agentic_audio_api import (
    _AUDIO,
    _bootstrap_decisions,
    _client_with_decisions,
    _create_session,
)


def _auth_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "tok_a:alice,tok_b:bob")
    return _client_with_decisions(tmp_path, _bootstrap_decisions() * 3)


# ---------------------------------------------------------------------------#
# the handshake                                                               #
# ---------------------------------------------------------------------------#


def test_an_unauthenticated_socket_is_refused_at_the_handshake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused before accept(): an unauthenticated peer must never end up
    holding an open socket while the server decides what to do with it."""
    client, _, _, _, source = _auth_client(tmp_path, monkeypatch)
    session = _create_session(client, source, headers={"Authorization": "Bearer tok_a"})
    sid = session["session_id"]

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"{_AUDIO}/sessions/{sid}/ws") as ws:
            ws.receive_json()


def test_a_bad_token_is_refused_at_the_handshake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _, _, source = _auth_client(tmp_path, monkeypatch)
    session = _create_session(client, source, headers={"Authorization": "Bearer tok_a"})
    sid = session["session_id"]

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"{_AUDIO}/sessions/{sid}/ws?token=nope") as ws:
            ws.receive_json()


def test_the_owner_can_open_a_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _, _, source = _auth_client(tmp_path, monkeypatch)
    session = _create_session(client, source, headers={"Authorization": "Bearer tok_a"})
    sid = session["session_id"]

    with client.websocket_connect(f"{_AUDIO}/sessions/{sid}/ws?token=tok_a") as ws:
        opened = ws.receive_json()
        assert opened["event_type"] == "session.opened"


def test_a_stranger_is_told_why_and_then_disconnected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A known user with no access to THIS session is handled differently from
    an unknown one, deliberately.

    An unauthenticated peer is refused at the handshake, because nothing can be
    sent before accept(). A signed-in person who simply is not a member is
    accepted just long enough to be told why — otherwise the console can only
    show them a socket that closed for no stated reason, and "ask the owner for
    an invite" is exactly the thing they need to hear.
    """
    client, _, _, _, source = _auth_client(tmp_path, monkeypatch)
    session = _create_session(client, source, headers={"Authorization": "Bearer tok_a"})
    sid = session["session_id"]

    with client.websocket_connect(f"{_AUDIO}/sessions/{sid}/ws?token=tok_b") as ws:
        frame = ws.receive_json()
        assert frame["event_type"] == "error"
        assert "access" in frame["payload"]["message"].lower()
        # …and the socket does not stay open after the explanation.
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_a_stranger_never_receives_the_session_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The explanation must not come with the session's contents attached."""
    client, _, _, _, source = _auth_client(tmp_path, monkeypatch)
    session = _create_session(client, source, headers={"Authorization": "Bearer tok_a"})
    sid = session["session_id"]

    with client.websocket_connect(f"{_AUDIO}/sessions/{sid}/ws?token=tok_b") as ws:
        frame = ws.receive_json()
    assert frame["event_type"] != "session.opened"
    assert "snapshot" not in frame.get("payload", {})


def test_a_foreign_origin_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The WebSocket handshake is not covered by the same-origin policy and has
    no preflight, so Origin is the only thing between a signed-in user's session
    and any page they happen to visit."""
    monkeypatch.setenv("AGENTIC_AUDIO_ALLOWED_ORIGINS", "https://studio.example.com")
    client, _, _, _, source = _auth_client(tmp_path, monkeypatch)
    session = _create_session(client, source, headers={"Authorization": "Bearer tok_a"})
    sid = session["session_id"]

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"{_AUDIO}/sessions/{sid}/ws?token=tok_a",
            headers={"Origin": "https://evil.example.com"},
        ) as ws:
            ws.receive_json()


def test_the_configured_origin_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTIC_AUDIO_ALLOWED_ORIGINS", "https://studio.example.com")
    client, _, _, _, source = _auth_client(tmp_path, monkeypatch)
    session = _create_session(client, source, headers={"Authorization": "Bearer tok_a"})
    sid = session["session_id"]

    with client.websocket_connect(
        f"{_AUDIO}/sessions/{sid}/ws?token=tok_a",
        headers={"Origin": "https://studio.example.com"},
    ) as ws:
        assert ws.receive_json()["event_type"] == "session.opened"


# ---------------------------------------------------------------------------#
# whose sessions you can see                                                  #
# ---------------------------------------------------------------------------#


def test_the_sessions_list_is_scoped_to_the_authenticated_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _, _, source = _auth_client(tmp_path, monkeypatch)
    alice = {"Authorization": "Bearer tok_a"}
    bob = {"Authorization": "Bearer tok_b"}
    mine = _create_session(client, source, headers=alice)["session_id"]

    seen = client.get(f"{_AUDIO}/sessions", headers=alice).json()["sessions"]
    assert mine in {s["session_id"] for s in seen}

    others = client.get(f"{_AUDIO}/sessions", headers=bob).json()["sessions"]
    assert mine not in {s["session_id"] for s in others}


def test_a_creator_query_parameter_cannot_widen_the_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client passes ?creator= and the server must ignore it while
    authenticated — otherwise the list is whatever the caller asks for."""
    client, _, _, _, source = _auth_client(tmp_path, monkeypatch)
    alice = {"Authorization": "Bearer tok_a"}
    bob = {"Authorization": "Bearer tok_b"}
    mine = _create_session(client, source, headers=alice)["session_id"]

    peeked = client.get(f"{_AUDIO}/sessions?creator=alice", headers=bob).json()["sessions"]
    assert mine not in {s["session_id"] for s in peeked}, "?creator= widened the scope"
