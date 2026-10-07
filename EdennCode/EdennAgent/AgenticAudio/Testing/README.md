# Testing/ — tiers, suites, and the agent E2E foundation

How to test agentic-audio at three cost levels, what each pytest suite locks in,
and how the e2e/ harness drives the real agent loop. Companion to
`../ARCHITECTURE.md`.

## The three test tiers

| Tier | What runs | Cost | How |
|---|---|---|---|
| **A — in-browser mock** | Frontend only; `frontend/mock-backend.js` fakes the whole backend in the page | free, zero infra | Serve `frontend/` statically and open with `?backend=mock` (standalone pages default to mock; `?mockfail=1` exercises the failed-candidate/retry UI) |
| **B — devserver** | The **real** router + agent loop + event stream over in-memory fakes; real Azure LLM and real video analysis when keys are present; music renders as **placeholder tones** | LLM + analysis tokens only | `design/devserver.py` with `EDENN_DEV_REAL_MUSIC=0`; open `http://127.0.0.1:8800/?backend=real` |
| **C — real music** | Tier B plus real provider generation (ProviderC/ProviderB/ProviderA) for jobs whose key exists, or the full production mount over Postgres | **paid provider credits** | devserver with `EDENN_DEV_REAL_MUSIC=1` (the default when unset — set `=0` to opt out), or the prodapi mount, or the env-gated real-provider pytest |

The frontend picks its backend from the `?backend=real` / `?backend=mock` query
override (`frontend/js/app.js:186`); the standalone dev page defaults to mock.

Devserver notes (`design/devserver.py`): it wires the real
`create_agentic_audio_router` over the same in-memory fakes the test-suite uses
(imported straight from `test_agentic_audio_api.py`), runs real scene/vision
analysis when API keys are present (disable with `EDENN_DEV_REAL_ANALYZE=0`),
and completes generation jobs with a length-matched sine tone marked
`placeholder: True` so the UI badges it — the agent/editing flows are real, the
audio is not (unless Tier C is on and the provider key exists; then real
generation runs and falls back to a badged placeholder on failure). Voice-over
follows the same pattern: real Azure TTS when keys exist, opt out with
`EDENN_DEV_REAL_VOICEOVER=0`. Uploads are validated with ffprobe — a file that
doesn't decode as video is rejected with a 400 instead of bootstrapping a
session on a fabricated duration. Set `EDENN_DEV_FORCE_DIRECTOR=1` to force the
offline scripted "dev director" LLM (no keys, zero tokens); with keys present
the live model is used and failures raise — there is no silent fallback to
canned output.

## Launch configs (`.agent/launch.json`)

| Name | Port | What |
|---|---|---|
| `agentic-audio-console` | 5599 | `python3 -m http.server` over `frontend/` — Tier A (open with `?backend=mock`) |
| `agentic-audio-devserver` | 8800 | `design/devserver.py` with `EDENN_DEV_REAL_MUSIC=0` — Tier B (placeholder tones) |
| `agentic-audio-devserver-realmusic` | 8800 | Same devserver with `EDENN_DEV_REAL_MUSIC=1` — Tier C (paid) |
| `agentic-audio-prodapi` | 8900 | The real app (`uvicorn EdennCode.Deployment.api:app`) with `AGENTIC_AUDIO_ENABLED=1`, `ASYNC_PIPELINE_V2_ENABLED=1`, `ASYNC_V2_QUEUE_NAMESPACE=smoke-zj-0702` — the production mount (console at `/api/v2/agentic/audio/app`) |

`ASYNC_V2_QUEUE_NAMESPACE` prefixes queue names (see the
`agentic-e2e:video-music-pipeline` assertion in `test_agentic_audio_api.py`),
so local smoke jobs on a shared Postgres are not consumed by workers listening
on other namespaces — keep a private namespace when pointing at a shared DB.

## Cache-busting when verifying frontend changes

`index.html` references `./styles.css`, `./mock-backend.js`, `./js/studio-ui.js`,
`./js/app.js`, `./js/timeline-mode.js`, and `./js/canvas-mode.js` with **no version query**, and neither the production
mount (plain `FileResponse` in `api/router.py`) nor the devserver/`http.server`
sets no-cache headers. The browser will happily keep serving a stale bundle
after you edit frontend JS/CSS. When verifying a frontend change in a browser,
hard-reload (Cmd+Shift+R) or keep DevTools open with "Disable cache" — otherwise
you are testing yesterday's code.

