# Agentic Audio — Current Architecture

A chat-first agent that turns a source video into the right audio, conversationally
and iteratively. An LLM decides which pipeline tool to run each step (no fixed
linear flow). Music is the core layer; voice-over (script → TTS → compose) is
shipped; SFX is deliberately not.

See `DESIGN_LLM_AGENT.md` for the rationale, phasing, and provider capability map.

## Key invariants (read first)

1. **The agent's own ffmpeg steps mux onto the ORIGINAL source video.**
   `adjust_remix` and `compose_mix` always overlay audio onto the original
   full-quality source video resolved from the session's source artifact, and
   `finalize` prefers a composed `mix.video_url` (built on the original) when a
   `compose_mix` has run (`tools/impls.py:252`). A straight candidate finalize,
   however, delivers the video-music pipeline's own render: candidate jobs are
   enqueued with `compression_flag=True` / `compression_max_height=1280`
   (`tools/media.py:812`), and the monolith worker runs the whole workflow —
   including its final music overlay — on that compressed working proxy;
   `compose_final_mix` just returns the linked job's `video_url` unchanged
   (`tools/media.py:363`).
2. **No cost-incurring generation without explicit user approval.** Enforced
   structurally where possible: `generate_candidates` raises
   `ApprovalRequiredError` until `state["approved_direction"]` is set — by the
   deterministic `/choices` proposal pick (a human action, high-assurance) or the
   auditable `approve_direction` tool; `generate_voiceover` raises the same until
   a script was drafted via `propose_script`. The reasoning loop catches
   `ApprovalRequiredError` and degrades to a re-ask instead of failing the turn
   (`agent/loop.py:205`). Free tools (`analyze_video`, `adjust_remix`,
   `compose_mix`, `finalize`, `set_production_plan`, `propose_script`,
   `approve_direction`) may run proactively. Note: against an adversarial
   *model*, only the `/choices` human gate is hard; the free-text path is
   auditable but model-mediated, and `edit_audio` has no per-edit state gate —
   it structurally requires an existing candidate (which itself required an
   approved direction), with per-edit consent enforced by the system prompt.
3. **The session never "ends".** After finalize the session stays open and keeps
   accepting edit turns (iteration model). The stage machine encodes this
   (`COMPLETED` legally transitions back into generating/composing).

## Module layout — `EdennCode/EdennAgent/AgenticAudio/`

| Path | Responsibility |
|---|---|
| `api/router.py` | FastAPI router: REST (`/sessions`, `/messages`, `/choices`) + WS + static console; `AGENTIC_AUDIO_ENABLED` mount gate; dependency wiring |
| `api/auth.py` | Principal resolution + session-owner authorization (header or `?token=` for WS) |
| `api/serialization.py` | Event/snapshot payload serialization for WS + REST |
| `agent/agent.py` | `AgenticAudioAgent` orchestrator: entry points, per-session turn locks, deterministic intent gate + `/choices` routing |
| `agent/loop.py` | `ReasoningLoop` — one bounded LLM turn: context build, decision, beats, dispatch, turn/memory persistence |
| `agent/dispatcher.py` | `ToolDispatcher` — the one place a tool name becomes a running tool (`ToolContext` construction) |
| `agent/session_io.py` | Shared helpers: `append_message`, `make_event`, `reasoning_event`, live `push` to the WS sink |
| `agent/prompts.py` | `SYSTEM_PROMPT` (director persona, tool contracts, approval + anti-pressure rules) |
| `tools/base.py` | `Tool` ABC, `ToolRegistry`, `ToolContext`, `ToolResult`, `ApprovalRequiredError`, `ToolError` |
| `tools/impls.py` | The ten concrete `Tool` subclasses + `build_tool_registry()` |
| `tools/media.py` | `AgenticAudioTools` — analysis, prompt fusion, ffmpeg remux/compose, async-job enqueue, result hydration + watchdog |
| `models.py` | Dataclasses, Pydantic models, `TOOL_SPECS` (single source of tool identity), `AGENT_DECISION_SCHEMA`, provider maps, voice catalog |
| `stages.py` | `Stage` enum + `StageMachine` transition graph; the single `transition()` choke point for phase writes |
| `events.py` | `EventType` — the single source of truth for the wire event vocabulary |
| `persistence/repositories.py` | Postgres persistence: sessions, messages, tool_calls, choices, snapshot building |
| `domain/` | Typed read layer over `state_json`: `SessionState`, `CandidateGraph`, `User` (no new tables; dicts stay canonical) |
| `planner.py` | `AgenticAudioPlanner` — thin back-compat facade delegating to the agent |
| `frontend/` | The single-page console (chat waterfall + canvas mode), served same-origin by the router |

