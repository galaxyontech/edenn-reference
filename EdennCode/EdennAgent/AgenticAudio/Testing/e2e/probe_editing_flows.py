"""Editing-flow probe (real LLM): drive the agent through every kind of edit and
INSPECT the regeneration-job payloads to prove the requested change actually
propagated to the job the music model will run — especially that a *music style
change* is genuinely engaged (audio_creative_edit with the new style in the job),
not merely acknowledged in chat.

Run: .venv/bin/python -m EdennCode.EdennAgent.AgenticAudio.Testing.e2e.probe_editing_flows
Writes evidence JSON to /tmp/editing_flows_evidence.json and prints each session.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Optional

from EdennCode.env import load_env

load_env()

from EdennCode.EdennAgent.AgenticAudio.agent import build_agentic_audio_agent_client
from EdennCode.EdennAgent.AgenticAudio.Testing.e2e.driver import E2EDriver
from EdennCode.EdennAgent.AgenticAudio.Testing.e2e.run_e2e import real_llm_available

EVIDENCE_PATH = Path("/tmp/editing_flows_evidence.json")


def _candidates(driver: E2EDriver) -> list[dict[str, Any]]:
    sess = driver.agent_repo.get_session(driver.session_id)
    return list((sess.state_json if sess else {}).get("candidates") or [])


def _turns(driver: E2EDriver) -> list[dict[str, Any]]:
    sess = driver.agent_repo.get_session(driver.session_id)
    return list((sess.state_json if sess else {}).get("turns") or [])


def complete_latest_candidate(driver: E2EDriver, *, provider: str, modelspec: str) -> Optional[dict[str, Any]]:
    """Simulate a finished candidate (a worker would normally do this) so a later
    creative_edit has a real track + capable provider to restyle."""
    sid = driver.session_id
    sess = driver.agent_repo.get_session(sid)
    state = dict(sess.state_json)
    cands = list(state.get("candidates") or [])
    if not cands:
        return None
    c = dict(cands[-1])
    c.update(
        status="completed",
        provider=provider,
        modelspec=modelspec,
        audio_url=f"https://fake.blob/audio/{c['candidate_id']}.mp3",
        video_url=f"https://fake.blob/video/{c['candidate_id']}.mp4",
        provider_audio_id=f"prov_{c['candidate_id']}",
    )
    cands[-1] = c
    state["candidates"] = cands
    driver.agent_repo.update_session(sid, state_json=state)
    return c


def job_facts(driver: E2EDriver, job_index_before: int) -> list[dict[str, Any]]:
    """Style-relevant fields of every generation job enqueued since the mark."""
    facts = []
    for env in driver.queue.envelopes[job_index_before:]:
        job = driver.async_repo.jobs.get(env.job_id)
        rj = (job.request_json if job else {}) or {}
        facts.append(
            {
                "job_type": getattr(job, "job_type", None),
                "user_prompt": rj.get("user_prompt"),
                "modelspec": rj.get("modelspec"),
                "source_audio_url": rj.get("source_audio_url"),
                "agentic_edit_kind": rj.get("agentic_edit_kind"),
                "agentic_extend_mode": rj.get("agentic_extend_mode"),
                "extend_seconds": rj.get("extend_seconds"),
                "preserve_original_audio": rj.get("preserve_original_audio"),
            }
        )
    return facts


def transcript(driver: E2EDriver) -> list[dict[str, str]]:
    msgs = driver.snapshot().get("messages", []) if driver.session_id else []
    return [{"role": m["role"], "content": m["content"]} for m in msgs]


def run_flow(name: str, desc: str, steps, client) -> dict[str, Any]:
    """steps: list of callables(driver) that drive one turn (return optional note)."""
    with tempfile.TemporaryDirectory() as tmp:
        driver = E2EDriver(Path(tmp), client)
        notes = []
        for step in steps:
            note = step(driver)
            if note:
                notes.append(note)
        return {
            "name": name,
            "desc": desc,
            "transcript": transcript(driver),
            "turns": [{"intent": t.get("intent"), "tools": t.get("tools")} for t in _turns(driver)],
            "candidates": [
                {
                    "candidate_id": c.get("candidate_id"),
                    "parent_candidate_id": c.get("parent_candidate_id"),
                    "version": c.get("version"),
                    "edit_kind": c.get("edit_kind"),
                    "requested_edit_kind": c.get("requested_edit_kind"),
                    "extend_mode": c.get("extend_mode"),
                    "provider": c.get("provider"),
                    "prompt": c.get("prompt"),
                    "music_volume": c.get("music_volume"),
                    "preserve_original_audio": c.get("preserve_original_audio"),
                    "status": c.get("status"),
                }
                for c in _candidates(driver)
            ],
            "notes": notes,
        }


# ---- step builders -------------------------------------------------------

def s_create(msg):
    def step(driver):
        driver.create(msg)
    return step


def s_msg(msg):
    def step(driver):
        before = len(driver.queue.envelopes)
        driver.message(msg)
        facts = job_facts(driver, before)
        return {"after": msg[:60], "jobs": facts} if facts else {"after": msg[:60], "jobs": []}
    return step


def s_complete(provider, modelspec):
    def step(driver):
        c = complete_latest_candidate(driver, provider=provider, modelspec=modelspec)
        return {"completed_candidate": (c or {}).get("candidate_id"), "provider": provider, "modelspec": modelspec}
    return step


def main() -> None:
    if not real_llm_available():
        print("SKIP: real LLM not configured")
        return
    client = build_agentic_audio_agent_client()

    flows = [
        ("A_style_change_restyle", "Style change on a finished STUDIO track -> should ENGAGE creative_edit with the new style.", [
            s_create("I want premium, studio-quality cinematic music for my product video."),
            s_msg("Love it — go with the cinematic direction."),
            s_complete("provider_c", "edenn_studio"),
            s_msg("Actually, can you turn this into a lo-fi chillhop version of the same track?"),
        ]),
        ("B_variation_regenerate", "Same-style new take -> edit_audio regenerate (not a restyle).", [
            s_create("Upbeat indie-pop for my travel reel, please."),
            s_msg("Great — go with that direction."),
            s_complete("provider_c", "edenn_studio"),
            s_msg("Give me another version in the same style — just a different take."),
        ]),
        ("C_extend_lengthen", "Lengthen -> edit_audio extend with extend_seconds.", [
            s_create("Warm acoustic background music for my vlog."),
            s_msg("Yes, go with the warm acoustic one."),
            s_complete("provider_c", "edenn_studio"),
            s_msg("My video got longer — extend the track to about 30 seconds."),
        ]),
        ("D_mix_adjust_cost_free", "Volume / keep-original -> adjust_remix, NO new generation job.", [
            s_create("Energetic electronic music for my gym promo."),
            s_msg("Go with the energetic electronic direction."),
            s_complete("provider_c", "edenn_studio"),
            s_msg("Lower the music a bit and keep my original audio underneath."),
        ]),
        ("E_restyle_fallback_honesty", "Restyle on a BASIC (ProviderA) track -> creative_edit FALLS BACK to regenerate; is the agent honest?", [
            s_create("I want one calm ambient background track for a quick demo clip."),
            s_msg("Approve the first direction and generate it now — no need to ask."),
            s_complete("provider_a", "edenn_basic"),
            s_msg("Can you make it a lo-fi version of this exact track?"),
        ]),
    ]

    results = []
    for name, desc, steps in flows:
        print(f"\n{'='*78}\n{name} — {desc}\n{'='*78}")
        try:
            r = run_flow(name, desc, steps, client)
        except Exception as exc:  # noqa: BLE001
            print(f"  !! flow errored: {exc!r}")
            results.append({"name": name, "desc": desc, "error": repr(exc)})
            continue
        for m in r["transcript"]:
            who = "you  " if m["role"] == "user" else "EDENN"
            print(f"  {who}│ {m['content']}")
        print("  ── turns:", [f"{t['intent']}:{','.join(t['tools'] or []) or '-'}" for t in r["turns"]])
        for n in r["notes"]:
            if "jobs" in n and n["jobs"]:
                for j in n["jobs"]:
                    print(f"  ── JOB after «{n['after']}»: type={j['job_type']} edit_kind={j['agentic_edit_kind']} "
                          f"extend_mode={j['agentic_extend_mode']} ext_s={j['extend_seconds']} "
                          f"src_audio={'Y' if j['source_audio_url'] else 'N'}")
                    print(f"        prompt: {j['user_prompt']}")
            elif "jobs" in n:
                print(f"  ── after «{n['after']}»: NO generation job enqueued")
            elif "completed_candidate" in n:
                print(f"  ── [simulated completion] {n['completed_candidate']} provider={n['provider']} model={n['modelspec']}")
        print("  ── final candidates:")
        for c in r["candidates"]:
            print(f"        {c['candidate_id']} v{c['version']} parent={c['parent_candidate_id']} "
                  f"edit_kind={c['edit_kind']} requested={c['requested_edit_kind']} "
                  f"provider={c['provider']} vol={c['music_volume']} keep_orig={c['preserve_original_audio']} status={c['status']}")
            print(f"          prompt: {c['prompt']}")
        results.append(r)

    EVIDENCE_PATH.write_text(json.dumps(results, indent=2))
    print(f"\nEvidence → {EVIDENCE_PATH}")


if __name__ == "__main__":
    main()
