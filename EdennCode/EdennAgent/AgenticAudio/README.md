# agentic_audio

A chat-first **AI audio director**: the user uploads a source video and converses
with an agent that analyzes it, proposes music directions, generates candidates,
adds an optional voice-over, and lets them iterate (re-mix, restyle, extend,
branch) — forever (a session never "ends"; see `stages.py`). The served
**Director Console** has two views over the same session snapshot: the waterfall
**chat** and an infinite lineage **canvas** (`frontend/js/canvas-mode.js`). All
heavy (paid) generation rides the existing `async_pipeline_v2` durable job/worker
system; the agent itself only decides, gates, enqueues, and hydrates results back
onto candidate cards.

## Component map

```
agentic_audio/
  models.py        shared models: DB dataclasses, API request/response, the TOOL_SPECS
                   table (single source of truth for the 14 tools' identity +
                   heavy/generation flags), the decision schema
  events.py        EventType — the one source for the 26 wire event names (FE + BE)
  stages.py        Stage enum + StageMachine (declared transitions, soft-validated);
                   top-level because BOTH tools and the agent use it
  planner.py       thin back-compat facade over AgenticAudioAgent

  api/             HTTP/WS surface — router + console serving, gated auth, event serialization
  agent/           the brain — orchestrator (per-session turn locks), reasoning loop,
                   tool dispatcher, SYSTEM_PROMPT, session-I/O helpers
  tools/           the Tool ABC + registry + the 10 agent-facing tools, and the
                   media/job toolkit (analysis, prompt fusion, enqueue, hydrate, ffmpeg)
  domain/          first-class entities: User + typed Artifacts/CandidateGraph over state_json
  persistence/     AgenticAudioRepository (Postgres, 13 tables rooted at a session) +
                   migrations/ 001..007 (replayed idempotently on first use)
  frontend/        the served "Director Console" (vanilla JS, no build): chat + canvas
                   views, offline mock backend
  design/          design-time only: the zero-infra devserver + UX evaluation notes
  Testing/         pytest suite + the e2e/ agent E2E foundation (drive the real loop)
```

The redesign's **User / Agent / Artifacts** separation maps to `domain/` (User +
Artifacts) and `agent/` + `tools/` (the Agent: orchestration, reasoning loop, stages,
tools). Each package has its own README (see the doc index below).

## Core invariant — cost safety

The three `is_generation` tools in `models.py TOOL_SPECS` spend real money, and
each implements (or omits) its own gate — there is no generic dispatcher-level
check over the generation tools:

- `generate_candidates` is **structurally gated on explicit user approval**: it
  raises `ApprovalRequiredError` unless `state["approved_direction"]` was set by
  a deterministic `/choices` proposal pick or an `approve_direction` tool call
  tied to a shown direction (`tools/impls.py`).
- `generate_voiceover` is structurally gated behind a script drafted and shown
  via `propose_script` (its own `ApprovalRequiredError`); it does **not** check
  `approved_direction`.
- `edit_audio` has **no structural spend gate in code**: it only requires a
  resolvable parent take (so approval is transitive from the original
  generation) and relies on the SYSTEM_PROMPT approval/pressure rules.

Coercion ("just spend now") is refused at the prompt level (the PRESSURE rule in
`agent/prompts.py`); the agent never spends proactively. For the tools that do
raise `ApprovalRequiredError`, the loop catches it and degrades to an approval
question instead of erroring (`agent/loop.py`). The deterministic proposal path
additionally refuses a **second** proposal approval while any usable (non-failed)
take exists (`ApprovalRequiredError` in `agent/agent.py` — regenerating a
direction would double-spend and replace the existing takes; branch instead).

## Behavior notes (current)

- **Video-grounded prompts** — `tools/media.py fuse_music_style_prompt` fuses the
  approved direction's prose with the bootstrap observation (mood / tempo /
  instruments) and an evenly-sampled timed scene arc (≤8 scenes spanning the whole
  video; reads both the real analysis `start_timestamp`/`end_timestamp`/
  `visual_summary` shape and the older `start_s`/`end_s`/`label` shape). Applied
  on base generation AND `edit_audio` regenerate/extend; `creative_edit` keeps a
  lightweight control prompt and gets a compact mood + tempo grounding only (the
  parent audio is the main guide).
- **Generation watchdog** — `hydrate_candidate_results` flags a queued/processing
  job older than `AGENTIC_AUDIO_GEN_STALL_SECONDS` (default 180) with
  `stalled_seconds` on the candidate card; read-only (flag, never auto-fail).
- **Turn serialization** — per-session `asyncio.Lock` in `agent/agent.py` so a WS
  turn racing a REST turn (or a second tab) can't lose `state_json` updates
  (in-process only; multi-worker needs optimistic concurrency).
- **Liveness** — the loop emits a deterministic `agent.reasoning` beat at turn
  start (`agent/loop.py`) so the thinking trail has content while the first LLM
  step is still running.
- **Canvas context refs** — messages may carry `payload.context_refs`
  (@-referenced takes from the canvas, deduped and capped at 8); `agent/loop.py
  _context_ref_note` folds them into the model's context so "make the drop
  harder" targets the referenced take without a "which version?" ask.
- **Watch layering** — `analyze_video` snapshots the source video (URL / poster /
  duration) into `state["source_video"]` so the console can play a take's audio
  under the muted picture without a per-take render (`tools/impls.py`).
