# persistence/ — data layer

| File | Role |
|---|---|
| `repositories.py` | `AgenticAudioRepository` — Postgres-backed CRUD over the four session-rooted tables; `ensure_schema()` replays the migration on first use; `build_snapshot()` assembles the full `AgenticAudioSessionSnapshot` the API returns. |
| `migrations/001_agentic_audio.sql` | The schema (`MIGRATION_PATH` resolves here, `repositories.py:21`). |
| `__init__.py` | Re-exports `AgenticAudioRepository` + `MIGRATION_PATH` so `agentic_audio.persistence` is the home for persistence concerns. |

## Connection model — a fresh client per operation

Every repository method opens its own `with self._client_factory() as client:`
block; the default factory is `PostgresClient.from_env`
(`EdennCode/Deployment/postgres_wrapper.py:184`), so each operation is an
open-query-commit-close round-trip. Configuration comes from `DATABASE_URL` or
`PGHOST`/`PGPORT`/`PGDATABASE`/`PGUSER`/`PGPASSWORD` (plus `PGSSLMODE`,
`PGCONNECT_TIMEOUT`, `PGAPPNAME`) — names only; values live in the environment.
Tests inject a different `client_factory` (or replace the repository outright).

`update_session` alone accepts an optional `client=` and routes through
`_client_context` (`repositories.py:45`) so a caller holding a connection can
reuse it; everything else is strictly per-op.

**Schema bootstrap:** every public data method calls `ensure_schema()` first
(`repositories.py:34`; `build_snapshot` gets it transitively via
`get_session`). It is a double-checked lock (`threading.Lock` +
`_schema_ready` flag — both **instance** attributes, `repositories.py:31-32`)
that runs `migrations/001_agentic_audio.sql` once per repository instance; in
the production mount that means once per process, since
`create_agentic_audio_router` builds a single repository (`api/router.py:94`).
The SQL is all `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT
EXISTS`, so replay is idempotent and there is no separate migration runner —
add a new numbered file AND wire it up if the schema ever grows.

**Update semantics gotcha:** `update_session` uses `COALESCE(%s, column)` for
every field (`repositories.py:216-220`), so passing `None` means "leave
unchanged" — you cannot null out `selected_candidate_id` or shrink
`linked_job_ids` to NULL through this path. `finished=True` stamps
`finished_at = now()`; `updated_at` is always bumped.

## Tables (all rooted on `session_id`, child rows `ON DELETE CASCADE`)

From `migrations/001_agentic_audio.sql`:

- `agentic_audio_sessions` — `session_id` PK, `source_video_artifact_id`,
  `creator_user_id`, `status`, `phase`, `selected_candidate_id`,
  `linked_job_ids JSONB` (default `[]` — the latest `generate_candidates`
  batch's job ids plus every subsequent edit/voice-over job: `edit_audio` and
  `generate_voiceover` append to the existing list (`tools/impls.py:777-779`,
  `:540-542`), while `generate_candidates` builds the list from only its new
  batch and the COALESCE update replaces the whole array (`tools/impls.py:181-185`,
  `repositories.py:219`) — so ids from a fully-failed earlier batch are dropped
  on regeneration), `state_json JSONB` (default `{}` — the de-facto artifact +
  memory store), `created_at`/`updated_at`/`finished_at`. Indexed on
  `(status, phase, created_at)`. `create_session` always inserts with
  `status = active`.
- `agentic_audio_messages` — chat lines: `message_id` PK, `role`, `content`,
  `payload_json JSONB`. Indexed + read back `ORDER BY created_at, message_id`.
- `agentic_audio_tool_calls` — an audit row per tool run: `tool_name`, `status`
  (`started/completed/failed`, `AgenticToolStatus` in `models.py:65`),
  `input_json`, `output_json`, `error_json`, `linked_job_id`,
  `linked_artifact_ids JSONB`, `finished_at`.
- `agentic_audio_choices` — recorded structured UI choices: `choice_type`
  (`proposal/candidate/compose/clarification/mix/variation/voiceover`, the
  `AgenticAudioChoiceRequest` literal in `models.py:146`), `target_id`,
  `payload_json`.

Row ids are minted via `new_id()` (`async_pipeline_v2/models.py:13`) with the
prefixes `agent_session`, `agent_msg`, `agent_tool`, `agent_choice`. Cross-store
references to `async_pipeline_v2` (jobs/artifacts) are by opaque string id —
there is no FK across the two schemas.

Only user/assistant chat lines, tool-call audit rows, and choices persist:
the live "thinking trail" (`agent.reasoning` events, including the
deterministic turn-start beat emitted before the first LLM call returns —
`agent/loop.py:85-100`) is built with `session_io.make_event` and streamed
only; it never hits the tables.

Domain artifacts (proposals/candidates/mix/layers/final) live inside
`state_json` (typed read access via `domain/session_state.py`), not their own
tables — an MVP choice; a typed artifacts table is the natural next step.

