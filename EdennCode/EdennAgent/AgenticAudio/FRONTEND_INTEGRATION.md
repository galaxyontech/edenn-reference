# Agentic Audio — Frontend Integration Guide

Handoff for building the **chat-first audio-director UI**. This doc is self-contained:
the mental model, the API surface, every event the UI must render, the card-based
interaction model, and the UX workflows (information delivery + approval). For the
backend rationale see `ARCHITECTURE.md` and `DESIGN_LLM_AGENT.md`. The wire
vocabulary below is verified against `events.py`, `api/router.py`,
`api/serialization.py`, `api/auth.py`, `agent/agent.py`, `agent/loop.py`,
`agent/session_io.py`, `tools/base.py`, `tools/impls.py`, `tools/media.py`,
`models.py`, and `persistence/repositories.py`.

---

## 1. Mental model

The user uploads a video and **chats with an audio director**. The agent reasons,
proposes directions, generates music (and optionally voice-over), and the user
iterates — lowering the music, restyling, adjusting the mix — forever (the session
never "ends"). The UI is a **conversation with rich cards and live status**, not a
form.

Three things the UI must get right:
1. **Stream the agent's thinking** (a live "Composing your tracks…" status), so
   the chat never feels frozen while the agent works.
2. **Render structured cards** (proposals, candidates, clarify chips, mix
   controls) — most user actions are taps on cards, not typing.
3. **Gate spend behind explicit approval** — generation costs money/time; the UI
   must make the user click "approve/generate," and show progress while jobs run.

---

## 1.1 Reference console (bundled)

A complete reference implementation of everything in this guide ships in
`frontend/` (`index.html` + `styles.css` + `mock-backend.js` + `js/app.js` +
`js/canvas-mode.js`, no build step) — the **canonical** frontend. It is served
**same-origin** by the API itself at:

```
GET /api/v2/agentic/audio/app
```