Run the standalone studio interaction contract checks with
`node --test EdennCode/Deployment/agentic_audio/Testing/studio-ui.test.cjs` from
the repository root. They cover bootstrap completion, narration-only routing,
explicit demo effects, background hydration, and failed generation/retry.

## Pytest suites (82 collected)

| Path | What |
|---|---|
| `test_agentic_audio_api.py` | The main suite (60 tests): the FastAPI router over in-memory fakes with a scripted LLM (`_ScriptedAgentClient`). See coverage map below. Also two real E2E tests that **assert** (not skip) their env. |
| `test_domain.py` | Unit tests for the typed domain layer (`CandidateGraph`, `SessionState`, `User` over `state_json` — lossless roundtrips, graph resolve/versioning). |
| `test_media_compose.py` | Media correctness: actually runs ffmpeg and **measures** the output by FFT band (VO lands at the requested offset, music ducks by the requested gain, VO-only deliverable non-empty). Skips without ffmpeg/numpy. |
| `test_eval_harness.py` | Scripted harness self-tests + `test_real_llm_eval_safety_gate` (skipif without Azure env; real model must never generate before approval). |
| `e2e/test_e2e_foundation.py` | Hermetic self-test of the E2E foundation: scripted scenario + gates, and the watch-transcript renderer (every commit, no model). |
| `e2e/test_editing_flows.py` | Hermetic editing-flow regressions: style change → true `audio_creative_edit` job carrying the new style; same-style variation → `regenerate`; extend carries `extend_seconds`; ProviderA restyle honestly falls back; volume tweak enqueues nothing; mix without an explicit target falls back to the latest completed take. |
| `run_eval.py` | Real-LLM multi-turn eval runner: gold catalog + deterministic metrics + an **LLM judge** (`e2e/gates.py judge_transcript`); exits non-zero if the safety gate fails, SKIP (exit 0) without Azure env. |
| `eval_harness.py`, `eval_scenarios.py` | Pure back-compat shims re-exporting from `e2e/` — new code imports `Testing.e2e` directly. |

### What `test_agentic_audio_api.py` covers

- **Session flow**: bootstrap observes the video and persists a snapshot; message
  append rebuilds it; multi-turn generate → adjust → restyle; iteration after
  finalize (the session never ends).
- **WS events**: `session.opened` carries the snapshot on connect;
  choice-over-WS yields the exact ordered event sequence
  (`choice.recorded → tool.started → tool.completed → candidate.cards →
  phase.changed → message.created → session.opened`); the agent emits
  reasoning/status events (`test_agent_emits_reasoning_status_events` asserts
  the "Locking in your approval…" / "Composing your tracks…" beats and that
  reasoning lands before `candidate.cards`).
- **Spend gating**: generation without approval re-asks gracefully; approval
  intent alone doesn't auto-spend; `approve_direction` unlocks; a proposal
  `/choices` pick sets the approval flag.
- **Edits**: `edit_audio` branches a versioned candidate; native extend routes
  to ProviderC/ProviderB when a `provider_audio_id` exists; ProviderA extend/creative
  fall back to `regenerate`; `creative_edit` enqueues `audio_creative_edit` and
  the worker hydrates results back onto the card.
- **Fusion regressions** (the video-grounded `music_style_prompt`,
  `tools/media.py:203 fuse_music_style_prompt`): the fused prompt keeps the
  chosen direction dominant and adds footage mood + tempo + instrumentation +
  an evenly-sampled timed scene arc (max 8 scenes spanning the whole video); it
  reads **both** scene shapes (`start_timestamp`/`end_timestamp`/`visual_summary`
  from the real analysis and `start_s`/`end_s`/`label` from tests/older data —
  regression: real jobs once carried `0–0s …` arcs); regenerate/extend **edit
  jobs carry the same fusion** as base generation (`tools/media.py:752`);
  `creative_edit` gets compact grounding only (mood + tempo appended to the
  user prompt, no scene arc — the parent audio is the guide,
  `tools/media.py:664`).
- **Stale SAS re-sign**: `_refresh_signed_url` (`tools/media.py:392`) re-signs
  an expired SAS URL on our storage account from the blob path and passes
  foreign URLs through untouched (regression: `compose_mix` 403'd downloading a
  5h-old candidate URL and the turn crashed).
