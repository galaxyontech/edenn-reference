"""Collab-mode API tests: threads, comments, reactions, participants, share
roles, and the @agent handoff. Hermetic — in-memory repos + the scripted LLM
client from the main API test-suite, no network, no Postgres.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.EdennAgent.AgenticAudio.api import create_agentic_audio_router
from EdennCode.EdennAgent.AgenticAudio.persistence.collab import (
    InMemoryCollabRepository,
    build_collab_payload,
)
from EdennCode.EdennAgent.AgenticAudio.tools import AgenticAudioTools

from EdennCode.EdennAgent.AgenticAudio.Testing.test_agentic_audio_api import (
    _MemoryAgenticRepository,
    _MemoryAsyncRepository,
    _MemoryQueue,
    _ScriptedAgentClient,
    _bootstrap_decisions,
    _context,
    _fake_analyze,
    _seed_source_video,
)


def _collab_client(
    tmp_path: Path,
) -> tuple[TestClient, InMemoryCollabRepository, _MemoryAgenticRepository]:
    agent_repo = _MemoryAgenticRepository()
    async_repo = _MemoryAsyncRepository()
    queue = _MemoryQueue()
    _seed_source_video(async_repo)
    collab_repo = InMemoryCollabRepository()
    context = _context(tmp_path)
    tools = AgenticAudioTools(
        async_repository=async_repo,
        queue=queue,
        settings=context.settings,
        analyze_fn=_fake_analyze,
    )
    app = FastAPI()
    app.include_router(
        create_agentic_audio_router(
            context,  # type: ignore[arg-type]
            repository=agent_repo,
            async_repository=async_repo,
            queue=queue,
            tools=tools,
            llm_client=_ScriptedAgentClient(_bootstrap_decisions()),
            collab_repository=collab_repo,
        )
    )
    tc = TestClient(app)
    _COLLAB_REPOS[id(tc)] = agent_repo
    _ASYNC_REPOS[id(tc)] = async_repo
    return tc, collab_repo, agent_repo


# Maps a collab TestClient to its agent repo so _create_session can seed a real
# lineage node — comment anchors are validated against the session's nodes, so
# tests must anchor to a candidate that actually exists (mirrors the frontend,
# which only ever anchors to real canvas nodes).
_COLLAB_REPOS: dict[int, Any] = {}
#: …and to its job store, for the tests that ask who a render was billed to.
_ASYNC_REPOS: dict[int, Any] = {}


def _async_repo_for(client: TestClient) -> Any:
    return _ASYNC_REPOS[id(client)]


def _create_session(client: TestClient, headers: dict[str, str] | None = None) -> str:
    response = client.post(
        "/api/v2/agentic/audio/sessions",
        json={
            "source_video_artifact_id": "artifact_source_video",
            "creator_user_id": "creator_1",
            "initial_message": "Score it.",
        },
        headers=headers or {},
    )
    assert response.status_code == 200, response.text
    session_id = response.json()["session_id"]
    repo = _COLLAB_REPOS.get(id(client))
    if repo is not None:
        sess = repo.get_session(session_id)
        state = dict(sess.state_json)
        state["proposals"] = list(state.get("proposals") or []) + [
            {"proposal_id": "proposal_1", "title": "Take", "prompt": "x", "modelspec": "edenn_basic"}
        ]
        state["candidates"] = list(state.get("candidates") or []) + [
            {"candidate_id": "candidate_1", "proposal_id": "proposal_1", "title": "Take 1",
             "status": "completed", "audio_url": "https://cdn.test/c1.mp3", "version": 1}
        ]
        repo.update_session(session_id, state_json=state)
    return session_id


def test_collab_thread_rejects_unknown_anchor_node(tmp_path: Path) -> None:
    """Scenario 8.10: a thread anchored to a node that isn't in the lineage is a
    404, not a silently-stored dangling anchor."""
    client, _, _ = _collab_client(tmp_path)
    session_id = _create_session(client)
    base = f"/api/v2/agentic/audio/sessions/{session_id}/collab"
    r = client.post(f"{base}/threads",
                    json={"body": "on nothing", "anchor_node_id": "candidate_does_not_exist_xyz"})
    assert r.status_code == 404, r.text
    # A real node (seeded candidate_1) + the source still work.
    assert client.post(f"{base}/threads",
                       json={"body": "ok", "anchor_node_id": "candidate_1"}).status_code == 200
    assert client.post(f"{base}/threads",
                       json={"body": "src", "anchor_node_id": "__source__"}).status_code == 200


def test_collab_body_length_is_capped(tmp_path: Path) -> None:
    """Scenario 8.10: an unbounded comment/thread body is rejected, not stored."""
    client, _, _ = _collab_client(tmp_path)
    session_id = _create_session(client)
    base = f"/api/v2/agentic/audio/sessions/{session_id}/collab"
    r = client.post(f"{base}/threads",
                    json={"body": "A" * 100000, "anchor_node_id": "candidate_1"})
    assert r.status_code == 422, r.status_code  # Pydantic max_length rejection


def test_collab_thread_lifecycle(tmp_path: Path) -> None:
    client, _, _ = _collab_client(tmp_path)
    session_id = _create_session(client)
    base = f"/api/v2/agentic/audio/sessions/{session_id}/collab"

    # Empty panel payload to start.
    empty = client.get(base)
    assert empty.status_code == 200
    assert empty.json()["threads"] == []
    assert [a["id"] for a in empty.json()["agents"]] == ["edenn"]

    # Create an anchored thread.
    created = client.post(
        f"{base}/threads",
        json={
            "body": "The swell at 0:14 is muddy — thin the low end.",
            "anchor_node_id": "candidate_1",
            "anchor_label": "Scene 2 / Ambience",
            "anchor_start_s": 12.0,
            "anchor_end_s": 18.0,
            "author_id": "yuna",
            "author_name": "Yuna He",
        },
    )
    assert created.status_code == 200, created.text
    thread = created.json()["thread"]
    thread_id = thread["thread_id"]
    assert thread["anchor_node_id"] == "candidate_1"
    assert thread["anchor_label"] == "Scene 2 / Ambience"
    assert thread["status"] == "open"
    assert len(thread["comments"]) == 1
    first_comment = thread["comments"][0]
    assert first_comment["author_name"] == "Yuna He"

    # Reply.
    reply = client.post(
        f"{base}/threads/{thread_id}/comments",
        json={"body": "Agreed — and 10% quieter.", "author_id": "marco", "author_name": "Marco Reyes"},
    )
    assert reply.status_code == 200
    assert len(reply.json()["thread"]["comments"]) == 2

    # Resolve, then a new reply reopens.
    resolved = client.patch(
        f"{base}/threads/{thread_id}", json={"status": "resolved", "actor_id": "yuna"}
    )
    assert resolved.status_code == 200
    assert resolved.json()["thread"]["status"] == "resolved"
    assert resolved.json()["thread"]["resolved_by"] == "yuna"
    reopened = client.post(
        f"{base}/threads/{thread_id}/comments",
        json={"body": "Actually, one more pass?", "author_id": "marco"},
    )
    assert reopened.status_code == 200
    assert reopened.json()["thread"]["status"] == "open"

    # Edit + react + soft-delete on the first comment.
    comment_id = first_comment["comment_id"]
    edited = client.patch(
        f"{base}/comments/{comment_id}", json={"body": "Thin the low end please."}
    )
    assert edited.status_code == 200
    assert edited.json()["comment"]["body"] == "Thin the low end please."
    assert edited.json()["comment"]["edited_at"] is not None

    reacted = client.put(
        f"{base}/comments/{comment_id}/reactions",
        json={"emoji": "👍", "on": True, "actor_id": "marco"},
    )
    assert reacted.status_code == 200
    assert reacted.json()["comment"]["reactions"] == {"👍": ["marco"]}
    unreacted = client.put(
        f"{base}/comments/{comment_id}/reactions",
        json={"emoji": "👍", "on": False, "actor_id": "marco"},
    )
    assert unreacted.json()["comment"]["reactions"] == {}

    deleted = client.delete(f"{base}/comments/{comment_id}")
    assert deleted.status_code == 200
    assert deleted.json()["comment"]["deleted"] is True
    assert deleted.json()["comment"]["body"] == ""  # never leaks the text

    # The panel payload reflects everything.
    panel = client.get(base).json()
    assert len(panel["threads"]) == 1
    assert len(panel["threads"][0]["comments"]) == 3


def test_collab_thread_validation(tmp_path: Path) -> None:
    client, _, _ = _collab_client(tmp_path)
    session_id = _create_session(client)
    base = f"/api/v2/agentic/audio/sessions/{session_id}/collab"

    empty_body = client.post(
        f"{base}/threads", json={"body": "   ", "anchor_node_id": "candidate_1"}
    )
    assert empty_body.status_code == 400

    missing_thread = client.post(
        f"{base}/threads/thread_nope/comments", json={"body": "hello"}
    )
    assert missing_thread.status_code == 404

    other_session = client.get("/api/v2/agentic/audio/sessions/nope_1/collab")
    assert other_session.status_code == 404


def test_collab_unread_counts_per_viewer(tmp_path: Path) -> None:
    client, collab_repo, _ = _collab_client(tmp_path)
    session_id = _create_session(client)
    base = f"/api/v2/agentic/audio/sessions/{session_id}/collab"
    created = client.post(
        f"{base}/threads",
        json={"body": "First", "anchor_node_id": "candidate_1", "author_id": "yuna"},
    )
    thread_id = created.json()["thread"]["thread_id"]
    client.post(
        f"{base}/threads/{thread_id}/comments", json={"body": "Second", "author_id": "marco"}
    )
    # Auth is off (viewer=None on GET), so assert per-viewer unread via the
    # payload builder directly: Yuna has 1 unread (Marco's), Marco 0 (he read on post).
    yuna = build_collab_payload(collab_repo, session_id, "yuna")
    marco = build_collab_payload(collab_repo, session_id, "marco")
    assert yuna["threads"][0]["unread"] == 1
    assert marco["threads"][0]["unread"] == 0
    collab_repo.mark_read(thread_id, "yuna")
    assert build_collab_payload(collab_repo, session_id, "yuna")["threads"][0]["unread"] == 0


def test_collab_agent_mention_hands_off_to_the_model(tmp_path: Path) -> None:
    client, _, _ = _collab_client(tmp_path)
    with client:  # persistent portal loop so the background pickup task runs
        session_id = _create_session(client)
        base = f"/api/v2/agentic/audio/sessions/{session_id}/collab"
        created = client.post(
            f"{base}/threads",
            json={
                "body": "@Edenn thin the low end on this take.",
                "anchor_node_id": "candidate_1",
                "mentions": [{"id": "edenn", "name": "Edenn Director", "kind": "agent"}],
                "author_id": "yuna",
            },
        )
        assert created.status_code == 200
        thread_id = created.json()["thread"]["thread_id"]

        # The pickup runs as a background task on the app loop; poll the panel.
        agent_comment: dict[str, Any] | None = None
        for _ in range(80):
            panel = client.get(base).json()
            comments = panel["threads"][0]["comments"]
            agent_comment = next(
                (c for c in comments if c["author_kind"] == "agent"), None
            )
            if agent_comment:
                break
            time.sleep(0.05)
        assert agent_comment is not None, "agent never replied in the thread"
        assert agent_comment["author_id"] == "edenn"
        assert agent_comment["body"]  # scripted fallback reply text
        assert thread_id == panel["threads"][0]["thread_id"]

        # The @agent turn's chat messages (user + assistant) are tagged
        # source="comment" so the console keeps them in the thread, out of the
        # MAIN chat transcript (prevents the confusing interleaved ordering).
        snap = client.get(f"/api/v2/agentic/audio/sessions/{session_id}").json()
        comment_msgs = [m for m in snap["messages"] if (m.get("payload") or {}).get("source") == "comment"]
        assert any(m["role"] == "user" for m in comment_msgs), "comment user message not tagged"
        assert any(m["role"] == "assistant" for m in comment_msgs), "comment reply not tagged"


def test_collab_agent_thread_replies_continue_without_mention(tmp_path: Path) -> None:
    """Once the agent has answered in a thread, a PLAIN reply (no @mention)
    keeps the conversation going — the follow-up must dispatch a pickup too."""
    client, _, _ = _collab_client(tmp_path)
    with client:
        session_id = _create_session(client)
        base = f"/api/v2/agentic/audio/sessions/{session_id}/collab"
        created = client.post(
            f"{base}/threads",
            json={
                "body": "@Edenn thin the low end on this take.",
                "anchor_node_id": "candidate_1",
                "mentions": [{"id": "edenn", "name": "Edenn Director", "kind": "agent"}],
                "author_id": "yuna",
            },
        )
        thread_id = created.json()["thread"]["thread_id"]

        def agent_comment_count() -> int:
            panel = client.get(base).json()
            return sum(
                1
                for c in panel["threads"][0]["comments"]
                if c["author_kind"] == "agent"
            )

        for _ in range(80):
            if agent_comment_count() >= 1:
                break
            time.sleep(0.05)
        assert agent_comment_count() == 1, "first pickup never replied"

        # Follow-up WITHOUT any mention — conversational continuity.
        followup = client.post(
            f"{base}/threads/{thread_id}/comments",
            json={"body": "make the music starting point more punchy", "author_id": "yuna"},
        )
        assert followup.status_code == 200
        for _ in range(80):
            if agent_comment_count() >= 2:
                break
            time.sleep(0.05)
        assert agent_comment_count() == 2, "plain reply in an agent thread did not dispatch"

        # And the agent's own replies never re-trigger a pickup (no runaway).
        time.sleep(0.3)
        assert agent_comment_count() == 2


def test_collab_share_roles_enforced_with_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.delenv("AGENTIC_AUDIO_API_KEYS", raising=False)
    monkeypatch.setenv("AGENTIC_AUDIO_TOKEN_IS_USER", "1")
    # Token-is-user: any bearer authenticates as the user it names. It is a
    # local convenience, so it must be asked for by name — an empty key map
    # alone now refuses every request rather than opening the door.
    monkeypatch.setenv("AGENTIC_AUDIO_TOKEN_IS_USER", "1")
    client, _, _ = _collab_client(tmp_path)
    owner = {"Authorization": "Bearer owner_1"}
    viewer = {"Authorization": "Bearer viewer_1"}
    commenter = {"Authorization": "Bearer commenter_1"}
    stranger = {"Authorization": "Bearer stranger_1"}

    session_id = _create_session(client, headers=owner)
    base = f"/api/v2/agentic/audio/sessions/{session_id}/collab"

    # Un-shared: only the owner can even read the panel.
    assert client.get(base, headers=stranger).status_code == 403
    assert client.get(base, headers=owner).status_code == 200

    # Owner shares: viewer (view) + commenter (comment). Non-owners can't share.
    denied = client.post(
        f"{base}/participants",
        json={"user_id": "x", "role": "view"},
        headers=stranger,
    )
    assert denied.status_code == 403
    for user, role in (("viewer_1", "view"), ("commenter_1", "comment")):
        shared = client.post(
            f"{base}/participants",
            json={"user_id": user, "role": role},
            headers=owner,
        )
        assert shared.status_code == 200, shared.text

    # view: can read the panel AND the session snapshot, cannot post.
    assert client.get(base, headers=viewer).status_code == 200
    snapshot = client.get(f"/api/v2/agentic/audio/sessions/{session_id}", headers=viewer)
    assert snapshot.status_code == 200
    blocked = client.post(
        f"{base}/threads",
        json={"body": "hi", "anchor_node_id": "candidate_1"},
        headers=viewer,
    )
    assert blocked.status_code == 403

    # comment: can start a thread; author identity comes from the principal.
    posted = client.post(
        f"{base}/threads",
        json={"body": "hello", "anchor_node_id": "candidate_1", "author_id": "spoofed"},
        headers=commenter,
    )
    assert posted.status_code == 200
    assert posted.json()["thread"]["comments"][0]["author_id"] == "commenter_1"

    # Only the author (or owner) may edit/delete.
    comment_id = posted.json()["thread"]["comments"][0]["comment_id"]
    thread_id = posted.json()["thread"]["thread_id"]
    assert (
        client.patch(
            f"{base}/comments/{comment_id}", json={"body": "hacked"}, headers=viewer
        ).status_code
        == 403
    )
    assert (
        client.patch(
            f"{base}/comments/{comment_id}", json={"body": "edited"}, headers=commenter
        ).status_code
        == 200
    )
    assert (
        client.delete(f"{base}/comments/{comment_id}", headers=owner).status_code == 200
    )
    del thread_id


def test_collab_ws_turns_require_iterate_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A view/comment participant can OPEN the WS (live updates) but any turn
    frame is rejected — the same iterate bar as REST /messages and /choices."""
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.delenv("AGENTIC_AUDIO_API_KEYS", raising=False)
    monkeypatch.setenv("AGENTIC_AUDIO_TOKEN_IS_USER", "1")
    client, _, _ = _collab_client(tmp_path)
    owner = {"Authorization": "Bearer owner_1"}
    session_id = _create_session(client, headers=owner)
    base = f"/api/v2/agentic/audio/sessions/{session_id}"
    shared = client.post(
        f"{base}/collab/participants",
        json={"user_id": "viewer_1", "role": "view"},
        headers=owner,
    )
    assert shared.status_code == 200
    with client.websocket_connect(f"{base.replace('/api', '/api')}/ws?token=viewer_1") as ws:
        opened = ws.receive_json()
        assert opened["event_type"] == "session.opened"  # view role CAN watch
        ws.send_text('{"content": "generate something expensive"}')
        rejected = ws.receive_json()
        assert rejected["event_type"] == "error"
        assert "iterate" in rejected["payload"]["message"]
    # The owner's socket still turns normally.
    with client.websocket_connect(f"{base}/ws?token=owner_1") as ws:
        assert ws.receive_json()["event_type"] == "session.opened"
        ws.send_text('{"content": "hello director"}')
        # The turn streams events and ends with a fresh snapshot.
        for _ in range(50):
            evt = ws.receive_json()
            if evt["event_type"] == "session.opened":
                break
        else:
            raise AssertionError("owner turn never completed")