## Runtime flow

```
            REST: POST /sessions  /sessions/{id}/messages  /sessions/{id}/choices
            WS:   /sessions/{id}/ws  (streams events LIVE via the emit sink) ───┐
                                                                                ▼
   bootstrap_session / handle_user_message / handle_choice ── AgenticAudioAgent
     (message + choice turns run inside the per-session _turn_lock)  │
        ┌────────────────── ReasoningLoop.run (<= max_steps_per_turn) ─────────────────────┐
        │  turn-start beat (deterministic) ─► messages = [SYSTEM_PROMPT, state_summary,     │
        │    conversation(+context_ref notes), scratch]                                     │
        │  decision = llm.complete_messages(messages, json_schema=AGENT_DECISION_SCHEMA)    │
        │  decision = { thought, intent, memory_update?, assistant_message, action }        │
        │  emit agent.reasoning ─► dispatch action ─► (light tool: feed result back into    │
        │    scratch, continue | heavy tool / propose / clarify / ask / noop: end turn)     │
        └──────────────────────────────────────────┬────────────────────────────────────────┘
                                                    │ records turn + folds memory at turn end
                                                    ▼
                          ToolDispatcher ─► Tool.run(ToolContext) ─► AgenticAudioTools
   light / in-process ──►  analyze_video  adjust_remix  compose_mix  finalize
                           set_production_plan  propose_script  approve_direction
   heavy / async ───────►  generate_candidates  edit_audio  generate_voiceover
                                          │ enqueue async_pipeline_v2 jobs
                                          ▼
   ┌── video-music-pipeline ──────────┐ ┌─ audio-creative-edit-pipeline ─┐ ┌─ voiceover-pipeline ─┐
   │ VideoMusicMonolithWorker         │ │ AudioCreativeEditWorker        │ │ VoiceoverWorker      │
   │ analyze->generate->match->remix, │ │ restyle audio -> overlay onto  │ │ TTS the approved     │
   │ honors payload music_style_prompt│ │ source video                   │ │ script (preset voice)│
   │ result: audio_url, video_url,    │ │ result: audio_url, video_url   │ │ result: audio_url    │
   │  provider_audio_id/task_id       │ │                                │ │                      │
   └──────────────────────────────────┘ └────────────────────────────────┘ └──────────────────────┘
                                          │ hydrate_candidate_results / hydrate_voiceover_layer
                                          ▼             read result_json onto cards (+ watchdog)
                               session snapshot ─► WS / REST response
```

## The reasoning loop (`agent/loop.py`)

`ReasoningLoop.run` is one **bounded** turn: at most `max_steps_per_turn` LLM
calls (default 4, `AGENTIC_AUDIO_MAX_STEPS_PER_TURN`). Each step re-reads the
session, builds `[SYSTEM_PROMPT, state summary, conversation, scratch]`, and asks
for one structured-JSON decision against `AGENT_DECISION_SCHEMA`.

- **Beats are model output.** The live `agent.reasoning` event carries the
  decision's own `thought` (truncated to 400 chars in
  `agent/session_io.py:reasoning_event`) plus a status label derived from
  `TOOL_SPECS` / the action type — it can only land after that step's LLM call
  returns. So the loop also emits a **deterministic turn-start beat**
  ("Reading your direction" + a quote of the first 160 chars of the user
  message) before the first LLM call, so the thinking trail has content *while*
  step 1 runs (`agent/loop.py:85`). Message-driven turns only; the intent-gate
  bootstrap emits its own deterministic beat instead.
- **Canvas context refs.** `_build_messages` renders a user message's
  `payload_json.context_refs` (the canvas @-reference: candidate ids or
  `__source__`) into the LLM's view of that turn via `_context_ref_note`
  (`agent/loop.py:290`) — deduped, capped at 8, unresolvable ids dropped — so
  "make the drop harder" unambiguously targets the referenced take. The persisted
  message stays clean; only the model's context is annotated.
- **Light vs heavy.** A light tool's raw result is appended to the scratchpad as
  a `[tool_result …]` message (capped at 4000 chars) and the loop continues; a
  tool in `AGENT_HEAVY_TOOLS` ends the turn (the async job's result hydrates
  later). `propose`, `clarify`, `ask`, and `noop` also end the turn.
