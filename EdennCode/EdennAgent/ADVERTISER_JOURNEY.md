# Advertiser Journey — e2e product design (wireframe stage, 2026-07-28)

Owner brief: design the workflow where an advertiser uploads brand assets →
into our generation loop → an ads **market agent** publishes, monitors campaign
effects, and feeds those effects back into the generation loop. Goal: an e2e
walkthrough of the product. Wireframes first; no visual detail yet.

**Fixed constraint (owner, same session): the existing agentic creation
backbone — generation + creation on the stable chat ⇄ canvas — is good and
does not change.** This design wraps a campaign shell AROUND it; stations 2–3
below are the existing surface, referenced as-is.

Wireframes: the design tool project **"Edenn — Advertiser Journey (Wireframes)"**
(8 cards, W0–W7); sources in `design/advertiser-journey/`.

## The shape: a loop, not a funnel

```
1 INTAKE → 2 UNDERSTAND → 3 CREATE → 4 PUBLISH → 5 LEARN
                ↑  (existing stable surface)  ↓
                └────────── Proposal ─────────┘
```

- **Spine object: the Campaign** — brief → variants → publications → outcomes
  → proposals → next round. Everything user-visible hangs off it.
- **Objects flowing:** Asset → Annotation → Bundle → Plan → Variant →
  Publication → OutcomeSeries → Proposal → (Plan …). Every arrow is a typed
  object we already store or a thin new one (Campaign, LaunchPlan, Proposal).
- **Two agent hats, one thread:** the *creative director* speaks in 2–3
  (existing), the *ads market agent* speaks in 4–5. The advertiser learns one
  product, not two.
- **Three spend gates:** ① render-lock (small; per variant) · ② launch (real
  budget) · ③ iterate (both). Everything before a gate is free and SHOWN.

## Station-by-station (see the wireframe cards for layout)

**W1 · Brand intake** — drag-anything upload (footage/stills/audio/past ads),
brand kit (logo/colors/claims — treatments reference it, never invent claims),
channel connect (OAuth per channel; sandbox mode = full loop, zero spend),
goals (KPI, budget guardrail, markets → become the market agent's default
success/kill criteria). Exit: ≥1 analyzed video → library.

**W2 · Understanding review** — the trust gate. Library board + per-asset
moments with corrections (rename / ★hero / ⊘never-use → standing EXCLUDE),
readiness checklist, and the agent's honest gap note ("no vertical b-roll").
Nothing costs money here; the screen exists to earn the right to plan.

**W3 · Campaign brief** — one NL box + @refs + structured constraints
(variants/duration/channel/flight/budget). Ambiguity → ask-first question
cards (built resolver). Exit: roles resolved → plan all variants free.

**W4 · Creation review** — the EXISTING creation loop, framed per-campaign:
variants side-by-side (hypothesis + treatment + strip + preview), adjust via
locks/overrides (never a manual timeline fork), Gate ① renders all previewed
cuts. Backbone unchanged.

**W5 · Launch plan** — the market agent's contract: per-variant budget split
with WHY (a real A/B: one axis varies per pair), schedule/pacing, success +
kill criteria (editable), and **rules of engagement** — the autonomy boundary
stated up front (pull outcomes: autonomous · alert: autonomous · reallocate
≤15%: autonomous-but-logged · pause variant: asks · new round: asks). Gate ②.

