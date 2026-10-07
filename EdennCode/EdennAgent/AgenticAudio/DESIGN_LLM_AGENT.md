# Agentic Audio — LLM-Driven, Tool-Decomposed, Iterative Design

Status: Phase 1 IMPLEMENTED (LLM agent loop + coarse tools analyze/propose/
generate/finalize, dedicated agent client, tests green). Phase 2 PARTIAL:
`adjust_remix` (cheap in-process ffmpeg re-mux, no generation) and `edit_audio`
(`regenerate`/`extend`, branched/versioned candidates) implemented + tested;
`edit_audio` `creative_edit` deferred to Phase 3 (needs an async_pipeline_v2
audio-creative-edit worker task — today it is only a v1 sync endpoint). Phase 3
pending.

## Goal

Turn `EdennCode/EdennAgent/AgenticAudio/` from a hardcoded state machine with
static proposal cards into a **real LLM agent** that:

1. Reasons over the video + conversation and **decides which pipeline stage/tool
   to run next** (no fixed linear flow, no keyword matching, no canned proposals).
2. **Reuses the existing video-music pipeline's core logic**, but exposed as
   smaller, individually-callable tools instead of one end-to-end run.
3. Stays **continuously interactive and iterative** — even after a final mix
   exists, the user can keep editing (regenerate, extend, creative-edit, re-mix,
   adjust volume), branching from any prior result.

## Decisions locked in (from review)

- **Execution: hybrid.** Light tools (agent reasoning, ffmpeg re-mux, selection)
  run in-process within the agent turn. Heavy provider tools (music
  generate / regenerate / extend / creative-edit) enqueue `async_pipeline_v2`
  jobs and hydrate results back into the session.
- **Granularity: coarse tools first.** Ship ~6 coarse tools; split into full
  per-stage tools in a later iteration.
- **Agent LLM: a dedicated `AzureMultimodalClient` instance** built for the
  agent (its own deployment/config), separate from the pipeline's shared client.
  Decisions are made via **strict-JSON structured output** (the repo has no
  native function-calling; every LLM call already uses `json_schema`).
- **Deliverable: this written plan first.**

---

## Current state (what we replace)

- `planner.py` — `AgenticAudioPlanner`: deterministic phase machine
  (`_observe_video` → `_propose_music_plan` → `_generate_music_candidates` →
  `_select_candidate_and_compose` → `_compose_final_mix`). User messages routed
  by substring checks (`"first" in lowered`). **Replace with LLM agent loop.**
- `tools.py` — `AgenticAudioTools`: `observe_video` (metadata only),
  `propose_music_plan` (3 hardcoded `MusicProposalCard`s), `generate_music_candidates`
  (enqueues `video_music` monolith jobs), `hydrate_candidate_results`,
  `compose_final_mix`. **Keep the job-enqueue + hydrate plumbing; replace the
  static proposal logic; add edit/adjust tools.**
- `models.py`, `repositories.py`, `api.py`, `event_stream.py`, frontend, migration
  — mostly reusable; additive changes only.

---

## Target architecture

```
            ┌─────────────────────────── api.py (FastAPI, unchanged surface) ──────────────────────────┐
            │  POST /sessions   POST /sessions/{id}/messages   POST /sessions/{id}/choices   WS /ws     │
            └───────────────────────────────────────────┬───────────────────────────────────────────┘
                                                         │ every user turn
                                                         ▼
                                        ┌──────────── AgenticAudioAgent (NEW, agent.py) ───────────┐
                                        │  dedicated AzureMultimodalClient + AGENT_DECISION_SCHEMA  │
                                        │  loop: decide → dispatch tool → feed result → decide ...  │
                                        │  pauses on: ask | propose | finalize | heavy-job-queued   │
                                        └───────────────────────────┬──────────────────────────────┘
                                                                     │ dispatches
                                                                     ▼
                              ┌──────────────────── AgenticAudioTools (tools.py, EXPANDED) ───────────────────┐
   light / in-process ───────▶  propose_plans (agent LLM)   adjust_remix (ffmpeg)   finalize (select)         │
   heavy / async  ───────────▶  analyze_video*              generate_candidates     edit_audio                │
                              └───────────┬───────────────────────────────────┬──────────────────────────────┘
                                          │ reuse existing stage classes        │ enqueue async_pipeline_v2 jobs
                                          ▼                                     ▼
                  VideoGenerationOrchestrator.preview_pre_generation   AsyncPipelineV2Repository + PostgresTaskQueue
                  SceneSegmentationStage / VideoUnderstandingStage     (video_music monolith / audio-creative-edit)
                  MusicPromptOrchestrationStage / overlay_music_on_video
```

