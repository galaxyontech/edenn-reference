# Campaign console — frontend prototype (no backend)

The advertiser journey built e2e as a REAL frontend in the product design
system. Owner directive 2026-07-30: "build it e2e on the frontend side. No
need to connect the backend code hook up yet. On the frontend, adopt our
existing design system pattern."

Run: launch entry `campaign-console` (port 5601, plain http.server on
`frontend/`). `frontend/ds` is a symlink to the STABLE agentic_audio
frontend, so `ds/styles.css` is the one source of truth for the design
system; `campaign.css` only adds `cp-*` components.

## Architecture

- `js/store.js` — `window.CampaignStore`: the mock world + the stage machine
  (`brief → roles → planned → rendered → live → proposal → round2`, plus an
  ingest question). Screens read `store.get()` / `store.selectors` and mutate
  ONLY via `store.actions.*`. Every action appends thread messages — the chat
  transcript IS the walkthrough. Backend hookup later replaces action bodies;
  the screen contract does not change.
- `js/components.js` — `window.CampaignUI`: design-system builders
  (`agentMsg`, `userMsg`, `questionCard`, `progress`, `topbar`,
  `confirmSpend` (promise), `toast`, `strip`, `statePill`, `el`, `esc`).
  Screens NEVER hand-roll chat/consent markup.
- `js/router.js` — hash routes → `window.CampaignScreens[name].render(mount,
  store, ui)`. Re-renders on every store emit (renders must be pure functions
  of state — no local state that can't be re-derived; transient UI state like
  "drawer open" may live on `screen`-module scope but must survive re-render
  sensibly).

## Screen registration contract

Each `js/screens/<name>.js`:

```js
(function () {
  "use strict";
  window.CampaignScreens = window.CampaignScreens || {};
  window.CampaignScreens.<name> = {
    /** @param {HTMLElement} mount @param {typeof window.CampaignStore} store
        @param {typeof window.CampaignUI} ui */
    render: function (mount, store, ui) { /* build DOM, append to mount */ },
  };
})();
```

Screen scaffold: `ui.topbar(title, segs, activeSeg)` first, then optionally
`ui.progress([...], activeIx)`, then a `div.cp-body` (or `.cp-body--thread`).
Campaign screens share the seg triple:

```js
const SEGS = [
  { id: "thread", label: "Thread", icon: "ti-message-2", route: "#/campaign" },
  { id: "canvas", label: "Canvas", icon: "ti-layout-grid", route: "#/campaign/canvas" },
  { id: "console", label: "Console", icon: "ti-chart-dots", route: "#/campaign/console" },
];
```

Stage → progress strip on campaign screens:
`["Brief","Roles","Create","Launch","Live"]` with activeIx from
`{brief:0, roles:1, planned:2, rendered:2, live:4, proposal:4, round2:2}`
(launch screen shows 3 while stage === "rendered").

## Hard rules (all screens)

- Design system first: use ds classes (`agent-row/ub/clarify/qchips/glass/
  pill/btn-primary/btn-ghost/cv-seg/topbar/progress/confirm-card/toast`) and
  `cp-*` from campaign.css. Do NOT edit campaign.css or ds files; if a style
  is genuinely missing, minimal inline `style=` is acceptable in this
  prototype.
- Mutations only via `store.actions`; spend gates via `ui.confirmSpend(...)
  .then(ok => ok && store.actions.X())`.
- Escape ALL fixture text through builders or `ui.esc` (XSS discipline even
  in a prototype — house lesson).
- No third-party AI provider/company/model names anywhere.
- Vanilla JS IIFE, `"use strict"`, no external requests beyond what
  index.html already loads.

## Per-screen specs

### home (`#/home`)
Topbar "Campaigns" (no segs). Body `.cp-narrow`: a `.cp-card` table
(`.cp-table`) of campaigns — "Launch week — spring line" row whose stage cell
reflects `world.stage` live (`brief…→ LIVE · day 4`), click → `#/campaign`
(or `#/campaign/console` when live/proposal); a second static row "Holiday
teaser (draft)". Below: "Needs you" card — visible only when
`stage === "proposal"`: the proposal one-liner + "Review →" →
`#/campaign/console`. Right-aligned in topbar area or body top: buttons
"Add assets" → `#/library` (ghost) and "New campaign" → `#/campaign`
(primary). A `.cp-card` brand-health strip: library count (live from
`world.assets.length` + ingest pending marker), budget guardrail, channels.

