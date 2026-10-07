# Agentic Creation — capability-anchored design (owner direction, 2026-07-25)

Owner decisions (verbatim intent):
1. Creation and transformation are **one agentic creation capability**, living in
   the current creation loop/platform — the stable design (agentic_audio entrance
   → Chat ⇄ Canvas), not a separate experimental surface.
2. **The interface and architecture anchor on a SET OF CAPABILITIES, not on
   vertical workflows.** Vertical flows (long→shorts is an *example*) are worth
   cataloguing as supported recipes, but they are compositions of capabilities —
   never the design unit. New flows must fall out of the capability set without
   new machinery.
3. For the rephrase-original audio treatment: **house voice is the accepted
   default; voice cloning is a next step** (provider-capability + rights gate).

## The capability set (the anchor)

Everything the agent can do, expressed as composable primitives. Each has an
owner in the codebase today or a named gap:

| Capability | What it is | Today |
|---|---|---|
| **Understand** | asset → tree/annotations: scenes, beats, speech spans, signals, semantics | recompose understanding + longform; AUL ingest/producers (experimental) |
| **Reference** | stable @-refs to assets/spans/nodes; lineage edges | AUL refs/edges (experimental); canvas `@` refs (stable, audio) |
| **Plan** | model-planned proposals under hard rules by construction (cut plans, section plans, scripts) | recompose planner+llm_planner; agentic_audio direction/plan stages |
| **Transform** | operate on EXISTING material: cut/recompose, trim, mux/duck, restore, retime | recompose assembly (server ffmpeg); no client trim yet |
| **Generate** | create NEW material: music, narration/TTS; later voice-clone, visuals | MusicGenerationCore, TTS narration; ALWAYS spend-gated behind approval |
| **Show** | make proposals watchable/legible BEFORE spend | audio: canvas dock (stable); visual: gap → OpenCut Tier-1 (preview strip + cut-list playback) |
| **Adjust** | human-in-the-loop edits that feed BACK into Plan: locks, knobs, trims, branches | Knobs/locks/pick_overrides exist; trim UI is a gap |
| **Persist** | outputs are assets with full lineage | LineageRecorder + AUL store (experimental) |
| **Publish & learn** | channels, outcomes, attribution back onto components | ads loop (experimental; sandbox live, TikTok env-gated) |

The session loop is the same regardless of flow: **propose → show → adjust →
lock → render/spend once → persist → (optionally) publish & learn.**

## Vertical flows (the catalog — compositions, not design units)

- **Score my video** (shipped, stable): Understand → Plan → Generate(music/VO) →
  Show → Adjust → lock. The reference implementation of the loop.
- **Long video → shorts → audio on top** (the example that sparked this): 
  Understand(longform) → Plan(N shorts, per-short hypothesis, cross-short
  no-reuse) → per short an **audio treatment**: keep-original (bites) |
  **rephrase-original** (transcribe → model rewrite for the short's arc →
  house-voice TTS) | new-narration | music-only; music generate-or-variant →
  Show(strip+preview) → Adjust(trim/locks) → render → publish & learn.
- **Multi-image → slideshow + song** (shipped as v1/v2 API): Understand(images)
  → Plan(sequence+lyrics) → Generate(song) → render.
- **Ad variants for performance** (experimental e2e validated): Plan K variants
  on one source → publish → attribution → iterate on the winning component.
- **Remix/extend an existing piece**: Reference(prior output) → Plan(edit) →
  Generate(variant)/Transform → Show → lock (agentic_audio edit path today).
- Future flows (fall out for free if capabilities hold): highlight reel from
  event footage, localization pass (rephrase treatment in another language),
  library-wide refresh (re-cut old assets to a new track).

Rule of thumb: if a proposed flow needs a primitive that isn't in the table,
that's a capability discussion first, not a flow feature.

## Why rephrase-original matters (the new primitive-level piece)

It extends **Generate** with a treatment that is *grounded in Transform*:
verbatim bites are hostage to where source sentences fall (voice_bites case:
8.7s static midsection). Rephrase decouples message from source timeline — best
visual moments carry a rewritten version of the original message at the short's
pace. House voice = honest redub framing; clone deferred.

## Surface mapping (stable design)

One entrance, one chat, one canvas. Session type is inferred (or chosen) but
everything renders through the same capability loop: canvas lineage shows
source → outputs as branches → audio takes as children; `@` refs address any
node/moment; the dock Shows; chat Adjusts; locks trigger the one paid render.
The experimental library/AUL layer remains the substrate (refs, lineage,
outcomes) — not the front door.

## Multi-source `@` references across visual and audio (refined 2026-07-25)

The natural request shape: *"@launch_master @crowd_reel @retro_track — 20s
hook-first short, keep the announcer feel."* Several source entities, mixed
kinds, one plan. Design principles:

