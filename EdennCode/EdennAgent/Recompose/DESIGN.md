# Recompose — Design (post-W0)

**Status:** W0 spike passed its human gate (2026-07-15): beat-cut assembly of
real footage works and reads musically. Owner feedback that drives this
design:

1. Beats are right; **semantic coherence is missing** — segments must not
   jump between unrelated content as if it were one story.
2. Segmentation should be **adaptive-depth**: segment a video into smaller
   scenes only as deep as the music demands ("after monitoring the beats,
   should we cut more or less?"), and build understanding of the video in
   **textual space** (like the video-music work).
3. Input shape for now: **video + images, maximum two assets** (list-shaped
   from day one).
4. W0's cuts were **too dense** — taste parameters are subjective, so control
   must go **back to the user through the agentic loop**, not live in code
   constants.

---

## 1. Mental model

The pipeline is **pure transformations between five durable representations**.
The agent's job is negotiating the *parameters* of those transformations with
the user. Nothing else is stateful.

```
AssetRecord ──understand──▶ SegmentTree (textual space)
                                   │
Track ──analyze──▶ MusicSheet      │ lazy deepen (only where CutSpec demands)
                       │           │
                       ▼           ▼
        knobs ──▶  CutSpec ──▶ RecomposePlan (passages → slots → node refs)
   (user taste)                    │
                                   ▼
                            RenderedVariant (+ lineage + cut report)
```

### AssetRecord
One input (video or image). Technical metadata + content hash. The model is
`source_assets: list[AssetRecord]` from day one; the MVP enforces `len ≤ 2`
at the API — a product constraint, not a schema constraint.

### SegmentTree — adaptive-depth segmentation, understood in text
Per video asset, a **hierarchy**, not a flat list:

- **Level 0**: the whole asset.
- **Level 1**: scenes — `detect_scene_cuts` (visual boundaries) reconciled
  with the analysis pipeline's semantic scenes (the agentic-audio
  observation shape: summary / key_actions / mood per scene).
- **Level 2+**: sub-shots, created **lazily** by `split(node, target_dur)` —
  pyscenedetect at higher sensitivity inside the node's window, falling back
  to motion-valley cut points.

Every node carries its understanding **in textual space** — `summary`,
`mood`, `entities` (keyword-extracted), plus cheap numeric signals (`motion`,
`brightness`, `duration`, `quality`). Children inherit parent text until an
optional keyframe-refine pass; the planner only ever reads text + numbers,
never pixels. That is what makes planning cacheable and scalable.

**Adaptive depth is demand-driven:** the tree deepens only where the CutSpec
(music × user taste) asks for shots shorter than the leaves that exist. If
the user dials cut density down, no deepening happens at all. Depth follows
music × taste — it is never a fixed constant. This scales unchanged from 2
assets to 200.

**Images are leaf segments** with `is_still=True` and unbounded duration —
they can fill any slot (rendered as hold + slow zoom/pan; the slideshow
machinery already does this).

### MusicSheet
The track's objective structure: beat grid, tempo, RMS energy curve,
**phrase boundaries** (energy valleys + lyric gaps when word timestamps
exist), sections. Computed once per track, knob-independent.

### CutSpec — where taste enters
`CutSpec = f(MusicSheet, knobs)`. The knobs are **first-class plan fields**:

| Knob | Values (v1) | Meaning |
|---|---|---|
| `cut_density` | sparse / medium / dense | beats-per-slot mapping over energy terciles (e.g. sparse 8/6/4, medium 6/4/2, dense 4/2/1) + `min/max_shot_s` clamps |
| `coherence_mode` | single_story / spine_and_accents / interleave | how much cross-asset mixing is allowed |
| `energy_literalness` | low / high | how strictly slot density tracks the energy curve |
| per-slot `locks` | slot → node | user-pinned choices survive re-plans |

W0 shipped the equivalent of `dense` (44 slots / 45 s) — the owner's "too
dense" makes **medium the default**. Defaults are just defaults: every knob
is surfaced in the plan card and adjustable in conversation.

### RecomposePlan — passages give semantic coherence
The plan is not a flat slot list. It is:

```
RecomposePlan
├── hypothesis (what this variant argues creatively)
├── knobs (above)
├── passages[]           ← aligned to MusicSheet phrase boundaries
│   ├── semantic_focus   (subject/theme cluster; with ≤2 assets: which asset
│   │                     is the narrative spine vs the accent)
│   ├── arc_note         (one line: what this passage does)
│   └── slots[]          (beat-aligned; each references a SegmentTree node,
│                         records why, carries the lock flag)
└── parent_plan_id       (every knob change / re-plan is a child — lineage)
```