## `state_json` — the session's artifact + memory store

Keys written by the tools (`tools/impls.py`) and the loop (`agent/loop.py`):

| Key | Written by | Contents |
|---|---|---|
| `observation` | `analyze_video` (`tools/impls.py:59`) | The video-understanding output the agent (and prompt fusion) grounds on: `music_prompt` (mood/tempo/instrumentation), the scene list, `suggested_modelspec`, `detected_language`, `duration_s`, … It is the observation-side input to `fuse_music_style_prompt` (`tools/media.py:203`), which fuses proposal prose + observation mood/tempo/instruments + an **evenly-sampled timed scene arc** (max 8 entries spanning the whole cut) into the `music_style_prompt` the generation job carries. Fusion reads BOTH scene shapes — the real analysis emits `start_timestamp`/`end_timestamp`/`visual_summary`, older/test shapes use `start_s`/`end_s`/`label` (`tools/media.py:234-248`). Applied on base generation (`tools/media.py:276-284`) AND on regenerate/extend edits (`tools/media.py:752-754`); `creative_edit` gets only a compact mood+tempo grounding since the parent audio is the main guide (`tools/media.py:666-673`). The fused prompt travels on the **job payload**, while the candidate card keeps the human-readable direction text. |
| `source_video` | `analyze_video` (`tools/impls.py:67`) | Best-effort client-facing projection of the source artifact — `{url, poster_url, content_type, duration_s}` — so the canvas can layer a muted source video under a take's audio ("watch" contract). A missing artifact never fails the turn — on lookup failure the tool runs `state.setdefault("source_video", None)` (`tools/impls.py:74`), so `None` is recorded only when no projection exists yet; a previously captured `source_video` survives a failed re-analysis lookup. |
| `proposals` | the loop's propose action (`agent/loop.py:528` `_store_proposals`) | `MusicProposalCard` dumps, hard-capped at 2 directions per turn. |
| `candidates` | `generate_candidates` **replaces** the list (`tools/impls.py:180`); `edit_audio` **appends** a versioned branch (`tools/impls.py:774-776`) | `MusicCandidateCard` dumps: prompt, modelspec, `linked_job_id`, status, lineage (`parent_candidate_id`, `version`, `edit_kind`, `requested_edit_kind`, `extend_mode`), mix params + `remixed_video_url` (plus a `remixes` history appended by `adjust_remix`, `tools/impls.py:699-708`), and provider identity (`provider`, `provider_audio_id`, `provider_task_id`). Because generation *replaces* the list, the agent refuses a second proposal approval while any usable (non-failed) take exists (`ApprovalRequiredError`, `agent/agent.py:320-330`). |
| `approved_direction`, `approved_proposal_id`, `selected_proposal_id` | `approved_direction` + `approved_proposal_id`: the deterministic proposal choice (`agent/agent.py:333-336`) and the `approve_direction` tool (`tools/impls.py:110-112`); `selected_proposal_id`: `generate_candidates` (`tools/impls.py:179`) | The cost gate: `generate_candidates` is structurally blocked until `approved_direction` is set (`tools/impls.py:135-139`). |
| `selected_candidate_id`, `final_artifact` | `finalize` (`tools/impls.py:262-263`) | Note `selected_candidate_id` is **also** a session column — `finalize` writes both. |
| `production_plan`, `layers` | `set_production_plan`, `propose_script`, `generate_voiceover` | `{mode, layers}` plus the layer map (`layers.voiceover` holds `script`/`voice_id`/`language`/`tone`/`speed`/`linked_job_id`/`status`, and `audio_url` after hydration). |
| `mix`, `last_mix_volume` | `compose_mix` / `adjust_remix` | Current multi-layer mix knobs (`music_candidate_id`, volumes, `duck_gain_db`, `voiceover_start_s`, `preserve_original_audio`) + composed `video_url`; `last_mix_volume` is the deterministic memory signal for the user's latest chosen level. |
| `turns` | loop turn-end (`agent/loop.py:374` `_record_turn_and_memory`) | Bounded history (last 100) of `{turn, user_message, intent, actions, tools, phase}`; the last `AGENT_MEMORY_RECENT_TURNS` (8, `models.py:351`) feed back into the agent context. |
| `memory` | loop turn-end | Durable merged memory: `creative_direction`, `style_keywords`, `avoid`, `preferences`, recent intents. |
| `pending_clarification` | loop turn-end / the intent-gate bootstrap (`agent/agent.py:247-249`) | The open clarify card (question + options), cleared/replaced per turn (and cleared when a clarification chip is answered, `agent/agent.py:432-434`). |

## Candidate hydration (`refresh_session_state`)

