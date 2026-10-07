# Asset Understanding Layer (AUL) — design

**Status:** direction approved 2026-07-16 (owner). No implementation yet.
**Problem:** understanding today is ephemeral — recompose trees mint fresh
node ids per process, observations live in scratch JSON, and nothing
downstream can durably *cite* anything upstream. Downstream features are
capped by upstream structure (owner, 2026-07-16). This layer is the floor
under every vertical: one reference system ("@"), one lineage ledger.

## 1. References: content + coordinates, never runtime objects

```
ref := asset_id ( "#" span )?
asset_id := content-addressed (sha256) identity of an ingested asset
span     := t=<start>-<end>            (seconds; regions/crops additive later)

examples:
  asset_9f31                      the whole upload
  asset_9f31#t=193.2-203.5        ten seconds of it (a "clip ref")
  take_c41a                       a generated artifact (music take, VO, render)
  plan_7788 / plan_7788/slot/7    a plan and one slot of it
```

Rules:
- A ref's identity NEVER depends on analysis state. Scene summaries, moods,
  speech flags, embeddings are **annotations attached to spans**, not the
  identity. Views (e.g. recompose's SegmentTree) are materialized FROM
  annotations and are disposable; refs are forever.
- Every ref presents three faces, uniformly, everywhere:
  1. **playable preview** (UI),
  2. **textual card** (LLM context — the generalization of agentic_audio's
     `_context_ref_note`),
  3. **ledger entry** (lineage edges, usage, rights-lite).
- "@" in any surface = search over the catalog scoped to the session/project,
  resolving to a ref chip. One scheme across agentic chat, canvas, recompose,
  and future verticals.

## 2. Understanding: layered, versioned annotations

| Layer | Content | Producer | Cost |
|---|---|---|---|
| L0 technical | duration, codecs, resolution, luma, audio-activity spans | ffprobe/deterministic | free, on ingest |
| L1 structural | shot boundaries, speech spans, beat grid + phrases (audio), long-form chunk merges | detectors **@version** | cheap, on ingest |
| L2 semantic | scene summaries, moods, entities, role guesses | model **@prompt-version** | paid, on demand |
| L3 derived | embeddings, role probabilities, quality scores | model @version | paid, lazy |

Annotation record: `(ref-span, layer, producer@version, inputs_hash, payload)`.
Append-only; a detector upgrade writes new annotations beside old ones and
views pick a version. **Understanding-on-demand is the layer's core
contract** (the lazy-deepening principle generalized): downstream declares
needs as queries ("shots ≥2s with speech in this range"); the resolver
answers from the catalog or runs-and-persists the missing analysis.

## 3. Lineage: edges between refs

`edge := (dst_ref, derived_from=[src_refs], operation, params, versions,
session/job ids)` for every operation we already perform — trims, concats,
music generation, TTS, restore, mixes. Payoffs:
- PRD §15 audit questions become queries;
- the reverse index (span → everywhere used) turns `exclude_intervals` into
  a durable **footage economy**: cross-output distinctness, overuse
  detection, "what's unused" — for free.

## 4. Storage: rows, not a platform

Three tables in the existing shared Postgres (async_v2's home):
`assets` (hash, kind, tech), `annotations` (span, layer, producer@version,
payload JSONB), `edges` (src, dst, op, params, session/job) + a usage view.
Explicit non-goals for now: vector DB, DAM UI, rights engine, workspace
model. Embeddings arrive later as just another annotation layer.

## 5. Migration map (nothing built is thrown away)

| Today | Becomes |
|---|---|
| recompose observations (scratch JSON) | L2 annotations |
| audio_activity, cut caches, beat grids | L1 annotations |
| motion/brightness/speech signals | L0/L1 annotations |
| SegmentTree | derived view (rebuildable) |
| plans / variants / takes | lineage nodes + edges |
| agentic_audio candidates + `@` refs | same ref scheme (retrofit AFTER its deploy milestone) |

Decisions taken 2026-07-16: time-spans first (regions additive); shared
Postgres; recompose is the first native citizen; annotations append-only.

## 6. Frontend: how a user SEES the layer

Full mockup: `design/understanding_inspector.html` (open in a browser).
Three surfaces, one atomic element:

- **The ref chip** — the atom. `@asset#t=193-203` renders identically in
  chat, canvas, plan slots, and the inspector: tiny thumbnail + kind icon +
  time span + one-line label. Click → Span Card. Drag/`@` → cite it.
- **Library** (grid): assets as cards — thumbnail/waveform, duration,
  understanding progress (L0/L1/L2 chips), usage heat bar (footage economy),
  auto-flags (dark→restored, speech-heavy). The search box IS the
  @-resolver.
- **Asset Inspector** (the money view): a filmstrip timeline with
  **understanding as stacked lanes over one time axis** — filmstrip, scenes
  (L2 blocks, hover = summary/mood), speech spans (bite-eligible
  highlighted), motion/brightness sparklines, and a **usage lane** showing
  which spans each output consumed (colored per output). Click any span →
  **Span Card**: ref chip + copy, summary, entities, signals, "used in"
  ledger, actions (@-reference, preview, lock).
- **Lineage** reuses the existing canvas tree — nodes become refs (assets →
  plans → takes → renders), edges labeled with operations.

Design language: the existing agentic console (light, minimal, rounded
cards); the canvas lineage view is the precedent users already know.