### library (`#/library`)
Topbar "Library". Body: search bar (`.cp-searchbar`, input filters the board
by asset/moment label match — live `input` filtering, case-insensitive; a
`.cp-tiny` hint "search by what's IN it — try “pour”"). Collections as a
`.pills` row of `.cp-pill`s with counts (live from store; clicking filters
to that collection, click again clears). Board `.cp-board`:
- Family rows (`.cp-card` + `.cp-family`): source thumb (`.cp-thumb`,
  ti-player-play + name/meta) → children `.cp-child` boxes with their notes
  ("published · 2.6% CTR" etc.) from `world.families`; `used` as a
  `.cp-pill`.
- Moment assets: for `event_reel`, children = its `moments` (label + at,
  hero ★). Clicking a moment or child: `ui.toast("In product: opens the
  detail view")`.
- Archive collection rows carry their `world.archiveNumbers` metric as a
  `.cp-pill is-blue`.
INGEST CARD (top of body, only while `world.ingest.pending`): a `.glass`-free
`.cp-card` conversation fragment built with `ui.agentMsg` + `ui.questionCard`
wired to `store.actions.ingestAnswer(choice)`; plus the dedupe `.cp-tiny`
line. After answering: card is replaced (next render) by a `.cp-card` with
"✓ Filed — the Library is ready" + button "Start the campaign →" →
`#/campaign` (btn-primary).

### thread (`#/campaign`)
Topbar campaign name + SEGS(active "thread") + progress. Body
`.cp-body--thread` holding a `div.thread > div.thread__inner` that renders
`world.campaign.thread` in order by `msg.kind`:
- `agent` → `ui.agentMsg`, `user` → `ui.userMsg`
- `question` → `ui.questionCard` wired to
  `store.actions.answerQuestion(msg.id, choice)`
- `roles` → a `.cp-card` two-col list of `world.campaign.roles`
- `plans` → the three variant cards (see below) — RENDER FROM
  `world.campaign.variants` filtered to round-1 ids (A/B/C)
- `banner` → a `.cp-card` with `ti-refresh` icon + text (round-2 seed)
When `stage === "round2"`, after the banner also render the round-2 variant
cards (A2/B2/C2) the same way.
Variant card (`.cp-card`): `.cp-row` head (label bold, grounding `.cp-pill`,
spacer, `ui.statePill(v.state)`, "▶ watch" ghost button → `ui.toast("Free
preview — nothing renders until Gate ①")`); `ui.strip(v.slots, biteIx)`
(bite slot: index 1 for B — the founder bite — else none); relative line
`.cp-sub` ("≈ relative: NAME — note · METRIC" or "no published relative —
the NEW bet").
Below plans (while `stage === "planned"`): a `.cp-row` with btn-primary
"Lock & render all 3 — Gate ①" → `ui.confirmSpend("Render 3 × 15s?",
"Render cost applies now. No channel budget is spent until Launch.",
"Render — Gate ①")` → `store.actions.confirmRender()`.
When `stage === "rendered"`: agent line already appended by store; show
btn-primary "Go to Launch →" → `#/campaign/launch`.
Composer at the bottom (`.session-composer` pattern): input prefilled with
the brief while `stage === "brief"`, send (`.send-btn`) →
`store.actions.sendBrief()`; after brief it becomes a disabled "Adjust
anything…" placeholder (prototype).

### canvas (`#/campaign/canvas`)
Topbar + SEGS(active "canvas") + progress. Body: `.cp-cstage` with
`.cp-legend` ("— locked path — branches · campaign projection"), an
absolute-positioned tree from `store.selectors.tree()`: `.cp-node`s laid out
by columns (source col 0; round-1 col 1 stacked; overlay/ghost near their
parents; round-2 col 2), `.cp-edges` svg lines between node anchor points
(compute positions from a fixed layout map, then draw lines between box
edges). Node classes: `is-ghost` for kind ghost/overlay, `is-dim` for killed,
`is-focus` for the focused node (module-scope focusId, default "A"),
`is-locked` when `useTake` chose it (module-scope). Halos (`v.halo`) render
as `.cp-node__halo` (add `is-bad` for killed). Click node → focus + dock
update. DOCK at the bottom: reuse ds `cv-dock` classes verbatim
(`cv-dock/cv-dock__thumb/__play/__main/__hd/__title/__time/__path/__scrub/
__fill/__btns/cv-dbtn`): title/path from the focused node ("launch_footage ›
hook-first › take 1"), buttons "✓ Use this" (primary → locked state +
`ui.toast`), "⑂ Branch" → toast about /choices contract, "↓ Download" →
toast. While `stage === "planned"` a dock-adjacent btn-primary "Lock &
render all 3 — Gate ①" mirrors the thread gate (same confirmSpend +
action). Ghost proposal node (stage proposal) click → `#/campaign/console`.