- **Approval degradation.** If a paid tool raises `ApprovalRequiredError`, the
  loop does not 400: it records the step as an `approval_prompt` with the
  blocked tool's name (the tool did not run), replies with a deterministic
  confirm-first message, and ends the turn.
- **Redundant-modality guard.** If `production_plan` is already set and the model
  tries to `clarify` the modality again, the whole step is skipped and a
  corrective system note is pushed into scratch (`_is_redundant_modality_clarify`).
- **Turn record + memory.** At turn end (user-driven turns only) the loop appends
  a bounded `turns[]` entry (last 100 kept) and folds `memory_update`s plus
  deterministic signals (last mix volume, preferred modelspec) into
  `state["memory"]`; the last `AGENT_MEMORY_RECENT_TURNS` (8) turns/intents feed
  back into the next turn's state summary.
- **Failure containment.** A failed LLM call logs, apologizes in-conversation,
  and ends the turn — it never crashes the transport.

## Turn serialization (`agent/agent.py`)

Turns read-modify-write the whole `state_json` blob, so
`handle_user_message` and `handle_choice` each run inside a **per-session
`asyncio.Lock`** (`_turn_lock`, `agent/agent.py:255`). Without it, a WS turn
racing a REST-fallback turn (or a second tab) silently loses state via
lost-update. `bootstrap_session` is not locked — it runs once inside
`POST /sessions`, before the session id is returned to any client. This locking
is in-process only — a multi-worker deployment needs optimistic concurrency in
the repository.

## Deterministic paths (no LLM round-trip)

**Intent gate.** The production mount (`mount_agentic_audio_router`) defaults
`require_intent_gate=True`: a fresh session's bootstrap runs `analyze_video`,
greets, and asks "What are you adding today?" (full audio / music only /
voice-over only) as a clarify card — no LLM step, so the modality is captured
explicitly every time. The gate is idempotent (skipped when an `observation`
already exists). The answer is recorded via `set_production_plan`
(voice-over-only passes `force_music=False`), then the loop runs on the answer.

**`/choices` routing** (`AgenticAudioChoiceRequest.choice_type`):

| choice_type | What it does |
|---|---|
| `proposal` | Sets `approved_direction` + `approved_proposal_id`, dispatches `generate_candidates`, and acknowledges the spend deterministically ("this spends generation credits… takes a few minutes") |
| `candidate` / `compose` | Dispatches `finalize`; the ack says what's next when the plan still owes a voice-over |
| `clarification` | Records the answer, clears `pending_clarification`, feeds the chosen option back in as the user's turn (intent-gate answers also set the production plan first) |
| `mix` | Slider values → `compose_mix` when a voice-over has rendered, else the cheap `adjust_remix` |
| `variation` | Branches a new take via `edit_audio` (default `regenerate`; honors `prompt` / `extend_seconds` from the payload) |
| `voiceover` | Optionally re-drafts the script (`propose_script`, satisfying the gate), then `generate_voiceover` |

**Proposal re-approve guard.** `generate_candidates` REPLACES
`state["candidates"]`, so a second proposal approval would both double-spend and
wipe every existing take/branch. The proposal path therefore raises
`ApprovalRequiredError` while any **usable** candidate exists (status not
`failed`/`error`) — all-failed sessions may regenerate (`agent/agent.py:320`).
The chat UI disables consumed proposal cards — they stay visible in the thread
with their pick buttons disabled (`frontend/js/app.js:546`) — so re-approval is
impossible from chat; this guard closes the same door for the canvas/REST paths
(surfaces as a 400 on REST, a crafted `error` event on WS).

## Stage machine (`stages.py`)

The bare `phase` string is now a declared `Stage` enum with a canonical
transition graph (`LEGAL_TRANSITIONS`), and **every phase write goes through one
`transition()` function** (tools use `ctx.transition_to`). Stages:

`created → observing → proposing → awaiting_plan_choice → generating_candidates
→ awaiting_candidate_choice → composing → completed` (+ `failed`).

Validation is currently **soft** (`StageMachine.validate` logs an undeclared
transition rather than raising) because a session is intentionally never-ending:
`completed` legally re-enters `generating_candidates` / `composing` /
`awaiting_candidate_choice` when the user keeps iterating. `Stage` values are
byte-identical to the old phase strings, so persisted rows and the wire contract
are unchanged.