\* `analyze_video` runs in-process for the first cut (mirrors the existing
synchronous `pre-generation-preview` endpoint); promoted to an async stage task
in a later iteration when we go full-granularity.

---

## The dedicated agent LLM client

New helper, e.g. `build_agentic_audio_agent_client()` (in `agent.py`), modeled on
`build_azure_client()` in `Util/MediaUtils/pipeline_util.py`. It constructs an
`AzureMultimodalClient` from **agent-specific env vars with fallback** to the
shared ones, so the agent can run a different (e.g. stronger-reasoning) deployment:

```
AGENTIC_AUDIO_AZURE_ENDPOINT     -> fallback AZURE_ENDPOINT
AGENTIC_AUDIO_AZURE_MODEL        -> fallback AZURE_MODEL
AGENTIC_AUDIO_AZURE_API_KEY      -> fallback AZURE_API_KEY
AGENTIC_AUDIO_AZURE_API_VERSION  -> fallback AZURE_API_VERSION (2024-12-01-preview)
AGENTIC_AUDIO_AZURE_API_TIMEOUT  -> fallback AZURE_API_TIMEOUT (60)
AGENTIC_AUDIO_AZURE_API_MODE     -> fallback AZURE_API_MODE (chat_completions)
```

Constructor (already supports everything we need):
```python
AzureMultimodalClient(
    azure_endpoint=..., azure_api_version=..., azure_model=...,
    api_key=..., timeout=..., label="agentic_audio", api_mode=...,
)
# call: await client.complete_messages(messages, json_schema=AGENT_DECISION_SCHEMA, max_tokens=...)
# returns (parsed_dict, usage_dict)
```

Injectable into `AgenticAudioAgent.__init__(..., llm_client=...)` so tests pass a fake.

---

## The agent decision loop

`AGENT_DECISION_SCHEMA` (strict JSON, lives in `models.py`):

```jsonc
{
  "name": "agentic_audio_decision",
  "schema": {
    "type": "object",
    "properties": {
      "thought": { "type": "string" },              // private reasoning (not shown verbatim)
      "assistant_message": { "type": "string" },     // what the user sees this step
      "action": {
        "type": "object",
        "properties": {
          "type": { "enum": ["call_tool", "ask", "propose", "finalize", "noop"] },
          "tool_name": { "type": "string" },         // when type == call_tool
          "tool_args": { "type": "object" },         // when type == call_tool
          "options": { "type": "array", "items": {"type": "object"} } // when type == propose
        },
        "required": ["type"], "additionalProperties": false
      }
    },
    "required": ["thought", "assistant_message", "action"],
    "additionalProperties": false
  },
  "strict": true
}
```

Loop (per user turn), in `AgenticAudioAgent.handle_turn(session, user_message)`:

1. Build messages = `[system_prompt, serialized_state, conversation_history, user_message]`.
   `system_prompt` describes the tool catalog (names, when-to-use, args), the
   current phase/artifacts, and the rules (always confirm before spending money
   on `generate_candidates`/`edit_audio`; prefer `adjust_remix` for volume-only
   tweaks; etc.).
2. `decision = await llm_client.complete_messages(messages, json_schema=AGENT_DECISION_SCHEMA)`.
3. Persist `assistant_message` as an assistant message; emit `message.created`.
4. Dispatch on `action.type`:
   - `call_tool` → run tool via `AgenticAudioTools`; record `agentic_audio_tool_calls`
     row (already exists); emit `tool.started`/`tool.completed`. **If the tool is
     a light tool**, append its result to the loop context and go back to step 2
     (bounded by `MAX_STEPS_PER_TURN`, e.g. 4). **If the tool enqueues a heavy
     async job**, emit the queued event and **break** (return to user; result
     arrives later via hydrate).
   - `ask` / `propose` → emit `proposal.cards` / `phase.changed`; **break** (await user).
   - `finalize` → run `finalize` tool; emit `final.artifact`; **break**.
   - `noop` → **break**.
