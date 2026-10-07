"""Keeping the credential out of the URL.

A WebSocket handshake and a media element both refuse an Authorization header,
so the caller's bearer token went into the query string — where it lands in
access logs, browser history and Referer headers, and travels to whoever the URL
is shared with. A ticket is minted over a normal authenticated request, lives
about a minute, is bound to a purpose, and is spent on use.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

from EdennCode.EdennAgent.AgenticAudio.api import tickets
from EdennCode.EdennAgent.AgenticAudio.api.auth import AuthError
from EdennCode.EdennAgent.AgenticAudio.api.tickets import (
    PURPOSE_MEDIA,
    PURPOSE_WS,
    mint_ticket,
    redeem_ticket,
)
from EdennCode.EdennAgent.AgenticAudio.Testing.test_agentic_audio_api import (
    _AUDIO,
    _bootstrap_decisions,
    _client_with_decisions,
    _create_session,
)

ALICE = {"Authorization": "Bearer tok_a"}
BOB = {"Authorization": "Bearer tok_b"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    tickets.reset_for_tests()
    monkeypatch.setenv("AGENTIC_AUDIO_TICKET_SECRET", "test-ticket-secret")
    yield
    tickets.reset_for_tests()


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "tok_a:alice,tok_b:bob")
    return _client_with_decisions(tmp_path, _bootstrap_decisions() * 3)


# ---------------------------------------------------------------------------#
# the ticket itself                                                           #
# ---------------------------------------------------------------------------#


def test_a_ticket_proves_the_principal_that_minted_it() -> None:
    ticket = mint_ticket("alice")["ticket"]
    assert redeem_ticket(ticket, purpose=PURPOSE_WS) == "alice"


def test_a_ticket_is_spent_on_use() -> None:
    """A credential that sits in a URL should survive being read exactly once."""
    ticket = mint_ticket("alice")["ticket"]
    assert redeem_ticket(ticket, purpose=PURPOSE_WS) == "alice"
    with pytest.raises(AuthError) as exc:
        redeem_ticket(ticket, purpose=PURPOSE_WS)
    assert "already been used" in exc.value.detail


def test_a_media_ticket_cannot_open_a_socket() -> None:
    """Purposes are namespaces, not labels — the signature covers the purpose."""
    ticket = mint_ticket("alice", purpose=PURPOSE_MEDIA)["ticket"]
    with pytest.raises(AuthError):
        redeem_ticket(ticket, purpose=PURPOSE_WS)


def test_a_ticket_bound_to_one_session_cannot_be_pointed_at_another() -> None:
    ticket = mint_ticket("alice", session_id="sess_a")["ticket"]
    with pytest.raises(AuthError):
        redeem_ticket(ticket, purpose=PURPOSE_WS, session_id="sess_b")


def test_an_expired_ticket_is_refused() -> None:
    ticket = mint_ticket("alice", ttl_s=-1)["ticket"]
    with pytest.raises(AuthError) as exc:
        redeem_ticket(ticket, purpose=PURPOSE_WS)
    assert "expired" in exc.value.detail


def test_a_tampered_ticket_is_refused() -> None:
    ticket = mint_ticket("alice")["ticket"]
    payload, sig = ticket.split(".", 1)
    forged = payload[:-2] + "AA." + sig
    with pytest.raises(AuthError):
        redeem_ticket(forged, purpose=PURPOSE_WS)


def test_a_share_grant_cannot_be_presented_as_a_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two capabilities with different lifetimes must not share a signing key."""
    from EdennCode.EdennAgent.AgenticAudio.api.auth import mint_share_grant

    monkeypatch.setenv("AGENTIC_AUDIO_SHARE_SECRET", "test-ticket-secret")
    grant = mint_share_grant("sess_a", "comment")
    with pytest.raises(AuthError):
        redeem_ticket(grant, purpose=PURPOSE_WS)