## The decision schema (every step)

`{ thought, intent (required, controlled enum), memory_update?, assistant_message, action }`

- `action.type` ∈ `call_tool | propose | ask | clarify | noop`
- `intent` ∈ `analyze, request_proposals, approve_direction, adjust_mix, restyle,
  lengthen, new_variation, select_final, compare, ask_question, revert,
  plan_audio, add_voiceover, other`
- `memory_update?` — optional durable preferences/direction the model chooses to remember
- `action.clarification` — when `type == clarify`: `{question, options:[{id,label,hint}]}`
- `action.tool_name` enum, the heavy/generation classifications, and the whole
  of `action.tool_args` are **derived from `models.TOOL_SPECS`** — adding a tool
  there is enough. `tool_args` is flat rather than a per-tool union because the
  discriminator is its sibling; the per-tool mapping is generated into the
  prompt from the same table.

Proposals are hard-capped at 2 directions in `_store_proposals`, regardless of
what the model returns.

## Tools

| Tool | Kind | Cost gate | What it does |
|---|---|---|---|
| `analyze_video` | light (in-proc) | free | Understanding pipeline → `observation`; also snapshots `state["source_video"]` (url/poster/duration) for the canvas watch dock |
| `approve_direction` | light | free | Records explicit user approval (`approved_direction`) that unlocks generation; never spends |
| `generate_candidates` | heavy (job) | **gated: needs `approved_direction`** | Enqueues `video_music` monolith jobs (with the fused style prompt) → candidate cards |
| `adjust_remix` | light (in-proc ffmpeg) | free | Re-mux existing music at a new volume (`music_volume`) and keep/drop the original audio (`preserve_original_audio`) onto the **original** video; falls back to the most recent rendered candidate when nothing is selected. No ducking knob — `duck_gain_db` belongs only to `compose_mix` |
| `edit_audio` | heavy (job) | prompt-gated (needs an existing candidate) | Branches a versioned candidate: `regenerate` / `extend` / `creative_edit` |
| `finalize` | light | free | Marks the selected candidate (or the composed voice-over-only mix) as the final deliverable |
| `set_production_plan` | light | free | Records `{mode, layers}` + scaffolds `layers`; strips SFX; idempotent (re-asserting the same plan is a no-op) |
| `propose_script` | light | free | Records a VO script draft + tone, shows preset voice options (`voiceover.script` card) |
| `generate_voiceover` | heavy (job) | **gated: needs a drafted script** | TTS via `voiceover-pipeline` → `layers.voiceover.audio_url` |
| `compose_mix` | light (in-proc ffmpeg) | free | Layers music + VO (or VO alone) onto the **original** video; knobs merge into `state["mix"]` |

`edit_audio` routing: `creative_edit` → dedicated `audio_creative_edit` job
(audio-to-audio-capable providers only, `models.CREATIVE_EDIT_PROVIDERS`; a
basic-tier or un-rendered parent falls back to `regenerate` with
`requested_edit_kind` recorded); `extend` routes to the provider's native
in-place extend when it supports one and a `provider_audio_id` is held
(`extend_mode: native`), else `regenerate_fallback`.

## Grounding generation in the video: `fuse_music_style_prompt`

The proposal prose alone reads like a generic score spec — the pipeline treats a
detailed user prompt as an explicit style override and skips its own scene
orchestration, so the result doesn't track the video.
`AgenticAudioTools.fuse_music_style_prompt` (`tools/media.py:203`) fuses:

1. the proposal's creative direction (dominant, capped at 700 chars),
2. the bootstrap observation's `music_prompt` facts — footage mood, tempo (BPM),
   up to 10 instruments,
3. a **timed scene arc**: up to 8 scenes sampled **evenly across the whole
   video** (not the first 8), each as `start–end s + label`, plus total length.

It reads both scene shapes — the real analysis emits
`start_timestamp`/`end_timestamp`/`visual_summary`; tests and older shapes use
`start_s`/`end_s`/`label` (a scene with neither label falls back to its `mood`;
unlabeled or zero-length scenes are skipped). Output is capped at 1800 chars; if
nothing video-derived is available it returns `None` and the pipeline
orchestrates itself.

Where the fusion is applied:

- **Base generation** — `generate_music_candidates` puts the fused text into the
  job payload as `music_style_prompt` (the candidate card keeps the
  human-readable direction for the UI).