### launch (`#/campaign/launch`)
Topbar + SEGS(active "thread" — launch is a thread stage, keep segs) +
progress(activeIx 3). Body `.cp-narrow`: agent intro (`ui.agentMsg`, market
agent voice); `.cp-card` split table (`world.campaign.launch.split` →
`.cp-table` Variant/Budget/Why); `.cp-card` pacing line `.cp-sub`;
`.cp-card` autonomy ("What I'll do on my own vs ask" + the autonomy line +
`.cp-pill` "edit rules" → toast); rendered-variants row (three `.cp-thumb`s
"▶ A · 15s" etc.). Footer `.cp-row`: btn-ghost "Adjust plan" → toast;
btn-primary "Launch →" → `ui.confirmSpend("Launch this campaign?",
"3 variants go live on TikTok. $1,500 over 14 days from your connected ad
account. The market agent operates within the rules you just saw.",
"Launch — spends budget")` → `store.actions.confirmLaunch()` + navigate
`#/campaign/console`. Guard: if `stage` is before "rendered", body shows a
`.cp-empty` "Render the variants first — Gate ① lives in the thread" +
button to `#/campaign`.

### console (`#/campaign/console`)
Topbar + SEGS(active "console") + progress(activeIx 4). Guard: before
"live", `.cp-empty` "The console lights up at launch." Body otherwise:
KPI `.cp-card` (`.cp-table` from `world.campaign.outcomes`; killed row
`is-dim`; clicking a row highlights (`is-hl`) it + matching feed items);
feed `.cp-card` (`.cp-feed` from `world.campaign.feed`; the proposal item
`is-proposal`, click → drawer); spend/pacing `.cp-sub` line. PROPOSAL DRAWER
(`.cp-drawer`, module-scope open flag; render when open AND stage is
"proposal"/"live"): evidence list (`.cp-sub` rows), the three actions as
`.cp-action`s with checkboxes bound to `proposal.actions[i].on`, footer
btn-primary "Approve selected — Gate ③" → `ui.confirmSpend("Approve the
proposal?", "iterate compiles a CreationRequest: LOCK A's opening · LOCK B's
voice · EXCLUDE C's close · vary only the close. Render cost applies for
round 2.", "Approve — Gate ③")` → `store.actions.approveProposal()` + close
drawer + navigate `#/campaign` (the thread shows the round-2 seed banner +
new plans). When `stage === "round2"`: a `.cp-card` note "Round 2 planned —
outcomes wrote this brief" + button "Open the thread →".

## Hookup contract (front-to-back, 2026-07-30 — owner: "hook up everything")

Backend: `backend/` (models.py typed domain · service.py CampaignService over
the existing engines · router.py FastAPI). Devserver serves frontend statics
AND `/api` on ONE origin (port 5601 stays), seeding media per the design
devserver pattern. **No paid providers**: render = the local compose path,
publish = the sandbox channel.

Endpoints mirror `store.actions` 1:1 — the store keeps its shape and screens
do not change; only action bodies become fetches:

```
GET  /api/state                 → full snapshot (world shape the store already exposes)
POST /api/ingest/answer         {choice}
POST /api/campaign/brief        {}            (sends the prefilled brief)
POST /api/campaign/answer       {qid, choice}
POST /api/campaign/plan         {}            (free; refuses if questions open)
POST /api/campaign/render       {}            (Gate ① — refuses un-previewed plans)
POST /api/campaign/launch       {}            (Gate ② — sandbox publish)
POST /api/campaign/approve      {actions:[{id,approved}]}  (Gate ③ — compiles round 2)
GET  /api/campaign/tree         → campaign lineage projection for the canvas
```

Store hookup rules: every action = POST → on 2xx, GET /api/state → replace
world → emit. 409 responses carry `{questions}` or `{error}` — surfaced via
`ui.toast`, never swallowed. The render action returns immediately;
`/api/state` polling (2.5s, the product's cadence) picks up state flips
while any variant is `rendering` or the stage is `live` (proposal arrival).

**Canvas caution (owner):** the campaign canvas is NOT the audio session
canvas. It renders `/api/campaign/tree` — variants-as-branch-heads with
DIFFERENT creation treatments per branch, rounds as generations, halos from
outcomes — its own cp-* projection. The audio canvas module is not imported.

## Definition of done

Full loop clicks e2e with no console errors: library ingest answer → thread
brief → 2 questions → plan → watch → Gate ① render (staggered pills) →
launch Gate ② → console live → proposal drawer → Gate ③ approve → thread
round-2 banner + A2/B2/C2 → canvas shows the grown tree (round-2 nodes off
A) with halos. Design-system fidelity: chat is real agent-row/ub, consents
are the real confirm-card, canvas dock is real cv-dock markup.