def test_collab_agent_mention_gated_by_iterate_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """@agent from a comment-role member must NOT run a (spend-capable) agent
    turn — it gets an honest in-thread note instead."""
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.delenv("AGENTIC_AUDIO_API_KEYS", raising=False)
    monkeypatch.setenv("AGENTIC_AUDIO_TOKEN_IS_USER", "1")
    client, _, agent_repo = _collab_client(tmp_path)
    owner = {"Authorization": "Bearer owner_1"}
    with client:
        session_id = _create_session(client, headers=owner)
        base = f"/api/v2/agentic/audio/sessions/{session_id}/collab"
        client.post(
            f"{base}/participants",
            json={"user_id": "commenter_1", "role": "comment"},
            headers=owner,
        )
        chat_before = len(agent_repo.list_messages(session_id))
        posted = client.post(
            f"{base}/threads",
            json={
                "body": "@Edenn Director regenerate this take louder",
                "anchor_node_id": "candidate_1",
                "mentions": [{"id": "edenn", "name": "Edenn Director", "kind": "agent"}],
            },
            headers={"Authorization": "Bearer commenter_1"},
        )
        assert posted.status_code == 200
        # The note posts from a background task — poll briefly.
        note = None
        for _ in range(60):
            panel = client.get(base, headers=owner).json()
            comments = panel["threads"][0]["comments"]
            note = next((c for c in comments if c["author_kind"] == "agent"), None)
            if note:
                break
            time.sleep(0.05)
        assert note is not None
        assert "iterate access" in note["body"]
        # Crucially: NO agent turn ran (no new chat messages were appended).
        assert len(agent_repo.list_messages(session_id)) == chat_before


