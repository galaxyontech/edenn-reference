"""Post-deploy endpoint smoke for the agentic-audio console + API.

Runs the deploy checklist from ITERATION_PLAN.md Iteration 4 against any
base URL (local prod mount or the deployed test endpoint), as a user would:

  1. console assets — index.html + every allow-listed asset serves (the
     FRONTEND_ASSETS drift trap surfaces here, not in the devserver)
  2. upload — a real (tiny, generated) video lands as a source artifact
  3. session — create; bootstrap runs the real analysis pipeline
  4. websocket — events flow live during a turn (message.created ack +
     agent.reasoning turn-start beat)
  5. resume — GET snapshot returns messages + observation for the session
  6. [--with-generation] choose a proposal, wait for a take, probe playback
     (paid: spends one real generation through the deployed worker fleet)

Run:
    .venv/bin/python -m EdennCode.EdennAgent.AgenticAudio.Testing.e2e.endpoint_smoke \
        --base-url http://127.0.0.1:8900 [--token <api key>] [--with-generation]

Exit code 0 = all checks green. Prints one line per check.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

PREFIX = "/api/v2/agentic/audio"
CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


class Client:
    def __init__(self, base_url: str, token: Optional[str]) -> None:
        self.base = base_url.rstrip("/")
        self.token = token

    def _headers(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        headers = dict(extra or {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get(self, path: str, timeout: float = 60) -> tuple[int, bytes]:
        req = urllib.request.Request(f"{self.base}{path}", headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def post_json(self, path: str, body: dict[str, Any], timeout: float = 900) -> tuple[int, dict[str, Any]]:
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(body).encode(),
            method="POST",
            headers=self._headers({"Content-Type": "application/json"}),
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                return exc.code, json.loads(payload)
            except Exception:
                return exc.code, {"raw": payload.decode(errors="replace")[:300]}

    def upload_video(self, path: Path, timeout: float = 600) -> tuple[int, dict[str, Any]]:
        boundary = "----agenticsmoke"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="video"; filename="{path.name}"\r\n'
            f"Content-Type: video/mp4\r\n\r\n"
        ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"{self.base}/api/v2/assets/video",
            data=body,
            method="POST",
            headers=self._headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}),
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())


def make_test_video(dest: Path, seconds: int = 6) -> bool:
    """A tiny real video (color bars + tone) so analysis has something to chew."""

    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=320x240:rate=24",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(dest),
    ]
    return subprocess.run(cmd, capture_output=True).returncode == 0 and dest.exists()


def check_console_assets(client: Client) -> None:
    status, html_bytes = client.get(f"{PREFIX}/app/")
    check("console index serves", status == 200, f"HTTP {status}")
    assets = re.findall(r'(?:src|href)="\./([^"]+)"', html_bytes.decode(errors="replace"))
    check("console index references assets", bool(assets), f"{len(assets)} found")
    bad = []
    for asset in assets:
        astatus, _ = client.get(f"{PREFIX}/app/{asset}")
        if astatus != 200:
            bad.append(f"{asset}:{astatus}")
    check("every referenced asset serves", not bad, ", ".join(bad) or f"all {len(assets)} ok")


async def check_ws_beats(base: str, token: Optional[str], session_id: str) -> tuple[bool, str]:
    try:
        import websockets  # type: ignore
    except ImportError:
        return False, "websockets package not installed — skipped (install to enable)"
    ws_base = base.replace("http://", "ws://").replace("https://", "wss://")
    query = f"?token={token}" if token else ""
    url = f"{ws_base}{PREFIX}/sessions/{session_id}/ws{query}"
    seen: list[str] = []
    try:
        async with websockets.connect(url, open_timeout=30) as ws:
            # CONTRACT: live events stream on the WS that initiated the turn
            # (router scopes emit to the receiving socket; REST turns are
            # covered by snapshot polling) — so submit the turn over the WS,
            # exactly like the real console client.
            await ws.send(json.dumps(
                {"content": "One quick question — what mood did you read off the footage?"}
            ))
            deadline = time.time() + 240
            while time.time() < deadline:
                try:
                    frame = await asyncio.wait_for(ws.recv(), timeout=10)
                except asyncio.TimeoutError:
                    continue
                event = json.loads(frame)
                seen.append(str(event.get("event_type") or event.get("type")))
                if "agent.reasoning" in seen and any(s.startswith("message.") for s in seen):
                    break
    except Exception as exc:  # noqa: BLE001 - smoke check: any failure is a FAIL, not a crash
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"
    ok = "agent.reasoning" in seen and any(s.startswith("message.") for s in seen)
    return ok, f"events seen: {sorted(set(seen))}"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token", default=None, help="API key when AGENTIC_AUDIO_REQUIRE_AUTH=1")
    parser.add_argument("--with-generation", action="store_true",
                        help="spend one real generation through the deployed workers")
    parser.add_argument("--generation-timeout-s", type=float, default=600)
    args = parser.parse_args(argv)

    client = Client(args.base_url, args.token)
    print(f"agentic-audio endpoint smoke → {args.base_url}", flush=True)

    # 1. console assets
    check_console_assets(client)

    # 2. upload
    with tempfile.TemporaryDirectory() as tmp:
        video = Path(tmp) / "smoke.mp4"
        if not check("test video rendered (ffmpeg)", make_test_video(video)):
            return _summary()
        try:
            status, up = client.upload_video(video)
        except Exception as exc:  # noqa: BLE001
            check("upload accepted", False, str(exc)[:150])
            return _summary()
        artifact_id = up.get("artifact_id") or up.get("source_artifact_id")
        check("upload accepted", status == 200 and bool(artifact_id), f"artifact={artifact_id}")
        if not artifact_id:
            return _summary()

    # 3. session + real analysis (bootstrap blocks on it)
    t0 = time.time()
    status, created = client.post_json(f"{PREFIX}/sessions", {
        "source_video_artifact_id": artifact_id,
        "creator_user_id": "endpoint-smoke",
        "initial_message": "Score this clip — short instrumental.",
    }, timeout=1200)
    session_id = created.get("session_id")
    check("session bootstrap (real analysis)", status == 200 and bool(session_id),
          f"{time.time()-t0:.0f}s, phase={created.get('phase')}")
    if not session_id:
        return _summary()

    # 4. live WS events during a turn
    ws_ok, ws_detail = asyncio.run(check_ws_beats(args.base_url, args.token, session_id))
    check("websocket beats live", ws_ok, ws_detail)

    # 5. resume/snapshot
    status, snap_bytes = client.get(f"{PREFIX}/sessions/{session_id}", timeout=120)
    snap = json.loads(snap_bytes or b"{}")
    state = snap.get("state") or {}
    check("resume snapshot has history + observation",
          status == 200 and bool(snap.get("messages")) and bool(state.get("observation")),
          f"messages={len(snap.get('messages') or [])}")

    # 6. optional paid generation through the deployed fleet
    if args.with_generation:
        proposals = state.get("proposals") or []
        if not proposals:
            client.post_json(f"{PREFIX}/sessions/{session_id}/messages",
                             {"content": "Propose a couple of directions, please."}, timeout=900)
            _, snap_bytes = client.get(f"{PREFIX}/sessions/{session_id}", timeout=120)
            proposals = ((json.loads(snap_bytes).get("state")) or {}).get("proposals") or []
        if not check("proposals offered", bool(proposals)):
            return _summary()
        target = proposals[0]["proposal_id"]
        client.post_json(f"{PREFIX}/sessions/{session_id}/choices",
                         {"choice_type": "proposal", "target_id": target}, timeout=900)
        deadline = time.time() + args.generation_timeout_s
        audio_url = None
        while time.time() < deadline:
            _, snap_bytes = client.get(f"{PREFIX}/sessions/{session_id}", timeout=120)
            cands = ((json.loads(snap_bytes).get("state")) or {}).get("candidates") or []
            done = [c for c in cands if c.get("status") == "completed" and c.get("audio_url")]
            if done:
                audio_url = done[0]["audio_url"]
                break
            if any(c.get("status") == "failed" for c in cands):
                break
            time.sleep(20)
        check("real generation completed", bool(audio_url))
        if audio_url:
            req = urllib.request.Request(str(audio_url), headers={"Range": "bytes=0-99"})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    check("take playback probe", resp.status in (200, 206),
                          resp.headers.get("Content-Range") or f"HTTP {resp.status}")
            except Exception as exc:  # noqa: BLE001
                check("take playback probe", False, str(exc)[:120])

    return _summary()


def _summary() -> int:
    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"\n{'PASS' if not failed else 'FAIL'} — {len(CHECKS) - len(failed)}/{len(CHECKS)} checks green"
          + (f"; failed: {', '.join(failed)}" if failed else ""), flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