5. Always `refresh_session_state` (hydrate any completed heavy jobs) before
   building the snapshot returned to the API.

This preserves the existing event vocabulary (`message.created`, `tool.started`,
`tool.completed`, `proposal.cards`, `candidate.cards`, `phase.changed`,
`final.artifact`, `choice.recorded`) so `event_stream.py`, the WS endpoint, and
the frontend keep working with minimal/no changes.

---

## Coarse tool catalog (first implementation)

Each tool delegates to existing code. `*` = heavy/async (returns `linked_job_id`,
hydrated later); others run in-process during the turn.

### 1. `analyze_video(args: {focus?: str})`  — understanding
- Reuses `VideoGenerationOrchestrator.preview_pre_generation(...)` (stages 0–3.1:
  intent preprocess, preprocess/metadata, scene segmentation, video understanding,
  music-prompt orchestration). Needs the source video on local disk → materialize
  the `source_video` artifact with the existing media-resolution helper
  (`resolve_optional_media_source_to_disk`, used by vocal-clone).
- Output stored in `state["observation"]`: duration, dims, scenes (summary/mood),
  `video_title`, `video_description`, `overall_mood`, `core_message`,
  `detected_language`, `detected_include_vocals`, `detected_vocal_gender`,
  sanitized prompt, and draft `music_prompt` (style/lyrics/combined).
- Cached: if `state["observation"]` exists, the agent skips re-running unless the
  user changed the video or explicitly asks to re-analyze.

### 2. `propose_plans(args: {count?: int, direction?: str})`  — agent LLM
- The agent's **own** LLM call (second schema, `MUSIC_PLAN_SCHEMA`) turns the
  observation + user intent into 1–3 concrete `MusicProposalCard`s
  (title, prompt, modelspec ∈ {edenn_basic, edenn_enhanced, edenn_studio},
  include_vocals, vocal_gender, music_volume). **Replaces the 3 hardcoded cards.**
- Stored in `state["proposals"]`; emits `proposal.cards`.

### 3. `generate_candidates(args: {proposal_id|inline_plan, count?: int})`  *
- Reuses the existing `_enqueue_video_music_candidate_job` plumbing (creates
  `video_music` monolith jobs tagged with `agentic_session_id`/`agentic_candidate_id`,
  clones the source artifact, enqueues on `video-music-pipeline`).
- Returns `MusicCandidateCard`s with `linked_job_id`; `refresh_session_state`
  hydrates `audio_url`/`video_url`/`status` from the job result (existing
  `hydrate_candidate_results`). Emits `candidate.cards`.

### 4. `edit_audio(args: {candidate_id, edit_kind, prompt?, ...})`  *
- `edit_kind ∈ {creative_edit, regenerate, extend}`:
  - `creative_edit` → enqueue an **audio-creative-edit** job
    (`audio_creative_edit_workflow`) seeded with the candidate's existing audio +
    the user's new direction (ProviderB/ProviderC only).
  - `regenerate` → enqueue a fresh `video_music` job with a tweaked prompt
    (new variation from the same/adjusted plan).
  - `extend` → enqueue a `video_music`/provider job using the ProviderB/ProviderC
    extend-track path to lengthen the existing track.
- Produces a **new versioned candidate** linked to the parent (branching). Emits
  `candidate.cards`.

### 5. `adjust_remix(args: {candidate_id, music_volume?, preserve_original_audio?, duck_gain_db?})`  — ffmpeg, in-process
- **No new generation.** Re-muxes the chosen candidate's existing matched audio
  onto the source video with new mix params via `overlay_music_on_video(...)`
  (volume, ducking using `video_metadata.audio_activity`, preserve/replace).
- Uploads the new remixed video, registers it as a new artifact version on the
  candidate. Emits `final.artifact` (or `candidate.cards`).
- This is the cheap path for "just lower the music" / "keep the original talking."

