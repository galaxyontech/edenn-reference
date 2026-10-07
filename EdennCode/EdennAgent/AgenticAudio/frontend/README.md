# frontend/ — the Director Console (served, no build step)

The canonical chat-first console. Vanilla JS + CSS, no bundler, no framework.
Served same-origin by the API at `GET /api/v2/agentic/audio/app`, or statically
for offline work. The only external dependency is the Tabler icons webfont
(CDN `<link>` in `index.html`); everything else is local.

## The session shell

One layout: **conversation on the left, the work on the right.** The chat pane
is permanent — it is how everything in here is edited — and is resizable by the
grip between the two (300–560px, remembered in `localStorage`). A toggle in the
right pane's own header (`#rpane-hd`) swaps only that pane:

| Right pane | What it is |
|---|---|
| **Timeline** (default) | The video, big, with its audio underneath on the same clock: music / voiceover / sound-effect lanes over a ruler of the video's own scene cuts (plus the spotting-sheet moment strip when the backend provides one). |
| **Canvas** | The read-only lineage tree of takes + the focused-take dock. |

A cut-shorts (transform) session is *not* a third position — it takes the right
pane over on its own (`#xrail`) and hides the pane header while it holds it.

There is no phase strip. The `Start → Observe → … → Done` breadcrumb that used
to sit under the topbar was removed: the chat pane already narrates each phase
as it happens, so a second, coarser copy cost a full-width band across both
panes and said nothing new.

| File | Role |
|---|---|
| `index.html` | The single page (entrance + the session shell: `#chatpane`, `#rgrip`, and `#rpane` holding `#tstage` / `#cstage` / `#xrail`). Loads `mock-backend.js`, then `js/app.js`, then the view modules. |
| `styles.css` | The warm-light "warm-glass" design system (chat widgets + `tl-*` timeline + `cv-*` canvas + the library/gallery styles; `.lane-*` is the one lane-colour contract shared by both panes). |
| `mock-backend.js` | In-browser mock backend mirroring the real contract, so the whole flow runs **offline**. `?mockfail=1` makes generated takes fail, to exercise the failed-candidate/retry UI. Fixture media carry realistic lengths (`MUSIC_S`/`VO_S`) because the timeline measures them, and it carries the seeded SFX fixture (`seedSfx()`). |
| `js/app.js` | The console controller: transport, snapshot-driven rendering, live thinking trail, card→`/choices` actions, session persistence/reconnect, page routing (`?page=`), and the right-pane view toggle (`setRightView`). One IIFE module. |
| `js/timeline-mode.js` | The default right pane: hero video + the three audio lanes, scene ruler, spotting strip, playhead — and the chat-pane resize grip. Intervals are **measured** from the audio; planned moments draw as timed nubs, never guessed widths. |
| `js/canvas-mode.js` | Additive canvas view: lineage tree + dock player + @-references/slash commands. Only does work when the user switches to Canvas. |
| `js/collab-mode.js` | Additive collab layer over the canvas, built to the designer's Collab spec: comment pins (new/unread/read/agent/resolved), floating thread cards (composer states, @mentions incl. agents, region anchors, attachments, reactions, edit/resolve), threads rail (48px ⇄ panel), inherit-from-thread, facepile + Share. Contract-backed via `transport.collab` (mock and the real `/collab` endpoints) + live `comment.*` events. |
| `js/library-ui.js` | The studio library pages — `?page=sessions` (session index: search, sort, selection, browser-local folders/archive) and `?page=gallery` (starter directions with energy diagrams, filters, one persistent player). Presets and behaviour live here; routing is in `js/app.js`. |
| `js/studio-ui.js` | Shared presentation helpers (waveform peaks read from the real asset, never a fixture pattern) used by the chat and library surfaces. |
| `js/studio-state.js` | Small state primitives the controller leans on (request registry for in-flight turns/retries, activity-step upserts). |
| `js/ai-elements.js` | Built bundle for the React component island (activity disclosures + assistant prose only). Source and rebuild steps live in `ui/` — see `ui/README.md`; commit the bundle with any source change. |
| `UI_AUDIT_STATUS.md` | What the design audit asked for and what the implementation actually does, area by area. |
| `CANVAS_MODE_PLAN.md` | The canvas-mode phasing plan (Phases 1–2 built; deep-link etc. tracked there). |
| `COLLAB_MODE_PLAN.md` | Collab-mode design + implementation notes (frontend + backend contract + mock parity + known limits). |

