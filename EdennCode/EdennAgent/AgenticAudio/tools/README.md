# tools/ — the Tool abstraction + the media toolkit

Two layers live here. `base.py`/`impls.py` are the **agent-facing tool layer**:
small, stateless `Tool` objects the reasoning loop dispatches, each owning one
capability and its persistence/event side effects. `media.py` is the **media/job
toolkit** (`AgenticAudioTools`) those tools call into: video analysis, prompt
fusion, async-job enqueue to `async_pipeline_v2`, result hydration (+ the
generation watchdog), and in-process ffmpeg remux/compose.

| File | Role |
|---|---|
| `base.py` | `Tool` (ABC), `ToolContext`, `ToolResult`, `ToolError`, `ToolRegistry`, `ApprovalRequiredError` |
| `impls.py` | The ten concrete `Tool` subclasses + `build_tool_registry()` (`impls.py:810`) |
| `media.py` | `AgenticAudioTools` + `normalize_music_modelspec` + `fuse_music_style_prompt` |

Dispatch lives one package over: `agent/dispatcher.py` (`ToolDispatcher`) is the
one place a tool name becomes a running tool — used by both the agent's
deterministic paths (`/choices`, intent gate) and the reasoning loop's
`call_tool` action, so registry lookup and `ToolContext` construction exist in
exactly one spot. Message and choice turns are serialized by the agent's
per-session turn lock (`agent/agent.py:255`); the one-time session bootstrap
(`bootstrap_session` → `analyze_video` / the first loop run,
`agent/agent.py:179`) dispatches outside it — it completes before the session
id is even returned to the client (`api/router.py:187`), so no second turn can
race it.

## Tool ABC + registry (`base.py`)

A `Tool` (`base.py:184`) declares only a `name` and an
`async run(ctx: ToolContext, args: dict) -> ToolResult`. Everything else —
`is_heavy`, `requires_approval` (= the spec's `is_generation`), the live status
label — is **derived** from the matching `ToolSpec` in `models.TOOL_SPECS`, the
single source of truth for tool identity. The decision-schema tool enum,
`AGENT_HEAVY_TOOLS`, `AGENT_GENERATION_TOOLS`, and the status map all derive
from the same table, so adding a tool is a one-line spec entry + a `Tool`
subclass registered in `build_tool_registry()`.

`ToolRegistry` (`base.py:212`) enforces this at construction: every tool must be
named, unique, and have a `ToolSpec` — the registry and the derived maps can
never drift apart.

`ToolResult` (`base.py:45`) carries the events to stream plus the raw `data`
fed back into the loop's scratchpad (light tools only; a tool in
`AGENT_HEAVY_TOOLS` ends the turn, `agent/loop.py:223`).

`ApprovalRequiredError` (`base.py:21`) is the cost gate's signal: a paid tool
raises it when the user hasn't approved the spend. It subclasses `ValueError`
(surfaces as 400 if it ever escapes), but the loop catches it specifically
(`agent/loop.py:205`) and degrades to a confirm-first re-ask instead of failing
the turn. The agent's proposal path raises the same error as the **re-approve
guard** (`agent/agent.py:325`): while any usable candidate exists (status not
`failed`/`error`), a second proposal approval is refused, because
`generate_candidates` replaces `state["candidates"]` — it would double-spend
and wipe every existing take.

## ToolContext (`base.py:54`)

Everything a tool needs, with no back-reference to the agent. Built fresh per
dispatch. Bundles `session_id`, the persistence `repository`, the `media`
toolkit, and `max_candidates`, plus the shared helpers:

- `require_session()` — load-or-KeyError the session.
- `event(type, payload)` / `append_message(role, content)` — event construction
  and persisted-message append (returns the `message.created` event).
- `transition_to(phase, **updates)` (`base.py:113`) — the single choke point
  for a tool's phase changes; validated by the `StageMachine` and applied in the
  same write as any other field updates.
- `no_target_message(verb)` — graceful in-conversation reply when there is no
  music track to act on yet (edits before any music used to 404).
- `user` / `session_state()` — typed domain views (`User`, `SessionState`).
- `resolve_candidate(session, candidates, args)` — target from tool args →
  session selection → the only unambiguous one, via `CandidateGraph`.
