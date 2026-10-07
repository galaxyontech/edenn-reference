# EdennAgent — Edenn's agent-driven creative verticals

This folder is the home for Edenn's **agentic capability**: products where an
agent understands existing assets, proposes an explicit creative plan, spends
generation/compute only behind user approval, executes through durable jobs,
and preserves full creative lineage. It exists so new verticals share one set
of agentic primitives instead of each inventing session/plan/approve/lineage
machinery from scratch.

Consolidated 2026-08-17: the former `Deployment/agentic/` platform tree and
`Deployment/agentic_audio/` studio merged here as `EdennCode/EdennAgent/` with
domain-named first-level modules (mechanical move, path changes only).

## Structure

```
EdennAgent/
├── README.md          ← this charter
├── core/              ← shared agentic machinery (extracted on demand, see below)
├── AgenticAudio/       ← the agentic audio (was agentic_audio): reference
│                        implementation — agent loop, tools, stages,
│                        persistence, api, frontend
├── Recompose/         ← music-led video recomposition vertical (MVP: MVP_PLAN.md)
├── AssetLibrary/      ← Asset Understanding Layer: refs, store (Postgres +
│                        in-memory twin), ingest, tree view, lineage recording
│                        (design: UNDERSTANDING_LAYER.md; DDL: AssetLibrary/migrations/)
├── AdsAdapters/       ← channel adapters: TikTok Business API (activates via
│                        TIKTOK_ADS_ACCESS_TOKEN / TIKTOK_ADS_ADVERTISER_ID) +
│                        sandbox twin on the same contract; outcome ingestion
│                        + component attribution (design: CREATIVE_LOOP.md)
├── AssetLibraryApi/   ← read-only Library API + single-file frontend
│                        (Board / connections / Performance over the AssetLibrary store)
├── Creation/          ← creation service (treatments, preview, bundle)
├── Campaign/          ← campaign console (devserver :5601; mounts AgenticAudio at /studio)
├── GoldenSets/        ← golden fixtures
├── AgentDesign/       ← design docs + HTML wireframes
└── AgentTesting/      ← cross-package tests (AUL round-trip + ads loop; hermetic)
```

The loop these packages form is validated end-to-end against the real backend
(Postgres store, LLM plans, real renders, channel adapter, 7 synthetic outcome
days, attribution, live UI): golden ad → `AssetLibrary.ingest` → tree rebuilt
FROM the store → 3 recompose variants → `AssetLibrary.lineage.record_variant` →
`AdsAdapters.outcomes.publish_variant` via `pick_adapter()` → `pull_outcomes` →
`component_attribution` → `AssetLibraryApi` serves it all. Runner scripts live
in the session scratchpad (`validate_loop.py`, `serve_library.py`); the
hermetic equivalent runs in CI via `AgentTesting/test_aul_and_ads.py`.

`AgenticAudio/` is the reference implementation of the shared machinery.

## What counts as `core/` (extraction candidates)

Machinery that is vertical-agnostic in `AgenticAudio/` today:

- **Reasoning loop** — one bounded LLM turn per step, beats as model output,
  turn-start beat, context refs (`AgenticAudio/agent/loop.py`).
- **Tool layer** — Tool ABC + registry + dispatcher, ToolContext helpers,
  spend-gating via `ApprovalRequiredError` (`AgenticAudio/tools/base.py`,
  `agent/dispatcher.py`).
- **Stage machine + per-session turn locks** (`AgenticAudio/stages.py`,
  `agent/agent.py`).
- **Session persistence + snapshot contract** — sessions/messages/tool_calls/
  choices, `state_json` snapshots, resume (`AgenticAudio/persistence/`).
- **Event vocabulary + WS streaming contract** — live beats stream on the
  initiating socket; REST turns reconcile via snapshots
  (`AgenticAudio/events.py`, `api/router.py`).
- **Auth** — token→principal map, session ownership (`AgenticAudio/api/auth.py`).
- **Job hydration + watchdog** — linked-job status → domain object fields,
  `stalled_seconds` (`AGENTIC_AUDIO_GEN_STALL_SECONDS` pattern).
- **Console scaffolding** — snapshot-driven reconcile, optimistic UI +
  thinking trail, canvas lineage view, dock player, asset allow-list serving
  (`AgenticAudio/frontend/`).

## Migration rules (strangler, not big-bang)

1. **New verticals land here** (`Recompose/` first). Plans and docs land here
   from day one.
2. **Extract into `core/` only when a second vertical concretely needs the
   piece** — copy-then-converge is acceptable for one iteration; a shared
   `core/` module is the end state, an import from `AgenticAudio` is not.
3. ~~`agentic_audio` does not move until after its staging-app deploy
   milestone~~ — superseded: the studio moved here as `AgenticAudio/` in the
   2026-08-17 repo restructure (dedicated mechanical commit, path changes
   only, no behavior changes; deployed module contracts untouched).
4. Anything extracted must keep the AgenticAudio test suite green — those
   tests are the contract for the shared machinery.

## Verticals

| Vertical | Status | Plan |
|---|---|---|
| `AgenticAudio/` | Built; local prod-mount verified; pre-deploy | `AgenticAudio/ITERATION_PLAN.md` |
| `Recompose/` | Planning — W0 spike next | `Recompose/MVP_PLAN.md` |

Product context for the platform direction: the Edenn PRD v0.1
(“Existing-Asset Creative Transformation Platform”) — assets in, understood,
planned, selectively transformed, lineage out. `Recompose/MVP_PLAN.md` §0
records how the PRD maps onto code that already exists in this repo.