## Serving + the asset allow-list (read before adding a file)

Two very different servers host this directory:

- **Production mount** — `api/router.py` serves `GET /app/` (the page) and
  `GET /app/{asset}` for assets in the **`FRONTEND_ASSETS` allow-list only**
  (`FRONTEND_ASSETS` in `EdennCode/EdennAgent/AgenticAudio/api/router.py`): `index.html`,
  `styles.css`, `mock-backend.js`, `js/app.js`, `js/library-ui.js`,
  `js/studio-ui.js`, `js/studio-state.js`, `js/ai-elements.js`,
  `js/timeline-mode.js`, `js/canvas-mode.js`, `js/collab-mode.js`,
  `js/transform-mode.js`. Anything
  not listed 404s (`serve_console_asset` in the same file — allow-list check plus
  a resolved-path-inside-`FRONTEND_DIR` defense). `GET /app` (no slash)
  redirects to `app/` so relative `./` asset paths resolve.
- **Devserver** — `design/devserver.py:814` mounts the whole directory with
  `StaticFiles(directory=frontend, html=True)`, so it serves **everything**.

That asymmetry is a trap: a new JS module works on the devserver and silently
404s in prod. **When you add an asset referenced from `index.html`, add it to
`FRONTEND_ASSETS` too.** The drift test
`test_console_asset_allowlist_covers_every_index_asset`
(`Testing/test_agentic_audio_api.py:3671`) extracts every relative
`src`/`href` from `index.html`, asserts each is allow-listed, and fetches each
from the production mount (regression: `js/canvas-mode.js` once shipped without
an entry).

**Cache caveat for browser verification:** assets are served as plain
`FileResponse`s with no `Cache-Control` headers, so browsers heuristically
cache them — after editing JS/CSS, a plain reload can run the stale file. In
the page console run `fetch(url, {cache: "reload"})` for the changed asset(s)
before reloading (or disable cache in devtools).

## `js/app.js` — the console controller

### Transport (mock vs real)

`pickTransport()` (app.js:183): served under `/api/v2/agentic/audio` → default
**RealTransport**; opened standalone → **MockTransport**. Override with
`?backend=real` / `?backend=mock`; the connection pill in the topbar doubles as
a one-click toggle (rewrites the query param and reloads).

- **MockTransport** wraps `window.MockBackend` — same snapshot/event contract,
  per-page (no cross-session history, no resume).
- **RealTransport** (app.js:70): REST (`POST /sessions`, `/messages`,
  `/choices`, `GET /sessions[/{id}]`) + WS (`/sessions/{id}/ws`). Auth via
  `?token=` on the page URL — added as a `Bearer` header on REST and a
  `?token=` query param on the WS (browsers can't set WS headers; the router
  honors both, router.py:307).
- **`deliver()`** (app.js:147) — a frame must never be dropped silently.
  `ws.send()` on a non-OPEN socket discards the frame without an error, and the
  `open` flag alone is stale while the socket is CLOSING — so it gates on the
  live `ws.readyState === WebSocket.OPEN`, and otherwise falls back to REST
  (`POST /messages` or `/choices`), replaying `res.events` and a synthesized
  `session.opened` through the same `onEvent` path. REST failures surface via
  `onError` → toast + `finalizeThinking()` (app.js:337), so the trail never
  spins on a frame that never reached the server. Server-side, the WS turn and
  a REST-fallback turn can't corrupt each other: the agent serializes turns per
  session (`agent/agent.py:255` `_turn_lock`).
- **`maybePoll()`** (app.js:917) — while any candidate or voice-over is
  `queued`/`processing`, re-fetch the snapshot every 2.5s and feed it back
  through `onEvent` as `session.opened` (generation runs on separate workers;
  the WS only pushes on turns).

### Snapshot-driven rendering (`reconcile`)

The **`session.opened` snapshot is the single source of truth** (app.js:395).
The WS sends one when the socket opens (router.py:333) and one at the end of
every turn (router.py:375); intermediate events (`message.created`, `clarify.cards`,
…) update content through the next snapshot, avoiding duplicate cards.
Activity also responds immediately to `agent.reasoning`, `tool.started`, and
`tool.completed`. Errors preserve an inline explanation and a review/retry action.
Request identities and snapshot revisions reject stale updates. Initial socket
failure loads the session through HTTP without automatically resending an action.