**W6 · Campaign console** — monitoring: variant KPI table with trends,
component attribution ("which opening carries the click", "sound that
converts"), pacing, and the **agent feed** — timestamped observations, each
traceable to data. Design rule: no raw "edit campaign" form; every change goes
through the agent as a proposal or a logged autonomous act.

**W7 · Feedback proposal** — the thesis in one card: evidence (linked to
console data) → typed actions (`iterate | reallocate | retire | expand`), each
priced and separately approvable. **Approving `iterate` compiles a
CreationRequest**: winning spans → locks, winning treatment → TreatmentSpec,
losers → excludes, winning plans → style refs — and the loop re-enters W4
seeded. Round history strip keeps the lineage visible forever.

## The organization model (owner feedback round 2, 2026-07-28)

Owner: "people can have loads of different assets — how do we best see it
organized?" Plus three walkthrough corrections: (a) ingestion should ORGANIZE
BY ASKING (by campaign, then by asset); (b) creation must visibly ground its
enhancement on existing refs/assets; (c) creation and publishing should sit
CLOSE together, with previously published assets serving as auxiliary signal
when judging new variants.

Reference methodology (borrowed, not copied): Flora — workspaces per client +
visible genealogy of every idea; Dreamina — organization is the agent's
continuous job inside one conversation, idea→export; Google AI Studio —
search-first, near-zero taxonomy burden. **Our uniqueness: nobody organizes by
UNDERSTANDING + PERFORMANCE.**

Three axes, one library page (S2 v2):
1. **Collections** (human axis) — campaign/shoot groupings PROPOSED BY THE
   AGENT AT INGEST via ask-first (S2b): shoot-detection groups clips,
   finished-ad detection routes to a "Published archive", dedupe is automatic,
   only genuine ambiguity becomes a question. No folder dialogs, ever.
   Collections are proposals, not prisons — any asset is @-referenceable from
   any campaign.
2. **Meaning** (our axis) — search over real understanding annotations ("pour
   shot", "founder speaking"); moments are first-class retrievable items.
   Search is the primary navigation; collections just cap the visible set.
3. **Families + performance** (our axis) — the board groups a SOURCE with
   everything cut from it and how each performed (lineage IS the org chart);
   assets carry performance halos ("best child: 2.6% CTR") and honest idle
   states ("unused — 4 hero moments waiting").

Creation ⇄ publishing adjacency (S4 v2 "Variants — create ⇄ publish"):
- every variant card carries a **grounding chip** (which collection/moments,
  generated-vs-reused ratio) — grounding is visible, not claimed;
- every variant shows its **nearest published relative** (matched by lineage/
  moment overlap + treatment) WITH that relative's real numbers — the
  auxiliary signal; a variant with no relative is labeled "the NEW bet";
- the publish panel (channel/flight/budget split, drafted FROM the relatives'
  history) lives in the same screen's rail — Gate ① render and Gate ② launch
  are two consents on one surface, never two far-apart tools.
- The "Published archive" from ingest is what makes this work day one: an
  advertiser's PAST ads become comparison material with real numbers before
  our first campaign ever runs.

## Interactive walkthrough (round 3, 2026-07-28)

Owner: "make the wireframes a bit more complete with actual ux walkthrough of
what happens or effects of each button clicked or page directs." The product
screens (S1–S6 + W0) are now **clickable**, with two layers:

1. **Annotation layer** — `design/advertiser-journey/_wire.js` (inlined into
   each screen at push time by `_inline_wire.py`; repo files reference it via
   `<script src>` for single-source editing). Blue = annotation, gray =
   product. Hover any element → tooltip with its effect (+ destination);
   **⚡ hotspots** → numbered badges + a legend panel listing EVERY click on
   the page and where it leads; **● guide** → pulses the one click that
   advances the story; a fixed walkthrough bar steps 1/8 → 8/8 (‹ › or arrow
   keys). W0 sits outside the sequence — its bar offers "Start the
   walkthrough ▶" and each station deep-links to its product screen.
2. **Working effects** — the story chain actually executes in-page:
   S1 "Add assets" → S2b answer the IMG_2214 question (counts update 8→9,
   agent confirms, "Open the Library →" appears) → S2 "Create from this
   collection →" → S3 send brief → answer both role questions → "Plan 3
   variants →" → S4 "Lock & render all 3" → Gate ① consent modal → staged
   render chips → "…then Launch →" → S5 "Launch →" → Gate ② modal ("Launch —
   spends budget") → S6 (?just=launched toast) proposal card → drawer with
   three separately-approvable typed actions → compile modal (the
   CreationRequest: locks/excludes/style-ref) → "Start round 2 →" →
   S4?seed=round2 (seeded banner, A2/B2/C2 cards citing round-1 LIVE numbers,
   "The loop, closed" end card). Side interactions work too: meaning-search
   with collection/flat board states (S2), rename/refile chips (S2b),
   preview/side-by-side/grounding modals + quick-adjust (S4), editable
   autonomy rules + split adjustments (S5), evidence-linked feed + variant
   filtering (S6).

Conventions and the full per-screen effect inventory:
`design/advertiser-journey/INTERACTIONS.md`. Cross-page params: `ctx`
(S2→S3 grounding chip), `seed=round2` (S4 round-2 state), `just=launched`
(S6 arrival toast). Verified end-to-end by a live browser click-through of
the whole chain (zero console errors); best experienced on the local server
(design-pane cards are sandboxed per-card, so cross-card navigation may not
carry there).

## Round 4 — density + the capability bridge (owner feedback, 2026-07-30)

Owner: screens felt "a bit crowded in general with amount of information
feeding to the customer", and "not sure how this design connect with our
existing agentic generation capabilities."

**Density — the disclosure ladder** (spec: INTERACTIONS.md §4A): Layer 1 =
only what drives the next decision (title, visual, ONE signal chip, primary
action); Layer 2 = history/reasons behind a "▸" expander (`data-more` in the
runtime); Layer 3 = explanations demoted to hover tooltips. Applied: S4
relative rows → "≈ April teaser · 2.6% ▸" chips, quick-adjust folded, rail
merged and the Gate explainer card deleted into the buttons' tooltips; S2
one performance chip per family row; S6 feed = 3 recent + "earlier ▸",
attribution 2 rows + "more ▸"; S5 Why cells ≤4 words; S2b explainer behind
"why keep these? ▸"; S1 rail 4→3 cards. No interaction was removed — only
its trigger relocated; the full story chain re-verified by browser
click-through after the pass.

**Capability bridge — built vs new made visible** (spec: §4B inventory):
every major region now carries `data-cap="built|partial|new:label — module"`,
and the annotation bar gained **⚙ built vs new** — outlines regions (green
solid = runs today, amber dashed = engine exists/surface new, magenta double
= new shell object) with a legend mapping each region to its module path.
Plus a new card **W8 · Capability bridge** (`w8-capability-map.html`): the
three-column map Journey screen ↔ capability ↔ module + status. Net counts:
12 built · 8 partial · 11 new. The headline the overlay makes visible:
stations 2–4 sit almost entirely on the BUILT creation backbone
(`creation/…` + the stable chat surface + `aul/…`), and everything NEW is a
thin shell — Campaign, LaunchPlan, agent feed, Proposal, and the
iterate→CreationRequest compiler.

## Round 5 (v2) — the REAL canvas comes into play (owner correction)

Owner: "i mean the canvas view in the original agentic audio part." The v1
cards invented a "lanes workbench"; superseded. The real canvas
(`agentic_audio/frontend/js/canvas-mode.js`, verified live on the mock
console) is a **pannable lineage tree of takes + a dock player**: node cards
linked by parent, rooted at the source video, locked path highlighted; dock
shows the focused take with its lineage path and **Use this / Branch** —
every action routing through the same `/choices` contract as chat.

**The campaign mapping needs nothing new in the canvas mechanics:**
- **Variants are branch heads.** A campaign's three variants are three
  first-generation branches from the same source — exactly the shape the
  tree already draws. "Use this" IS the lock; "Branch" IS variation.
- **Rounds are generations.** The approved proposal compiles into locks →
  round 2's takes are children of round 1's winners. The loop is visible as
  tree depth; lineage comes from the store we already write
  (`aul/lineage.py`).
- **Outcomes and proposals are overlays.** Performance halos paint on
  published nodes; the pending proposal renders as a dashed next generation
  that becomes real on approval. S2's family rows are the FLAT projection of
  this same lineage; the tree is the zoomable one.

Cards rewritten in place (names updated): **S4c · Canvas — the campaign in
the lineage tree** (root → A/B/C branch heads, ghost "≈ April teaser"
overlay node, campaign strip with Gate ①, faithful dock) and **S6c · Canvas
— generations + performance** (Round-1 band with node halos + honest-kill
dimming, dashed Round-2 band of proposed children, dock on the published
winner). NEW is only: branch-head campaign labels, the published-relative
overlay node, node performance halos, and the dashed proposal generation.

## The e2e walkthrough (the demo narrative)

Maya runs marketing for a beverage brand. **(W1)** She drags in 14 files —
launch footage, product stills, two tracks, last year's ads — connects TikTok,
sets CTR as the goal with a $1.5k guardrail. **(W2)** Ten minutes later the
library shows what Edenn understood: 30 scenes, the announcer speech found,
three hero moments; she stars the pour shot and bans the old logo animation.
**(W3)** She types one sentence: three 15s TikTok variants — hook-first,
original voice, rephrased. Edenn asks two questions (which reel leads; use or
vary the referenced track). **(W4)** Three plans appear with previews; she
watches all three before anything renders, drags one slot, locks — Gate ①.
**(W5)** The market agent proposes the launch: 40/30/30 split with reasons,
even pacing, kill criteria, and its own rules of engagement; she approves —
Gate ②, real budget. **(W6)** Day 3: the agent has already logged a 10%
reallocation; day 4 it drafts a proposal. **(W7)** Evidence: A's opening
carries CTR +21%, B's voice converts +18%, C hit kill criteria. Actions:
iterate (lock A's opening + B's voice, vary only the close), reallocate C's
remaining budget, retire C. She approves — Gate ③ — and round 2 begins,
pre-seeded, fully lineaged. **The product's promise is now a screenshot:
outcomes literally wrote the next brief.**

## What exists vs. what the shell adds

| Piece | Status |
|---|---|
| Ingest + understanding + library + corrections write-back | built (corrections producer = small gap) |
| Bundle/roles/ask-first, plan/preview/lock, treatments incl. rephrase | **built — unchanged backbone** |
| Publisher, channel registry (sandbox live, TikTok env-gated), outcomes, attribution | built |
| Campaign object (brief + flight + budget wrapper) | new, thin |
| LaunchPlan (splits, pacing, criteria, autonomy rules) | new — market agent's first artifact |
| Agent feed (narration over outcomes) + autonomous-act log | new |
| Proposal object (evidence refs + typed actions) + iterate→CreationRequest compiler | new — the loop-closing piece |
| Multi-round campaign history / lineage view | new (reads existing lineage) |

## Open design questions (owner)

1. **Autonomy defaults** — is "reallocate ≤15% autonomous-but-logged" the right
   starting boundary, or should v1 ask for everything?
2. **Proposal cadence** — event-driven only (criteria hits), daily digest, or
   both?
3. **Multi-campaign brand space** — W2's library is standing; do campaigns
   share correction state and excludes globally (my lean: yes, brand-level)?
4. **Channel scope for v1 walkthrough** — sandbox-only demo, or gate on real
   TikTok creds first?