- **`regenerate` / `extend` edits** — `edit_audio` fuses the derived edit prompt
  with the same observation before enqueueing (`tools/media.py:752`).
- **`creative_edit`** — melody-guided restyles keep a lightweight control prompt
  (the parent audio is the main guide): only a compact mood + tempo grounding is
  appended, never the full scene arc (`tools/media.py:666`).

Downstream, the monolith worker passes `request["music_style_prompt"]` into
`VideoGenerationOrchestrator.run`
(`EdennCode/Deployment/async_pipeline_v2/workers/monolith_worker.py:505`).

## Session state (`state_json`)

`observation` · `source_video` {url, poster_url, content_type, duration_s} ·
`proposals` · `approved_direction` / `approved_proposal_id` · `candidates[]`
(provider, version, parent, edit_kind/requested_edit_kind, extend_mode, remix
info, provider_audio_id/task_id, stalled_seconds?, placeholder?) ·
`selected_proposal_id` · `selected_candidate_id` · `final_artifact` ·
`last_mix_volume` · `mix` · `turns[]` (per-turn: user_message, intent, actions,
tools, phase; last 100) · `memory` (preferences, creative_direction,
style_keywords, avoid, recent_intents) · `pending_clarification` ·
`production_plan` {mode, layers} · `layers` {music, voiceover, sfx}

`domain/session_state.py` provides a typed read layer (`SessionState`,
`CandidateGraph`) over this dict; mutation stays dict-side in the tools so the
persisted/wire shape is unchanged.

## Provider mapping & take counts

`edenn_basic` / `edenn_enhanced` / `edenn_studio` each map to an upstream music
provider (`models.PROVIDER_BY_MODELSPEC` — the internal provider keys stay
server-side). `provider_audio_id` / `provider_task_id` persist end-to-end
(monolith path) onto candidate cards; `None` on the basic tier, whose provider
has no addressable track id. Native extend + creative edit are enhanced/studio
capabilities (`models.NATIVE_EXTEND_PROVIDERS` / `models.CREATIVE_EDIT_PROVIDERS`).

Default takes per approved direction: basic 1, enhanced/studio 2
(`default_candidate_count_for_modelspec`), capped by `max_candidates`
(`AGENTIC_AUDIO_MAX_CANDIDATES`, default 3).

**Vendor identity never reaches the client.** Snapshot and event models scrub
provider keys at serialization (`models._redact_snapshot_payload`,
`redact_client_keys`); REST/WS error paths use `scrub_provider_names` /
`public_error_payload`; the system prompt forbids naming vendors in messages.

## Async pipeline integration & the generation watchdog

- Jobs/tasks ride `async_pipeline_v2` (`AsyncPipelineV2Repository` +
  `PostgresTaskQueue`), queue names namespaced via `namespaced_queue_name`.
- `generate_candidates` / `edit_audio(regenerate|extend)` → job_type
  `video_music`, task `video_music_monolith` on `video-music-pipeline`.
- `edit_audio(creative_edit)` → `audio_creative_edit` on
  `audio-creative-edit-pipeline` (`AudioCreativeEditWorker`).
- `generate_voiceover` → `voiceover` on `voiceover-pipeline` (`VoiceoverWorker`).
- Results hydrate back onto cards via `hydrate_candidate_results` /
  `hydrate_voiceover_layer` (called from `refresh_session_state` on every
  GET/message/choice/WS-open).

