"""Whose media is this?

Every take, mix and effects bed on the deployment is served from one route, and
it asked only whether the caller was SOMEBODY. Any valid token read any other
customer's footage and finished music, given a filename — and filenames travel:
into a shared link, a copied URL, a support ticket. Authenticated is not
authorised, and a random filename is obscurity rather than access control.

The question is answered from the CALLER's side: the sessions they can already
open, their own and any they were invited to. An unknown filename is simply not
found, which is also the honest answer — a customer who may not have a file
should not learn from us that it exists.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _deployed_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("EDENN_DEV_REAL_MUSIC", "0")
    monkeypatch.delenv("EDENN_CREATION_MEDIA_DIR", raising=False)
    monkeypatch.setenv("CONTAINER_APP_NAME", "edenn-standalone")
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "tok_a:alice,tok_b:bob")
    mod = importlib.import_module("EdennCode.EdennAgent.AgenticAudio.design.devserver")
    return mod, mod.build_app()


def _plant_media(app, name: str, owner: str) -> None:
    """A media file on disk, referenced by a session that `owner` owns."""
    media_dir = Path(app.state.media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / name).write_bytes(b"RIFF....WAVEfmt ")

    repo = app.state.agent_repository
    from EdennCode.EdennAgent.AgenticAudio.models import AgenticAudioSessionPhase

    repo.create_session(
        source_video_artifact_id="art",
        creator_user_id=owner,
        phase=AgenticAudioSessionPhase.AWAITING_CANDIDATE_CHOICE,
        state_json={"candidates": [{"candidate_id": "c1",
                                    "audio_url": f"/dev/media/{name}"}]},
    )


def test_another_customer_cannot_read_your_music(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole finding, in one assertion."""
    mod, app = _deployed_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        _plant_media(app, "gen_alice_take.mp3", "alice")

        mine = client.get("/dev/media/gen_alice_take.mp3",
                          headers={"Authorization": "Bearer tok_a"})
        assert mine.status_code == 200, "the owner cannot read their own take"

        theirs = client.get("/dev/media/gen_alice_take.mp3",
                            headers={"Authorization": "Bearer tok_b"})
        assert theirs.status_code == 404, (
            "another customer read this take with nothing but its filename"
        )


def test_an_unauthenticated_caller_still_gets_nowhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod, app = _deployed_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        _plant_media(app, "gen_take.mp3", "alice")
        assert client.get("/dev/media/gen_take.mp3").status_code in (401, 403)


def test_a_file_nobody_references_is_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Not 403. A refusal that distinguishes "exists but not yours" from "does
    not exist" hands out the existence of other people's work."""
    mod, app = _deployed_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        media_dir = Path(app.state.media_dir)
        media_dir.mkdir(parents=True, exist_ok=True)
        (media_dir / "orphan.mp3").write_bytes(b"RIFF")

        assert client.get("/dev/media/orphan.mp3",
                          headers={"Authorization": "Bearer tok_a"}).status_code == 404


def test_a_laptop_with_auth_off_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With auth off every caller is an implicit owner, which is what the rest
    of this server already assumes locally. The check must not make local
    development need credentials."""
    monkeypatch.setenv("EDENN_DEV_REAL_MUSIC", "0")
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("WEBSITE_HOSTNAME", raising=False)
    monkeypatch.delenv("EDENN_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("AGENTIC_AUDIO_REQUIRE_AUTH", raising=False)
    monkeypatch.delenv("AGENTIC_AUDIO_API_KEYS", raising=False)
    mod = importlib.import_module("EdennCode.EdennAgent.AgenticAudio.design.devserver")
    app = mod.build_app()
    with TestClient(app) as client:
        media_dir = Path(app.state.media_dir)
        media_dir.mkdir(parents=True, exist_ok=True)
        (media_dir / "local.mp3").write_bytes(b"RIFF")
        assert client.get("/dev/media/local.mp3").status_code == 200
