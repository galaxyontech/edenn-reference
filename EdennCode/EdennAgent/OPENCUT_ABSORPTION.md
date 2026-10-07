# OpenCut → Edenn visual-layer absorption study (2026-07-22)

Question (owner): can we absorb anything from https://github.com/opencut-app/opencut
for the VISUAL layer, without interrupting the existing audio layer?

Method: 6 parallel deep-readers over the OpenCut codebases + 1 mapper over Edenn's
own visual layer; every load-bearing claim adversarially re-verified against the
fetched sources (8/8 upheld). This doc is the durable summary; scope decisions
belong to the owner.

## Verdict

Yes — there is a clean, legally-safe absorption path, but from the **archived
`opencut-classic`** repo, not the hyped rewrite:

- **License is clean (verified):** both repos plain MIT, no CLA/DCO/NOTICE/trademark
  constraints anywhere. Only obligation: keep their copyright line on vendored files.
- **Source choice (verified):** `opencut-app/opencut` (78.5k★) is a ground-up rewrite —
  Rust engine crates literally commented out of the workspace, contributions frozen,
  no docs. The archived **`opencut-classic`** (read-only since 2026-05-17) contains the
  *working* editor. Mine classic; watch the rewrite (its planned editor API / MCP
  server / headless mode may become a de-facto standard later).
- **Edenn constraint (verified):** our stable frontend (agentic_audio) and the
  experimental library console are strictly vanilla JS — zero build tooling in the
  repo, and the prod mount serves a hardcoded asset allow-list. So absorption means
  **porting pure-math/logic modules into vanilla files or standalone pages** — NOT
  vendoring React components. (Committing a prebuilt React bundle is possible but
  recommended against: against every repo pattern, unauditable blob.)

The single most valuable discovery: **classic's preview architecture solves exactly
our recompose gap** — playing a *cut-list of source spans* against a master clock
without pre-rendering. Today every taste-loop iteration (knob tweak, slot swap)
costs a full server ffmpeg render; their pattern makes plan preview instant and free.

## How classic's editor actually works (all observed, paths cited)

- **Framework-free core (their own architecture proves our constraint is fine):**
  React/zustand hold only UI prefs; ALL editor state lives in a framework-agnostic
  `EditorCore` singleton with 12 managers and a manual subscribe/notify pattern
  (`apps/web/src/core/`). A 78.5k★ product keeps its whole timeline core vanilla.
- **Data model** (`timeline/types.ts`): scenes → `SceneTracks {overlay[], main, audio[]}`
  → typed elements extending `BaseTimelineElement {id, name, startTime, duration,
  trimStart, trimEnd, sourceDuration?, params}`, media by `mediaId` string indirection.
  Fully JSON-serializable (3 small leaks: AudioBuffer, Dates, the MediaTime brand).
- **Integer-tick time** (`wasm/media-time.ts`): all timeline math in branded integer
  ticks (mirrors a Rust i64) — no float drift across cuts. Conversions currently call
  their `opencut-wasm` npm package; reimplementable as ~200 lines of pure JS.
- **Preview** (`services/renderer/` + `services/video-cache/`): NOT stacked <video>
  tags, NOT ffmpeg.wasm. Three stages: declarative render-node tree from tracks →
  per-frame resolve (playhead → per-clip source time) pulling decoded frames from a
  **WebCodecs frame cache** built on the MIT `mediabunny` library → GPU compositor
  (their Rust/wgpu wasm) drawing into a canvas.
- **Clock discipline** (`core/managers/playback-manager.ts`): a wall-clock rAF playhead
  quantized to frame ticks is the master; video frames AND Web-Audio-scheduled audio
  slave to it. Media elements never own time. This is the exact pattern our canvas
  dock already approximates for audio (drift-corrected layering) — and the right one
  for cut-list preview.
- **Export** (`services/renderer/scene-exporter.ts`, ~170 lines): the same renderer
  driven in a deterministic frame-stepping loop, encoded client-side via
  mediabunny/WebCodecs (mp4-avc/aac or webm-vp9). Zero server cost.
- **Storage**: media binaries in OPFS, metadata/projects in IndexedDB behind a generic
  `StorageAdapter<T>` + versioned migrations; the server DB holds only auth/feedback.
- **Feature modules**: subtitles (real SRT/ASS parsers, pure TS), canvas text engine
  (measure/wrap/draw incl. background pills; OffscreenCanvas-safe — headless-capable),
  production-grade keyframe engine (bezier handles/tangents, pure math), constant-rate
  retime (clip↔source mapping pure math; NO speed ramps exist), masks (large, tested,
  but interaction-bound), CSS-gradient parser+painter (already MIT-vendored style),
  fully-local in-browser transcription (on-device ASR via a JS inference runtime).

## Absorption menu (ranked; audio layer untouched by all of it)

