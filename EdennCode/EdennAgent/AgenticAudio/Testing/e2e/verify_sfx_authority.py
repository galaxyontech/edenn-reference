"""Does the SFX render honour the plan, and does a redo honour what was kept?

Two claims, both about money and both previously false.

The first: the render spots the moments the user approved. Before plan authority
the answer was "only by coincidence" — the plan was flattened into prose and the
workflow re-spotted the video itself.

The second: redoing one hit costs one generation. The console says so on the
confirm dialog ("every other sound in the bed is kept as it is"), the tool built
the map of effects to keep, the map reached the job payload — and the only code
that ran it never read the field. Every redo re-synthesised the whole bed and
charged for it. The proof here is byte-level: the kept effects must come back
pointing at the SAME rendered file, not a new one that sounds similar.

SPENDS REAL MONEY: one effect per planned moment, plus one for the redo.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PREFIX = "/api/v2/agentic/audio"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def _req(self, method: str, path: str, body: Any = None, timeout: float = 300):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(f"{self.base}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            return exc.code, {"error": exc.read().decode()[:500]}

    def get(self, path: str):
        return self._req("GET", path, None)

    def post(self, path: str, body: Any, timeout: float = 600):
        return self._req("POST", path, body, timeout)

    def upload(self, path: Path):
        boundary = "----sfxverify"
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


def state_of(client: Client, sid: str) -> dict[str, Any]:
    return client.get(f"{PREFIX}/sessions/{sid}")[1].get("state") or {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8800")
    ap.add_argument("--video", default="EdennCode/TestSuites/assets/smoke/videos/Videos2026-04-08_104426_347.mp4")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()
    client = Client(args.base_url)

    print("\n=== SFX plan authority E2E ===\n", flush=True)
    status, up = client.upload(Path(args.video))
    artifact_id = up.get("artifact_id")
    if not check("upload", status == 200 and bool(artifact_id)):
        return 1

    status, session = client.post(
        f"{PREFIX}/sessions",
        {
            "source_video_artifact_id": artifact_id,
            "creator_user_id": "sfx_verify",
            "initial_message": "Add sound effects only — no music, no narration.",
        },
    )
    sid = session.get("session_id")
    if not check("session bootstraps", status == 200 and bool(sid)):
        print(json.dumps(session, indent=2)[:800])
        return 1

    # Answer whatever the agent asks (modality gate, then the SFX treatment card)
    # until a plan exists.
    plan: list[dict[str, Any]] = []
    for turn in range(8):
        st = state_of(client, sid)
        plan = ((st.get("layers") or {}).get("sfx") or {}).get("events") or []
        if plan:
            break
        pending = st.get("pending_clarification") or {}
        options = pending.get("options") or []
        if options:
            pick = next(
                (o for o in options if "sound" in str(o.get("label", "")).lower()
                 or "sfx" in str(o.get("id", "")).lower()),
                options[0],
            )
            client.post(
                f"{PREFIX}/sessions/{sid}/choices",
                {"choice_type": "clarification", "target_id": pick.get("id"),
                 "payload": {"layers": ["sfx"]}},
            )
        else:
            client.post(
                f"{PREFIX}/sessions/{sid}/messages",
                {"content": "Spot the sound effects now — a couple of clear hits is plenty."},
            )
    if not check("agent produced a plan", bool(plan), f"{len(plan)} event(s)"):
        print(json.dumps(state_of(client, sid).get("layers") or {}, indent=2)[:1200])
        return 1

    planned_starts = [round(float(e.get("start_s") or 0), 2) for e in plan]
    print(f"    planned: {[(e.get('label'), s) for e, s in zip(plan, planned_starts)]}", flush=True)

    # Render it (the one paid step).
    t0 = time.time()
    status, _ = client.post(f"{PREFIX}/sessions/{sid}/choices", {"choice_type": "sfx", "payload": {}})
    check("render dispatched", status == 200)

    variant: dict[str, Any] = {}
    deadline = time.time() + args.timeout
    last = ""
    while time.time() < deadline:
        sfx = ((state_of(client, sid).get("layers") or {}).get("sfx") or {})
        variants = sfx.get("variants") or []
        if variants:
            variant = variants[-1]
            now = f"{variant.get('status')} ({int(time.time()-t0)}s)"
            if now != last:
                print(f"    … variant: {now}", flush=True)
                last = now
            if variant.get("status") in {"completed", "failed", "error"}:
                break
        time.sleep(10)

    if not check(
        "a real SFX variant rendered",
        variant.get("status") == "completed" and not variant.get("placeholder"),
        f"status={variant.get('status')} placeholder={variant.get('placeholder')}",
    ):
        print(json.dumps(variant, indent=2)[:1000])
        return 1

    # THE CLAIM: the render honoured the approved plan.
    check(
        "the variant reports how it was spotted",
        variant.get("spotting") == "plan",
        f"spotting={variant.get('spotting')!r}",
    )
    rendered = variant.get("rendered_events") or []
    check("the variant carries a rendered-event manifest", bool(rendered), f"{len(rendered)} event(s)")
    if rendered:
        rendered_starts = [round(float(e.get("start_s") or 0), 2) for e in rendered]
        print(f"    rendered: {[(e.get('label'), s) for e, s in zip(rendered, rendered_starts)]}", flush=True)
        check(
            "every approved moment was rendered",
            len(rendered) == len(plan),
            f"planned {len(plan)} -> rendered {len(rendered)}",
        )
        drift = [
            abs(r - p) for r, p in zip(sorted(rendered_starts), sorted(planned_starts))
        ]
        check(
            "the hits landed where the user put them",
            bool(drift) and max(drift) <= 0.05,
            f"max drift {max(drift):.3f}s" if drift else "no events",
        )
        check(
            "the events are attributed to the user, not the engine",
            all(e.get("origin") == "user" for e in rendered),
            f"origins={sorted({str(e.get('origin')) for e in rendered})}",
        )

    # ---- the redo leg: one effect, not the whole bed --------------------
    # Only effects the first take actually RENDERED can be kept: an event that
    # came back empty has nothing to reuse, and counting it as "kept" fails
    # the run on behaviour that is correct. Identity is the served URL — the
    # result records each effect under the URL it was first published at, and
    # a kept effect keeps that URL while a regenerated one gets a fresh one.
    playable = [
        e for e in rendered
        if e.get("rendered") and str(e.get("audio_url") or "")
    ]
    if len(playable) >= 2:
        first_urls = {
            str(e.get("id")): str(e.get("audio_url") or "") for e in playable
        }
        redo_id = str(playable[0].get("id") or "")
        kept_ids = sorted(set(first_urls) - {redo_id})
        print(f"\n    redoing {redo_id}, expecting {len(kept_ids)} kept", flush=True)

        t1 = time.time()
        status, _ = client.post(
            f"{PREFIX}/sessions/{sid}/choices",
            {"choice_type": "sfx", "payload": {"event_ids": [redo_id]}},
        )
        check("redo dispatched", status == 200)

        redone: dict[str, Any] = {}
        deadline = time.time() + args.timeout
        last = ""
        while time.time() < deadline:
            sfx = ((state_of(client, sid).get("layers") or {}).get("sfx") or {})
            variants = sfx.get("variants") or []
            if len(variants) >= 2:
                redone = variants[-1]
                now = f"{redone.get('status')} ({int(time.time()-t1)}s)"
                if now != last:
                    print(f"    … redo variant: {now}", flush=True)
                    last = now
                if redone.get("status") in {"completed", "failed", "error"}:
                    break
            time.sleep(10)

        if check(
            "the redo renders",
            redone.get("status") == "completed",
            f"status={redone.get('status')}",
        ):
            reused = [str(x) for x in (redone.get("reused_event_ids") or [])]
            check(
                "the effects the user kept were not generated again",
                sorted(reused) == kept_ids,
                f"reused={sorted(reused)} expected={kept_ids}",
            )
            second_urls = {
                str(e.get("id")): str(e.get("audio_url") or "")
                for e in (redone.get("rendered_events") or [])
            }
            same = [i for i in kept_ids if second_urls.get(i) == first_urls.get(i)]
            check(
                "each kept effect is the SAME served file, not a similar one",
                sorted(same) == kept_ids,
                f"{len(same)}/{len(kept_ids)} identical by URL",
            )
            check(
                "the redone effect really was made again",
                bool(second_urls.get(redo_id))
                and second_urls.get(redo_id) != first_urls.get(redo_id),
                f"{first_urls.get(redo_id)} -> {second_urls.get(redo_id)}",
            )
            check(
                "no rendered effect leaks a server path",
                all(
                    "audio_path" not in e
                    for e in (redone.get("rendered_events") or [])
                ),
                "result entries carry audio_url only",
            )

    print(f"\n  session: {sid}")
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n=== {len(RESULTS)-len(failed)}/{len(RESULTS)} checks passed ===\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
