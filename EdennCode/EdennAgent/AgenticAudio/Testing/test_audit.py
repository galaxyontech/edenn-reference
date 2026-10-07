"""Who did the things that cost money or changed access.

When a bill is disputed, a session is wiped, or a collaborator sees something
they should not have, the question is always "who did this, and when" — and the
studio could not answer it. Sessions record what happened to them, not who
caused it, and tool calls are attributed to the agent rather than to the person
who asked.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from EdennCode.EdennAgent.AgenticAudio.api import audit
from EdennCode.EdennAgent.AgenticAudio.Testing.test_agentic_audio_api import (
    _AUDIO,
    _client_with_memory_collab,
    _create_session,
)

OWNER = {"Authorization": "Bearer tok_owner"}
GUEST = {"Authorization": "Bearer tok_guest"}


def _lines(caplog: pytest.LogCaptureFixture) -> list[dict]:
    out = []
    for record in caplog.records:
        if record.name != "edenn.agentic_audio.audit":
            continue
        out.append(json.loads(record.getMessage().split("audit ", 1)[1]))
    return out


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "tok_owner:owner,tok_guest:guest")
    monkeypatch.setenv("AGENTIC_AUDIO_SHARE_SECRET", "test-share-secret")
    c, source = _client_with_memory_collab(tmp_path)
    return c, source


# ---------------------------------------------------------------------------#
# the shape of a line                                                         #
# ---------------------------------------------------------------------------#


def test_a_line_names_the_actor_the_action_and_the_subject(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="edenn.agentic_audio.audit"):
        audit.record("session.deleted", actor="alice", session_id="sess_1",
                     kind=audit.DESTRUCTIVE)
    line = _lines(caplog)[0]
    assert line["actor"] == "alice"
    assert line["action"] == "session.deleted"
    assert line["session_id"] == "sess_1"
    assert line["kind"] == "destructive"
    assert line["ts"] > 0


def test_an_unknown_actor_says_so_rather_than_being_blank(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A blank actor field reads like a bug in the logger rather than a fact
    about the request.

    It says "unknown", not "unauthenticated": the deployment logged every billed
    generation as unauthenticated while the callers were signed in, which turned
    a missing wire into a claim about the request (live audit, 2026-08-30).
    """
    with caplog.at_level(logging.INFO, logger="edenn.agentic_audio.audit"):
        audit.record("generation.generate_candidates", actor=None, session_id="s")
    assert _lines(caplog)[0]["actor"] == "unknown"


def test_content_never_reaches_the_log(caplog: pytest.LogCaptureFixture) -> None:
    """An audit trail must not become a second copy of the user's work sitting
    in a log aggregator with different access rules from the database."""
    with caplog.at_level(logging.INFO, logger="edenn.agentic_audio.audit"):
        audit.record(
            "generation.generate_voiceover",
            actor="alice",
            detail={
                "script": "Some moments do not need a stage at all.",
                "filename": "my_private_wedding_video.mp4",
                "voice_id": "warm_female",
                "count": 3,
            },
        )
    detail = _lines(caplog)[0]["detail"]
    assert "voice_id" in detail and "count" in detail
    assert "Some moments" not in json.dumps(detail), "a script was copied into the log"