### Tier 1 — port now, pure math/logic, direct fit (low risk)
1. **Plan-preview player pattern** (playback-manager + video-cache): a vanilla-JS
   player that walks a `RecomposePlan`'s slots (`seg_in_s`, `dur_s`, `t_out`) against
   one muted source `<video>` (or a WebCodecs frame cache where supported) with the
   music slaved to the master clock — instant preview of a plan BEFORE paying for a
   server render. Mounts as a standalone page or an experimental-console view; the
   agentic_audio dock is not touched.
2. **Read-only timeline strip** (their track/element layout math + our beats): slot
   blocks over the MusicSheet beat grid — makes a plan legible for the first time.
   Lowest-risk piece of all; needs only a plan+sheet JSON route.
3. **Integer-tick time discipline** (pattern): adopt for all new visual-timeline math
   (we already met float-drift pain in the lyric-timestamp work).
4. **Small pure vendors with attribution:** snapping resolver (~22 lines + point
   builders), ripple module (4 files, immutable, zero DOM), rational FrameRate utils
   (NTSC 29.97/23.976 family, tested), gradient parser+painter, export options enums.

### Tier 2 — port when the interactive editor becomes a priority (medium)
5. **Timeline data model + Command-pattern undo/redo** (`timeline/types.ts`,
   `commands/`): the schema and a ~180-line undo core (incl. the pragmatic
   before/after `TracksSnapshotCommand` escape hatch). Port = stub MediaTime with
   pure ints, trim animation/effect/mask type imports, drop the singleton (inject).
6. **Trim-adjust interaction** (their controller pattern: pure compute + DOM-event
   controllers with preview/commit callbacks): drag slot edges with beat-snapping →
   `pick_overrides`/locks re-plan → targeted re-render. Needs a small validated
   server endpoint (slot spans must stay inside legal windows — our planner already
   enforces the rules; expose them).
7. **Canvas text engine + subtitle chunker**: branded end-cards and transcript
   captions for ad cuts; OffscreenCanvas-safe so it could even run server-side later.
8. **mediabunny-based client export + media probe**: browser-side mp4 encode and an
   ffprobe-substitute for uploads (keeps big files off the wire pre-validation).

### Tier 3 — patterns/watchlist only
9. **Keyframe engine, retime mapping model, masks definitions registry** — absorb as
   specs/math when needed; skip their interaction layers.
10. **Rust wgpu effect/compositor crates** (classic has a WORKING 8-crate workspace,
    MIT): only if we ever want GPU effects; medium coupling to their WGSL registry.
11. **Rewrite repo's editor API / MCP / headless contracts**: not actionable now
    (unreleased); revisit when published.

### Do NOT absorb
- The `opencut-wasm` compiled npm binary (unpatchable frozen artifact; reimplement
  the ~200 lines of tick math instead).
- React UI components / a prebuilt bundle (violates the no-build repo constraint).
- In-browser transcription worker (we transcribe server-side; large model downloads
  to end users for no reason).
- Their local-first OPFS/IndexedDB strategy wholesale (conflicts with our
  server-rendered, durable-jobs platform; the `StorageAdapter<T>` interface itself
  is a fine pattern).

## Addendum (owner Q, 2026-07-25): "isn't OpenCut famous for NLP-driven clipping?"

Checked directly against both repos. **No such feature exists in the code today.**
- `opencut-classic` (the working editor) is a manual CapCut-style editor — README
  says "most basic CapCut features"; all 6 subsystem readers + a README pass found
  zero AI/NLP/auto-clip code (its `transcription/` is captions-only).
- The fame is the **agent-drives-the-editor story**: the rewrite's README lists
  "MCP server (for AI agents)", an Editor API, headless/batch mode, scripting, and
  a plugin-first architecture — **all explicitly under "What's coming" (planned)**;
  `apps/` has no mcp/agent/cli code yet.
- Muddying the water: several unrelated products share the name (OpenCutAI,
  opencut.video, an OpenCut-AI fork, an MWM mobile app) and DO advertise
  prompt-driven clipping — part of the reputation belongs to them.

Strategic read for Edenn: the NLP→cuts capability OpenCut is famous for *promising*
is what our recompose engine already does (LLM plan over understood footage, hard
no-reuse/beat rules, rendered variants). They have the editor substrate we lack;
we have the planner they haven't built. Watchlist both directions once their API
ships: (a) Edenn's agent driving OpenCut via MCP as an export/polish surface,
(b) emitting a RecomposePlan as an OpenCut project for manual touch-up.

## Suggested first slice (when approved)

"**Plan preview strip**" into the experimental console (library_api) or a standalone
page: GET /plan already exists; add the sheet/beats to it; render Tier-1 items 1+2
(player + strip) as one vanilla file, using the source `<video>`-seek fallback first
(WebCodecs cache as an enhancement). Zero contact with agentic_audio; zero server
render cost per iteration. Attribution block for vendored snippets at file top.