Coherence is enforced structurally, not hoped for:
- slots **within a passage draw only from that passage's focus pool**;
- a **continuity term** in selection prefers entity/subject overlap with the
  previous slot (unless the hypothesis says contrast);
- asset roles (`spine` vs `accent`) cap how often the secondary asset
  appears and where (accents land on phrase boundaries, not mid-passage).

This directly kills the W0 failure (war footage interleaved with a vacation
slideshow because both had "somber" scenes).

### RenderedVariant
Assembly output + full lineage (slot → node → asset, timecodes, why) + the
measured cut report (cut→beat offsets). Per-slot artifacts are cached so a
knob tweak or single-slot swap re-renders only what changed.

## 2. The agentic loop is the taste interface

Reusing the agentic-audio scaffolding (per `../README.md` strangler rules):

1. Session opens with ≤2 assets + 1 track → understanding + MusicSheet run.
2. Agent presents: music summary (tempo, phrases, energy shape), the knob
   card with proposed defaults, and 2–3 **hypotheses** (proposals) that vary
   on knob axes — e.g. "Single story, sparse cuts" vs "Spine + still accents,
   medium".
3. Approve → variants render (deterministic, seconds — no spend gate needed;
   iteration is free).
4. Natural language becomes **knob deltas or slot ops**: "fewer cuts" →
   `cut_density` down, re-render; "stay on the main video" →
   `coherence_mode=single_story`; "keep that opening" → slot lock. Every
   revision is a child plan — the canvas lineage tree renders the taste
   exploration for free.

## 3. Implementation plan (no code until approved)

| # | Milestone | Contents | Reuses | ~Size |
|---|---|---|---|---|
| M1 | Domain + SegmentTree | `domain.py` (five objects, serializable, versioned); tree builder (level-1 = detect_scene_cuts ∪ analysis scenes); lazy `split()`; image leaves | detect_scene_cuts, pyscenedetect | 1 wk |
| M2 | Understanding in text | per-video: reuse `preview_pre_generation`; per-node inherit + numeric signals (W0 motion/brightness); image caption via existing image analysis; keyword entities | analysis pipeline, W0 code | 1 wk (∥ M1) |
| M3 | MusicSheet + CutSpec | repackage W0 librosa; phrase boundaries; knob→slot-skeleton generator with property tests (coverage, clamps, cuts-on-beats) | W0 code | 0.5 wk |
| M4 | Planner v1 (the core) | passage assigner (phrases × asset roles); selection = W0 scorer + continuity term + passage pools; bounded one-turn LLM pass for final picks + arc notes (deterministic fallback = pure scorer); hypothesis axes = coherence_mode × cut_density | W0 selector, agentic loop pattern | 1.5 wk |
| M5 | Assembly v2 | W0 renderer + Ken Burns stills + passage-boundary crossfades; per-slot cache keyed (node, dur, format); cut-report module w/ nearest-match fix | W0 renderer, slideshow graph | 1 wk (∥ M4) |
| M6 | Agentic wiring | `video_recompose` job type; session flow (≤2 assets + track); knob card + hypotheses as proposals; NL→knob-delta handling; canvas lineage reuse; extract needed core pieces to `agentic/core/` | agentic_audio scaffolding | 1.5 wk |
| M7 | Eval (continuous) | E1 module (passing); **coherence judge** ("reads as one piece?") pairwise vs W0 selector; density-preference capture (same plan @ 3 densities → pick data); golden set = 3 asset-pairs | e2e gate patterns | with M3–M6 |

~4 weeks to an agentic demo. Order: M1+M2 ∥ → M3 → M4 ∥ M5 → M6, M7 threaded
throughout.

### Explicit decisions taken (flag if wrong)
1. Max-2 assets enforced at the API; the data model stays list-shaped.
2. Stills render as Ken Burns holds; no generation anywhere.
3. Density ships as presets (sparse/medium/dense) surfaced to conversation;
   a numeric slider is UI polish later.
4. `medium` default density ≈ half of W0's slot count.
5. Understanding cost: one analysis pass per asset (as today); node-level
   VLM refinement deferred until the coherence judge proves it's needed.
