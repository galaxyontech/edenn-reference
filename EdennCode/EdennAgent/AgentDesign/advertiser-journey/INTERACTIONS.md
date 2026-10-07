# Interactions spec — advertiser-journey wireframes (round 3: clickable walkthrough)

Owner ask: "make the wireframes a bit more complete with actual ux walkthrough of
what happens or effects of each button clicked or page directs."

Two layers deliver that:
1. **Annotation layer** (runtime `_wire.js`, blue): hover any element → tooltip
   with its effect; ⚡ toggle → numbered badges + a legend listing EVERY click on
   the page and where it leads. Blue = annotation, gray = product. Never style
   product chrome blue.
2. **Working effects**: the story-critical interactions actually happen in-page
   (chips resolve, modals open, renders simulate, pages navigate) so the owner
   can click through the whole journey end-to-end.

## Wiring conventions (all screens)

- Include ONCE, immediately before `</body>`:
  ```html
  <script>
    window.WIRE_PAGE = { title: "S3 · Brief" };
    window.WIRE_ACTS = { /* page acts */ };
  </script>
  <script src="_wire.js"></script>
  ```
  (Order matters: page script first, then the runtime.)
- `data-fx="…"` on **every** element that looks interactive (.btn, .chip, .nav,
  .tabs span, ▶ tiles, table rows that react, composer). One plain sentence, the
  effect from the USER's point of view. No dead-looking pixels without fx.
- `data-go="target.html?params"` for navigation. `data-act="key"` for in-page
  effects (key must exist in `WIRE_ACTS`). Elements with only `data-fx` toast
  "In product: …" — acceptable for secondary affordances, NOT for the story
  chain.
- `data-story` on the ONE element that advances the demo narrative right now.
  When an effect creates the next story click, call `WIRE.story("#nextEl")`.
- Overlays/modals: keep the existing `.overlay`/`.modal` classes but add inline
  `style="display:none"` and open via `WIRE.open("#id")`. No modal may be
  visible on load.
- Helpers: `WIRE.toast(msg)`, `WIRE.open(sel)`, `WIRE.close(sel)`,
  `WIRE.story(sel)`, `WIRE.param(name)`, `WIRE.go(href)`.
- New in-page states reuse the existing classes (.card .chip .kv .dash .row…).
  Grayscale product, blue only via the runtime or `.notes`.
- The first line of each file (`<!-- @dsCard … -->`) must stay **byte-identical**
  (the design-pane card index depends on it). Update the `.notes` line to
  mention: "interactive — ⚡ lists every click's effect".