### 6. `finalize(args: {candidate_id})`  — in-process
- Reuses `compose_final_mix` / selection: marks the candidate as
  `selected_candidate_id`, resolves the final artifact, sets session status to a
  resting `ready` state. Emits `candidate.selected` + `final.artifact`.

---

## Iteration model (never "done")

- The terminal `COMPLETED` phase becomes a resting **`ready`** status, not an end.
  After finalize, the agent keeps accepting turns.
- Every generated track and every remix is a **versioned artifact** in session
  state so the user can branch ("go back to candidate 2 and make it longer").
  Proposed storage: a `versions` list per candidate in `state_json`, plus an
  optional `agentic_audio_artifacts` table (see migration below) if we want
  first-class querying. **MVP: keep it in `state_json`**; add the table only if
  needed.
- Example iterative turns the loop handles naturally:
  - "make the drop harder" → `edit_audio(regenerate, prompt+="harder drop")`
  - "lower music 20%" → `adjust_remix(music_volume=0.68)` (no regen, no cost)
  - "the cut is now 40s, refit" → `edit_audio(extend)` then `adjust_remix`
  - "use this voice" → (future) `clone_vocal` → `edit_audio` with `vocal_id`

---

## File-by-file change list

- **`agent.py` (NEW)** — `AgenticAudioAgent` (loop), `build_agentic_audio_agent_client()`,
  system-prompt builder, tool-catalog description, state serializer.
- **`tools.py`** — keep `observe_*`, `hydrate_candidate_results`,
  `_enqueue_video_music_candidate_job`; add `analyze_video`, `propose_plans`
  (delegates to agent client), `edit_audio`, `adjust_remix`; keep/extend
  `compose_final_mix` for `finalize`. Add source-video materialization helper.
- **`planner.py`** — slim to a thin adapter that delegates `bootstrap_session`,
  `handle_user_message`, `handle_choice` to `AgenticAudioAgent` (keeps the API
  contract; `handle_choice` becomes "user picked option X" fed into the loop).
  `refresh_session_state` stays.
- **`models.py`** — add `AGENT_DECISION_SCHEMA`, `MUSIC_PLAN_SCHEMA`; add
  `edit_kind`/version fields to candidate cards; add `ready` status; keep phases
  for display/back-compat.
- **`repositories.py`** — no schema change required for MVP (use `state_json`);
  tool-call recording already supported. Optional `agentic_audio_artifacts` table
  later.
- **`api.py`** — construct `AgenticAudioAgent` (with dedicated client) in
  `create_agentic_audio_router`; inject into planner/adapter. Endpoint signatures
  unchanged. `/choices` now means "answer the agent's last proposal/ask."
- **`migrations/001_agentic_audio.sql`** — unchanged for MVP (optional new table
  later, as `002_*.sql`).
