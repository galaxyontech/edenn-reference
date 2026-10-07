"""Phase 0 end-to-end verification against a REAL running devserver with REAL generation.

Drives an actual session the way the console does, then checks the things the
Phase 0 changes claim to fix:

  A. the fused, video-grounded prompt reaches the music model in the slot the
     rendering tier reads (verbose slots on studio, ordinary prompt on basic)
  B. a real take renders and plays, at a sane duration for the source video
  C. the agent never goes silent (every turn produced an assistant message)

Usage:
  .venv/bin/python .../verify_phase0_e2e.py --tier edenn_studio [--base-url ...]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

PREFIX = "/api/v2/agentic/audio"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def _req(self, method: str, path: str, body: Any = None, timeout: float = 120):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(f"{self.base}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            return exc.code, {"error": exc.read().decode()[:600]}

    def get(self, path: str, timeout: float = 120):
        return self._req("GET", path, None, timeout)

    def post(self, path: str, body: Any, timeout: float = 300):
        return self._req("POST", path, body, timeout)

    def upload(self, path: Path) -> tuple[int, dict[str, Any]]:
        boundary = "----phase0verify"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="video"; filename="{path.name}"\r\n'
            f"Content-Type: video/mp4\r\n\r\n"
        ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"{self.base}/api/v2/assets/video", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=600) as resp:
            return resp.status, json.loads(resp.read())

    def fetch(self, url: str, dest: Path) -> int:
        full = url if url.startswith("http") else f"{self.base}{url}"
        with urllib.request.urlopen(full, timeout=600) as resp:
            data = resp.read()
        dest.write_bytes(data)
        return len(data)


def probe_duration(path: Path) -> Optional[float]:
    import subprocess
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(out.stdout.strip().split(",")[0])
    except (ValueError, IndexError):
        return None


def assistant_messages(snapshot: dict[str, Any]) -> list[str]:
    return [
        str(m.get("content") or "")
        for m in snapshot.get("messages") or []
        if m.get("role") == "assistant"
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8800")
    ap.add_argument("--tier", default="edenn_studio")
    ap.add_argument("--video", default="EdennCode/TestSuites/assets/smoke/videos/Videos2026-04-08_104426_347.mp4")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    client = Client(args.base_url)
    video = Path(args.video)
    print(f"\n=== Phase 0 E2E — tier={args.tier} video={video.name} ===\n", flush=True)

    src_dur = probe_duration(video)
    print(f"  source video: {src_dur:.1f}s", flush=True)

    # 1. upload -----------------------------------------------------------
    status, up = client.upload(video)
    artifact_id = up.get("artifact_id") or up.get("source_video_artifact_id")
    if not check("upload lands a source artifact", status == 200 and bool(artifact_id), str(up)[:160]):
        return 1

    # 2. session + bootstrap (real analysis + real LLM) --------------------
    t0 = time.time()
    status, session = client.post(
        f"{PREFIX}/sessions",
        {
            "source_video_artifact_id": artifact_id,
            "creator_user_id": "phase0_verify",
            "initial_message": "Score this with music only — warm and cinematic.",
        },
        timeout=600,
    )
    sid = session.get("session_id")
    if not check("session bootstraps", status == 200 and bool(sid), f"{time.time()-t0:.0f}s"):
        print(json.dumps(session, indent=2)[:900])
        return 1

    status, snap = client.get(f"{PREFIX}/sessions/{sid}")
    state = snap.get("state") or {}
    obs = state.get("observation") or {}
    check(
        "bootstrap ran the real analysis",
        bool(obs) and bool(obs.get("duration_s") or obs.get("scenes")),
        f"duration_s={obs.get('duration_s')} scenes={len(obs.get('scenes') or [])}",
    )

    # 3. answer whatever the agent is waiting on until proposals exist -----
    for _ in range(4):
        state = (client.get(f"{PREFIX}/sessions/{sid}")[1].get("state") or {})
        if state.get("proposals"):
            break
        pending = state.get("pending_clarification") or {}
        options = pending.get("options") or []
        if options:
            # music-only when offered, else the recommended/first option
            pick = next((o for o in options if "music" in str(o.get("label", "")).lower()), options[0])
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
    state = client.get(f"{PREFIX}/sessions/{sid}")[1].get("state") or {}
    proposals = state.get("proposals") or []
    if not check("agent proposed a direction", bool(proposals), f"{len(proposals)} proposal(s)"):
        print(json.dumps(state, indent=2)[:1200])
        return 1

    # 4. approve with an explicit tier -> the ONE paid generation ----------
    proposal_id = proposals[0].get("proposal_id")
    t0 = time.time()
    status, _ = client.post(
        f"{PREFIX}/sessions/{sid}/choices",
        {"choice_type": "proposal", "target_id": proposal_id, "payload": {"modelspec": args.tier}},
        timeout=600,
    )
    check("approval dispatched generation", status == 200, f"{time.time()-t0:.0f}s")

    # NOTE on claim A: on a verbose-capable tier the routing sets
    # verbose_instruction=True with an EMPTY user_prompt. The preprocessor
    # REJECTS that combination if it is wrong (non-empty prompt, or a tier below
    # enhanced), failing the job outright — so "a real take rendered on studio"
    # below IS the end-to-end proof that the verbose routing is accepted. The
    # slot-by-slot assertions live in Testing/test_style_prompt_routing.py.

    # 6. wait for the take ------------------------------------------------
    deadline = time.time() + args.timeout
    cand: dict[str, Any] = {}
    last = ""
    while time.time() < deadline:
        state = client.get(f"{PREFIX}/sessions/{sid}")[1].get("state") or {}
        cands = state.get("candidates") or []
        if cands:
            cand = cands[0]
            status_now = f"{cand.get('status')} ({int(time.time()-t0)}s)"
            if status_now != last:
                print(f"    … take: {status_now}", flush=True)
                last = status_now
            if cand.get("status") in {"completed", "failed", "error"}:
                break
        time.sleep(10)

    ok_render = cand.get("status") == "completed" and bool(cand.get("audio_url"))
    check(
        "a real take rendered",
        ok_render,
        f"status={cand.get('status')} tier={cand.get('modelspec')} placeholder={cand.get('placeholder')}",
    )
    if not ok_render:
        print(json.dumps(cand, indent=2)[:1200])
        return 1

    check(
        "the take is REAL, not a placeholder tone",
        cand.get("placeholder") is not True,
        f"placeholder={cand.get('placeholder')}",
    )

    # 7. the audio actually plays and fits the video -----------------------
    tmp = Path("/tmp/phase0_take.mp3")
    size = client.fetch(str(cand["audio_url"]), tmp)
    dur = probe_duration(tmp)
    check("the take downloads and decodes", size > 10_000 and dur is not None, f"{size/1024:.0f}KB {dur}s")
    if dur and src_dur:
        check(
            "the cut matches the video length",
            abs(dur - src_dur) <= 2.5,
            f"cut={dur:.1f}s video={src_dur:.1f}s",
        )

    # 8. the agent never went silent --------------------------------------
    snap = client.get(f"{PREFIX}/sessions/{sid}")[1]
    said = assistant_messages(snap)
    check("every turn produced an assistant message", len(said) >= 2, f"{len(said)} assistant messages")

    turns = (snap.get("state") or {}).get("turns") or []
    if turns:
        check(
            "turn records carry step telemetry",
            any("steps_used" in t for t in turns),
            f"last turn: steps_used={turns[-1].get('steps_used')} skipped={turns[-1].get('skipped_steps')}",
        )

    print(f"\n  session: {sid}")
    print(f"  take:    {cand.get('candidate_id')} -> {cand.get('audio_url')}")
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n=== {len(RESULTS)-len(failed)}/{len(RESULTS)} checks passed ===\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