Activity and prose use the isolated [component build](ui/README.md). The existing
controller owns scrolling, media, and the remaining widgets.

- **Messages** are append-only and reconciled by index: render
  `msgs[renderedCount..]`, then clamp
  `renderedCount = max(renderedCount, msgs.length)` (app.js:423) — a stale
  snapshot from a poll racing a WS turn-end must never rewind the cursor and
  re-append the tail as duplicates. Optimistic user bubbles (typed sends,
  clarify picks) pre-increment `renderedCount` so the echo isn't re-rendered.
- **Blocks** (proposals/candidates/voiceover/mix/final) are placed via
  `placeBlock` and **memoized**: candidates re-render only when a
  JSON key of (id, status, urls, stalled-minutes, selection) changes
  (app.js:576) because the cards host live `<video>` players that a 2.5s poll
  rebuild would restart. The right-rail source block memoizes the same way
  (app.js:1082) and plays `state.source_video.url` — the source video is put
  into the snapshot by `analyze_video` (`tools/impls.py:67`).
- **Consumed proposals stay in the thread, disabled** ("Generated ✓"/"Use
  this" locked) instead of vanishing — history must stay reviewable.
- **Clarify persistence** (app.js:437): `state.pending_clarification` renders
  intent cards (backend tags the modality gate `gate:"intent"`) or quick-reply
  chips. When it clears, the node is *kept* with its buttons disabled — also
  covering typed (non-click) answers.

### Optimistic UI + the thinking trail

Per-turn `agent.reasoning` beats stream live into one `cot` block
(`startThinking`/`appendThinking`/`finalizeThinking`, app.js:1168). Rules:

- **`startThinking()` fires on every spend/action click** — proposal "Use
  this", candidate lock, variation/retry, voice-over generate, clarify pick,
  typed send — so there is immediate life before the first beat streams in.
- The backend guarantees a first beat: the loop emits a deterministic
  "Reading your direction" event at turn start, before the first LLM call
  returns (`agent/loop.py:86`).
- `tool.started` events map through **`TOOL_BEAT`** (app.js:351) to
  human-readable beats ("Watching your video", "Starting your takes", …) —
  only real, mapped events, nothing invented.
- Bootstrap runs ~10–15s of real analysis server-side; `startAnalyzing()`
  (app.js:1216) streams a staged "watching your video" trail meanwhile, and
  `reconcile` closes it with a beat built from the *actual* observation
  (scene count, duration, dialogue) before finalizing.
- `finalizeThinking()` runs on every `session.opened`: an empty block removes
  itself; a populated one collapses to "Thought for N steps" (click to toggle).

### Candidate cards

- **Honest media**: a completed take with a `video_url` gets a real inline
  `<video>` (source thumbnail as poster); otherwise the source poster frame;
  otherwise no thumb — never a decorative gradient pretending to be a preview.
  Dev placeholder tones are badged (`placeholder` flag / url sniff).
- **Stalled watchdog**: the backend stamps `stalled_seconds` onto a candidate
  whose job has sat `queued`/`processing` past the threshold —
  `AGENTIC_AUDIO_GEN_STALL_SECONDS`, default 180s, read in
  `tools/media.py:14` and applied in `hydrate_candidate_results`
  (`tools/media.py:326`). The card then stops pretending: "Taking longer than
  usual (N min) — the generator may be stuck" + a **Retry this take** button.
  Failed takes get the same retry. Retry/variation goes through
  `requestVariation` → spend confirm → `choice_type:"variation"`.
- A block-level hint appears after 25s of pending state with a **Check now**
  button that fetches a snapshot and reports honestly ("still composing" /
  "your takes are ready" / "couldn't reach the backend").

### Contract, persistence, misc

Card taps send **structured** `/choices` frames (`proposal` · `candidate` ·
`clarification` · `variation` · `mix` · `voiceover` · `sfx`); free text goes to
`/messages`. The intent-gate answer is a layer multi-select: it collapses to
the gate's exclusive option id (`layerChoiceId`) and carries the exact set in
`payload.layers` for the backend to consume. Events key off `events.py` (`EventType`). See
`../FRONTEND_INTEGRATION.md` for the full FE↔BE contract. Paid actions all
route through `confirmSpend` (the `#confirm-overlay` dialog) — mirroring the
backend's approval gate.

The `session_id` persists in the URL (`?session=`) and `resumeSession`
rehydrates via `GET /sessions/{id}` on refresh (real backend only). The
History popover lists the caller's sessions (`GET /sessions`) and resumes on
click. Export downloads final → mix → selected-candidate media, in that order.

`app.js` exposes `window.__edenn` (app, onEvent, requestVariation,
confirmSpend, toast, startThinking, setRightView, …) for headless tests and for
the view modules, so they act through the exact same `/choices` contract as the
cards.

## `js/timeline-mode.js` — the timeline view (default right pane)

The video with its audio laid out on the same clock. Three lanes — music,
voiceover, sound effects — over a ruler built from `observation.scenes[]`
(both `start_s/end_s` and the analyzer's `start_timestamp/end_timestamp` are
read), with the scene cuts continuing down through the lanes so you can see
whether an audio choice lands on a cut. When the backend ships a spotting
sheet (`state.spotting_sheet.moments[]`), a thin strip under the ruler marks
each moment in its owner's lane colour; the footage's own speech windows draw
as hatched keep-out spans.

**Where the intervals come from, and why it matters.** Three kinds of truth,
each drawn as itself:

| Lane | What the snapshot actually knows |
|---|---|
| music | One bed per take. `MusicCandidateCard` has no start or end — the length is **measured** off the take's own `audio_url`. |
| voiceover | A timed segment plan (`layers.voiceover.segments[]`, `start_s` each, `duration_s` when the renderer knows it) — or, for flat scripts, `mix.voiceover_start_s` + the measured recording. |
| sfx | The spotted plan: `layers.sfx.events[]` (`start_s` each, no length by design) + an optional continuous `ambience` bed drawn across the full clock. |

Measured lengths come from `probe(url)`: it loads the same `audio_url` the
cards already play and reads its real `duration`; until that resolves the clip
is a dashed pending nub anchored at its known start. Planned moments (narration
segments, SFX events) are **solid** nubs — a timed moment is not a loading
state. Nothing here ever falls back to a plausible number — a plausible number
would be indistinguishable from a measured one.

- **No overlap shading.** The lanes share one x-axis, so two layers sounding at
  once is already visible by looking down a column. Banding it as well drew a
  second grid of vertical edges on top of the scene cuts and made a busy
  timeline unreadable, so it was removed.
- **The hero prefers the composed result** (`final_artifact` → `mix.video_url`
  → the take's remix → source): watching the silent source while the lanes
  describe audio would be the console lying about what you are hearing. A URL
  that fails to load swaps in an explanatory panel and disables play.
- **Per-lane mute is a view filter, not a mix edit** — the composed video is
  one audio track and cannot honour it; the first use says so in a toast.
- **Editing stays chat-driven.** Clips seek and select; they do not drag.
  Drag-and-drop was discussed and deferred until the base visualization holds.
- The chat-pane **resize grip** (`#rgrip`, 300–560px, `--chat-w`, persisted as
  `edenn.chatWidth`) is wired here in `boot()` — if this module ever loads
  conditionally, the grip goes with it.

## `js/timeline-mode.js` — the timeline view (default right pane)

The video with its audio laid out on the same clock. Three lanes — music,
voiceover, sound effects — over a ruler built from `observation.scenes[]`, with
the scene cuts continuing down through the lanes so you can see whether an
audio choice lands on a cut.

**Where the intervals come from, and why it matters.** The backend carries
almost no timing for audio:

| Lane | What the snapshot actually knows |
|---|---|
| music | One bed per take. `MusicCandidateCard` has no start or end. |
| voiceover | `state.mix.voiceover_start_s` — a START. No duration. |
| sfx | `state.layers.sfx` exists as `[]`. **No tool writes it.** |

So clip lengths are **measured, not invented**: `probe(url)` loads the same
`audio_url` the cards already play and reads its real `duration`. Until that
resolves, the clip is a dashed pending nub anchored at its known start, which
says what it is waiting for. Nothing here ever falls back to a plausible number
— a plausible number would be indistinguishable from a measured one.

- **No overlap shading.** The lanes share one x-axis, so two layers sounding at
  once is already visible by looking down a column. Banding it as well drew a
  second grid of vertical edges on top of the scene cuts and made a busy
  timeline unreadable, so it was removed.
- **The hero prefers the composed result** (`final_artifact` → `mix.video_url`
  → the take's remix → source): watching the silent source while the lanes
  describe audio would be the console lying about what you are hearing. A URL
  that fails to load swaps in an explanatory panel and disables play.
- **Per-lane mute is a view filter, not a mix edit** — the composed video is
  one audio track and cannot honour it; the first use says so in a toast.
- **Editing stays chat-driven.** Clips seek and select; they do not drag.
  Drag-and-drop was discussed and deferred until the base visualization holds.

## `js/canvas-mode.js` — the lineage canvas (additive)

A second view per session: an infinite, pannable **lineage tree** of audio
variants + a focused-take **dock player**, rendered from the *same*
`snapshot.state` the chat uses (no backend change). Chat is the default;
`app.js reconcile()` calls `window.EdennCanvas.render(snap)` every snapshot,
which caches and no-ops until the user switches views.

- **Stage only** (`setView`, canvas-mode.js): the shell owns the chat pane, so
  switching views just shows or hides `#cstage`. This module used to build its
  own chat column and physically relocate `#thread` / `.session-composer` into
  it on every toggle, restoring them from captured anchors on the way back;
  with chat permanently on screen there is nothing to move and that machinery
  is gone. `app.js setRightView()` owns the toggle. There is deliberately no
  `?view=canvas` boot deep-link (Phase 2 item).
- **Forest layout** (`buildForest`, canvas-mode.js): root = source video;
  proposals hang off it; candidates parent to `parent_candidate_id` (branch) →
  `proposal_id` (first generation) → source (fallback). Columns = depth, rows
  = DFS leaf allocation with parents at their children's midpoint. The
  `selected_candidate_id` ancestor chain draws teal ("locked path"); branch
  edges are dashed indigo. A branch badge keys off `parent_candidate_id`,
  never the version number (sibling takes are v1/v2 but not branches).
- **HEAD-advance** (`cv.pendingBranches` + `advanceHead`, canvas-mode.js):
  when a branch is dispatched, the parent's *existing* children are
  snapshotted; when a NEW child completes, focus + the @-reference chip jump
  onto it (highest version wins). Armed only via `requestVariation`'s `onSent`
  callback — cancelling the spend dialog never latches HEAD. If every new
  child fails, the pending branch is dropped and HEAD stays on the parent.
- **Dock player** (`buildDock`/`updateDock`, canvas-mode.js): media rebuilds only when the
  `focusId|url|status` key changes, so 2.5s poll re-renders don't reset
  playback. A rendered take plays its video; an audio-only take layers the
  **muted source video** (`state.source_video.url`) slaved to the audio master
  clock (play/pause/seek + 0.3s drift correction); the source node plays the
  original clip. The primary button is context-aware: **Generate** on a
  proposal (spend-confirmed), **Use this** on a completed take (single-fire
  via `cv.pendingLockId` so poll re-renders can't re-arm it mid-dispatch),
  **Locked** once selected, **Try again** on failed *or* watchdog-stalled
  takes (same `stalled_seconds` badge as chat: "Stuck? N min (usually <3)").
- **Consumed-proposal gate**: Generate flips to a disabled "Generated" once
  any non-failed take exists, because re-approving a proposal would re-spend
  and *replace* all takes. The backend enforces the same rule deterministically
  — `handle_choice` raises `ApprovalRequiredError` when usable candidates
  exist (`agent/agent.py:326`), which the router/WS surface as a crafted 400
  message rather than a generic error.
- **@-references + slash commands** (`wireComposerContext`, canvas-mode.js): typing `@` opens a
  version autocomplete (candidates + source), `/` opens commands (`branch`,
  `voiceover`, `export`). Picking `@x` sets a persistent chip; sends attach it
  as `payload.context_refs` via `getComposerContext()` — **view-gated**: only
  in the canvas view and only if the id still resolves in the current session
  (chat sends stay byte-identical; a session switch clears the chip). The
  backend renders the reference into the model's view of the turn —
  `_context_ref_note` (`agent/loop.py:290`) dedupes/caps refs, resolves
  `__source__` and candidate ids to titles, and drops unresolvable ids — so
  "make the drop harder" targets the referenced take without a "which
  version?" ask. `/voiceover` is state-aware: re-records an existing script
  (spend-confirmed), refuses while recording, or asks the agent to draft one
  via a normal composer turn. Keydown is intercepted in the **capture phase**
  only while the menu has items, so `@`-prefixed text that matches nothing
  still submits normally.
- **Auto-follow** (`maybeFollow`, canvas-mode.js): an opt-in toggle in the zoom controls
  pans the active card into view — only on an actual focus change and only
  when the tree overflows the viewport, so it never fights manual panning.
  `fit()` floors zoom at 0.7 and anchors left rather than shrinking a large
  tree to unreadability.

## Verifying changes in a browser

1. Start the devserver (`design/devserver.py`) for the full-directory static
   mount, or hit the production mount `/api/v2/agentic/audio/app/` (allow-list
   applies — see above).
2. After editing an asset, force-refetch it before reloading:
   `fetch("./js/app.js", {cache: "reload"})` in the console (no cache headers
   are set, so plain reloads can serve stale JS).
3. Offline UI work: open `index.html` standalone (mock transport);
   `?mockfail=1` exercises the failure/retry paths.

> `js/app.js` is currently one module; splitting it into `transport`/`render`/
> `actions`/`app` ES modules is a tracked follow-up (each new file will need a
> `FRONTEND_ASSETS` entry — the drift test will catch misses).

## Test accounts (auth-on devserver)

The `agentic-audio-devserver-auth` launch entry runs the devserver with
`AGENTIC_AUDIO_REQUIRE_AUTH=1` and three ready-made accounts
(`AGENTIC_AUDIO_API_KEYS` maps token → user id):

| Token               | User id       | Use as                         |
| ------------------- | ------------- | ------------------------------ |
| `edenn-test-owner`  | `test_owner`  | the creator/owner              |
| `edenn-test-collab` | `test_collab` | an invited collaborator        |
| `edenn-test-viewer` | `test_viewer` | a view/comment-only guest      |

Sign in by opening `http://localhost:8800/?backend=real&token=edenn-test-owner`
(or paste the token into the sign-in card that appears on any 401 — Settings
also takes it). Tokens ride the URL for the tab only; nothing is stored.

Under auth, share links are **signed grants**: the owner's Share dialog mints
`?grant=` links (`POST /collab/links`), and the recipient — signed in as
themselves — redeems one via the join card (`POST /collab/join`), landing as a
participant at the granted role. The grant carries capability, never identity:
a leaked link can't impersonate anyone, links expire after 7 days, and
`AGENTIC_AUDIO_SHARE_SECRET` (or, failing that, the API key set) signs them.

## Studio library pages

The entrance sidebar now opens `?page=sessions` and `?page=gallery` while keeping
any backend or authentication query parameters. My sessions uses the transport's
session list and resumes a selected session. The mock transport seeds three
sample projects per page load; these reset on refresh. Live mode shows backend
sessions and exposes loading, empty, and retry states.

My sessions uses a compact index with search, sort, selection, and optional
folders. Folder placement and archive/restore are browser-local preferences,
scoped by backend mode; they do not change server permissions or delete sessions.
The list only displays metadata returned by the transport. Selection actions
float above the list and do not move its rows.

Gallery contains six starter directions with energy diagrams, search, style
filters, and browser-local bookmarks. Its original synthesized sound sketches
illustrate timing and energy; they are not generated soundtrack previews. One
player persists through filtering. Its fixed controls cover loading, playing,
paused, finished/replay, and retry states. Changing pages stops playback and
releases the audio URL; stale play promises and list responses are ignored.

Choosing a direction fills the new-session composer without uploading or
generating. The modeless details drawer overlays the page and restores focus on
close. Motion is limited to short opacity, color, and drawer transitions, with
reduced-motion support. A back button in the studio returns to My sessions.
Behavior and presets live in `js/library-ui.js`, with route integration in
`js/app.js`. Styling uses the existing tokens in `styles.css`.

For layout review, open `?backend=mock&page=sessions&preview=library`. This
explicit preview supplies eight active and three archived sample sessions with
illustrative thumbnails, durations, layers and take counts. Session names are
static rather than navigable. Folder and archive preferences use a separate
preview storage key; no live session fetches or mutations occur in this view.