def test_spent_tickets_do_not_accumulate_forever() -> None:
    """The replay record is bounded by the TTL, not by a count — an entry is
    only useful until the ticket would have expired anyway."""
    for _ in range(5):
        redeem_ticket(mint_ticket("alice", ttl_s=1)["ticket"], purpose=PURPOSE_WS)
    assert len(tickets._used) == 5
    time.sleep(1.1)
    redeem_ticket(mint_ticket("alice")["ticket"], purpose=PURPOSE_WS)
    assert len(tickets._used) == 1, "expired replay records were not swept"


# ---------------------------------------------------------------------------#
# through the API                                                             #
# ---------------------------------------------------------------------------#


def test_minting_needs_a_real_credential_in_the_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _, _, source = _client(tmp_path, monkeypatch)
    assert client.post(f"{_AUDIO}/tickets").status_code == 401


def test_a_socket_opens_with_a_ticket_and_no_token_in_the_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _, _, source = _client(tmp_path, monkeypatch)
    sid = _create_session(client, source, headers=ALICE)["session_id"]

    minted = client.post(f"{_AUDIO}/tickets?purpose=ws&session_id={sid}", headers=ALICE)
    assert minted.status_code == 200, minted.text
    ticket = minted.json()["ticket"]
    assert ticket and minted.json()["expires_in"] > 0

    with client.websocket_connect(f"{_AUDIO}/sessions/{sid}/ws?ticket={ticket}") as ws:
        assert ws.receive_json()["event_type"] == "session.opened"


def test_a_ticket_does_not_open_somebody_elses_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _, _, source = _client(tmp_path, monkeypatch)
    sid = _create_session(client, source, headers=ALICE)["session_id"]
    ticket = client.post(f"{_AUDIO}/tickets?purpose=ws", headers=BOB).json()["ticket"]

    with client.websocket_connect(f"{_AUDIO}/sessions/{sid}/ws?ticket={ticket}") as ws:
        frame = ws.receive_json()
    assert frame["event_type"] == "error"


def test_a_ticket_bound_to_another_session_is_refused_at_the_handshake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _, _, source = _client(tmp_path, monkeypatch)
    mine = _create_session(client, source, headers=ALICE)["session_id"]
    other = _create_session(client, source, headers=ALICE)["session_id"]
    ticket = client.post(
        f"{_AUDIO}/tickets?purpose=ws&session_id={other}", headers=ALICE
    ).json()["ticket"]

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"{_AUDIO}/sessions/{mine}/ws?ticket={ticket}") as ws:
            ws.receive_json()


def test_a_reused_ticket_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: a URL that leaks after the fact is worth nothing."""
    client, _, _, _, source = _client(tmp_path, monkeypatch)
    sid = _create_session(client, source, headers=ALICE)["session_id"]
    ticket = client.post(
        f"{_AUDIO}/tickets?purpose=ws&session_id={sid}", headers=ALICE
    ).json()["ticket"]

    with client.websocket_connect(f"{_AUDIO}/sessions/{sid}/ws?ticket={ticket}") as ws:
        ws.receive_json()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"{_AUDIO}/sessions/{sid}/ws?ticket={ticket}") as ws:
            ws.receive_json()


def test_a_raw_token_still_works_while_clients_catch_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Breaking every open console to close this would be the wrong trade; the
    server warns instead."""
    client, _, _, _, source = _client(tmp_path, monkeypatch)
    sid = _create_session(client, source, headers=ALICE)["session_id"]
    with client.websocket_connect(f"{_AUDIO}/sessions/{sid}/ws?token=tok_a") as ws:
        assert ws.receive_json()["event_type"] == "session.opened"


def test_with_auth_off_no_ticket_is_pretended_to_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no credential to keep out of the URL, and handing back a ticket
    would imply there was one."""
    monkeypatch.delenv("AGENTIC_AUDIO_REQUIRE_AUTH", raising=False)
    client, _, _, _, source = _client_with_decisions(tmp_path, _bootstrap_decisions())
    body = client.post(f"{_AUDIO}/tickets").json()
    assert body["required"] is False
    assert body["ticket"] == ""