def test_collab_author_name_pinned_to_principal_when_auth_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.delenv("AGENTIC_AUDIO_API_KEYS", raising=False)
    monkeypatch.setenv("AGENTIC_AUDIO_TOKEN_IS_USER", "1")
    client, _, _ = _collab_client(tmp_path)
    owner = {"Authorization": "Bearer owner_1"}
    session_id = _create_session(client, headers=owner)
    base = f"/api/v2/agentic/audio/sessions/{session_id}/collab"
    client.post(
        f"{base}/participants",
        json={"user_id": "carol", "role": "comment", "display_name": "Carol"},
        headers=owner,
    )
    posted = client.post(
        f"{base}/threads",
        json={
            "body": "approved, ship it",
            "anchor_node_id": "candidate_1",
            "author_name": "Alice (Owner)",  # spoof attempt
        },
        headers={"Authorization": "Bearer carol"},
    )
    assert posted.status_code == 200
    comment = posted.json()["thread"]["comments"][0]
    assert comment["author_id"] == "carol"
    assert comment["author_name"] == "Carol"  # server-resolved, not the spoof


def test_collab_session_read_stays_owner_only_for_strangers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.delenv("AGENTIC_AUDIO_API_KEYS", raising=False)
    monkeypatch.setenv("AGENTIC_AUDIO_TOKEN_IS_USER", "1")
    client, _, _ = _collab_client(tmp_path)
    owner = {"Authorization": "Bearer owner_1"}
    stranger = {"Authorization": "Bearer stranger_1"}
    session_id = _create_session(client, headers=owner)
    got = client.get(f"/api/v2/agentic/audio/sessions/{session_id}", headers=stranger)
    assert got.status_code == 403