- **`event_stream.py`, frontend/** — unchanged (event vocabulary preserved).
- **async_pipeline_v2** — reuse as-is for `generate_candidates`. For `edit_audio`
  creative-edit, reuse the audio-creative-edit job path. No worker changes needed
  for the coarse MVP (monolith handles generation end-to-end). Full per-stage
  decomposition (non-chaining "agent mode" for split workers) is a **later phase**.

---

## Config / feature flags

- Reuse `AGENTIC_AUDIO_ENABLED` to mount the router.
- Add the `AGENTIC_AUDIO_AZURE_*` client vars (above), all with fallback.
- Add tunables: `AGENTIC_AUDIO_MAX_STEPS_PER_TURN` (default 4),
  `AGENTIC_AUDIO_MAX_CANDIDATES` (default 3).

---

## Test strategy

Mirror `Testing/test_agentic_audio_api.py` (in-memory repos/queue, fake
orchestrator). Add a **fake agent LLM client** returning scripted decisions:

1. `test_agent_runs_analysis_then_proposes` — turn 1 yields `analyze_video`
   then `propose_plans`; snapshot has observation + LLM proposals (not the old
   static 3).
2. `test_agent_generates_on_user_approval` — user "do plan A" → `generate_candidates`
   enqueues `video_music` monolith jobs (assert queue contract identical to today's
   `test_proposal_choice_generates_candidates_with_existing_async_contract`).
3. `test_adjust_remix_no_new_generation` — "lower the music" → `adjust_remix`
   re-muxes via ffmpeg, **zero new queue jobs**, new remixed artifact version.
4. `test_edit_audio_branches_candidate` — "make it longer" → `edit_audio(extend)`
   creates a child candidate linked to parent; queue gets one job.
5. `test_iteration_after_finalize` — finalize → status `ready` → another edit
   turn still accepted.
6. Keep the existing real-provider E2E test green (generate path unchanged).
7. `test_decision_schema_validation` — malformed LLM output is rejected/retried.

---

## Phasing

- **Phase 1 (this plan):** dedicated agent client + decision loop; coarse tools
  `analyze_video`, `propose_plans`, `generate_candidates` (reuse), `finalize`.
  Achieves real LLM planning end-to-end with existing generation.
- **Phase 2:** iterative editing — `adjust_remix`, `edit_audio`
  (regenerate/extend/creative-edit), versioned/branchable candidates.
- **Phase 3 (full granularity):** non-chaining "agent mode" for the
  `async_pipeline_v2` split stages so the agent enqueues any single stage
  (preprocess / scenes / analysis / provider / selection-remix) independently;
  add `clone_vocal`; split `analyze_video` into per-stage tools.

### Phase 3 progress — provider-aware editing

"Edit" is not one operation; each stack supports a different set (see the
capability matrix below). Work landed so far:

1. **Provider identity persistence (DONE).** `provider_audio_id` /
   `provider_task_id` now thread from `MusicGenerationStageOutput` →
   `VideoMusicWorkflowE2EOutput` → `VideoGenerationResult` → `VideoJobResponse` →
   job `result_json`, and hydrate onto `MusicCandidateCard` (`provider` derived
   from modelspec via `provider_for_modelspec`). These are the handles native
   edit ops target. `None` for ProviderA (no addressable track id on this path).
2. **Native extend routing (DONE, agent-side).** `edit_audio(extend)` checks the
   parent's provider + `provider_audio_id`: ProviderC/ProviderB with a known id →
   `extend_mode="native"` and the payload carries
   `agentic_extend_mode`/`agentic_parent_provider`/`agentic_parent_provider_audio_id`
   (+ task id); otherwise `extend_mode="regenerate_fallback"`. ProviderA always
   falls back.

   **Boundary:** the worker does NOT consume `agentic_extend_mode="native"` — it
   still runs full generation, which is fine because of the next point.

3. **Native-extend worker — EVALUATED AND DROPPED (redundant).** The monolith
   generation stage ALREADY auto-extends ProviderC/ProviderB tracks to cover the video
   (`_extend_provider_c_track_if_needed` / `_extend_provider_b_track_if_needed`, up to
   `max_extension_rounds`). So "the video is long and the first take was too
   short" is handled automatically at generation time, and a `regenerate` against
   a longer re-cut auto-extends to the new length. A dedicated user-triggered
   extend would duplicate that. ProviderA has no auto-extend (5-min cap) and no
   native extend, so it can't be fixed this way regardless. The Phase-2
   `edit_audio(extend)` routing remains (it falls back to regenerate, which
   auto-extends); we did NOT build a separate extend worker.

   Pivot: spend the effort on `creative_edit` (Tier B), which is genuinely
   missing, instead.

4. **creative_edit — DONE, end-to-end.** `edit_audio(creative_edit)` restyles an
   existing track (audio-to-audio cover). For ProviderC/ProviderB candidates with rendered
   audio it enqueues a dedicated `audio_creative_edit` job; the new
   `AudioCreativeEditWorker` downloads the parent track + source video, runs the
   existing `AudioCreativeEditOrchestrator` (AudioCreativeEditWorkflow), re-muxes
   the restyled audio onto the video, and writes a result_json matching the
   video_music contract (`audio_url`/`video_url`) so candidate hydration is
   unchanged. ProviderA (edenn_basic) has no audio-to-audio path, so it falls
   back to `regenerate` with `requested_edit_kind="creative_edit"` recorded on the
   card. Covered by unit tests (enqueue contract + fallback), a hermetic
   worker e2e test (monkeypatched media + fake orchestrator), and multi-turn
   conversation tests (generate → adjust_remix → creative_edit; iteration after
   finalize).

Remaining Phase 3 steps: (5) inpaint/section-edit (ProviderA `music_v2` +
ProviderB `region-edit`); (6) stems / vocal-clone / persona; (7) non-chaining split
"agent mode".

### Conversational core — intent detection + session memory (DONE)

The agent now classifies every user turn into a controlled `intent` (analyze,
request_proposals, approve_direction, adjust_mix, restyle, lengthen,
new_variation, select_final, compare, ask_question, revert, other) as part of its
structured decision, and maintains durable session memory in `state_json`:

- `state["turns"]` — append-only per-turn log (user_message, intent, tools,
  phase), bounded to the last 100.
- `state["memory"]` — accumulating `preferences` (modelspec, music_volume, …,
  derived deterministically from concrete signals + LLM `memory_update`),
  `creative_direction`, `style_keywords`, `avoid`, and `recent_intents`.

Memory + recent turns are fed back into the agent context each turn (in the state
summary) so it stays consistent across a long session. Covered by tests for
intent capture, default intent, preference accumulation, and memory-in-context.

### Next-stage interaction goals (captured from review)

1. **Stream intermediate reasoning to the frontend.** Emit short status/reasoning
   events per loop step (the agent-Code style: "Analyzing…", "Mixing…") over the WS
   (and REST events), so the UI can show what the agent is doing between actions.
2. **Clarify on unclear/missing intent.** When the user's intent is ambiguous or
   required info is missing, the agent should ask via a structured `clarify`
   action that renders quick-choice cards (chips), instead of guessing.
3. **Plan multi-audio-event expansion first.** Today the agent is a *music*
   director. Expanding to other audio events (voice-over, SFX) is an
   architectural change — see "Audio-director expansion plan" below — so we plan
   before building.

## Audio-director expansion plan (PROPOSED — not built)

### The shift
Today the deliverable is "pick ONE music track and mux it on the video." Voice-over
and SFX are different *audio events* that layer **with** music, not instead of it.
So the core model changes from *one music candidate* to a **multi-stem mix**:
`music + voice-over + sfx`, each a layer with its own source, timing, volume and
ducking. The agent becomes an audio *director* composing layers, not just a music
picker.

### Building blocks already in the repo (reuse, don't rebuild)
- **Voice-over TTS:** `AzureModelGatewayTTS4OMini` in `ModelFactory/VoiceOverModelFactory/`
  (plus ProviderA/ProviderB voice cloning we already wire for music vocals).
- **SFX:** `VideoSFXModelFactory/CloudSoundEffectGen/provider_a_sound_effect.py`.
- **Layering + ducking:** `overlay_music_on_video(..., ducking_segments=...)` and
  `detect_audio_activity()` already support ducking music under speech.

### Proposed model (additive to today's state)
```
state["layers"] = {
  "music":     { selected_candidate_id, volume, ... },   # existing candidates feed this
  "voiceover": { script, voice (id/gender/style), language, audio_url, timing, volume },
  "sfx":       [ { prompt, at_ms, audio_url, volume } ],
}
state["mix"] = { video_url, per-layer settings, ducking }   # the composed deliverable
```
Music-only stays the default; voice-over/SFX are opt-in layers added by intent.

### New tools (each delegates to existing factories)
- `propose_script` (agent LLM) — draft a voice-over script from the observation +
  user intent (mirrors `propose` for music). User can also supply a script.
- `generate_voiceover` * — TTS the script (VoiceOverModelFactory), pick voice
  (preset or cloned), language. Produces the VO layer.
- `edit_voiceover` * — change script / voice / pacing.
- `generate_sfx` * — SFX at timestamps (VideoSFXModelFactory). [later phase]
- `compose_mix` (ffmpeg, in-process) — layer music + VO (+ SFX) with auto-ducking
  of music under VO (reuse `ducking_segments` from VO timing), per-layer volume.
  Extends `overlay_music_on_video` to N audio inputs (or a new multi-input util).
- `set_layer_volume` / `remove_layer` — cheap mix adjustments (like `adjust_remix`).

### Intent + memory additions
New intents: `add_voiceover`, `edit_script`, `change_voice`, `add_sfx`,
`set_layer_volume`, `remove_layer`. Memory learns voice preferences (voice_id,
gender, narration style), default-VO-on, and per-layer volume tendencies.

### Phasing
- **A — Voice-over as a layer:** `propose_script` → `generate_voiceover` →
  `compose_mix` (music ducked under VO). Highest value; needs the layer model +
  compose util + a VO job/worker (or in-process if TTS is fast enough).
- **B — SFX layer:** `generate_sfx` at timestamps; fold into `compose_mix`.
- **C — Timeline editing:** move/trim/retime layers; multi-segment VO.

### Decisions (locked in from review)
1. **Entry = intent-driven mode cards.** At the start (or when audio scope is
   unclear) the agent recommends and offers option cards: (1) **full plan e2e**
   (plan + generate music + voice-over together), (2) **music-first incremental**
   (music now, add layers one by one), (3) other. Reuses the clarify-card infra.
2. **VO script: both modes, user chooses** (agent drafts, or user provides) via a
   clarify card at VO time.
3. **VO execution: dedicated async job+worker** (consistent with creative_edit).
4. **Voice: preset voice catalog** (curated gender/style/language); cloning later.

### Phase A build sequence (sub-iterations)
- **A1 (this iteration): production-plan + mode foundation.** `production_plan`
  {mode, layers} state, the `layers` scaffold (music/voiceover/sfx), a
  `set_production_plan` tool, the mode-selection cards, and a `plan_audio` intent.
  "music_first" maps to today's behavior; "full_e2e" records intent to do all
  layers (consumed by later sub-iterations).
- **A2 (DONE): voice-over layer.** `propose_script` (free; agent-draft or
  user-provided) + preset voice catalog + approval-gated `generate_voiceover` async
  job → `VoiceoverWorker` (TTS via `AzureModelGatewayTTS4OMini`, injectable for tests) →
  `layers.voiceover.audio_url`. Enforces invariant #2 structurally (refuses without
  a drafted script). Covered by tests: script draft, gated enqueue, gate refusal,
  worker e2e.
- **A3 (DONE): compose_mix.** Light, re-runnable tool that layers music + VO onto
  the ORIGINAL video with user-adjustable balance (`music_volume`/
  `voiceover_volume`), ducking depth (`duck_gain_db`, window derived from VO
  timing), and VO position (`voiceover_start_s`). Voice `tone` is a separate axis
  (re-records the VO). `compose_voiceover_mix_on_video` ffmpeg util +
  injectable `compose_fn`. Tests: tone passthrough, compose layering, incremental
  knob adjustment.
- **A4: SFX layer.**

Capability matrix (wired vs native-but-unwired):

| Primitive | edenn_basic (ProviderA) | edenn_enhanced (ProviderB) | edenn_studio (ProviderC) |
|---|---|---|---|
| extend | inpaint plan (unwired) | `/v1/song/extend` (provider method wired; worker consumption pending) | `/generate/extend` (provider method wired; worker consumption pending) |
| section edit / inpaint | composition-plan chunks `music_v2` (unwired) | `/v1/song/region-edit` (unwired) | replace-section (unwired) |
| cover / restyle | conditioning_ref (unwired) | melody-guided (wired) + `/v1/song/remix` (unwired) | `/generate/upload-cover` (wired) |
| stems | n/a | `/v1/song/stem` (unwired) | separate-vocals (unwired) |
| vocal clone | n/a | `clone_vocal → vocal_id` (wired) | persona (unwired) |

Remaining Phase 3 steps (in order): (3) `creative_edit` as a v2 worker task over
the existing AudioCreativeEditWorkflow (ProviderB/ProviderC; ProviderA → regenerate);
(4) inpaint/section-edit (ProviderA `music_v2` + ProviderB `region-edit`);
(5) stems / vocal-clone / persona.

---

## Open sub-questions (non-blocking; will default if unanswered)

1. `analyze_video` in-process (MVP, default) vs async stage task now.
2. Versioning in `state_json` (MVP, default) vs new `agentic_audio_artifacts` table.
3. Should `/choices` be kept as an explicit endpoint or fully absorbed into
   `/messages` (default: keep `/choices` as a structured "option picked" turn).
