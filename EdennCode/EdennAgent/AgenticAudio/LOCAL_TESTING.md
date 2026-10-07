# AgenticAudio — local testing guide

Everything here runs on your machine, spends nothing by default, and needs no
cloud access. Point your the coding agent at this repo and paste any command below —
or just tell it "start the agentic audio devserver and open two tabs as two
users" and let it drive.

The product in one line: upload a video → an agent director analyzes it,
proposes directions, generates music/voice-over/SFX takes, and composes one
final mix — with live multi-user collab (comments, roles, shared sessions) on
top.

---

## 0. Prereqs (one-time)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

- `ffmpeg` on PATH (`brew install ffmpeg`) — remuxing/compose use it.
- Run everything from the **repo root** with `PYTHONPATH=.`.
- No LLM keys? Fine — the devserver falls back to a deterministic offline
  director (same flow, scripted reasoning). With the model gateway env configured
  you get live LLM turns.

## 1. Fastest look: offline mock (zero backend)

```bash
python3 -m http.server 5599 --directory EdennCode/EdennAgent/AgenticAudio/frontend
```

Open <http://localhost:5599>. Fully in-browser mock: entrance, chat, canvas,
comments — all clickable; generation returns silent placeholders instantly.
Good for UI work; useless for audio quality.

## 2. The real thing locally: dev backend

```bash
env PYTHONPATH=. EDENN_DEV_REAL_MUSIC=0 \
  .venv/bin/python EdennCode/EdennAgent/AgenticAudio/design/devserver.py
```

Open <http://localhost:8800> (it self-redirects to `?backend=real`). This runs
the REAL agent loop, REAL API router, real ffmpeg mixing — generation jobs
resolve to placeholder audio unless you add provider keys or a pregen bundle
(see §6). Sessions/collab live in memory: a server restart wipes them.

Add `&demo=1` to the URL to use the seeded demo clip instead of uploading, or
upload any mp4 ≥15s.

### The creator flow to smoke-test

1. Entrance → click the account chip (bottom-left) and name yourself.
2. Attach a video, type a direction ("cinematic and premium"), Start session.
3. Answer the intent card (pick **Full audio** for the whole tour).
4. Approve a direction → confirm spend → two takes appear; play them.
5. Lock a take ("Use this"), then ask in chat: *"Now draft the voice-over."*
   Edit the script text right in the card, Generate.
6. Ask: *"Add sound effects matched to the action."* Answer the treatment
   card, then try the hybrid proposers on the SFX card — **Suggest
   transitions** (deterministic, snapped to your cuts) and **Ideas beyond the
   frame** (LLM, rationale on every ghost). Accept a ghost (free) and it
   becomes a plan row; dismissals stay dismissed. Then **Edit plan** — retime
   a row, add one, Save (free) — and Generate.
7. Ask: *"Lock it in — compose the final mix on the video."* Answer any
   clarify it raises. Export downloads the deliverable.
8. Toggle **Canvas** (topbar) for the lineage-tree view. Home (the sparkle
   logo) → **My sessions** / **Gallery** to come back later.

## 3. Collab as two real users (one machine)

Origins isolate identity, so one browser is enough:

- **User A**: <http://localhost:8800> — create a session (steps above).
- Share (topbar) → pick the link role (view / comment / iterate) → Copy link.
- **User B**: open the link but swap the host to **127.0.0.1** — separate
  localStorage → a genuinely different person. The join card asks who you are.

Then verify, live, no refreshes anywhere:

- B's join makes them appear in A's facepile instantly.
- B comments on a canvas node (Canvas → Comment → click a node) → the pin pops
  up unread on A's canvas; A replies from the pin.
- With **iterate**, B can direct the agent from the composer — A's tab streams
  the same turn (reasoning beats, new takes) in real time.
- A demotes B to "Can view" in the Share dialog → B's tab toasts, reconnects,
  and loses the comment/direct affordances; promoting restores them live.
- B's **My sessions** shows the session badged "Shared with you".
- Share dialog → **Copy result link** → opens a public read-only player page.

## 4. Auth mode + ready-made test accounts

```bash
env PYTHONPATH=. EDENN_DEV_REAL_MUSIC=0 \
  AGENTIC_AUDIO_REQUIRE_AUTH=1 \
  AGENTIC_AUDIO_API_KEYS=edenn-test-owner:test_owner,edenn-test-collab:test_collab,edenn-test-viewer:test_viewer \
  .venv/bin/python EdennCode/EdennAgent/AgenticAudio/design/devserver.py
```

| Token               | Identity      | Use as                    |
| ------------------- | ------------- | ------------------------- |
| `edenn-test-owner`  | `test_owner`  | creator/owner             |
| `edenn-test-collab` | `test_collab` | invited collaborator      |
| `edenn-test-viewer` | `test_viewer` | view/comment-only guest   |

Open <http://localhost:8800/?backend=real&token=edenn-test-owner> — or open it
bare and paste a token into the sign-in card that appears on any 401 (Settings
takes it too). Tokens ride the tab URL only; nothing is stored.

Under auth, share links are **signed grants**: the owner's Share dialog mints
`?grant=` links; the recipient signs in as themselves (use the collab token on
the 127.0.0.1 side) and redeems it at the granted role. Things worth trying to
watch the enforcement be real: a spoofed `author_id` in a comment is replaced
server-side; the viewer token gets 403 trying to direct; non-owners get 403
minting links; tampered/expired grants get refused.

## 5. Test suite

```bash
PYTHONPATH=. .venv/bin/python -m pytest EdennCode/EdennAgent/AgenticAudio/Testing/ -q
```

~120 tests, hermetic, ~6 min. Writing collab tests? Use the
`_client_with_memory_collab` helper — the default collab repository in the
harness is the live pg one and will FK-violate against shared dev Postgres.

## 6. Optional extras

- **Real generated audio locally**: `EDENN_DEV_REAL_MUSIC=1` + provider keys
  in env (see `LocalEnv/.env` conventions) makes music/VO/SFX jobs hit real
  providers — this spends credits.
- **Demo bundle (fast + real, no spend)**: `EDENN_DEV_PREGEN=<dir>` serves
  pre-generated artifacts after an honest 4s beat. The dir needs a
  `manifest.json` mapping job types to files — see the "pre-generated demo
  bundle" section in `design/devserver.py` for the exact shape. This is how
  the walkthrough video in `docs/demo/` was recorded.
- **Cut-a-short (transform) chip**: needs `EDENN_CREATION_MEDIA_DIR=<dir of
  clips>` on the devserver; otherwise the chip explains itself and steps aside.

## 7. Gotchas

- **Stale frontend after pulling changes**: assets are versioned
  (`index.html` `?v=eN`); if you edit frontend files yourself, bump the `v`
  or hard-reload.
- **In-memory state**: sessions, collab threads, and jobs die with the
  devserver process. That's by design for dev.
- **Ports**: 8800 (devserver), 5599 (static mock). One devserver at a time.
- **The docs live next to the code**: `frontend/README.md` (console
  architecture + test accounts), `frontend/COLLAB_MODE_PLAN.md` (collab design
  + current limits), `DEPLOY_STANDALONE.md` (the cloud demo deployment —
  `max-replicas 1` there is load-bearing).