def test_a_render_is_attributed_to_whoever_actually_ran_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every job row was stamped with the session's OWNER, whoever pressed the
    button. A collaborator invited to iterate spends real provider credit, and
    the row said somebody else did — while the rate limiter and the audit log,
    reading the same request, recorded the truth. Two records of one action
    that disagree cannot become a bill, and billing is the next thing that
    reads this column."""
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.delenv("AGENTIC_AUDIO_API_KEYS", raising=False)
    monkeypatch.setenv("AGENTIC_AUDIO_TOKEN_IS_USER", "1")
    client, _, agent_repo = _collab_client(tmp_path)
    owner = {"Authorization": "Bearer owner_1"}
    helper = {"Authorization": "Bearer helper_1"}

    session_id = _create_session(client, headers=owner)
    shared = client.post(
        f"/api/v2/agentic/audio/sessions/{session_id}/collab/participants",
        json={"user_id": "helper_1", "role": "iterate"},
        headers=owner,
    )
    assert shared.status_code == 200, shared.text

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session_id}/choices",
        json={"choice_type": "voiceover", "payload": {
            "voice_id": "calm_male",
            "narration_segments": [{"text": "A quiet moment.", "start_s": 1.0}],
        }},
        headers=helper,
    )
    assert response.status_code == 200, response.text

    voiceover = agent_repo.get_session(session_id).state_json["layers"]["voiceover"]
    job = _async_repo_for(client).jobs[voiceover["linked_job_id"]]
    assert job.creator_user_id == "owner_1", "the session still belongs to its owner"
    assert job.actor_user_id == "helper_1", "the render was attributed to the wrong person"