(`/app` redirects to `/app/` so the page's relative asset paths resolve.) It
requires `AGENTIC_AUDIO_ENABLED=1` (the flag that mounts the router —
`api/router.py mount_agentic_audio_router`) and, for the upload step,
`ASYNC_PIPELINE_V2_ENABLED=1` (the `/api/v2/assets/video` endpoint). Open that
URL, upload a video, and chat — it exercises the full WS + REST contract below
and is the canonical example for a production UI.

> **Transport:** served under `/app` it talks to the **live** backend; opened
> standalone (e.g. a static file server) it falls back to the bundled in-browser
> **mock** so the whole flow runs offline. Override either way with
> `?backend=real` / `?backend=mock`. The console persists the `session_id` in the
> URL (`?session=…`) and rehydrates via `GET /sessions/{id}` on refresh (real
> backend only — the mock is per-page).

> **Asset serving is allow-listed.** `GET /app/{asset}` only serves paths in the
> `FRONTEND_ASSETS` set in `api/router.py` (`index.html`, `styles.css`,
> `mock-backend.js`, `js/app.js`, `js/canvas-mode.js`) and additionally verifies
> the resolved file stays inside `frontend/`. Adding a new frontend module means
> adding it to that set, or the router 404s it.

---

## 2. API surface

Base path: `/api/v2/agentic/audio` (see `api/router.py`).

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/sessions` | Create a session for a source video; optional first message |
| `GET` | `/sessions` | List the caller's sessions, most-recent first (History view) |
| `GET` | `/sessions/{id}` | Fetch the full session snapshot (use on load / reconnect) |
| `POST` | `/sessions/{id}/messages` | Send a free-text user message |
| `POST` | `/sessions/{id}/choices` | Send a structured choice (tap a card/chip) |
| `WS` | `/sessions/{id}/ws` | Live event stream (recommended primary channel) |

### Create session
```jsonc
POST /sessions
{ "source_video_artifact_id": "artifact_...", "creator_user_id": "...",
  "initial_message": "Make it cinematic and premium." }   // initial_message optional
// -> { session_id, status, phase, status_url, ws_url }
```
`source_video_artifact_id` comes from the asset-upload flow (`POST /api/v2/assets/video`).

**Auth** is gated by `AGENTIC_AUDIO_REQUIRE_AUTH` (default off; token→user mapping
via `AGENTIC_AUDIO_API_KEYS`, else the token IS the dev user id — `api/auth.py`).
When on, the session is owned by the authenticated caller and every other endpoint
403s for non-owners. When off, `GET /sessions` accepts an optional `?creator=`
filter instead.

### Messages and choices
`POST /messages` body: `{ "content": "lower the music", "payload": { ... } }`
(`payload` optional — see **context_refs** below; empty `content` is a 400).
`POST /choices` body: `{ "choice_type": "...", "target_id": "...", "payload": {} }`.

`choice_type` is one of (all dispatched **deterministically** in
`agent/agent.py handle_choice` — no LLM round-trip, except `clarification` which
feeds the answer back into the reasoning loop):
- `proposal` — user picked a music direction card (**this is also the approval to
  spend**). `target_id` = proposal_id. **Re-approve guard:** if the session already
  has usable takes (any candidate whose status is not `failed`/`error`), the
  backend refuses with an `ApprovalRequiredError` ("branch a variation instead")
  rather than double-spending and wiping the takes — surfaced as a 400 on REST or
  an `error` event on WS. All-failed sessions may re-generate.
- `candidate` — user picked a generated track to finalize. `target_id` = candidate_id.
- `compose` — finalize the currently selected candidate (errors if none selected).
- `clarification` — user tapped a quick-reply chip on a clarify card. `target_id`
  = option_id; optional `payload.text` overrides the chip label as the answer.
- `mix` — tune the blend from slider values (structured; replaces free-text).
  `payload` carries any of `music_volume` (0–1), `voiceover_volume` (0–1.5),
  `duck_gain_db` (−24–0), `voiceover_start_s` (0–15 s), `preserve_original_audio`
  (ranges are the reference console's slider bounds; the backend takes floats).
  Routes to `compose_mix` once a voice-over has rendered audio, else the cheap
  `adjust_remix` (which only reads `candidate_id` / `music_volume` /
  `preserve_original_audio`). **Free + instant.**
- `variation` — branch a new take from a candidate. `target_id` = candidate_id;
  optional `payload.edit_kind` (`regenerate` default / `extend` / `creative_edit`)
  + `prompt` / `extend_seconds`. **Spends** — confirm first.
- `voiceover` — (re)draft the supplied script (free), then run TTS. `payload`:
  `script`, `voice_id`, `tone`, optional `speed` / `language`. **Spends** — confirm first.

Both `/messages` and `/choices` return:
```jsonc
{ "session_id": "...", "snapshot": { ...full snapshot... }, "events": [ ...events... ] }
```
So even without the WS you can drive the whole UI from these responses. **Prefer
the WS** for live status; fall back to the REST `events`+`snapshot` if WS drops.

### context_refs (canvas @-references) — read by the agent

A user message's `payload` may carry `context_refs`: a list of candidate ids
and/or the sentinel `"__source__"` (the source video). This is how canvas mode
pins "make the drop harder" to a specific take. The reasoning loop **actually
consumes these** (`agent/loop.py _context_ref_note`): when building the LLM's
view of the conversation it appends a note naming the referenced take(s) so the
model targets that version directly instead of asking "which one?". Details:
- Refs are deduped and capped at 8 per message; unresolvable/stale ids are
  silently dropped (`__source__` renders as "the source video").
- The persisted message content stays clean — the note exists only in the LLM
  context. The refs remain visible in `snapshot.messages[].payload`.
- Works on both transports: REST `payload` and the WS message frame's `payload`.

---

## 3. WebSocket protocol

Auth: browsers cannot set WS headers, so pass `?token=…` as a query param (an
`Authorization` header also works for non-browser clients). Auth failures and
unknown sessions send an `error` event and close the socket with code 1008.

On connect the server sends a `session.opened` event with the full snapshot
(use it to render/rehydrate). Then send user input as JSON frames:
- a message: `{ "content": "lower the music", "payload": { "context_refs": ["candidate_..."] } }`
  (`payload` optional; a non-JSON text frame is treated as `{content: <raw>}`;
  an empty `content` gets an `error` event and the socket stays open)
- a choice: `{ "choice_type": "proposal", "target_id": "proposal_cinematic" }`
  (any frame containing `choice_type` is routed as a choice)

The server streams back the events below **live as the turn unfolds** (the WS
endpoint passes an emit sink into the agent), then a fresh `session.opened`
snapshot at the end of each successful turn. A turn that fails ends with just an
`error` event instead (no trailing snapshot) — and the socket stays open.

> **Turns are serialized per session.** `agent/agent.py` holds an asyncio lock
> per session id around every message/choice turn (`_turn_lock`), so a WS turn
> racing a REST fallback (or a second tab) queues instead of corrupting state.
> Don't rely on firing concurrent turns for the same session — the second one
> waits. (The lock is in-process; multi-worker deployments need repo-level
> concurrency control.)

---

## 4. Event vocabulary (what to render)

Every event is `{ event_type, session_id, payload }`. Render them in arrival order.
The `event_type` strings are defined once on the backend in `events.py` as the
`EventType` str-enum — the single source of truth for this vocabulary, so a rename
fails the backend tests rather than silently breaking the UI. Keep the frontend's
handler in sync with that enum.

| `event_type` | When | Payload | UI |
|---|---|---|---|
| `session.opened` | WS connect / end of each successful WS turn | `{ snapshot }` | Rehydrate the whole view |
| `message.created` | a chat line is persisted (user echo **and** assistant) | `{ message_id, role, content }` | Append a chat bubble; the `role:"user"` echo is your delivery ack |
| `agent.reasoning` | turn start + each agent step, **before** the effect | `{ status, intent, thought }` | Live status line + thinking trail |
| `tool.started` | a tool with visible work begins | `{ tool_name }` | Show "working" on the relevant card |
| `tool.completed` | that tool finishes | `{ tool_name, ...tool-specific }` | Clear the working state |
| `proposal.cards` | agent offers music directions (max **2**) | `{ proposals: [...] }` | Render selectable **proposal cards** |
| `candidate.cards` | takes created/updated (generate, remix, edit) | `{ candidates: [...] }` | Render **candidate cards** (audio/video players) |
| `candidate.selected` | finalize locked a track | `{ candidate_id }` | Highlight it |
| `clarify.cards` | agent (or the intent gate) needs input | `{ question, options:[{id,label,hint}], gate? }` | Render a **question + quick-reply chips** |
| `production.plan` | audio plan set | `{ mode, layers }` | Show the plan (music / +voiceover) |
| `voiceover.script` | VO draft to review | `{ script, voice_id, language, tone, voice_options }` | Show the **script + voice picker**, await approval |
| `voiceover.generating` | VO TTS queued | `{ script, voice_id, language, speed, tone, linked_job_id, status }` | Show "recording narration…" |
| `mix.updated` | `compose_mix` re-mixed the layers | `{ music_candidate_id, music_volume, voiceover_volume, duck_gain_db, voiceover_start_s, preserve_original_audio, status, video_url, message }` | Update the **mix panel** + preview player |
| `final.artifact` | final deliverable ready | `{ status, video_url, deliverable?, mix?, ... }` | Show the **final result** + download/share |
| `direction.approved` | the `approve_direction` **tool** recorded approval | `{ approved, proposal_id }` | Mark the direction approved |
| `phase.changed` | session phase moved | `{ phase }` | Update the stage indicator |
| `choice.recorded` | a card/chip tap was logged | `{ choice_id, choice_type, target_id }` | Audit / your ack for a `/choices` tap |
| `error` | something failed | see **Error payloads** below | Toast / inline error |

Notes verified against the emit sites:

- **`message.created` acks.** Every persisted message — including the *user's
  own* (appended by `handle_user_message`, the clarification answer, and the
  optional `initial_message` at bootstrap) — is emitted as `message.created`.
  Over the WS, seeing your own text come back with `role:"user"` confirms it was
  recorded. Deterministic assistant acks are also plain `message.created` events:
  after a proposal tap ("Generating takes of "…" now — this spends generation
  credits…") and after finalize ("Locked in "…"…"), no LLM call involved.
- **`agent.reasoning` beats.** Two kinds, same payload shape:
  1. a **deterministic turn-start beat** the moment a user message enters the
     reasoning loop (`agent/loop.py run` — free-text messages and clarification
     answers; purely deterministic `/choices` turns skip the loop and have no
     beats): `status: "Reading your direction"`, `intent: null`, `thought`
     quoting the first ~160 chars of the message — so the thinking trail has
     content while the first LLM step is still running. A bootstrap
     `initial_message` gets this beat **only on non-gated mounts**
     (`require_intent_gate=False`): on the production mount (gate on by default,
     `api/router.py`) the bootstrap persists the message (`message.created`) but
     returns before the loop runs (`agent/agent.py bootstrap_session`) — its
     only deterministic beat is the intent-gate one ("Watching your video…",
     `intent: "analyze"`) before its analysis runs;
  2. **per-step model beats** (at most `AGENTIC_AUDIO_MAX_STEPS_PER_TURN`,
     default 4, per turn): `status` is the tool's label from `models.TOOL_SPECS`
     ("Watching your video…", "Composing your tracks…", "Reworking the track…",
     …) or an action label ("Lining up directions…", "Checking a detail with
     you…" — `agent/session_io.py reasoning_event`); `intent` is the model's
     classified user intent; `thought` is the model's own reasoning, truncated
     to 400 chars.
- **`tool.started`/`tool.completed`.** Emitted by the tools with visible work:
  `analyze_video`, `generate_candidates`, `finalize`, `adjust_remix`,
  `edit_audio`, `generate_voiceover`, `compose_mix`. Light record-keeping tools
  (`approve_direction`, `set_production_plan`, `propose_script`) skip them and
  emit their card event directly. `tool.completed` carries a tool-specific extra
  key (`output`, `candidates`, `final_artifact`, `voiceover`, `mix`, `candidate`)
  — treat everything beyond `tool_name` as informational; the card events are the
  render source.
- **`candidate.cards` emitters.** `generate_candidates` (the fresh take set),
  `edit_audio` (full list with the new branch appended), and `adjust_remix`
  (full list with the target's `music_volume` / `preserve_original_audio` /
  `remixed_video_url` updated). Always carries the **complete** candidate list —
  replace, don't append.
- **`mix.updated` vs `candidate.cards` for mix taps.** A `mix` choice confirms
  via `mix.updated` only when it routed to `compose_mix` (voice-over audio
  exists); the music-only `adjust_remix` route confirms via `candidate.cards`
  instead. Handle both.
- **`direction.approved`** comes only from the LLM-path `approve_direction` tool.
  The deterministic proposal tap does **not** emit it — it emits
  `choice.recorded` and goes straight to generation; read approval state from
  `snapshot.state.approved_direction`.
- **`clarify.cards`** from the production intent gate carries `gate: "intent"`
  and the question "What are you adding today?" with options
  `full_audio` / `music_only` / `voiceover_only` (`agent/agent.py`). Model-raised
  clarifications carry just `{question, options}`.
- **`production.plan` is idempotent.** Re-asserting an identical plan is a no-op:
  `set_production_plan` emits nothing when `{mode, layers}` is unchanged
  (`tools/impls.py`). `voice_options` on `voiceover.script` are
  `{id, name, gender, style}` per preset.
- **`phase.changed`** is emitted on the user-visible transitions (proposals
  ready, takes queued, finalize). Transient in-tool phases (`observing`,
  `generating_candidates` mid-run) surface via `snapshot.state`/`phase` instead —
  treat the snapshot as the source of truth for the stage indicator.

### Error payloads (two shapes, both scrubbed)

The backend never leaks upstream vendor/model names to a client
(`EdennCode/Deployment/error_codes.py`). Handle both `error` payload shapes:

1. **Guidance rejections** (bad target id, empty content, spend blocked —
   `KeyError`/`ValueError` incl. `ApprovalRequiredError`, which subclasses
   `ValueError` in `tools/base.py`): `{ "message": "<crafted, scrubbed text>" }`.
   REST surfaces the same text as a 400/404 `detail`. Render inline; these are
   actionable user-facing sentences.
2. **Unexpected failures** (anything else): `public_error_payload(exc)` →
   `{ "error_code": <int>, "message": "<generic scrubbed text>", "retryable": <bool> }`.
   Toast it; offer retry when `retryable`.

Both are non-fatal on the WS — the socket stays open (only auth/404 at connect
time close it, code 1008).

> **Live streaming:** over the WebSocket these events are pushed **as they happen
> during the turn** (not batched at the end) — `agent.reasoning` beats arrive one
> by one while the agent works, giving a real agent-style "thinking"
> progression. There is no token-level streaming; the beats are turn-ordered
> status updates. (The REST endpoints still return the whole batch at once.)
>
> **Reasoning-trail UX:** the reference console shows `status` as the live
> headline and keeps a short trail of recent beats (with the model's `thought`
> as a muted sub-line), then retires the trail on the closing `session.opened`.
> Treat `thought` as untrusted text (render via `textContent`, never `innerHTML`).

---

## 5. Snapshot state (render source of truth)

`snapshot.state` is the durable session state (`persistence/repositories.py
build_snapshot`). Render from it on load and after each turn:

```jsonc
{
  "observation": { "duration_s", "scenes": [...], "video_title", ... },
  "source_video": {                            // set by analyze_video; may be null
      "url", "poster_url", "content_type", "duration_s"
  },
  "proposals": [ { "proposal_id", "title", "prompt", "modelspec",
                   "include_vocals", "vocal_gender", "music_volume",
                   // OPTIONAL — the direction comparison table renders a row per
                   // present field and degrades cleanly when absent. Nothing
                   // server-side emits it yet; this is the shape to fill in:
                   "attributes"?: { "summary", "energy": [n, ...], "energy_note",
                                    "bpm", "tempo_label", "instruments": [...],
                                    "feel": [...], "best_for" } } ],
  "approved_direction": true,                  // has the user approved spend?
  "candidates": [ {
      "candidate_id", "title", "status",       // planned|queued|processing|completed|failed
      "audio_url", "video_url",                // null until the job completes
      "remixed_video_url", "music_volume", "preserve_original_audio",
      "remixes": [...],                        // adjust_remix history (audit)
      "version", "parent_candidate_id", "edit_kind",   // branching/iteration
      "requested_edit_kind", "extend_mode",            // edit routing audit
      "stalled_seconds": 412,                  // only when the watchdog fired
      "placeholder": true                      // only for dev-harness stand-in audio
  } ],
  "selected_candidate_id": "...",
  "production_plan": { "mode": "music_first", "layers": ["music","voiceover"] },
  "layers": {
     "music": null,
     "voiceover": { "script", "voice_id", "language", "speed", "tone",
                    "status", "audio_url", "linked_job_id",
                    // Video-informed narration: the timed plan. Each segment has
                    // a real start; duration_s appears when the renderer knows
                    // it. The timeline's VO lane prefers this over the flat
                    // script + mix.voiceover_start_s shape.
                    "voice_rationale"?, "segments"?: [ { "id", "text", "start_s",
                                                         "delivery", "duration_s"? } ] },
     // SFX is a first-class layer: a dict with the spotted plan (events carry a
     // start each, no length by design), an optional continuous ambience bed,
     // rendered A/B variants, and the answered treatment. A legacy [] means "no
     // SFX layer". The timeline draws events as timed moment nubs and the
     // ambience as a full-clock bed.
     "sfx": { "status", "summary", "ambience",
              "events": [ { "id", "label", "prompt", "start_s", "reason" } ],
              "variants": [ { "variant_id", "label", "status", "audio_url", "video_url" } ],
              "selected_variant_id", "treatment", "density_cap", "cap_note",
              "over_budget", "suggestions": [...] } | []
  },
  "mix": { "video_url", "music_volume", "voiceover_volume",
           "duck_gain_db", "voiceover_start_s", "sfx_volume", "music_candidate_id" },
  // The spotting sheet: one moment list, one owner per moment, built for every
  // session from the observation (tools/spotting.py). The timeline renders it
  // as the ruler's annotation strip; owners are narrate|sfx|music|silence and
  // source_audio moments carry a real keep-out window.
  "spotting_sheet": { "moments": [ { "id", "t", "window": [lo, hi], "what",
                                     "mood", "source", "owner", "owner_source",
                                     "reason"?, "rank" } ],
                      "cut_source", "reliable" } | null,
  "pending_clarification": { "question", "options": [...], "gate"? } | null,
  "final_artifact": { "video_url", "deliverable"?, "mix"?, ... } | null,
  "memory": { "preferences": {...}, "creative_direction", "style_keywords",
              "avoid", "recent_intents": [...] },
  "turns": [ { "turn", "user_message", "intent", "actions", "tools", "phase" } ]
  // plus audit keys: approved_proposal_id, selected_proposal_id, last_mix_volume
}
```

`phase` and `status` are **top-level snapshot fields**, not keys inside
`snapshot.state` — read the stage from `snapshot.phase`, not
`snapshot.state.phase` (`build_snapshot` sets them from the session row and
passes `state_json` through unchanged; the reference console renders
`snap.phase`). The snapshot also carries top-level `source_video_artifact_id`,
`selected_candidate_id`, `linked_job_ids`, and `messages[]` / `tool_calls[]` /
`choices[]` for full history. `messages[].payload` preserves anything the client
sent (e.g. `context_refs`).

> **Vendor identity is redacted at the boundary.** Every snapshot and event dump
> passes through a serializer that drops `provider` / `provider_name` /
> `provider_audio_id` / `provider_task_id` anywhere in the payload and scrubs
> tool-call error blobs (`models.py` + `error_codes.redact_client_keys`). The UI
> must not expect a `provider` field on candidates — model tiers surface only as
> `modelspec` (`edenn_basic` / `edenn_enhanced` / `edenn_studio`).

### The generation watchdog: `stalled_seconds`

Candidates hydrate from their async job before every snapshot the router serves
(`agent/agent.py refresh_session_state`, called on `GET /sessions/{id}`, after
each message/choice turn, and on WS connect/turn-end). When a job has sat
`queued`/`processing` longer than `AGENTIC_AUDIO_GEN_STALL_SECONDS` (env var,
default **180**), the candidate gains `stalled_seconds` (its age in seconds) —
see `tools/media.py hydrate_candidate_results`. This is a **read-only flag**: the
backend does not auto-fail the job. The UI should switch the card from an
open-ended spinner to a "this looks stuck — retry?" state (the reference console
shows "~N min" and offers the variation/retry control).

### Phases (stage indicator)
`created → observing → proposing → awaiting_plan_choice → generating_candidates →
awaiting_candidate_choice → composing → completed` (a `failed` phase also exists
for hard failures — `models.py AgenticSessionPhase`). Treat `completed` as a
resting state — the user can keep editing afterward.

---

## 6. The card interaction model

Most actions are taps that POST to `/choices` (or send a WS frame):

- **Proposal card** → on "Use this", POST `{choice_type:"proposal", target_id: proposal_id}`.
  This *is* the spend approval; it kicks off generation. Show a confirm ("This
  creates the tracks — a minute or two") before sending. **Hide/disable proposal
  cards once takes exist** — the backend rejects a second approval while usable
  takes remain (see the re-approve guard in §2) and tells the user to branch a
  variation instead.
- **Clarify chips** → on tap, POST `{choice_type:"clarification", target_id: option_id}`.
  The chip's label becomes the user's answer; the agent continues automatically.
- **Candidate card** → players for `audio_url`/`video_url`; "Use this" → POST
  `{choice_type:"candidate", target_id: candidate_id}` to finalize.
- **Watch a take against the picture** → `state.source_video.url` lets the UI lay
  a take's `audio_url` over the muted source video without a per-take render
  (the canvas dock does exactly this). It's best-effort — handle `null`.
- **Voice-over script card** → editable script + a voice dropdown (`voice_options`)
  + a tone field. On confirm, POST `{choice_type:"voiceover", payload:{script, voice_id, tone}}`
  — the backend re-drafts the (possibly edited) script then runs TTS.
- **Mix panel** (when both music + VO exist) → sliders for music vs voice-over
  volume, a ducking slider, and a "narration starts at" control. Each change POSTs
  `{choice_type:"mix", payload:{<param>:<value>}}` (one typed param per slider);
  parameters merge with the prior mix server-side, so one knob at a time is fine.
  The agent re-mixes instantly (`mix.updated`, or `candidate.cards` on the
  music-only route — see §4). **Free + instant** — debounce and apply live.
- **Candidate "New variation" / retry** → POST `{choice_type:"variation", target_id: candidate_id}`
  to branch a fresh take (spends — confirm first; the same control retries a
  failed or stalled take).

> Free-text always works too as a fallback: anything the cards do, the user can also
> type ("lower the music", "make it lo-fi", "start the narration at 3s"). The cards
> use the **structured** choices above so the action is deterministic and typed.

---

## 7. UX workflows (information delivery + approval)

### A. The happy path
1. Upload video → create session (optionally with the first ask).
2. Agent: `agent.reasoning` "Watching your video…" → (production mount) the
   **intent-gate** `clarify.cards` ("What are you adding today?") → after the
   modality tap, `production.plan` then `proposal.cards`.
3. User taps a direction → **confirm spend** → `/choices proposal` →
   `choice.recorded` → `candidate.cards` (status `queued`) → a deterministic
   assistant ack explaining cost/latency.
4. Candidates start `queued`/`processing`; **poll `GET /sessions/{id}`** (or rely on
   WS `session.opened` refreshes) until `audio_url`/`video_url` populate. Show a
   per-card progress state; swap to a player when ready. If `stalled_seconds`
   appears, switch to a "looks stuck — retry?" state.
5. User taps "Use this" → `candidate.selected` → `final.artifact` → "Locked in…" ack.

### B. Approval is mandatory before the first spend
Two of the paid tools are **structurally cost-gated**: `generate_candidates`
refuses without `approved_direction` in session state, and `generate_voiceover`
refuses until a script has been drafted via `propose_script`
(`tools/impls.py`). If the model tries prematurely, the tool raises
`ApprovalRequiredError` and the loop **re-asks** ("want me to go ahead and
generate?") as a normal assistant message rather than erroring. `edit_audio` has
**no in-tool approval gate**: it only needs a resolvable target candidate — a
premature call on a fresh session gets a plain assistant reply ("There's no
music track to edit yet…", `tools/base.py no_target_message`), and once any
candidate exists an edit enqueues a paid job immediately, relying on the
direction approval already on record rather than a fresh gate. The UI should:
- Treat tapping a **proposal card** as the approval (high-assurance path).
- For free-text approvals, render the re-ask as a normal assistant message with a
  prominent "Yes, generate" affordance.
- Never show a spinner implying work is happening until you've seen
  `tool.started`/a `queued` candidate.

### C. Clarification
When `pending_clarification` is set (or a `clarify.cards` event arrives), render the
question + chips — tapping a chip (or typing) answers it and clears
`pending_clarification`. Disabling other inputs is optional.

### D. Cheap vs expensive — set expectations
- **Instant/free** (no spinner-minutes): `adjust_remix`, `compose_mix` (volume,
  ducking, VO position), `propose_script`, `set_production_plan`, `finalize`.
  Apply optimistically; `mix.updated`/`candidate.cards` confirm.
- **Minutes + costs money** (show progress, set expectations): `generate_candidates`,
  `edit_audio`, `generate_voiceover`. These enqueue jobs; results hydrate later.

### E. Iteration never ends
After `final.artifact`, keep the composer open. "Make the drop harder" branches a
new **versioned** candidate (`parent_candidate_id`, `version`, `edit_kind`); show
versions as a small history/branch UI so the user can compare and go back. Canvas
mode attaches `context_refs` so the instruction unambiguously targets a version
(§2). Edits that couldn't run as asked are honest about it: `requested_edit_kind`
records the original ask when e.g. a `creative_edit` on a Basic take fell back to
`regenerate`, and `extend_mode` says whether an extend ran natively
(`"native"`) or as a longer regenerate (`"regenerate_fallback"`).

### F. Reconnect / durability
On reconnect, `GET /sessions/{id}` (or the WS `session.opened`) returns the full
snapshot — re-render everything from it. In-flight jobs continue server-side and
hydrate into `candidates`/`layers` when done.

---

## 8. Suggested component map

| Component | Driven by |
|---|---|
| Chat transcript | `messages[]` / `message.created` |
| Live status pill + thinking trail | `agent.reasoning` (`status` + `thought`) |
| Stage indicator | `phase` / `phase.changed` |
| Direction cards | `proposals` / `proposal.cards` |
| Track cards (players + versions + stall badge) | `candidates` / `candidate.cards` |
| Watch dock (take audio over muted source) | `state.source_video` + candidate `audio_url` |
| Clarify prompt + chips | `pending_clarification` / `clarify.cards` |
| Voice-over panel (script + voice + tone) | `layers.voiceover` / `voiceover.script` |
| Mix panel (sliders) | `mix` / `mix.updated` |
| Final result | `final_artifact` / `final.artifact` |
| Audio-plan toggle | `production_plan` / `production.plan` |

---

## 9. Gotchas

- `audio_url`/`video_url` are **null until the job completes** — render a progress
  state, not a broken player. Re-fetch the snapshot to hydrate.
- A candidate's deliverable, once a music+VO mix exists, is `mix.video_url` /
  `final_artifact.video_url` (tagged `deliverable: "compose_mix"`), **not** the
  music-only candidate video. Voice-over-only sessions finalize with
  `deliverable: "voiceover_only"` and no selected candidate.
- A candidate's `prompt` is the **human-readable direction text** for the card.
  The music model actually receives a *fused* prompt (the direction grounded in
  the video's mood/tempo/instrumentation and an evenly-sampled timed scene arc —
  `tools/media.py fuse_music_style_prompt`) on base generation and on
  regenerate/extend edits (creative edits get a compact mood+tempo grounding
  instead — the parent audio is the guide); don't present `prompt` as the
  literal generation input.
- `error` events are non-fatal; the socket stays open. Expect **both** payload
  shapes in §4 (guidance `{message}` vs generic `{error_code, message, retryable}`).
  The agent also surfaces many "soft" problems as plain assistant messages (e.g.
  "there's no music track to adjust yet — generate one first?") rather than errors.
- Don't offer "regenerate this direction" once takes exist — the backend's
  re-approve guard rejects it; offer "new variation" on a take instead.
- Voice presets: `warm_female, bright_female, calm_male, narrator_male, neutral`
  (`models.VOICE_CATALOG`; default `warm_female`).
- `tone` changes re-record the VO (it's new audio); volume/ducking/position changes
  do not (instant re-mix).
- `placeholder: true` on a candidate marks dev-harness stand-in audio — badge it,
  never present it as the user's real take.
- **SFX is not a shipped layer** — never offer or render it (the backend drops it).
- **No `provider` field client-side** — vendor identity is redacted at
  serialization; key model-tier UI off `modelspec`.
