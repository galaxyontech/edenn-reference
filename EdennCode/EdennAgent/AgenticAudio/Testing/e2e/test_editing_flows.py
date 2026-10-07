"""Hermetic regression for the EDITING flows (runs every commit, no model).

Locks in the behaviors verified by the real-LLM probe + adversarial review:
a music STYLE CHANGE on a studio track engages a true ``audio_creative_edit``
job carrying the new style + the source track; a same-style variation is a
``regenerate``; an extend carries ``extend_seconds``; a basic-model restyle
honestly falls back to ``regenerate``; and a volume/keep-original tweak is
cost-free (no new generation job). Each test seeds a finished candidate directly
and drives ONE edit, then asserts the real job/candidate state.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Optional

from EdennCode.EdennAgent.AgenticAudio.models import AgenticAudioSessionPhase

from .driver import E2EDriver
from .fakes import ScriptedAgentClient


def _seed_completed(driver: E2EDriver, *, provider: str, modelspec: str, cid: str = "cand1", audio: bool = True) -> dict[str, Any]:
    """Seed a session whose single candidate has finished rendering — the state a
    real edit starts from (a fresh candidate would otherwise be queued/no-audio)."""
    cand = {
        "candidate_id": cid,
        "proposal_id": "p1",
        "title": "Take",
        "prompt": "warm cinematic strings, lush and emotive",
        "modelspec": modelspec,
        "provider": provider,
        "include_vocals": False,
        "vocal_gender": "female",
        "music_volume": 0.85,
        "preserve_original_audio": False,
        "status": "completed",
        "version": 1,
        "audio_url": f"https://fake.blob/audio/{cid}.mp3" if audio else None,
        "video_url": f"https://fake.blob/video/{cid}.mp4" if audio else None,
        "provider_audio_id": f"prov_{cid}",
    }
    session = driver.agent_repo.create_session(
        session_id="s1",
        source_video_artifact_id=driver.source.artifact_id,
        creator_user_id="eval",
        phase=AgenticAudioSessionPhase.GENERATING_CANDIDATES,
        state_json={
            "approved_direction": True,
            "approved_proposal_id": "p1",
            "candidates": [cand],
            "selected_candidate_id": cid,
        },
    )
    driver.session_id = session.session_id
    return cand


def _edit_decision(cid: str, edit_kind: str, *, prompt: Optional[str] = None, extend_seconds: Optional[float] = None) -> list[dict[str, Any]]:
    args: dict[str, Any] = {"candidate_id": cid, "edit_kind": edit_kind}
    if prompt:
        args["prompt"] = prompt
    if extend_seconds is not None:
        args["extend_seconds"] = extend_seconds
    return [
        {
            "thought": "Apply the requested edit.",
            "intent": "restyle",
            "assistant_message": "Working on that edit.",
            "action": {"type": "call_tool", "tool_name": "edit_audio", "tool_args": args},
        }
    ]


def _last_job(driver: E2EDriver) -> Any:
    env = driver.queue.envelopes[-1]
    return driver.async_repo.jobs[env.job_id]


def _child(driver: E2EDriver, parent: str) -> dict[str, Any]:
    cands = driver.agent_repo.get_session(driver.session_id).state_json["candidates"]
    return next(c for c in cands if c.get("parent_candidate_id") == parent)


def test_style_change_engages_creative_edit() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        driver = E2EDriver(Path(tmp), ScriptedAgentClient(_edit_decision("cand1", "creative_edit", prompt="lo-fi chillhop, warm tape, mellow drums")))
        _seed_completed(driver, provider="provider_c", modelspec="edenn_studio")
        before = len(driver.queue.envelopes)
        resp = driver.message("Turn this into a lo-fi version of the same track.")
        assert resp.status_code == 200
        assert len(driver.queue.envelopes) == before + 1, "exactly one regeneration job"

        job = _last_job(driver)
        rj = job.request_json
        assert job.job_type == "audio_creative_edit", "a true audio-to-audio restyle"
        assert rj["agentic_edit_kind"] == "creative_edit"
        assert rj["source_audio_url"], "restyles the ACTUAL finished track (non-null source audio)"
        assert "lo-fi" in rj["user_prompt"].lower(), "the NEW style reached the job"
        assert "cinematic" not in rj["user_prompt"].lower(), "not a copy of the old prompt"

        v2 = _child(driver, "cand1")
        assert v2["edit_kind"] == "creative_edit"
        assert not v2.get("requested_edit_kind"), "engaged, not a fallback"


def test_variation_regenerates_same_direction() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        driver = E2EDriver(Path(tmp), ScriptedAgentClient(_edit_decision("cand1", "regenerate")))
        _seed_completed(driver, provider="provider_c", modelspec="edenn_studio")
        before = len(driver.queue.envelopes)
        resp = driver.message("Give me another take in the same style.")
        assert resp.status_code == 200
        assert len(driver.queue.envelopes) == before + 1

        job = _last_job(driver)
        assert job.job_type == "video_music"
        assert job.request_json["agentic_edit_kind"] == "regenerate"
        assert "cinematic" in job.request_json["user_prompt"].lower(), "keeps the same direction"
        assert _child(driver, "cand1")["edit_kind"] == "regenerate"


def test_extend_carries_extend_seconds() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        driver = E2EDriver(Path(tmp), ScriptedAgentClient(_edit_decision("cand1", "extend", extend_seconds=30.0)))
        _seed_completed(driver, provider="provider_c", modelspec="edenn_studio")
        before = len(driver.queue.envelopes)
        resp = driver.message("Extend the track to about 30 seconds.")
        assert resp.status_code == 200
        assert len(driver.queue.envelopes) == before + 1

        job = _last_job(driver)
        rj = job.request_json
        assert job.job_type == "video_music"
        assert rj["agentic_edit_kind"] == "extend"
        assert rj["extend_seconds"] == 30.0, "the requested length reached the job"
        # Not "native": a tier that can extend and a handle to extend from are
        # two of three, and nothing yet performs the extension. The take is
        # honestly labelled a regeneration until a consumer exists — see
        # Testing/test_extend_honesty.py.
        from EdennCode.EdennAgent.AgenticAudio.models import (
            NATIVE_EXTEND_CONSUMER_AVAILABLE,
        )

        assert rj["agentic_extend_mode"] == (
            "native" if NATIVE_EXTEND_CONSUMER_AVAILABLE else "regenerate_fallback"
        ), "the take says which of the two actually happened"
        child = _child(driver, "cand1")
        assert child["edit_kind"] == "extend"
        assert child["extend_mode"] == rj["agentic_extend_mode"], (
            "the take and the job agree on what actually happened"
        )


def test_basic_restyle_falls_back_to_regenerate_honestly() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        driver = E2EDriver(Path(tmp), ScriptedAgentClient(_edit_decision("cand1", "creative_edit", prompt="lo-fi version, warm tape textures")))
        _seed_completed(driver, provider="provider_a", modelspec="edenn_basic")
        before = len(driver.queue.envelopes)
        resp = driver.message("Make it a lo-fi version of this exact track.")
        assert resp.status_code == 200
        assert len(driver.queue.envelopes) == before + 1

        job = _last_job(driver)
        assert job.job_type == "video_music", "ProviderA can't audio-to-audio restyle"
        assert job.request_json["agentic_edit_kind"] == "regenerate"
        child = _child(driver, "cand1")
        # The candidate honestly records the requested-vs-effective kind so the UI
        # / agent can disclose the fallback rather than over-promise a restyle.
        assert child["edit_kind"] == "regenerate"
        assert child["requested_edit_kind"] == "creative_edit"


def test_mix_adjust_is_cost_free() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # The /choices mix path is deterministic — no LLM decision needed.
        driver = E2EDriver(Path(tmp), ScriptedAgentClient([]))
        _seed_completed(driver, provider="provider_c", modelspec="edenn_studio")
        before = len(driver.queue.envelopes)
        resp = driver.choice(
            {
                "choice_type": "mix",
                "target_id": "cand1",
                "payload": {
                    "candidate_id": "cand1",
                    "music_volume": 0.6,
                    "preserve_original_audio": True,
                    "voiceover_volume": 1.0,
                    "duck_gain_db": -12.0,
                    "voiceover_start_s": 0.0,
                },
            }
        )
        assert resp.status_code == 200
        assert len(driver.queue.envelopes) == before, "volume/keep-original is cost-free — NO new generation job"

        cand = driver.agent_repo.get_session(driver.session_id).state_json["candidates"][0]
        assert cand["music_volume"] == 0.6
        assert cand["preserve_original_audio"] is True


def test_mix_without_target_falls_back_to_latest_completed() -> None:
    # Two finished candidates, none locked, and a mix tweak with NO candidate_id —
    # this used to dead-end ("no music track to adjust"). It must now apply to the
    # most recent rendered candidate instead.
    with tempfile.TemporaryDirectory() as tmp:
        driver = E2EDriver(Path(tmp), ScriptedAgentClient([]))
        _seed_completed(driver, provider="provider_c", modelspec="edenn_studio", cid="cand1")
        sess = driver.agent_repo.get_session(driver.session_id)
        state = dict(sess.state_json)
        second = dict(state["candidates"][0])
        second.update(candidate_id="cand2", title="Take 2")
        state["candidates"] = [state["candidates"][0], second]
        state.pop("selected_candidate_id", None)  # nothing locked
        driver.agent_repo.update_session(driver.session_id, state_json=state)

        before = len(driver.queue.envelopes)
        resp = driver.choice({"choice_type": "mix", "payload": {"music_volume": 0.5}})
        assert resp.status_code == 200
        assert len(driver.queue.envelopes) == before, "still cost-free (no generation job)"

        cands = driver.agent_repo.get_session(driver.session_id).state_json["candidates"]
        latest = next(c for c in cands if c["candidate_id"] == "cand2")
        assert latest["music_volume"] == 0.5, "mix applied to the latest completed track"
        msgs = " ".join(m["content"] for m in driver.snapshot().get("messages", []))
        assert "no music track" not in msgs.lower(), "must not dead-end with the no-target reply"
