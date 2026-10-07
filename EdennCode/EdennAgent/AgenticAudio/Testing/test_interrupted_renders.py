"""What a session is told when the container it was rendering on went away.

Sessions can be durable. The job table is not — it lives in memory on the
standalone. So a restart leaves a session pointing at a job id that exists
nowhere, and the candidate that referenced it sits at "queued" forever: a
spinner with nothing behind it, on work the customer has already paid for.
Nothing times it out and nothing reports it, because from the session's point of
view the render simply never came back.

This does not recover the money and does not pretend to — the generation may
well have finished upstream after the process died. It stops the lie, which is
the part the customer experiences.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient


def _app(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EDENN_DEV_REAL_MUSIC", "0")
    monkeypatch.delenv("EDENN_CREATION_MEDIA_DIR", raising=False)
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("AGENTIC_AUDIO_REQUIRE_AUTH", raising=False)
    mod = importlib.import_module("EdennCode.EdennAgent.AgenticAudio.design.devserver")
    return mod, mod.build_app()


def _session_with_orphan(app, *, job_id: str = "job_gone") -> str:
    from EdennCode.EdennAgent.AgenticAudio.models import AgenticAudioSessionPhase

    repo = app.state.agent_repository
    session = repo.create_session(
        source_video_artifact_id="art",
        phase=AgenticAudioSessionPhase.GENERATING_CANDIDATES,
        state_json={
            "candidates": [{
                "candidate_id": "c1", "status": "queued", "linked_job_id": job_id,
            }],
            "layers": {"voiceover": {"status": "queued", "linked_job_id": job_id}},
        },
    )
    return session.session_id


def test_a_render_that_cannot_still_be_running_is_marked_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod, app = _app(monkeypatch)
    with TestClient(app) as client:
        session_id = _session_with_orphan(app)
        # Startup already ran; do the pass again now the session exists, which
        # is what a restart with durable sessions would have found.
        repaired = app.state.reconcile_interrupted_renders()
        assert repaired == 1

        state = app.state.agent_repository.get_session(session_id).state_json
        candidate = state["candidates"][0]
        assert candidate["status"] == "failed"
        assert "restarted" in candidate["error"]
        assert state["layers"]["voiceover"]["status"] == "failed"


def test_a_render_whose_job_still_exists_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job that is genuinely still running must not be declared dead."""
    mod, app = _app(monkeypatch)
    with TestClient(app) as client:
        repo = app.state.agent_repository
        async_repo = app.state.async_repository
        job = async_repo.create_job(job_type="video_music", request_json={})

        from EdennCode.EdennAgent.AgenticAudio.models import AgenticAudioSessionPhase

        session = repo.create_session(
            source_video_artifact_id="art",
            phase=AgenticAudioSessionPhase.GENERATING_CANDIDATES,
            state_json={"candidates": [{
                "candidate_id": "c1", "status": "queued",
                "linked_job_id": job.job_id,
            }]},
        )
        app.state.reconcile_interrupted_renders()

        state = repo.get_session(session.session_id).state_json
        assert state["candidates"][0]["status"] == "queued"


def test_a_finished_take_is_never_re_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only work that is still claiming to be in progress is in question."""
    mod, app = _app(monkeypatch)
    with TestClient(app) as client:
        from EdennCode.EdennAgent.AgenticAudio.models import AgenticAudioSessionPhase

        repo = app.state.agent_repository
        session = repo.create_session(
            source_video_artifact_id="art",
            phase=AgenticAudioSessionPhase.AWAITING_CANDIDATE_CHOICE,
            state_json={"candidates": [{
                "candidate_id": "c1", "status": "completed",
                "linked_job_id": "job_long_gone",
                "audio_url": "/dev/media/take.mp3",
            }]},
        )
        app.state.reconcile_interrupted_renders()

        state = repo.get_session(session.session_id).state_json
        assert state["candidates"][0]["status"] == "completed"


def test_the_reconcile_never_blocks_a_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cleanup pass that can stop the service starting is worse than the
    problem it cleans up."""
    devserver = Path(
        "EdennCode/EdennAgent/AgenticAudio/design/devserver.py"
    ).read_text()
    startup = devserver.split("async def _startup()")[1][:900]
    assert "try:" in startup and "except Exception" in startup
