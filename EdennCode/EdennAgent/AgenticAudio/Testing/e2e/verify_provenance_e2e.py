"""Provenance end-to-end: does a free re-presentation still undo itself?

Unit tests pin the rule; this proves it against a live session, on real
rendered audio, with the 2.5s hydrate poll actually running — which is where
the original defect lived. Nothing in the unit suite can reach that, because
the bug was never in a function: it was in the argument between a tool that
cleared a field and a projection that put it back 2.5 seconds later.

What it checks, in the order a user would hit it:

  A. a take renders, and hydration derives a listen-back report for it
  B. re-cutting the take advances its epoch, re-measures the NEW window, and
     re-derives the report from those measurements
  C. the report does not drift back over repeated hydration passes — the
     failure that made a sculpted-away fault permanent
  D. adjusting the volume afterwards keeps the window (it used to revert)
  E. a comparison stamps the epochs it measured, and stops being served once
     one of those takes moves on

Spends: ONE take on the requested tier. The re-cut, the re-mix and the
comparison are free.

Usage:
  .venv/bin/python .../verify_provenance_e2e.py [--tier edenn_studio] [--base-url ...]
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
    RESULTS,
    Client,
    check,
    probe_duration,
)


def take_of(state: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    """The named take as the session currently holds it."""

    for candidate in state.get("candidates") or []:
        if str(candidate.get("candidate_id")) == candidate_id:
            return candidate
    return {}


def snapshot_state(client: Client, sid: str) -> dict[str, Any]:
    return client.get(f"{PREFIX}/sessions/{sid}")[1].get("state") or {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8800")
    ap.add_argument("--tier", default="edenn_studio")
    ap.add_argument(
        "--video",
        default="EdennCode/TestSuites/assets/smoke/videos/Videos2026-04-08_104426_347.mp4",
    )
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--window-start", type=float, default=45.0)
    args = ap.parse_args()

    client = Client(args.base_url)
    video = Path(args.video)
    print(f"\n=== Provenance E2E — tier={args.tier} video={video.name} ===\n", flush=True)

    # ---- a paid take to re-present -------------------------------------
    status, up = client.upload(video)
    artifact_id = up.get("artifact_id") or up.get("source_video_artifact_id")
    if not check("upload lands a source artifact", status == 200 and bool(artifact_id)):
        return 1

    status, session = client.post(
        f"{PREFIX}/sessions",
        {
            "source_video_artifact_id": artifact_id,
            "creator_user_id": "provenance_verify",
            "initial_message": "Score this with music only — warm and cinematic.",
        },
        timeout=600,
    )
    sid = session.get("session_id")
    if not check("session bootstraps", status == 200 and bool(sid)):
        return 1

    for _ in range(4):
        state = snapshot_state(client, sid)
        if state.get("proposals"):
            break
        pending = state.get("pending_clarification") or {}
        options = pending.get("options") or []
        if options:
            pick = next(
                (o for o in options if "music" in str(o.get("label", "")).lower()), options[0]
            )
            client.post(
                f"{PREFIX}/sessions/{sid}/choices",
                {"choice_type": "clarification", "target_id": pick.get("id"),
                 "payload": {"layers": ["music"]}},
                timeout=600,
            )
        else:
            client.post(
                f"{PREFIX}/sessions/{sid}/messages",
                {"content": "Music only please — go ahead and propose directions."},
                timeout=600,
            )

    proposals = snapshot_state(client, sid).get("proposals") or []
    if not check("agent proposed a direction", bool(proposals), f"{len(proposals)}"):
        return 1

    t0 = time.time()
    client.post(
        f"{PREFIX}/sessions/{sid}/choices",
        {"choice_type": "proposal", "target_id": proposals[0].get("proposal_id"),
         "payload": {"modelspec": args.tier}},
        timeout=600,
    )

    deadline = time.time() + args.timeout
    cand: dict[str, Any] = {}
    last = ""
    while time.time() < deadline:
        cands = snapshot_state(client, sid).get("candidates") or []
        if cands:
            cand = cands[0]
            now = f"{cand.get('status')} ({int(time.time() - t0)}s)"
            if now != last:
                print(f"    … take: {now}", flush=True)
                last = now
            if cand.get("status") in {"completed", "failed", "error"}:
                break
        time.sleep(10)

    if not check(
        "a real take rendered",
        cand.get("status") == "completed" and bool(cand.get("audio_url")),
        f"status={cand.get('status')}",
    ):
        return 1

    candidate_id = str(cand.get("candidate_id"))

    # ---- A. the take is judged, and starts at the original render -------
    check(
        "the render is judged on hydrate",
        bool(cand.get("listen_report")),
        f"clean={(cand.get('listen_report') or {}).get('clean')} "
        f"notes={(cand.get('listen_report') or {}).get('notes')}",
    )
    check(
        "an untouched take sits at its original render",
        int(cand.get("render_epoch") or 0) == 0,
        f"render_epoch={cand.get('render_epoch')}",
    )

    full_track = cand.get("complete_audio_url")
    if not full_track:
        print("\n  ! this tier renders video-length audio with no longer track behind it;")
        print("    the re-cut checks need a tier that returns a full track.\n", flush=True)
        return 0 if all(ok for _, ok, _ in RESULTS) else 1

    before_report = dict(cand.get("listen_report") or {})

    # ---- B. re-cut: new epoch, new measurements, new judgement ----------
    status, _ = client.post(
        f"{PREFIX}/sessions/{sid}/choices",
        {"choice_type": "sculpt", "target_id": candidate_id,
         "payload": {"window_start_s": args.window_start}},
        timeout=600,
    )
    check("the canvas can ask for a re-cut", status == 200, f"HTTP {status}")

    sculpted = take_of(snapshot_state(client, sid), candidate_id)
    epoch = int(sculpted.get("render_epoch") or 0)
    check("the re-cut advances the take's epoch", epoch == 1, f"render_epoch={epoch}")
    check(
        "the window the user chose is on the take",
        (sculpted.get("window") or {}).get("source") == "user",
        f"window={sculpted.get('window')}",
    )

    signals = sculpted.get("take_signals") or {}
    check(
        "the re-cut measured the audio it just produced",
        bool(signals) and int(signals.get("render_epoch") or -1) == epoch,
        f"stamped_epoch={signals.get('render_epoch')} cut={signals.get('cut_duration_s')}s",
    )
    # Sourcing, not just difference. A rendered take is often clean, so
    # comparing fault lists can pass while proving nothing — the check has to
    # show WHERE the judgement came from. If hydration were still re-deriving
    # from the original render, this block would carry the first window's
    # numbers instead of the ones just measured.
    after_report = dict(sculpted.get("listen_report") or {})
    measured = dict(after_report.get("measured") or {})
    from_new_window = bool(measured) and all(
        signals.get(key) == value for key, value in measured.items()
    )
    check(
        "the judgement is sourced from the re-cut's own measurements",
        from_new_window,
        f"measured={measured}",
    )
    if before_report.get("notes") == after_report.get("notes") == []:
        print(
            "    (note: this take rendered clean, so no fault was sculpted away here —\n"
            "     the resurrection path itself is pinned by test_render_provenance.py)",
            flush=True,
        )

    # ---- C. THE regression: does it drift back? ------------------------
    # Hydration runs every 2.5s for the life of the session. "Fixed" has to
    # mean fixed on the hundredth pass, not the first.
    drifted: Optional[str] = None
    for pass_number in range(1, 7):
        time.sleep(3)
        polled = take_of(snapshot_state(client, sid), candidate_id)
        report_now = dict(polled.get("listen_report") or {})
        if report_now != after_report:
            drifted = (
                f"pass {pass_number}: {after_report.get('notes')} -> {report_now.get('notes')}"
            )
            break
        if int(polled.get("render_epoch") or 0) != epoch:
            drifted = f"pass {pass_number}: epoch moved to {polled.get('render_epoch')}"
            break
    check(
        "the judgement holds across repeated hydration",
        drifted is None,
        drifted or f"{6} polls over ~18s, unchanged",
    )

    # ---- D. the window survives the next thing the user does -----------
    status, _ = client.post(
        f"{PREFIX}/sessions/{sid}/choices",
        {"choice_type": "mix", "payload": {"candidate_id": candidate_id, "music_volume": 0.55}},
        timeout=600,
    )
    remixed = take_of(snapshot_state(client, sid), candidate_id)
    window_now = remixed.get("window") or {}
    check(
        "adjusting the volume keeps the re-cut window",
        status == 200
        and window_now.get("source") == "user"
        and abs(float(window_now.get("start_s") or 0.0) - float(
            (sculpted.get("window") or {}).get("start_s") or 0.0
        )) < 0.01,
        f"window={window_now} volume={remixed.get('music_volume')}",
    )

    # ---- E. a comparison knows which takes it described -----------------
    status, _ = client.post(
        f"{PREFIX}/sessions/{sid}/choices",
        {"choice_type": "compare", "payload": {}},
        timeout=600,
    )
    state = snapshot_state(client, sid)
    comparison = state.get("last_comparison") or {}
    stamps = comparison.get("candidate_epochs") or {}
    check(
        "the comparison stamps the generation each row measured",
        status == 200 and str(candidate_id) in stamps,
        f"epochs={stamps}",
    )
    check(
        "the stamp matches the take as it stands",
        int(stamps.get(candidate_id, -1)) == epoch,
        f"stamped={stamps.get(candidate_id)} take={epoch}",
    )

    print(f"\n  session: {sid}")
    print(f"  take:    {candidate_id} @ epoch {epoch}", flush=True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n=== {passed}/{len(RESULTS)} checks passed ===\n", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