def test_a_long_value_is_dropped_rather_than_truncated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Truncation still copies the beginning of a prompt, which is the part that
    identifies it."""
    with caplog.at_level(logging.INFO, logger="edenn.agentic_audio.audit"):
        audit.record("generation.x", actor="alice", detail={"prompt": "p" * 400})
    assert "prompt" not in (_lines(caplog)[0].get("detail") or {})


def test_auditing_never_fails_the_action() -> None:
    """An audit call that can fail a request is an audit system people disable."""

    class Unserialisable:
        def __repr__(self):
            raise RuntimeError("nope")

    audit.record("x", actor="a", detail={"bad": Unserialisable()})  # must not raise


# ---------------------------------------------------------------------------#
# through the API                                                             #
# ---------------------------------------------------------------------------#


def test_deleting_a_session_is_recorded(client, caplog) -> None:
    c, source = client
    sid = _create_session(c, source, headers=OWNER)["session_id"]
    with caplog.at_level(logging.INFO, logger="edenn.agentic_audio.audit"):
        c.delete(f"{_AUDIO}/sessions/{sid}?confirm={sid}", headers=OWNER)
    actions = {l["action"]: l for l in _lines(caplog)}
    assert "session.deleted" in actions
    assert actions["session.deleted"]["actor"] == "owner"


def test_removing_a_collaborator_is_recorded(client, caplog) -> None:
    """The line someone comes looking for when access disappeared."""
    c, source = client
    sid = _create_session(c, source, headers=OWNER)["session_id"]
    grant = c.post(
        f"{_AUDIO}/sessions/{sid}/collab/links", json={"role": "comment"}, headers=OWNER
    ).json()["grant"]
    c.post(f"{_AUDIO}/sessions/{sid}/collab/join", json={"grant": grant}, headers=GUEST)

    with caplog.at_level(logging.INFO, logger="edenn.agentic_audio.audit"):
        c.delete(f"{_AUDIO}/sessions/{sid}/collab/participants/guest", headers=OWNER)
    actions = {l["action"]: l for l in _lines(caplog)}
    assert "collaborator.removed" in actions
    assert actions["collaborator.removed"]["subject"] == "guest"
    assert actions["collaborator.removed"]["actor"] == "owner"


def test_minting_and_revoking_a_link_are_both_recorded(client, caplog) -> None:
    c, source = client
    sid = _create_session(c, source, headers=OWNER)["session_id"]
    with caplog.at_level(logging.INFO, logger="edenn.agentic_audio.audit"):
        c.post(f"{_AUDIO}/sessions/{sid}/collab/links", json={"role": "comment"}, headers=OWNER)
        c.post(f"{_AUDIO}/sessions/{sid}/collab/links/revoke", headers=OWNER)
    actions = {l["action"] for l in _lines(caplog)}
    assert {"invite_link.minted", "invite_links.revoked"} <= actions


def test_exporting_a_session_is_recorded(client, caplog) -> None:
    """An export is the whole session in one response; taking it is worth a line."""
    c, source = client
    sid = _create_session(c, source, headers=OWNER)["session_id"]
    with caplog.at_level(logging.INFO, logger="edenn.agentic_audio.audit"):
        c.get(f"{_AUDIO}/sessions/{sid}/export", headers=OWNER)
    assert "session.exported" in {l["action"] for l in _lines(caplog)}


def test_reading_a_session_is_not_recorded(client, caplog) -> None:
    """A log that records everything is a log nobody reads. Only spending,
    access changes and destruction earn a line."""
    c, source = client
    sid = _create_session(c, source, headers=OWNER)["session_id"]
    with caplog.at_level(logging.INFO, logger="edenn.agentic_audio.audit"):
        c.get(f"{_AUDIO}/sessions/{sid}", headers=OWNER)
        c.get(f"{_AUDIO}/sessions", headers=OWNER)
    assert _lines(caplog) == []


def test_audit_lines_go_to_their_own_logger(caplog: pytest.LogCaptureFixture) -> None:
    """So a deployment can route or retain them differently from application
    noise without parsing message text."""
    with caplog.at_level(logging.INFO):
        audit.record("session.deleted", actor="alice")
    assert any(r.name == "edenn.agentic_audio.audit" for r in caplog.records)


def test_a_turn_driven_over_the_socket_names_who_drove_it(client, caplog) -> None:
    """The console talks to the agent over a WebSocket, and a socket has no HTTP
    middleware above it to bind who is calling. So the main path — the one a
    signed-in owner actually uses — wrote every audit line with a blank actor,
    including the spend lines. An audit trail whose actor is empty for the
    normal case is not a trail.
    """
    c, source = client
    sid = _create_session(c, source, headers=OWNER)["session_id"]

    with caplog.at_level(logging.INFO, logger="edenn.agentic_audio.audit"):
        with c.websocket_connect(
            f"{_AUDIO}/sessions/{sid}/ws?token=tok_owner"
        ) as websocket:
            assert websocket.receive_json()["event_type"] == "session.opened"
            # A free choice: it audits, and it does not spend.
            websocket.send_json(
                {"choice_type": "proposal", "target_id": "proposal_cinematic"}
            )
            assert websocket.receive_json()["event_type"] == "choice.recorded"

    lines = [l for l in _lines(caplog) if l.get("session_id") == sid]
    assert lines, "the socket turn recorded nothing at all"
    blank = [l for l in lines if not l.get("actor") or l["actor"] == "unknown"]
    assert not blank, f"socket turn audited with no actor: {blank}"