- Vanilla JS only, no external requests, no third-party names of any kind.
- Agent voice in injected copy: confident, concrete, first person ("Filed.
  Everything's sorted."), consistent with existing copy.

## Cross-page params

- `s3-brief.html?ctx=spring-shoot` → show attached-context chip "@Spring shoot ·
  8 assets" in the thread head.
- `s3-brief.html?ctx=founder-speaking` → chip "@'founder speaking' moment".
- `s4-creation.html?seed=round2` → round-2 state (see S4).
- `s6-console.html?just=launched` → toast "Live. The market agent monitors from
  here — you'll only hear from it when something matters."

## Story chain (the golden path — every link must work)

S1 "Add assets" → S2b answer question → "Open the Library →" → S2 "Create from
this collection →" → S3 send brief → answer Q1 → answer Q2 → "Plan 3 variants →"
→ S4 "Lock & render all 3" → Gate ① modal confirm → renders complete → "…then
Launch →" → S5 "Launch →" → Gate ② modal "Launch — spends budget" → S6 proposal
card → drawer "Approve selected →" → compile modal "Start round 2 →" →
S4?seed=round2 → end card "the loop, closed".

---

## W0 · w0-loop-map.html (light touch)

- Each station box gets `data-go` to its product screen + fx:
  INTAKE → `s2b-ingest-organize.html` ("Jump to the ingest conversation");
  UNDERSTAND → `s2-library.html`; CREATE → `s3-brief.html`;
  PUBLISH → `s5-launch.html`; LEARN → `s6-console.html`.
  The Proposal arrow/label → `s6-console.html` ("Where the loop closes — the
  proposal drawer").
- `WIRE_PAGE.title = "W0 · Flow map"` (not in SEQ → bar shows "Start the
  walkthrough ▶", which is the point of this card).

## S1 · s1-home.html

- Sidebar: "▤ Library" `data-go s2-library.html` fx "Opens the brand library —
  collections, meaning-search, families". "▦ Campaigns" fx "You're here".
  Create-section entries → fx or data-go s3.
- Live campaign card → `data-go s6-console.html` fx "A live campaign opens at
  its Console". Draft/other cards → `data-go s3-brief.html` fx "Drafts resume at
  the brief".
- "＋ New campaign" (add if missing) → `data-go s3-brief.html`.
- **Story**: an "Add assets" affordance (add to top bar if missing) →
  `data-go s2b-ingest-organize.html` `data-story` fx "Drop files — the agent
  organizes them as it asks (the walkthrough starts here)".
- Any activity/feed rows → data-go s6 or fx.

## S2b · s2b-ingest-organize.html

- Question chips (the IMG_2214 card):
  - "Spring shoot" `data-act fileSpring` **initial story**: replace the chip row
    with "✓ IMG_2214 filed to Spring shoot — 9 clips"; update the shoot card
    header 8→9 and rail card 8→9; append agent line "Filed. Everything's sorted
    — the Library is ready."; reveal a new chip `#toLibrary` "Open the Library →"
    `data-go s2-library.html` and `WIRE.story("#toLibrary")`.
  - "new collection…" `data-act fileNew`: inline text input; Enter → new rail
    card with the typed name · 1 asset; then same resolve + continue flow.
  - "just b-roll, unsorted" `data-act fileUnsorted`: rail gains "unsorted · 1";
    same resolve + continue.
- "rename" `data-act renameShoot`: inline input over the collection name; Enter
  renames every visible occurrence (card header, rail).
- "split" fx only: "Opens a split view to divide the 8 clips into two
  collections".
- "these are raw" `data-act rawNotAds`: finished-ads card flips to "OK — raw
  footage, filed to Spring shoot (+3)"; rail counts update; agent line "Noted —
  treating those as source footage, not comparison ads."
- "analyzing 11/14…" badge fx "Analysis runs in the background — moments appear
  as they're found".
- Clip tiles fx "Opens the clip with its detected moments".
- Composer ↑ `data-act looksRight` fx "Accepts the proposed organization as-is":
  resolves the open question to "unsorted", then the same continue flow.

## S2 · s2-library.html

Board has swappable states — build them as sibling containers toggled by acts:
`#stateFamilies` (current content), `#stateResults`, `#stateArchive`,
`#stateWinter`, `#stateFlat`.

- Search box `data-act searchOpen` fx "Search by what's IN the footage — moments
  are first-class": opens a suggestion dropdown ("pour shot", "founder
  speaking", "which footage is still unused?"). Each suggestion
  `data-act searchRun` → `#stateResults`: header chip "12 results for 'founder
  speaking' · 3 sources" + "× clear" `data-act searchClear`; 3 moment-hit cards,
  each with thumb dash, source + timecode, chips "★ hero" (`data-act starMoment`
  toggles ★ + toast "Hero-starred — the planner will prefer this moment") and
  "@ use in brief" `data-go s3-brief.html?ctx=founder-speaking`.
- Sidebar collections:
  - "Winter campaign" `data-act colWinter` → `#stateWinter` (1 collapsed family
    row + "…18 more assets" placeholder + rail update) fx "Collections cap the
    visible set — same three axes, different shelf".
  - "Published archive" `data-act colArchive` → `#stateArchive`: 3 finished-ad
    rows with real metric chips (e.g. "April teaser · 2.6% CTR · fatigued day
    9") + note "comparison material — these power the '≈ relative' rows in
    creation" fx.
  - "Spring shoot" `data-act colSpring` → back to `#stateFamilies`.
  - Music / Brand kit / unsorted → fx toasts.
- Filter chips video/stills/audio fx toasts. "flat" `data-act viewFlat` →
  `#stateFlat` (plain tile grid); "families" `data-act viewFamilies` → back.
- "▶ source" tiles `data-act openClip` → shared lightbox modal `#clipModal`
  (big dash player + strip + "close" `data-act closeClip`).
- Child tiles: "hook-first cut (published · 2.6%)" `data-go s6-console.html` fx
  "Published cuts link straight to their campaign numbers"; others fx.
- "31% used" fx "Coverage — how much of this source any cut has ever used";
  "best child: 2.6% CTR" `data-go s6-console.html`.
- Rail: example-question rows — first (`"which footage is still unused?"`)
  `data-act searchRun`; others fx. **Story**: "Create from this collection →"
  `data-go s3-brief.html?ctx=spring-shoot` `data-story` fx "Starts a campaign
  brief grounded on @Spring shoot".

## S3 · s3-brief.html

Two thread states: pre-send (`#preSend`: context chip if `ctx` param + composer
prefilled with the brief) and post-send (`#postSend`, hidden initially).

- If `WIRE.param("ctx")==="spring-shoot"` show chip "@Spring shoot · 8 assets
  attached"; `"founder-speaking"` → "@'founder speaking' moment attached".
- Composer prefilled: "Three 15s TikTok cuts from @Spring shoot — one
  hook-first, one with the founder's own voice, one rephrased in our voice."
  Send ↑ `data-act sendBrief` **story** fx "Sends the brief — the agent resolves
  roles and asks only what's genuinely ambiguous": appends the user bubble,
  reveals `#postSend` with a partially-resolved roles table + two question
  cards.
- Q1 "Which reel is the spine?" chips "launch_footage leads" / "event_reel
  leads" `data-act q1` — resolves to "✓ spine: launch_footage" (either chip
  resolves; update the roles table row). **story moves to Q1 → then Q2.**
- Q2 "spring_track — use as-is, or a variant in its style?" chips
  `data-act q2` → "✓ music: variant in its style".
- Both answered → agent line "Roles locked. Planning is free — nothing renders
  until you approve." + button `#toPlan` "Plan 3 variants →"
  `data-go s4-creation.html` → `WIRE.story("#toPlan")` fx "Generates three
  previewed plans — free, nothing rendered yet".
- Roles table rows fx (spine/accent/music/voice one-liners). Question cards get
  a small "why ask?" fx "Ask-first: ambiguity is never silently defaulted".

## S4 · s4-creation.html  (largest — two URL states)

**Round 1 (default):**
- "▶ watch" per variant `data-act watchVariant` → `#previewModal` (variant
  title, big dash "▶ preview — assembled from your moments, free", slot strip,
  close) fx "Free preview — watch before anything renders".
- "view side-by-side" `data-act sideBySide` → `#compareModal`: A's strip over
  April teaser's strip + its real numbers + note "same pour-shot opening".
- Grounding chips `data-act grounding` → `#groundModal` listing the actual
  moments ("pour ★ · terrace · hands · bottle · close — 6 from @Spring shoot ·
  0 generated") fx "See exactly what this cut is built from".
- "≈ relative" rows fx ("history as a signal: the closest published cut and its
  real numbers"; C's: "no relative — C is the round's deliberate experiment").
- Quick-adjust chips above the composer: "open on @pour_closeup"
  `data-act adjOpen` (slot 1 of A's strip restyles + agent line "A now opens on
  the pour close-up — preview updated, still free."); "swap C's music"
  `data-act adjMusic` (C card note updates) fx "Adjustments re-plan instantly;
  previews stay free".
- Rail: split card fx "Drafted from the relatives' history — you can override
  it"; channel/flight fx.
- "Lock & render all 3" `data-act lockRender` **story** fx "Gate ① — approves
  the small render cost; channel budget is NOT touched" → `#gateModal`:
  "Render 3 × 15s? Render cost applies now. No channel budget is spent until
  Launch." [Cancel `data-act gateCancel`] [Render — Gate ① `data-act doRender`].
- `doRender`: close modal; each variant card shows chip "rendering…" flipping to
  "✓ rendered" staggered (600/1200/1800ms); then agent line "Rendered. The
  market agent has a launch plan ready." and "…then Launch →" gains
  `data-go s5-launch.html` + `WIRE.story` (pre-render it is dimmed with fx
  "Enabled after Gate ① — render before launch").

**Round 2 (`?seed=round2`):** hide round-1 thread, show `#round2`:
- Banner card: "Round 2 — seeded by campaign outcomes: A's opening LOCKED ·
  B's voice LOCKED · C's close EXCLUDED · vary only the close."
- Three cards: "A2 · champion remix (A's opening + B's voice)" grounded chip
  "locks from round 1"; "B2 · new close — product hero"; "C2 · new close —
  street pour"; relative rows cite round-1 LIVE numbers ("parent ran 2.9% CTR
  in round 1").
- End card (**story**): "⟲ The loop, closed — outcomes wrote this brief." with
  "Back to the map" `data-go w0-loop-map.html` and "Restart" `data-go
  s1-home.html`.

## S5 · s5-launch.html

- The existing always-on `.overlay` becomes `#launchModal` hidden by default.
- "Launch →" `data-act openLaunch` **story** fx "Gate ② — opens the real-budget
  consent" → `WIRE.open("#launchModal")`.
- Modal: "Launch — spends budget" `data-go s6-console.html?just=launched`;
  "Cancel" `data-act closeLaunch`.
- "edit rules" `data-act editRules` → rules card swaps to editable form:
  checkboxes (pull outcomes daily ✓, alert on criteria ✓), a `<select>`
  "reallocate cap: 5% / 15% ✓ / 25% (logged)", asks-first toggles; "Save
  boundary" `data-act saveRules` → summary re-renders with chosen values +
  toast "The agent's autonomy is a setting, not a personality."
- "Adjust plan" `data-act adjustPlan` → reveals chips "even split"
  (`data-act evenSplit` — table becomes 34/33/33 + agent line "Evened. We learn
  slower but risk less.") and "protect the experiment — min $500 on C"
  (`data-act protectC`).
- Why cells fx "Each split cites preview evidence — the agent must justify its
  draft"; pacing line fx "Criteria become standing orders — the agent acts on
  them without another meeting"; rendered tiles fx "Rewatch the rendered cut".

## S6 · s6-console.html

- If `WIRE.param("just")==="launched"` → toast (see params).
- Tab "Thread" fx "The console is just another view of the same campaign
  thread". Variant table rows `data-act selVariant` → highlight that variant's
  attribution rows + feed items (add a `.hl` background class; click again
  clears) fx "Filters attribution and the feed to this variant".
- Attribution rows fx ("measured across variants, not vibes").
- Feed items `data-act evidence` → scroll+flash the data row that justifies the
  observation, toast "Every observation links to the data behind it". The
  reallocation entry `data-act reallocInfo` → toast "Moved 10% C→A on day 3 —
  inside the ≤15% boundary you approved. Logged, not asked."
- Proposal-waiting card `data-act openProposal` **story** fx "The agent's
  thesis — evidence, typed actions, each separately approvable" → `#propDrawer`
  (right-side drawer over the rail): evidence rows; typed actions with
  checkboxes — "iterate — lock A's opening + B's voice, vary the close (render
  cost)" ✓, "reallocate C's remaining $180" ✓, "retire C" ✓; footer "Approve
  selected →" `data-act approveProposal`.
- `approveProposal` → `#compileModal`: "Approved. `iterate` compiles a
  CreationRequest: LOCK A's opening · LOCK B's voice · EXCLUDE C's close ·
  style ref: A's plan · treatment: rephrase kept." + "Start round 2 →"
  `data-go s4-creation.html?seed=round2` (**story**).

---

# Round 4 — density pass + capability bridge (owner feedback)

Owner: "overall good design, but feel like a bit crowded in general with amount
of information feeding to the customer" + "not sure how this design connect
with our existing agentic generation capabilities."

## 4A · Density rules (progressive disclosure)

The disclosure ladder — every piece of info must earn its default slot:
- **Layer 1 (always visible)**: what drives the NEXT decision only — title,
  the visual (strip/thumb), ONE signal chip, the primary action.
- **Layer 2 (one click away)**: history, reasons, grounding detail — behind a
  `data-more` expander chip (runtime toggles the target block and flips ▸/▾;
  give the detail block an id and inline `style="display:none"`).
- **Layer 3 (hover)**: explanations that were sentences on the screen become
  `data-fx` tooltip copy. Deleting explainer text is fine when the tooltip
  already says it.

Per-screen required reductions (keep every existing interaction working; the
story chain must not change):
- **S4 (both rounds)**: each variant card keeps title + ONE grounding chip
  (shorten to e.g. "@Spring shoot · 6") + strip + "▶ watch". The "≈ relative"
  row collapses to a compact chip — "≈ April teaser · 2.6% ▸" — expanding via
  data-more to the current full row. Quick-adjust chips: collapse to one
  "adjust ▸" chip that expands the pair. Rail: merge channel/flight and split
  into ONE card; delete the "Gate ① render → Gate ② launch…" explainer card
  (move its sentence to the two buttons' data-fx).
- **S2**: family rows keep ONE performance chip ("best child: 2.6% CTR");
  "31% used" and the "family = source + everything…" explainer line move to
  data-fx / a data-more block. Rail: the three example questions become one
  card with a "try one ▸" expander is NOT needed — keep them, they're short —
  but drop any explanatory sentences that repeat the .notes line.
- **S2b**: the finished-ads card's two-line explanation ("These become
  COMPARISON material…") collapses to data-more behind "why keep these? ▸";
  keep the chips.
- **S6**: agent feed shows the 3 most recent items + "earlier ▸" (data-more);
  attribution card keeps 2 rows + "more ▸".
- **S5**: the Why column entries shorten to ≤4 words (full reason → data-fx);
  the rules-of-engagement card shows ONE summary line + "edit rules".
- **S1 / S3**: already light — S1: merge the brand-health rail's agent-note
  into the library card (one card fewer). S3: no change required.
- Never delete an interaction; relocate its trigger. Hotspot (data-fx)
  coverage must remain complete, including on expander chips ("Expands the
  full history for this variant").

## 4B · Capability tags (data-cap) — the authoritative inventory

Tag the ~4–8 major REGIONS per screen (cards/panels, not every chip) with
`data-cap="status:Label — module"`. Status: `built` (runs today), `partial`
(data/engine exists, this surface is new), `new` (new shell object). Labels
below are canonical — copy them verbatim. Module paths are relative to
`EdennCode/EdennAgent/`.

- **S1**: campaigns table → `new:Campaign object — thin wrapper over built
  sessions`; brand-health library card → `partial:asset stats from aul
  repository — surface is new (aul/repository.py)`; "needs you" proposal row
  → `new:Proposal object — loop-closing piece`.
- **S2b**: analysis badge + clip grouping → `built:ingest + understanding
  producers (aul/ingest.py)`; the question card → `partial:ask-first pattern
  from the creation resolver (creation/bundle.py) — collection filing is new`;
  dedupe note → `partial:idempotent ingest exists — byte-dedupe is new`;
  rail landing shape → `new:collections — agent-proposed organization`.
- **S2**: family rows → `partial:lineage + outcomes exist (aul/lineage.py,
  ads/campaign.py) — the family board view is new`; search bar →
  `partial:understanding annotations exist (aul/ingest.py producers) —
  meaning-search UI is new`; collections sidebar → `new:collections model`;
  "Create from this collection" → `built:@-reference grammar into a brief
  (creation/domain.py RequestBundle)`.
- **S3**: the thread itself → `built:the stable chat surface (agentic_audio
  frontend)`; question cards → `built:BundleResolver ask-first — /plan 409s
  with questions (creation/bundle.py, creation/router.py)`; roles table →
  `built:six source roles (creation/domain.py SourceRole)`; campaign rail →
  `new:Campaign brief wrapper`.
- **S4**: variant cards + strips → `built:plan → preview slots + beats
  (creation/service.py, creation/preview.py)`; watch modal → `built:free
  preview before any render — show-before-spend`; Gate ① modal →
  `built:render refuses un-previewed plans (creation/service.py
  render_short)`; treatment/adjust chips → `built:locks + treatment knobs
  (creation/treatments.py incl. rephrase-original)`; grounding chip →
  `partial:bundle sources are typed — the grounding chip surface is new`;
  "≈ relative" chips → `new:published-relative matching vs the archive`;
  publish rail → `partial:publisher exists (ads/campaign.py) — create⇄publish
  adjacency is new`.
- **S5**: launch table + pacing → `new:LaunchPlan — the market agent's first
  artifact`; rules of engagement → `new:autonomy boundary`; Gate ② modal →
  `built:same spend-consent pattern as generation (sandbox channel live —
  ads/sandbox.py, TikTok env-gated ads/tiktok.py)`; rendered-variant rail →
  `built:rendered takes from the creation loop`.
- **S6**: variant KPI table → `built:outcome series + dedup (ads/campaign.py
  CampaignPublisher, AttributionEngine)`; component attribution →
  `partial:attribution engine exists — component-level readout is new`;
  agent feed → `new:agent feed — narration over outcomes`; proposal drawer →
  `new:Proposal object`; compile modal → `new:compiler — but it compiles into
  the BUILT CreationRequest (locks/excludes/style refs, creation/domain.py)`.
- **W0**: tag each station box with the matching one-liner from above.

## 4C · W8 · Capability bridge card (new file `w8-capability-map.html`)

First line exactly:
`<!-- @dsCard group="A · Flow map (high-level)" name="W8 · Capability bridge" subtitle="Which existing module powers each screen — built vs partial vs new" -->`

A single-frame map (same chrome family as W0, no app sidebar): three columns —
**Journey screen** (S1…S6, data-go to each) | **Capability** (Understand ·
Reference · Plan · Show · Adjust · Transform · Generate · Persist · Publish &
learn, from AGENTIC_CREATION.md) | **What runs it today** (module path +
status chip BUILT/PARTIAL/NEW, statically visible — this card IS the legend).
Rows per the 4B inventory. Bottom note: "everything NEW is a thin shell around
the unchanged creation backbone: Campaign · LaunchPlan · agent feed · Proposal
· iterate→CreationRequest compiler." Include the runtime script pair like
every other page; WIRE_PAGE.title = "W8 · Capability bridge"; tag rows with
data-cap so ⚙ works here too; data-fx on the screen links.

---

# Round 5 v2 — CORRECTED: grounded in the REAL canvas (owner: "i mean the canvas view in the original agentic audio part")

The v1 canvas cards invented a "lanes workbench". The REAL canvas
(`EdennCode/EdennAgent/AgenticAudio/frontend/js/canvas-mode.js`, verified
live on the mock console) is:
- a **pannable, zoomable LINEAGE TREE**: takes as ~210px node cards linked by
  parent, rooted at the source video; the locked path (selected-candidate
  chain) highlighted; legend "— locked path — branches · drag to pan · scroll
  to zoom"; +/− and fit/center controls;
- a **DOCK player** at the bottom for the focused node: thumb, play, title,
  time, the take's LINEAGE PATH (e.g. "reel_v3 › Arc-driven electronic ›
  take 1"), scrubber, and buttons **Use this** (primary) / **Branch** /
  Download;
- the chat panel stays alongside — canvas is a second VIEW of the same
  snapshot, and every canvas action routes through the same `/choices`
  contract as the chat cards.

The campaign mapping that follows: **variants are branch heads, rounds are
generations, outcomes/proposals are overlays painted on nodes.** The canvas
mechanics need nothing new.

Rewrite BOTH cards (same filenames, same tab wiring). Node cards in the tree:
~210px wide, dashed .dash-style boxes with a name row, a small meta row, and
(for the wireframe) a "▶ focus" affordance; edges drawn as simple lines
(border/absolutely-positioned divs or a thin svg — keep it wireframe-gray).
Locked-path edges/nodes get a heavier dark border + a small "locked path"
tag; ordinary branches stay light. Include the legend line and the +/−/fit
zoom cluster (fx-only) so the anatomy matches the real thing.

## 5A(v2) · S4c — `s4c-canvas.html` (create stage: the campaign in the tree)

First line exactly:
`<!-- @dsCard group="B · Product screens (user view)" name="S4c · Canvas — the campaign in the lineage tree" subtitle="The real canvas (tree + dock): variants are branch heads, the relative is an overlay" -->`

Chrome: keep the campaign top bar + tabs exactly as v1 ([Thread data-go
s4-creation.html] [Canvas on] [Console fx "Goes live after launch"]).

Body (top→bottom): legend line + zoom cluster (fx) · the TREE · a slim
campaign strip · the DOCK.

- **Tree**: root node "▶ launch_footage · source · 3:30" → three
  first-generation branch heads: "A · hook-first — take 1" (FOCUSED: heavier
  border, drives the dock; edge source→A carries the "locked path" tag),
  "B · founder voice — take 1", "C · rephrased — take 1". Each node:
  name row + meta ("15s · from 6 moments") + "▶ focus" `data-act focusNode`
  (retargets the dock title/path to that node; focused node border moves).
  Beside A, a DASHED ghost node "≈ April teaser · published · 2.6% CTR" with
  tag "campaign overlay" — fx explains it is context, not session lineage.
- **Campaign strip** (one line): "3 branch heads planned · previews free" +
  button "Lock & render all 3 — Gate ①" `data-act lockRender` → `#gateModal`
  (v1 modal copy verbatim; Cancel `gateCancel` / "Render — Gate ①"
  `doRender`); doRender staggers "✓ rendered" chips onto the three branch
  nodes and reveals `#toLaunch` "Open launch in thread →" `data-go
  s5-launch.html`.
- **Dock**: thumb dash + ▶ play (fx) + title "A · hook-first — take 1" +
  time "0:00 / 0:15" + lineage path "launch_footage › hook-first › take 1"
  (`#dockPath`, updated by focusNode) + scrubber dash + buttons:
  "✓ Use this" `data-act useTake` (moves the locked-path tag to the focused
  branch + toast "Locked — the campaign will render this take for A."),
  "⑂ Branch" fx "Creates a child node from this take — same /choices
  contract as chat, still free until Gate ①", "↓ Download" fx.

data-cap (verbatim): tree region → `built:canvas mode — pannable lineage
tree of takes (agentic_audio frontend/js/canvas-mode.js)`; dock →
`built:dock player — focused take, lineage path, Use this / Branch
(canvas-mode.js buildDock)`; campaign strip → `built:render refuses
un-previewed plans (creation/service.py render_short)`; branch-head labels →
`new:variant branch heads labeled by campaign role`; ghost relative node →
`new:published-relative overlay on the tree`.

WIRE_PAGE.title "S4c · Canvas — the campaign in the lineage tree". Acts:
focusNode, useTake, lockRender, gateCancel, doRender (5).

## 5B(v2) · S6c — `s6c-canvas-performance.html` (live stage: generations)

First line exactly:
`<!-- @dsCard group="B · Product screens (user view)" name="S6c · Canvas — generations + performance" subtitle="Rounds are generations: outcomes as node halos, the proposal as a dashed next generation" -->`

Chrome: v1 tabs kept ([Thread fx] [Canvas on] [Console data-go
s6-console.html]); top bar keeps the LIVE · day 4 badge.

Body: legend + zoom cluster · the TREE (now two generation bands) ·
campaign strip · DOCK.

- **Generation band "Round 1 — live"**: the same root + A/B/C branch heads,
  now with performance ON the nodes: A "2.9% CTR · pacing 40%" (chip
  `data-act nodeEvidence`), B "CVR +18% · voice-led" (chip), C at reduced
  opacity "killed day 4 · $150 · CVR 0.31%" (chip). `nodeEvidence`
  highlights (.hl) the matching row in the campaign strip's evidence line +
  toast "Every number traces to the outcome series."
- **Generation band "Round 2 — proposed" (dashed)**: ghost children off A:
  "A2 · champion remix (A's opening + B's voice)", "B2 · new close", "C2 ·
  new close (alt)" — all dashed, each fx "Approving the proposal makes this
  node real — the tree grows a generation"; clicking any `data-go
  s6-console.html` (the proposal drawer lives in the thread).
- **Campaign strip**: three compact evidence rows (ids for nodeEvidence) +
  "Open proposal in thread →" `data-go s6-console.html`.
- **Dock**: focused A — title "A · hook-first (published)", path
  "launch_footage › hook-first › take 1 › published", time, scrubber;
  buttons: "▶ watch" fx, "⑂ Branch" fx "Manual branching stays available —
  but the proposal already compiled the winning branches for you."

data-cap (verbatim): tree → the same `built:canvas mode — pannable lineage
tree of takes (agentic_audio frontend/js/canvas-mode.js)`; dock → the same
`built:dock player…` label; lineage data → `built:lineage store
(aul/lineage.py LineageRecorder)`; performance halos on nodes →
`partial:attribution engine exists — component-level readout is new`; ghost
generation → `new:Proposal object — approving it grows the tree a
generation`.

WIRE_PAGE.title "S6c · Canvas — generations + performance". Acts:
nodeEvidence (1; plus any focus helper you need).

Shared: grayscale; the "heavier border = locked/focused" convention replaces
teal/indigo from the product (wireframes stay gray; ⚙/annotation supplies
color); runtime pair before </body>; full data-fx coverage; modals hidden on
load; no third-party AI provider names.

# Round 5 (v1, SUPERSEDED by v2 above) — the canvas projection (owner: "how does the canvas come into play?")

Design rule: **chat decides, canvas works.** Chat ⇄ Canvas are two projections
of the SAME session objects (bundle → plans → slots → takes). The thread stays
light (round-4 disclosure ladder) because the canvas is where full density is
legitimate: everything folded behind "▸" in the thread is spread out, aligned,
and scrubbable on the canvas. Two new cards; the parent screens' tab bars
already link to them (Thread · Canvas · Console).

## 5A · S4c — `s4c-canvas.html` (create-stage canvas)

First line exactly:
`<!-- @dsCard group="B · Product screens (user view)" name="S4c · Canvas — variants workbench" subtitle="Chat decides, canvas works: lanes, locks, shelf, relatives — full density lives here" -->`

Chrome: same sidebar + top bar as S4 (title "Launch week — spring line", stage
"Brief › Roles › Create ⇄ Publish › Live"); tabs = [Thread `data-go
s4-creation.html`] [Canvas on] [Console fx "Goes live after launch"]. The
.thread column is replaced by a full-width work area; keep a right rail.

Work area, left→right:
- **Moment shelf** (~150px column, dash tiles): "pour close-up ★", "terrace
  sunset", "hands pour", "bottle hero", "logo-safe close", "founder speaking ·
  9s · speech". Tile "pour close-up ★" has `data-act placePour` — clicking
  restyles lane A's first slot (visually distinct, e.g. .bite style) + toast
  "A now opens on the pour close-up — re-planned, preview still free." + a
  hidden rail chip `#adjNote` ("1 adjustment · re-planned free") becomes
  visible. Other tiles: data-fx "Drag onto any slot — the planner re-plans
  around what you place" (toast-only).
- **Three lanes** (stacked .cards, full width): A · hook-first, B · founder's
  voice, C · rephrased. Each lane: title row (name + "▶ watch" data-act
  reusing a shared #previewModal like S4's) + its strip. EVERY slot (`.strip i`)
  in lane A gets `data-act lockSlot` + data-fx "Lock this slot — the planner
  must keep it; everything else stays fluid." — the act toggles a lock style
  (thicker dark border) on that slot + toast. B's bite slot keeps the .bite
  style with fx "The founder bite is kept intact by role — voice slots don't
  get re-cut."
  - Under lane A: **comparison lane** — a dimmed/50%-opacity strip aligned
    beneath, labeled "≈ April teaser (published · 2.6% CTR) — same opening
    slot", fx explains alignment. Under B: "Founder story (archive)". C gets
    a slim tag "no relative — the round's one new bet".
- **Watch dock** (bottom row): three dash mini-players "▶ A / ▶ B / ▶ C", fx
  "The watch dock — scrub any variant without leaving the board."

Rail: (1) card "This canvas is a projection — same plan objects as the
thread; locks made here appear there instantly." (2) `#adjNote` hidden chip.
(3) "Lock & render all 3" `data-act lockRender` → `#gateModal` (copy the S4
Gate ① modal verbatim: Cancel `data-act gateCancel` / "Render — Gate ①"
`data-act doRender`); doRender closes the modal, adds "✓ rendered" chips to
each lane title row (#stA/#stB/#stC pattern), and reveals `#toLaunch` "Open
launch in thread →" `data-go s5-launch.html`. (4) "Back to Thread →"
`data-go s4-creation.html`.

data-cap (verbatim): lanes region → `new:variant lanes — projecting N plans
onto one canvas`; comparison lanes → `new:relative comparison lane — the
published relative aligned under its child`; moment shelf → `partial:moments
are typed + retrievable — the shelf surface is new`; strips/watch dock →
`built:the stable canvas furniture — strips, players, watch dock
(agentic_audio frontend, transform rail)`; lock pins (lane A) →
`built:locks + treatment knobs (creation/treatments.py incl.
rephrase-original)`; Gate button → `built:render refuses un-previewed plans
(creation/service.py render_short)`.

WIRE_PAGE.title "S4c · Canvas — variants workbench". Not in SEQ (side view —
bar shows "Start the walkthrough ▶"; that is fine). No data-story required.

## 5B · S6c — `s6c-canvas-performance.html` (live-stage canvas)

First line exactly:
`<!-- @dsCard group="B · Product screens (user view)" name="S6c · Canvas — performance overlay" subtitle="The same strips with outcomes painted on the slots — attribution you can see" -->`

Chrome: same top bar as S6 (title + "LIVE · day 4" badge); tabs = [Thread fx
"The conversation view of this campaign"] [Canvas on] [Console `data-go
s6-console.html`].

Work area:
- Three lanes again — SAME strips as S4c, now with performance painted on:
  - Lane A (spend bar "40% · $600 · pacing on-track"): opening slot carries a
    chip "+21% CTR carried" `data-act slotEvidence`; fx "Component
    attribution ON the material — this slot, not a table row."
  - Lane B (30%): the bite slot chip "voice converts · CVR +18%"
    `data-act slotEvidence`.
  - Lane C: whole lane at reduced opacity, tag "killed day 4 — criteria hit
    ($150 spent, CVR 0.31%)", close slot chip "− close underperforms"
    `data-act slotEvidence`; fx honest-kill copy.
  - `slotEvidence` highlights (adds .hl to) the matching evidence row in the
    rail + toast "Every number traces to the outcome series — nothing is
    vibes."
- Watch dock row as in S4c.

Rail: (1) "Evidence" card with three rows (A-opening +21%, B-voice +18%,
C-close kill), each with an id so slotEvidence can .hl them. (2) Proposal
summary card: "Proposal waiting — iterate: lock A's opening + B's voice ·
vary only the close" + "Open proposal in thread →" `data-go
s6-console.html`. (3) Note card: "Approve it and round 2's canvas opens with
these locks already pinned." fx references the compiler.

data-cap (verbatim): lanes/strips/watch dock → the same `built:the stable
canvas furniture…` label as S4c; slot performance chips → `partial:attribution
engine exists — component-level readout is new`; spend bars + numbers →
`built:outcome series + dedup (ads/campaign.py CampaignPublisher,
AttributionEngine)`; performance-halo treatment as a whole (work-area region)
→ `new:performance halos painted on slots`; proposal rail card →
`new:Proposal object — loop-closing piece`.

WIRE_PAGE.title "S6c · Canvas — performance overlay". Not in SEQ.

Shared rules for both cards: grayscale product (halos/chips stay gray-family;
the ⚙/annotation layer supplies color), runtime script pair before </body>,
every interactive-looking element carries data-fx, modals hidden on load,
no third-party AI provider names, vanilla JS.

---

## Definition of done (per screen)

1. Every visually interactive element carries `data-fx`; ⚡ legend reads as a
   complete "what does each button do" reference for the screen.
2. The story chain on this screen works by actual clicking, ending in the
   `data-go` that reaches the next screen.
3. All `data-act` keys exist in `WIRE_ACTS`; no modal visible on load; first
   line (`@dsCard`) byte-identical; grayscale product preserved; no external
   requests; no third-party provider names.