- **Asset allow-list drift**: `test_console_asset_allowlist_covers_every_index_asset`
  parses every relative `./`-prefixed `src`/`href` in `frontend/index.html`
  (CDN references are exempt) and asserts each is in `FRONTEND_ASSETS`
  (`api/router.py:45`) **and** fetchable from the production `/app/{asset}`
  mount — the devserver serves the whole directory and hides missing entries
  (regression: `js/canvas-mode.js` shipped without one and 404'd in prod).
- **Auth**: disabled by default (no credentials needed); required-mode rejects
  missing creds; owner binding + ownership enforcement; API-key-map token→user
  resolution; session listing scoped to the caller.
- **Voice-over + mixing**: script drafting, the gated `generate_voiceover`
  (refused without a script), worker TTS hydration, tone→instructions,
  `compose_mix` knobs merging one at a time, voice-over-only deliverables.

### Behavior worth knowing that this suite does NOT cover

Real code paths, documented here for context — none has an automated test in
this directory today; where verified at all, it was via the browser E2E:

- **Turn-start reasoning beat**: `agent/loop.py:85` pushes a deterministic
  "Reading your direction" `AGENT_REASONING` event before the first LLM step
  returns, so the thinking trail is never empty while the model works. It fires
  during the reasoning-events test's turn but no test asserts it — removing the
  beat would not fail this suite. The only check anywhere is the non-pytest
  `e2e/endpoint_smoke.py`, which merely requires *some* live `agent.reasoning`
  event over the WS.
- **Re-approve guard**: `agent/agent.py _handle_choice_locked` raises
  `ApprovalRequiredError` on a second proposal approval while usable takes
  exist, instead of double-spending and wiping the candidate list. Implemented,
  not regression-locked by any test yet.
- **Canvas `@`-references**: `payload_json.context_refs` are folded into the
  turn by `agent/loop.py _context_ref_note` (deduped, capped at 8) — exercised
  only by the browser E2E, not by this suite.
- **Watchdog surface**: `hydrate_candidate_results` (`tools/media.py:303`)
  flags queued/processing candidates older than
  `AGENTIC_AUDIO_GEN_STALL_SECONDS` (default 180, `tools/media.py:14`) with
  `stalled_seconds` — read-only (flag, don't kill); the UI renders the "looks
  stuck" badge from it. Suite tests create jobs fresh, so the stall branch
  never fires in them.
- Snapshots expose `state["source_video"]` (`tools/impls.py:67`) so the canvas
  dock can layer a take's audio over the muted source video, and per-session
  **turn locks** (`agent/agent.py _turn_lock`) serialize WS/REST/second-tab
  turns against `state_json` lost-updates (in-process only). Neither is
  asserted by this suite today.

## Run

```bash
# fast deterministic suite (deselect the env-hungry tests: the two E2E tests
# ASSERT their env, and the safety gate spends real tokens when keys exist)
.venv/bin/python -m pytest EdennCode/EdennAgent/AgenticAudio/Testing -q \
  -k "not real_provider_postgres and not e2e_video_upload and not real_llm_eval_safety_gate"

# runtime agentic E2E (real model) — watch every persona session + the safety gate
.venv/bin/python -m EdennCode.EdennAgent.AgenticAudio.Testing.e2e.run_e2e

# editing-flow probe (real model) — inspect the actual job payloads
.venv/bin/python -m EdennCode.EdennAgent.AgenticAudio.Testing.e2e.probe_editing_flows
```

`test_agentic_audio_e2e_video_upload_...` needs the smoke video under
`EdennCode/TestSuites/assets/smoke/videos/` (+ ffmpeg); `test_agentic_audio_real_provider_postgres_storage_e2e`
needs Postgres + storage + Azure LLM + `PROVIDER_A_API_KEY` env and generates a
unique `ASYNC_V2_QUEUE_NAMESPACE` per run (`agentic-real-e2e-<uuid>`).

## e2e/ — drive the real agent loop + watch the session

```
e2e/
  fakes.py     single import seam for the in-memory fakes (re-exports from
               test_agentic_audio_api — no reaching into test internals)
  driver.py    E2EDriver — the real router over fakes with a pluggable LLM;
               injects RecordingCompose so free edits stay hermetic; run_scenario
  scenarios.py Turn/Scenario gold catalog: S1 happy path, S3 adversarial
               pressure-to-spend, S5 cheap edits, S10 clarify-on-ambiguity, S8
               memory continuity, and the four UX personas (design/UX_EVALUATION.md)
  gates.py     per-scenario metrics: intent accuracy, clarify appropriateness,
               deliverable_reached, the HARD gate unauthorized_generation==0,
               and the optional LLM judge (judge_transcript, real-LLM runs)
  watch.py     render a turn-by-turn transcript ("watch the session")
  run_e2e.py   CLI: real-model run over the catalog → transcripts + gate summary;
               exit≠0 on a safety/deliverable gate failure; prints SKIP (exit 0)
               without AGENTIC_AUDIO_AZURE_* / AZURE_* env; calls load_env() first
  probe_editing_flows.py  real-LLM probe that drives every edit kind and inspects
               the enqueued job payloads (style actually engaged, not just
               acknowledged); writes evidence to /tmp/editing_flows_evidence.json
  endpoint_smoke.py  post-deploy endpoint smoke CLI: console assets + upload +
               session bootstrap + live WS events + resume against any base URL
               (--base-url); --with-generation spends one real paid take
  test_e2e_foundation.py  hermetic self-test (scripted; every commit)
  test_editing_flows.py   hermetic regressions locking in the probe's findings
  BROWSER_E2E_REPORT_2026-07-01.md  browser E2E report (see below)
```

Only the agent's reasoning (and, in real mode, the LLM judge) hits a model — no
Postgres or music providers. Scenario gold is permissive where several intents
are defensible (`accepted_intents`); `approval_given` marks the turn from which
spend is authorized, and any generation tool/job before that counts against the
hard safety gate (`gates.py compute_metrics` checks `AGENT_GENERATION_TOOLS`
usage and enqueued jobs per turn).

## The browser E2E report

`e2e/BROWSER_E2E_REPORT_2026-07-01.md` is the full point-in-time browser
walkthrough of chat + canvas against the real router / real LLM / real analysis /
real provider generation: wiring matrix, wrong-path catalog, Tier C audio
verification, and 13 ranked findings with repros. It is a **report, not a
living doc** — several findings have since been fixed on this branch (e.g. F1:
canvas `@`-reference `context_refs` are now read by `agent/loop.py
_context_ref_note` and folded into the turn; F6/F10/F13 devserver fidelity
fixes). Read it for the method and the still-open items, not as current defect
state.

## Paid runs — provider credentials + cost caveats

Tier C and the env-gated real-provider tests spend **real provider credits**
(music generation) on top of Azure LLM tokens. Credentials resolve through
`EdennCode.env.load_env()` (`EdennCode/env.py`), which loads
`EdennCode/LocalEnv/.env` — put keys there for local paid runs. Relevant env
var names (values never in the repo): `PROVIDER_A_API_KEY`, `PROVIDER_C_API_KEY` / `PROVIDER_C_API_KEY_<n>`,
`PROVIDER_B_API_KEY` / `PROVIDER_B_API_KEY_<n>` / `EDENN_ENHANCED_PROVIDER_B_API_KEY`
(any numbered key counts, gaps allowed — see
`design/devserver.py _music_provider_key_present`),
`AZURE_ENDPOINT`/`AZURE_MODEL`/`AZURE_API_KEY` (or the `AGENTIC_AUDIO_AZURE_*`
overrides), Postgres (`DATABASE_URL` or `PGHOST`/`PGDATABASE`/`PGUSER`), and
storage (`AZURE_STORAGE_CONNECTION_STRING`/`AZURE_STORAGE_ACCOUNT_URL` or the
`COS_*` set). Keep paid verification scoped: one session, few takes, and a
private `ASYNC_V2_QUEUE_NAMESPACE` when a shared database is involved.

## Tests that need a real database

`test_state_concurrency.py` is about what Postgres does when two transactions
touch one row, which no fake can answer. It skips unless you point it at a
throwaway database — never a shared one, since it writes and locks rows:

```bash
docker run -d --name edenn-test-pg -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=edenn_test -p 55432:5432 postgres:16-alpine

EDENN_TEST_PG_DSN=postgresql://postgres:postgres@127.0.0.1:55432/edenn_test \
  .venv/bin/python -m pytest EdennCode/EdennAgent/AgenticAudio/Testing/test_state_concurrency.py

docker rm -f edenn-test-pg          # when you are done
```

Everything else in this directory runs with no database, no network and no
provider credit: `conftest.py` strips the Postgres environment variables from
the default run, so a repository built with its default constructor cannot
reach real infrastructure by accident.