- `resolve_proposal(session, args)` / `next_version(...)` / `find_by_id(...)`.

## The toolset (`impls.py`)

`analyze_video`, `approve_direction`, `generate_candidates`*, `finalize`,
`adjust_remix`, `edit_audio`*, `set_production_plan`, `propose_script`,
`generate_voiceover`*, `compose_mix` (`*` = heavy: enqueue an async job and end
the turn; the result hydrates later). Behavior notes beyond the per-tool table
in `../ARCHITECTURE.md`:

- **`analyze_video`** (`impls.py:46`) transitions to `observing`, runs
  `media.analyze_video`, and stores the flattened `observation`. It also
  **stashes `state["source_video"]`** — `{url, poster_url, content_type,
  duration_s}` read from the source artifact (+ its `thumbnail_url` metadata) —
  so the canvas dock can layer the muted source video under a take's audio
  ("watch" without a per-take render). The stash is best-effort: a missing or
  odd artifact never fails the analysis turn (`impls.py:63`).
- **`generate_candidates`** (`impls.py:128`) raises `ApprovalRequiredError`
  unless `state["approved_direction"]` is set; an unresolvable proposal degrades
  to a "which direction?" message instead of a 404. Take count = explicit
  `count` → proposal `candidate_count` → the modelspec default (basic tier 1,
  enhanced/studio 2, `models.default_candidate_count_for_modelspec`), clamped to
  `ctx.max_candidates`. It **passes the bootstrap
  `observation` through** to `media.generate_music_candidates` so the fused
  prompt grounds the job in the video (see the fusion section below).
- **`edit_audio`** (`impls.py:744`) resolves the parent candidate (graceful
  no-target reply otherwise), validates `edit_kind` ∈
  `regenerate | extend | creative_edit`, computes the branch `version`, and
  calls `media.edit_audio` — **also with the `observation`** so edits stay
  video-grounded. The new candidate is appended (never replaces the graph).
- **`generate_voiceover`** (`impls.py:489`) is script-gated: it raises
  `ApprovalRequiredError` unless a script was drafted via `propose_script` —
  the structural half of invariant #2 for the voice-over layer.
- **`adjust_remix`** (`impls.py:653`) falls back to the most recent *rendered*
  candidate when nothing is selected; **`compose_mix`** (`impls.py:567`) merges
  each knob with the prior `state["mix"]` so the user tweaks one at a time;
  **`finalize`** (`impls.py:216`) prefers a composed `mix.video_url` over the
  music-only candidate and can finalize a voice-over-only mix with no candidate
  at all.
- **`set_production_plan`** strips SFX (not shipped) and is idempotent —
  re-asserting the same plan is a no-op (no re-recorded tool call, no event).

## `AgenticAudioTools` (`media.py`)

Constructed with the async-v2 repository, task queue, settings, and storage.
The heavy media functions are injectable (`analyze_fn` / `remix_fn` /
`compose_fn`) so tests stay hermetic; the video-music orchestrator is imported
lazily so loading the module never pulls the full workflow stack.

### Analysis → observation

`analyze_video` (`media.py:125`) resolves the source video via
`VideoSourcePreparationService` (stage name `agentic_audio_analyze`) and reuses
the pipeline's pre-generation preview (understanding stages 0–3.1). The result
is flattened (`media.py:163`) into the observation the agent reasons over:
`duration_s`, `width`/`height`, `video_title`/`video_description`/`video_summary`,
`scenes[]` (`scene_index`, `start_timestamp`, `end_timestamp`, `visual_summary`,
`key_actions`, `mood`), `detected_language`, `detected_category`,
`detected_include_vocals`/`detected_vocal_gender`, `sanitized_prompt`,
`music_prompt` (global mood / tempo / instruments), `suggested_modelspec`.

### Job enqueue: the `video_music` payload contract

`_enqueue_video_music_candidate_job` (`media.py:784`) is the one enqueue path
for base generation and `regenerate`/`extend` edits. It creates a job
(`job_type="video_music"`) whose `request_json` is:

