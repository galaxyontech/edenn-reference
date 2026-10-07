"""The whole product, once, on a real video, with real generation.

Every other harness here proves one claim. This one asks the question a
customer asks: can I take a video and come out the other end with a finished
piece of audio, using the things this product says it can do?

It walks the journey in order — upload, understand, direction, a paid take,
then every free way to shape that take, then narration, then sound design,
then the master mix and the deliverable — and reports what worked, what was
refused, and what silently did nothing. A step that is not reachable is as
interesting as a step that fails, so nothing is skipped quietly.

SPENDS REAL MONEY: one music take, one narration render, one effects bed, and
one extra take if the regenerate leg runs. Everything else on this path is free
by design, and the report says which was which.

Usage:
  .venv/bin/python .../verify_full_journey.py [--tier edenn_studio] [--base-url ...]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from EdennCode.EdennAgent.AgenticAudio.Testing.e2e.verify_phase0_e2e import (  # noqa: E402
    PREFIX,
    Client,
    probe_duration,
)

#: (name, outcome, detail). Outcome is PASS / FAIL / UNREACHABLE / SKIPPED.
RESULTS: list[tuple[str, str, str]] = []
SPEND: list[str] = []


def record(name: str, outcome: str, detail: str = "") -> bool:
    RESULTS.append((name, outcome, detail))
    mark = {"PASS": "PASS", "FAIL": "FAIL", "UNREACHABLE": "UNRCH", "SKIPPED": "SKIP"}[outcome]
    print(f"  [{mark:5s}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return outcome == "PASS"


def state_of(client: Client, sid: str) -> dict[str, Any]:
    return client.get(f"{PREFIX}/sessions/{sid}")[1].get("state") or {}


def choose(client: Client, sid: str, body: dict[str, Any]) -> tuple[int, Any]:
    return client.post(f"{PREFIX}/sessions/{sid}/choices", body, timeout=600)


def say(client: Client, sid: str, text: str) -> tuple[int, Any]:
    return client.post(f"{PREFIX}/sessions/{sid}/messages", {"content": text}, timeout=600)


def wait_for(
    client: Client, sid: str, done, *, timeout: int, label: str
) -> Optional[dict[str, Any]]:
    """Poll session state until `done(state)` returns a value, or give up."""

    deadline = time.time() + timeout
    last = ""
    started = time.time()
    while time.time() < deadline:
        state = state_of(client, sid)
        got = done(state)
        if got is not None:
            return got
        # The director asks before it spends — the sound-design treatment card
        # is the clearest case. A harness that only polls will wait out the
        # timeout while the product sits there behaving correctly, and then
        # report the product as broken. Answer, and keep waiting.
        pending = state.get("pending_clarification") or {}
        options = pending.get("options") or []
        if options:
            pick = next((o for o in options if o.get("recommended")), options[0])
            print(f"    … answering: {pick.get('label')}", flush=True)
            choose(client, sid, {"choice_type": "clarification",
                                 "target_id": pick.get("id"), "payload": {}})
            continue
        marker = f"{label}: {int(time.time() - started)}s"
        if marker != last and int(time.time() - started) % 20 == 0:
            print(f"    … {marker}", flush=True)
            last = marker
        time.sleep(5)
    return None


def main() -> int:  # noqa: C901 - a journey is a sequence; splitting it hides it
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8800")
    ap.add_argument("--tier", default="edenn_studio")
    ap.add_argument(
        "--video",
        default="EdennCode/TestSuites/assets/smoke/videos/Videos2026-04-08_104426_347.mp4",
    )
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    client = Client(args.base_url)
    video = Path(args.video)
    duration = probe_duration(video) or 0.0
    print(f"\n=== FULL JOURNEY — tier={args.tier} video={video.name} ({duration:.1f}s) ===\n",
          flush=True)

    # ---- 1. arrive with a video ------------------------------------------
    status, up = client.upload(video)
    artifact_id = up.get("artifact_id") or up.get("source_video_artifact_id")
    if not record("upload a video", "PASS" if artifact_id else "FAIL", str(up)[:120]):
        return 1

    status, session = client.post(
        f"{PREFIX}/sessions",
        {"source_video_artifact_id": artifact_id, "creator_user_id": "journey",
         "initial_message": "Score this — warm and cinematic, music only for now."},
        timeout=600,
    )
    sid = session.get("session_id")
    if not record("start a session", "PASS" if sid else "FAIL", f"{sid}"):
        return 1

    observation = state_of(client, sid).get("observation") or {}
    record(
        "the video is understood",
        "PASS" if observation.get("duration_s") else "FAIL",
        f"{observation.get('duration_s')}s, {len(observation.get('scenes') or [])} scenes",
    )

    # ---- 2. a direction, then one paid take ------------------------------
    for _ in range(4):
        state = state_of(client, sid)
        if state.get("proposals"):
            break
        pending = state.get("pending_clarification") or {}
        options = pending.get("options") or []
        if options:
            pick = next((o for o in options if "music" in str(o.get("label", "")).lower()),
                        options[0])
            choose(client, sid, {"choice_type": "clarification", "target_id": pick.get("id"),
                                 "payload": {"layers": ["music"]}})
        else:
            say(client, sid, "Music only please — propose some directions.")
    proposals = state_of(client, sid).get("proposals") or []
    if not record("the agent proposes directions", "PASS" if proposals else "FAIL",
                  f"{len(proposals)}"):
        return 1

    choose(client, sid, {"choice_type": "proposal",
                         "target_id": proposals[0].get("proposal_id"),
                         "payload": {"modelspec": args.tier}})
    SPEND.append("music take")
    take = wait_for(
        client, sid,
        lambda s: next((c for c in (s.get("candidates") or [])
                        if c.get("status") in {"completed", "failed", "error"}), None),
        timeout=args.timeout, label="rendering a take",
    )
    if not record("a real take renders", "PASS" if (take or {}).get("status") == "completed" else "FAIL",
                  f"status={(take or {}).get('status')}"):
        return 1
    cid = str(take["candidate_id"])

    record("the take is judged on arrival",
           "PASS" if take.get("listen_report") else "FAIL",
           f"clean={(take.get('listen_report') or {}).get('clean')}")
    record("the take knows its own track",
           "PASS" if take.get("complete_audio_url") else "UNREACHABLE",
           "no longer track behind it" if not take.get("complete_audio_url") else "full track present")
    structure = take.get("music_structure") or {}
    record("the music's shape is known",
           "PASS" if structure.get("sections") else "UNREACHABLE",
           f"{len(structure.get('sections') or [])} sections, "
           f"{len(structure.get('beat_times_s') or [])} beats")

    has_full = bool(take.get("complete_audio_url"))

    # ---- 3. every free way to shape it -----------------------------------
    if has_full:
        status, _ = choose(client, sid, {"choice_type": "sculpt", "target_id": cid,
                                         "payload": {"window_start_s": 30.0}})
        after = next((c for c in state_of(client, sid).get("candidates") or []
                      if str(c.get("candidate_id")) == cid), {})
        record("re-cut the take to another moment",
               "PASS" if (after.get("window") or {}).get("source") == "user" else "FAIL",
               f"window={after.get('window')}")
        record("the re-cut measured itself",
               "PASS" if (after.get("take_signals") or {}).get("render_epoch") == after.get("render_epoch")
               else "FAIL",
               f"epoch={after.get('render_epoch')}")

        sections = (after.get("music_structure") or {}).get("sections") or []
        if len(sections) >= 2:
            pieces = [
                {"start_s": sections[-1]["start_s"], "duration_s": max(4.0, duration / 2)},
                {"start_s": sections[0]["start_s"], "duration_s": max(4.0, duration / 2)},
            ]
            status, _ = choose(client, sid, {
                "choice_type": "sculpt", "target_id": cid,
                "payload": {"sculpt_kind": "splice", "segments": pieces},
            })
            spliced = next((c for c in state_of(client, sid).get("candidates") or []
                            if str(c.get("candidate_id")) == cid), {})
            record("arrange the take from two pieces",
                   "PASS" if (spliced.get("window") or {}).get("segments") else "FAIL",
                   f"segments={len((spliced.get('window') or {}).get('segments') or [])}")
        else:
            record("arrange the take from two pieces", "SKIPPED", "too few sections to splice")
    else:
        record("re-cut the take to another moment", "SKIPPED", "tier has no longer track")
        record("the re-cut measured itself", "SKIPPED", "")
        record("arrange the take from two pieces", "SKIPPED", "")

    status, _ = choose(client, sid, {"choice_type": "mix",
                                     "payload": {"candidate_id": cid, "music_volume": 0.6}})
    record("adjust the mix level", "PASS" if status == 200 else "FAIL", f"HTTP {status}")

    status, _ = choose(client, sid, {
        "choice_type": "mix",
        "payload": {"candidate_id": cid, "music_volume": 0.6,
                    "music_envelope": [{"start_s": 4.0, "end_s": 9.0, "gain_db": -9.0}]},
    })
    mix = state_of(client, sid).get("mix") or {}
    record("fade the music at a chosen moment",
           "PASS" if mix.get("music_envelope") else "UNREACHABLE",
           f"{len(mix.get('music_envelope') or [])} fade(s)")

    status, _ = choose(client, sid, {"choice_type": "compare", "payload": {}})
    comparison = state_of(client, sid).get("last_comparison") or {}
    record("compare the takes", "PASS" if comparison.get("takes") else "FAIL",
           f"{comparison.get('compared')} take(s)")

    # ---- 4. narration -----------------------------------------------------
    say(client, sid, "Add a short voice-over — two lines, warm and calm.")
    script = wait_for(
        client, sid,
        lambda s: ((s.get("layers") or {}).get("voiceover") or {}).get("segments") or None,
        timeout=240, label="drafting a script",
    )
    record("a narration script is drafted", "PASS" if script else "UNREACHABLE",
           f"{len(script or [])} line(s)")

    if script:
        SPEND.append("narration render")
        status, _ = choose(client, sid, {"choice_type": "voiceover", "payload": {}})
        vo = wait_for(
            client, sid,
            lambda s: (((s.get("layers") or {}).get("voiceover") or {}).get("audio_url") and
                       (s.get("layers") or {}).get("voiceover")) or None,
            timeout=args.timeout, label="recording narration",
        )
        record("the narration records", "PASS" if vo else "FAIL",
               f"status={(vo or {}).get('status')}")
        if vo:
            record("the narration is judged",
                   "PASS" if vo.get("alignment") else "FAIL",
                   f"clean={(vo.get('alignment') or {}).get('clean')}")
            segments = vo.get("segments") or []
            keeps = [s for s in segments if s.get("audio_path")]
            record("each line is kept for a retake",
                   "PASS" if keeps else "UNREACHABLE",
                   f"{len(keeps)}/{len(segments)} line(s) have their own audio")

    # ---- 5. sound design ---------------------------------------------------
    say(client, sid, "Add a few sound effects on the cuts.")
    plan = wait_for(
        client, sid,
        lambda s: ((s.get("layers") or {}).get("sfx") or {}).get("events") or None,
        timeout=300, label="spotting effects",
    )
    record("sound effects are spotted", "PASS" if plan else "UNREACHABLE",
           f"{len(plan or [])} moment(s)")

    if plan:
        SPEND.append("effects bed")
        status, _ = choose(client, sid, {"choice_type": "sfx", "payload": {}})
        sfx = wait_for(
            client, sid,
            lambda s: next((v for v in (((s.get("layers") or {}).get("sfx") or {}).get("variants") or [])
                            if v.get("status") in {"completed", "failed", "error"}), None),
            timeout=args.timeout, label="rendering effects",
        )
        record("the effects render", "PASS" if (sfx or {}).get("status") == "completed" else "FAIL",
               f"status={(sfx or {}).get('status')}")
        if sfx:
            manifest = sfx.get("rendered_events") or []
            record("the render says what it placed",
                   "PASS" if manifest else "FAIL", f"{len(manifest)} event(s)")
            record("each effect is kept for a redo",
                   "PASS" if any(e.get("audio_path") for e in manifest) else "UNREACHABLE",
                   f"{sum(1 for e in manifest if e.get('audio_path'))} reusable")

    # ---- 6. the deliverable ------------------------------------------------
    status, _ = choose(client, sid, {"choice_type": "mix", "payload": {"candidate_id": cid}})
    mixed = wait_for(
        client, sid,
        lambda s: ((s.get("mix") or {}).get("video_url") and s.get("mix")) or None,
        timeout=args.timeout, label="composing the master",
    )
    record("the layers compose into one piece", "PASS" if mixed else "FAIL",
           f"status={(mixed or {}).get('status')}")
    if mixed:
        record("the deliverable is listened back to",
               "PASS" if mixed.get("listen_report") else "UNREACHABLE",
               f"clean={(mixed.get('listen_report') or {}).get('clean')} "
               f"notes={(mixed.get('listen_report') or {}).get('notes')}")

    status, _ = choose(client, sid, {"choice_type": "candidate", "target_id": cid, "payload": {}})
    final = state_of(client, sid).get("final_artifact") or {}
    record("the session finalises", "PASS" if final.get("video_url") else "FAIL",
           f"{'has a deliverable' if final.get('video_url') else final}")

    if final.get("video_url"):
        out = Path("/tmp/journey_final.mp4")
        try:
            size = client.fetch(str(final["video_url"]), out)
            played = probe_duration(out)
            record("the deliverable downloads and plays",
                   "PASS" if size > 10_000 and played else "FAIL",
                   f"{size/1024:.0f}KB, {played}s vs source {duration:.1f}s")
            if played:
                record("the deliverable is the length of the video",
                       "PASS" if abs(played - duration) <= 2.5 else "FAIL",
                       f"{played:.1f}s vs {duration:.1f}s")
        except Exception as exc:  # noqa: BLE001
            record("the deliverable downloads and plays", "FAIL", str(exc)[:120])

    # ---- the report --------------------------------------------------------
    counts = {k: sum(1 for _, o, _ in RESULTS if o == k) for k in
              ("PASS", "FAIL", "UNREACHABLE", "SKIPPED")}
    print(f"\n  session: {sid}")
    print(f"  spent on: {', '.join(SPEND) or 'nothing'}")
    print(f"\n=== {counts['PASS']} passed · {counts['FAIL']} failed · "
          f"{counts['UNREACHABLE']} unreachable · {counts['SKIPPED']} skipped ===\n")
    if counts["FAIL"] or counts["UNREACHABLE"]:
        print("  Not working:")
        for name, outcome, detail in RESULTS:
            if outcome in ("FAIL", "UNREACHABLE"):
                print(f"    [{outcome}] {name} — {detail}")
        print()
    return 0 if counts["FAIL"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