**Watchdog:** a take normally renders within ~3 minutes. When a linked job sits
`queued`/`processing` longer than `AGENTIC_AUDIO_GEN_STALL_SECONDS` (default
180), hydration stamps `stalled_seconds` (the job's age) onto the candidate so
the UI can say "this looks stuck" instead of spinning forever
(`tools/media.py:327`). Read-only: it flags from a GET path, never auto-fails
the job. Dev-harness stand-in tones carry `placeholder: true` through to the
card for honest labeling.

## Event vocabulary (WS + REST)

Single source of truth: `events.py` (`EventType`, a `str` enum — the member IS
the wire string).

`session.opened · error · message.created · agent.reasoning · tool.started ·
tool.completed · proposal.cards · candidate.cards · candidate.selected ·
clarify.cards · production.plan · voiceover.script · voiceover.generating ·
mix.updated · final.artifact · direction.approved · phase.changed ·
choice.recorded`

On WS, events stream **live** as the turn unfolds (the entry points accept an
`emit` sink; `session_io.push` appends + emits each event as produced), followed
by a fresh snapshot. On REST the same events return as a batch in `events[]`.

## API surface (`api/router.py`)

Mounted at `/api/v2/agentic/audio` by `mount_agentic_audio_router` only when
`AGENTIC_AUDIO_ENABLED` is truthy; the production mount defaults the intent gate
on. Endpoints: `POST /sessions` (validates the source artifact, bootstraps),
`GET /sessions` (history list), `GET /sessions/{id}` (hydrated snapshot),
`POST /sessions/{id}/messages`, `POST /sessions/{id}/choices`, and
`WS /sessions/{id}/ws` (browser auth via `?token=` since WS headers aren't
settable; also honors an Authorization header). Ownership is enforced per
session (`authorize_owner`); WS errors mirror REST's crafted 400/404 details
(including `ApprovalRequiredError`) and everything else goes through
`public_error_payload`.

The bundled console is served same-origin at `/app/` from `frontend/`, with a
hard **allow-list** of asset paths (`FRONTEND_ASSETS`: `index.html`,
`styles.css`, `mock-backend.js`, `js/app.js`, `js/canvas-mode.js`,
`api/router.py:45`) plus a resolved-path containment check — arbitrary user
paths are never resolved off disk.

## LLM client

`build_agentic_audio_agent_client` (`agent/agent.py:90`) builds a dedicated
multimodal LLM client for agent reasoning from `AGENTIC_AUDIO_AZURE_ENDPOINT`
/ `AGENTIC_AUDIO_AZURE_MODEL` / `AGENTIC_AUDIO_AZURE_API_KEY` /
`AGENTIC_AUDIO_AZURE_API_VERSION` / `AGENTIC_AUDIO_AZURE_API_MODE` /
`AGENTIC_AUDIO_AZURE_API_TIMEOUT`, each falling back to the shared `AZURE_*`
variable — so the agent can run a different deployment from the video-music
pipeline without extra setup. Decisions are schema-constrained JSON completions
against `AGENT_DECISION_SCHEMA` via `complete_messages` (`agent/loop.py:105`).

## Built vs planned

- **Built:** LLM agent loop (bounded turns, live beats, context refs) · turn
  locks · intent gate · music generate/regenerate/extend · adjust_remix ·
  creative_edit (+ worker) · fused style prompts on generation AND edits ·
  provider-identity persistence · re-approve guard · stage machine · generation
  watchdog · session memory / turn history · clarification cards ·
  production-plan / layer foundation (A1) · voice-over (A2): `propose_script`
  (free) + script-gated `generate_voiceover` + `VoiceoverWorker` · multi-layer
  `compose_mix` (A3), including the voice-over-only deliverable + finalize path.
- **Planned:** A4 SFX (currently stripped from any plan the model requests) ·
  inpaint / section-edit · stems / vocal-clone.

### Voice-over (A2/A3) flow

`propose_script` records the draft (agent-written or user-provided), `tone`, and
voice options; `generate_voiceover` raises `ApprovalRequiredError` unless a
script exists — the structural enforcement of invariant #2. Preset voices:
`warm_female, bright_female, calm_male, narrator_male, neutral` — each maps to
an upstream TTS voice plus default delivery instructions
(`models.VOICE_CATALOG`). `tone` steers delivery; changing tone re-records
(a fresh `generate_voiceover`).

### Mixing controls (A3 `compose_mix`)

A free, instant, repeatable re-mux (no generation) that lands invariant #1 for
the multi-layer deliverable. Knobs merge with the prior `state["mix"]` so the
user tweaks one at a time:

- `music_volume` / `voiceover_volume` — the layer **balance / ratio**.
- `duck_gain_db` — how much the **music ducks** under the narration (more
  negative = quieter music; window derived from VO timing).
- `voiceover_start_s` — **where the narration begins** in the video.
- `preserve_original_audio` — keep the source's original audio in the mix.

Emits `mix.updated`. With music it uses `compose_voiceover_mix_on_video`; a
voice-over-only session uses `overlay_voiceover_on_video` (delay-based, so the
narration *starts* at `voiceover_start_s`) — both in
`EdennCode/Util/MediaUtils/ffmpeg_utils.py`, injectable (`compose_fn`/`remix_fn`) for
hermetic tests. Voice **tone** is a separate axis (re-records the VO), whereas
balance/ducking/position are free. `finalize` prefers a composed
`mix.video_url` over the music-only candidate when one exists, and can finalize
a voice-over-only mix with no candidate at all.