| Key | Value |
|---|---|
| `source_video_artifact_id` | `{job_id}:source_video:input` — a **per-job linked copy** of the session's source artifact (same container/blob/url; metadata records `source_artifact_id`, `source_asset_job_id`, `linked_to_agentic_candidate_job: true`) so the worker resolves its input like any other job |
| `requested_source_video_artifact_id` | the session's original source artifact id |
| `modelspec` | normalized via `normalize_music_modelspec` (`media.py:64`: legacy map + validity check, fallback `edenn_basic`) |
| `user_prompt` | the human-readable direction prose (what the candidate card shows) |
| `include_vocals`, `vocal_gender`, `music_volume` | from the proposal |
| `preserve_original_audio: false`, `water_mark: false` | fixed for agentic takes |
| `compression_flag: true`, `compression_max_height: 1280` | generation may use a compressed proxy internally (invariant #1 remuxes onto the original later) |
| `mode: "monolith"`, `max_attempts: 3` | worker routing |
| `agentic_session_id`, `agentic_candidate_id` | the back-references hydration and the worker's diagnostics key on |
| `video_id`, `creative_id`, `primary_music_id`, `secondary_music_id`, `selected_music_id`, `alignment_id` | durable serving ids (`RecommendationAssetIds.create`) |
| `job_received_timestamp` | epoch seconds |

An optional **`extra_payload` dict is merged last** (`payload.update(...)`), so
callers can add or override keys. In practice it carries:
`music_style_prompt` (the fused prompt, below) and — on edits —
`agentic_edit_kind`, `agentic_parent_candidate_id`, `extend_seconds`,
`agentic_extend_mode` (`native` vs `regenerate_fallback`), and the parent's
provider handles (`agentic_parent_provider`,
`agentic_parent_provider_audio_id`, `agentic_parent_provider_task_id`) when a
native in-place extend is possible.

The matching `TaskEnvelope` goes to queue
`namespaced_queue_name("video-music-pipeline")` with
`task_type="video_music_monolith"` and idempotency key
`{job_id}:agentic_video_music_monolith:v1`, and a `job.created` event
(stage `agentic_audio`) is recorded. With no queue wired (unit tests), the
enqueue returns `None` and the candidate stays `planned`.

**Queue namespacing:** every queue name passes through `namespaced_queue_name`
(`EdennCode/Deployment/async_pipeline_v2/queue_names.py:26`), which prefixes
`{namespace}:` when `ASYNC_V2_QUEUE_NAMESPACE` (or the legacy
`ASYNC_V2_QUEUE_PREFIX`) is set — so an isolated deployment can run a private
queue family against the shared DB without changing task types or stage names.

Two sibling enqueue paths follow the same job/task/event pattern:

- `_enqueue_audio_creative_edit_job` (`media.py:888`) —
  `job_type="audio_creative_edit"` on `audio-creative-edit-pipeline`; payload
  adds `source_audio_url` (the parent's track, **re-signed** first) and the
  `agentic_parent_*` keys; the worker restyles the audio and re-muxes onto the
  source video, mirroring the `video_music` result contract
  (`audio_url`/`video_url`) so hydration is unchanged.
- `enqueue_voiceover` (`media.py:983`) — `job_type="voiceover"` on
  `voiceover-pipeline`; payload carries the script plus the resolved preset
  (`voice_id`, `tts_voice`, `tts_instructions` — `tone` is appended to the
  preset's delivery instructions), `speed`, `language`, and
  `agentic_voiceover_id` (`voiceover_{job_id}`).

### Hydration + the generation watchdog

`hydrate_candidate_results` (`media.py:303`) maps each candidate's
`linked_job_id` to the job row and copies job state onto the card: `status`,
`audio_url` (result `audio_url` or `complete_audio_url`), `video_url`, a
`placeholder: true` badge for dev-harness stand-in tones, and the
provider-native handles (`provider_audio_id`, `provider_task_id`) that native
edits target. `provider` is always derived from the modelspec, even pre-result.

**Watchdog:** a take normally renders within ~3 minutes. When a job sits
`queued`/`processing` longer than `AGENTIC_AUDIO_GEN_STALL_SECONDS` (default
180, `media.py:11`), hydration stamps `stalled_seconds` (the job's age) onto
the candidate so the UI can say "this looks stuck" instead of spinning forever.
Read-only: it flags from a GET path and never auto-fails the job.

`hydrate_voiceover_layer` (`media.py:347`) does the same for the voice-over
layer's `status`/`audio_url`. Both run from
`AgenticAudioAgent.refresh_session_state` (`agent/agent.py:541`) on every
snapshot read — GET, message, choice, and WS open.

### Remux / compose (in-process, free)

`adjust_remix` (`media.py:422`) and `compose_mix` (`media.py:507`) download the
needed audio, run ffmpeg against the **original** source video, and upload the
result (`agentic/remix/...` / `agentic/mix/...` blobs). Candidate/layer URLs
were signed when their jobs completed and can expire before a compose runs, so
`_refresh_signed_url` (`media.py:392`) re-signs any URL pointing into our own
storage account right before download (foreign URLs pass through untouched;
best-effort). `compose_final_mix` (`media.py:363`) doesn't render anything: it
reads the selected candidate's job result (or reports that the job is still
pending).

## Grounding generation in the video: `fuse_music_style_prompt`

`AgenticAudioTools.fuse_music_style_prompt` (`media.py:203`) exists because of a
found-live failure mode: jobs that ship only the proposal prose produce music
unrelated to the footage. The pipeline's prompt-orchestration templates treat a
non-empty caller prompt as the **highest-priority style constraint**
(`EdennCode/ModelFactory/PromptFactory/prompts.py:785`), so a detailed prose
prompt acts as an explicit style override and the pipeline's own scene-driven
orchestration contributes nothing — the music model ends up with no tempo, no
footage mood, and no timed arc. The fusion keeps the chosen direction dominant
while carrying the video-derived structure the bootstrap analysis already
synthesized:

1. **The proposal's creative direction** — first, capped at 700 chars (stays
   dominant).
2. **Footage facts** from `observation.music_prompt`: `global_mood` (≤160
   chars), `tempo_bpm` ("tempo ≈ N BPM"), and up to 10 instruments — rendered
   as one "Ground the score in the video — …" sentence.
3. **A timed scene arc**: up to 8 scenes **sampled evenly across the whole
   video** (not the first 8 of a long cut), each as `start–end s label`
   (label ≤48 chars), plus "Total length ≈ Ns" when the duration is known.

It reads **both scene shapes**: the real analysis emits
`start_timestamp`/`end_timestamp`/`visual_summary` (with `mood` as the label
fallback); tests and older shapes use `start_s`/`end_s`/`label`. Scenes missing
a label or a positive end timestamp are skipped (`media.py:246`). Output is
capped at 1800 chars. If there is
nothing video-derived to add (no observation, or facts/arc both empty) it
returns `None` — the payload then omits `music_style_prompt` and the pipeline
orchestrates from its own scene analysis as before.

Delivery: the fused text ships in the job payload as **`music_style_prompt`**,
which the monolith worker forwards into the generation orchestrator
(`EdennCode/Deployment/async_pipeline_v2/workers/monolith_worker.py:505`). The
candidate card's `prompt` keeps the human-readable direction for the UI — the
fused text is a payload-only contract with the worker.

Which paths fuse:

- **Base generation** — `generate_music_candidates` (`media.py:276`) fuses each
  take's (variation-suffixed) direction with the observation and ships it via
  `extra_payload`.
- **`regenerate` / `extend` edits** — `edit_audio` fuses the derived edit
  prompt with the same observation before enqueueing (`media.py:752`).
- **`creative_edit`** — melody-guided restyles keep a **lightweight control
  prompt** (the parent audio is the main guide): only a compact
  `footage mood + tempo` grounding is appended to the edit prompt
  (`media.py:666`), never the instrument list or the timed arc.

## Notes

- `compose_mix` lays music + voice-over over the video (ducked); with
  **voice-over alone** (no music) it overlays the narration directly via the
  delay-based helper — the voice-over-only deliverable.
- `media.py` imports heavy deps lazily; `base.py`/`impls.py` import `media` by
  its submodule path (not the package `__init__`), and `tools/__init__.py`
  loads `media` (a leaf) before `base` to avoid init-time cycles with `domain`.
- Never surface upstream vendor identity: candidate/provider keys are scrubbed
  at snapshot/event serialization (`models._redact_snapshot_payload`,
  `redact_client_keys`); tool code should only ever reference the internal
  provider keys and modelspec tiers.