`AgenticAudioAgent.refresh_session_state` (`agent/agent.py:541`) runs on every
snapshot-returning API path (`GET /sessions/{id}`, after `/messages` and
`/choices`, on WS open and after each WS turn — `api/router.py:223,243,273,322,374`):
it calls `AgenticAudioTools.hydrate_candidate_results` (`tools/media.py:303`)
and `hydrate_voiceover_layer` (`tools/media.py:347`), and persists `state_json`
back **only if something changed**. Per candidate with a `linked_job_id`,
hydration reads the async-pipeline job and fills:

- `status` (job status), `audio_url` (`audio_url` or `complete_audio_url` from
  `result_json`), `video_url`;
- `provider` — always set, derived from the modelspec even pre-completion;
- `provider_audio_id` / `provider_task_id` — provider-native handles for later
  native edits (extend/restyle); `None` on the basic modelspec's provider;
- `stalled_seconds` — **computed, read-only watchdog** (`tools/media.py:327-333`):
  when the job is still `queued`/`processing` and its `created_at` age exceeds
  `AGENTIC_AUDIO_GEN_STALL_SECONDS` (default 180s, `tools/media.py:11-16`), the
  age in seconds is surfaced so the UI can say "this looks stuck" instead of
  spinning forever. It flags, never kills — no auto-fail from a GET path;
- `placeholder` — honest labeling: dev harnesses mark stand-in tones with
  `placeholder: true` in the job result, and hydration carries it onto the
  candidate so the UI badges it (`tools/media.py:337-338`).

## Snapshot serialization contract

`build_snapshot` (`repositories.py:407`) assembles the wire object from the
four tables:

- `state` — the session's `state_json`, passed through **whole**. Vendor
  identity (provider names, `provider_audio_id`/`provider_task_id`) stays in
  the DB for server-side native edits; it is scrubbed at the single egress
  chokepoint — `AgenticAudioSessionSnapshot`'s `model_serializer`
  (`models.py:220`, via `_redact_snapshot_payload` →
  `error_codes.redact_client_keys`/`redact_error_blob`) — on every dump (HTTP
  `response_model`, WS, real or in-memory repo).
- `messages` — `{message_id, role, content, payload, created_at}`. Note the
  rename: the DB column `payload_json` becomes the **`payload`** key on the
  wire. This is where canvas mode's `context_refs` ride (client sends them in
  the `/messages` request payload; `agent/loop.py:290` `_context_ref_note`
  reads `payload_json["context_refs"]` — deduped, capped at 8, `__source__`
  resolving to the source video, unresolvable ids dropped — to render
  "@-referenced version" notes into the LLM context while the persisted
  message content stays clean).
- `tool_calls` — `{tool_call_id, tool_name, status, input, output, error,
  linked_job_id, linked_artifact_ids, created_at, finished_at}` (again renamed
  from `*_json` columns); `error` blobs get an extra free-text provider scrub.
- `choices` — `{choice_id, choice_type, target_id, payload, created_at}`.

Timestamps serialize as ISO-8601 or `null`. `list_sessions`
(`repositories.py:163`) is the lightweight History query — dict rows, no
snapshot assembly, most-recent first by `updated_at`.

## Status + phase lifecycle

Both are plain TEXT columns; the enums live in `models.py:34-51`:

- `status` ∈ `active · ready · completed · failed · canceled`
  (`AgenticSessionStatus`). `finalize` flips `active → completed` (and stamps
  `finished_at`) once a deliverable `video_url` exists (`tools/impls.py:274-284`).
- `phase` ∈ `created · observing · proposing · awaiting_plan_choice ·
  generating_candidates · awaiting_candidate_choice · composing · completed ·
  failed` (`AgenticSessionPhase`; byte-identical to the `Stage` enum in
  `stages.py:24`). Every phase write funnels through `stages.transition`
  (`stages.py:86`) → `repository.update_session(phase=...)`;
  `StageMachine.validate` is **soft** (logs undeclared transitions, never
  blocks) because sessions are intentionally never-ending — post-final edits
  legitimately loop back to `generating_candidates` (`stages.py:55-57`).

## Concurrency caveat

Turns read-modify-write the whole `state_json` blob. The agent serializes turns
per session with an in-process `asyncio.Lock` (`agent/agent.py:255-260`
`_turn_lock`, held by both `handle_user_message` and `handle_choice`) so a WS
turn racing a REST turn (or a second tab) can't lost-update state — but that
lock is **in-process only**. Multi-worker deployments need optimistic
concurrency in this repository (e.g. a version column) before it is safe to fan
the API out.

## Testing

The deterministic suites never touch Postgres: `Testing/e2e/fakes.py` is the
import seam re-exporting the in-memory stand-ins (`MemoryAgenticRepository`,
`MemoryAsyncRepository`, defined in `Testing/test_agentic_audio_api.py`) that
implement the same method surface, and snapshot redaction still applies because
it lives on the Pydantic model, not the repository.