### 1. Refs stay dumb; roles stay per-request
A ref is ONLY content + coordinates (`asset_id`, `asset_id#t=a-b`, a tree node,
a prior output). Its function in a session — spine, accent/b-roll, music, voice
source, style reference, exclusion — is a **role in the request bundle**, never
a property of the ref. Same grammar for visual and audio; no per-kind ref types.
(Voice is NOT a new ref type: a "voice source" is an asset ref + the speech-span
annotations we already store; speaker identity lives in annotations, not refs.)

### 2. The request bundle
A session/message carries `refs: [{ref, role?, note?}]` where role ∈
`spine | accent | music | voice | style | exclude` (open set, small). Roles are
**inferred by kind + intent, then CONFIRMED visibly in the plan** — the
grounded-plan "From your library" chips become role-tagged chips; the user
overrides in chat ("@crowd_reel as b-roll only"). `@` order is a soft priority
hint, never load-bearing semantics.

### 3. What each capability does with a bundle
- **Plan (visual)**: N-source pooling generalizes the existing machinery —
  slots already carry per-asset provenance, exclude_intervals is already keyed
  by asset, scene keys are per-asset. The MVP two-asset cap is a *product* cap;
  raise deliberately (2 → 4) with probe-based feasibility messaging per source.
  Spine selection: explicit role wins; else the planner picks and the plan says
  so ("spine: launch_master — richest coverage").
- **Audio treatment**: TreatmentSpec gains `voice_source_ref` and `music_ref`.
  Cross-modal composition falls out: *visuals from A, message from B* —
  rephrase-original can transcribe @old_ad and speak (house voice) over the cut
  of @product_demo. `music: provided(@track) | variant-of(@track) | generate`.
- **Style refs**: a ref to a PRIOR OUTPUT carries its persisted plan meta
  (knobs, hypothesis, cut_report) — "cut like @summer_short" = seed knobs/energy
  curve from that plan, stated in the proposal. No new storage; outputs already
  persist their knobs.
- **Hard rules across sources**: no-reuse and per-scene caps hold per source
  asset; cross-output exclusion extends per source; bites may be carved from any
  `voice`-role source. Attribution needs nothing new — slots already carry
  asset_id, so multi-source components rank correctly for free.

### 4. Conflicts and gates (agent behavior, not errors)
Owner rule (2026-07-25): **no silent defaults — ask first, then propose.**
- Unambiguous bundles (roles clear from kind + intent) proceed straight to the
  role-tagged plan for confirmation.
- AMBIGUOUS roles (e.g. two spine-worthy videos, a track that could be music or
  style ref) → the agent ASKS the disambiguating question first — one short
  question with the options it sees — and only then proposes the plan.
- `@track` referenced AND "fresh music" asked → contradiction, so ask: use it,
  make a variant of it, or generate fresh?
- `voice` role on an asset with no usable speech (ASR confidence gate) → surface
  the gap and ask: new-narration (priced) or drop the treatment?
- Every bundle still resolves to a visible role-tagged contract in the plan
  BEFORE any spend — asking first narrows options; the plan remains the consent
  surface.
- Approved role vocabulary: `spine | accent | music | voice | style | exclude`
  (owner-approved; extend only through a capability discussion).
- Source cap: 2 today, raise to 4 behind per-source feasibility probes
  (owner-approved ceiling; revisit only with evidence).

### 5. Surface impact (stable design, additive only)
Canvas `@` today = ONE HEAD-like context chip (audio sessions). Multi-source
extends this to a chip ROW with role tags; single-chip behavior is the
degenerate case, so the shipped audio flow is untouched. The `@` picker is
backed by AUL search (text over understanding) and the library board. The
`context_refs` message payload (already persisted + read by the loop since fix
F1) generalizes from one id to the role-tagged bundle.

## Open items for the build session (deliberately not started)

- Capability contracts: treatment enum as TreatmentSpec wrapping Knobs (knobs =
  cut taste; treatment = audio policy) — keep flow-agnostic. TreatmentSpec now
  also carries voice_source_ref / music_ref per the multi-source design above.
- Request-bundle contract: `refs: [{ref, role?, note?}]`; role inference rules
  (kind + intent) and the confirm-in-plan surface; multi-chip `@` row in canvas
  composer (single chip = degenerate case, audio flow untouched).
- N-source planner cap: raise 2 → 4 behind per-source feasibility probes; watch
  LLM offer-payload size as sources grow.
- Rephrase gate: minimum ASR content-confidence before offering; fallback to
  new-narration. Server-side ASR only (do not absorb in-browser ASR).
- Session-type inference at the entrance vs explicit choice.
- Show-for-visual: OpenCut Tier-1 absorption (OPENCUT_ABSORPTION.md).
- Voice-clone gate: provider capability + rights sign-off (explicitly next-step).