- **Console serving** — `api/router.py` serves `frontend/` under
  `/api/v2/agentic/audio/app` with an explicit `FRONTEND_ASSETS` allow-list
  (`index.html`, `styles.css`, `mock-backend.js`, `js/app.js`,
  `js/canvas-mode.js` — never resolves arbitrary paths).

## Public entry points (unchanged across the restructure)

- `agentic_audio.api.mount_agentic_audio_router(app, context)` — production mount
  (gated by `AGENTIC_AUDIO_ENABLED`; sets `require_intent_gate=True`). Called from
  `EdennCode/Deployment/api.py`.
- `agentic_audio.api.create_agentic_audio_router(...)` — build the router (DI for
  tests: repository / queue / tools / planner / llm_client are all injectable).

REST + WS live under `/api/v2/agentic/audio` (`/sessions`, `/sessions/{id}`,
`/sessions/{id}/messages`, `/sessions/{id}/choices`, `/sessions/{id}/ws`); see
`FRONTEND_INTEGRATION.md` for the full contract.

## Quickstart

Launch configs exist in `.agent/launch.json` (`agentic-audio-console` :5599,
`agentic-audio-devserver` :8800, `agentic-audio-devserver-realmusic` :8800,
`agentic-audio-prodapi` :8900 — the prodapi config also pins
`ASYNC_V2_QUEUE_NAMESPACE` so smoke jobs stay off shared queues). Equivalent
commands:

```bash
# deterministic suite (hermetic, fast — deselect the env-hungry tests)
.venv/bin/python -m pytest EdennCode/EdennAgent/AgenticAudio/Testing -q \
  -k "not real_provider_postgres and not e2e_video_upload and not real_llm_eval_safety_gate"

# runtime agentic E2E — "watch the session" against the real model
.venv/bin/python -m EdennCode.EdennAgent.AgenticAudio.Testing.e2e.run_e2e

# devserver: REAL router + agent over in-memory fakes, zero external infra
# (offline "dev director" LLM unless AZURE_*/AGENTIC_AUDIO_AZURE_* keys are set;
#  real music defaults ON when provider keys exist — EDENN_DEV_REAL_MUSIC=0 opts out)
PYTHONPATH=. EDENN_DEV_REAL_MUSIC=0 .venv/bin/python \
  EdennCode/EdennAgent/AgenticAudio/design/devserver.py
# open http://127.0.0.1:8800/?backend=real

# the console fully offline (in-browser mock backend): static-serve frontend/
python3 -m http.server 5599 --directory EdennCode/EdennAgent/AgenticAudio/frontend

# local PROD mount (real app, Postgres + async v2 required)
PYTHONPATH=. AGENTIC_AUDIO_ENABLED=1 ASYNC_PIPELINE_V2_ENABLED=1 \
  .venv/bin/python -m uvicorn EdennCode.Deployment.api:app --port 8900
# console at http://127.0.0.1:8900/api/v2/agentic/audio/app
```

## Feature flags / env

- `AGENTIC_AUDIO_ENABLED` — mount the router.
- `AGENTIC_AUDIO_AZURE_*` (fallback `AZURE_*`) — the agent's dedicated reasoning
  LLM (`ENDPOINT` / `MODEL` / `API_KEY` / `API_VERSION` / `API_MODE` /
  `API_TIMEOUT` suffixes; see `agent/agent.py build_agentic_audio_agent_client`).
- `AGENTIC_AUDIO_REQUIRE_AUTH` (+ `AGENTIC_AUDIO_API_KEYS` token:user pairs) —
  enable auth (default OFF; see `api/auth.py`).
- `AGENTIC_AUDIO_MAX_STEPS_PER_TURN` (default 4) / `AGENTIC_AUDIO_MAX_CANDIDATES`
  (default 3).
- `AGENTIC_AUDIO_GEN_STALL_SECONDS` (default 180) — the stalled-take watchdog.
- `EDENN_DEV_REAL_MUSIC`, `EDENN_DEV_REAL_ANALYZE`, `EDENN_DEV_REAL_VOICEOVER`,
  `EDENN_DEV_FORCE_DIRECTOR` (force the offline dev director even with keys),
  `EDENN_DEV_PORT`, `EDENN_DEV_HOST` — devserver-only toggles
  (`design/devserver.py`).

## Doc index

| Doc | What it covers |
|---|---|
| `ARCHITECTURE.md` | The current architecture: invariants, runtime flow, tools, state, async-v2 integration |
| `DEPLOYMENT.md` | The deployment runbook: env flags, worker compatibility matrix, image build/rollout, smoke checklist |
| `DESIGN_LLM_AGENT.md` | The LLM-driven design rationale, phasing, provider capability map |
| `FRONTEND_INTEGRATION.md` | The FE↔BE contract: API surface, every event, the card interaction model |
| `ITERATION_PLAN.md` | The E2E-ready-on-`staging-app` iteration plan and current state |
| `EVALUATION_PLAN.md` | The multi-turn evaluation strategy (harness tiers, gates) |
| `api/README.md` · `agent/README.md` · `tools/README.md` · `domain/README.md` · `persistence/README.md` | Per-package internals |
| `frontend/README.md` | The Director Console: files, transport (mock vs real), contract |
| `frontend/CANVAS_MODE_PLAN.md` | The canvas-mode build plan (lineage canvas alongside chat) |
| `Testing/README.md` | The test tiers (free / LLM / real-music), suites, and run commands |
| `design/README.md` · `design/UX_EVALUATION.md` | Design-time artifacts: the devserver, personas, UX findings |
