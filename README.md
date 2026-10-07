# Edenn

A generative media backend: it takes video or images in, and returns them
scored with music, sound effects, and narration. The interesting part is the
bridge — three different kinds of audio, each meeting the picture at a different
granularity — carried by a job pipeline that survives a worker dying mid-render
and an agent that directs the work conversationally instead of through a form.

> **This is a curated reference snapshot, not a deployable system, and not a
> mirror of the private repository.** It carries no upstream history, it
> carries no credentials, and its deployment surface was removed.
> **[REDACTIONS.md](REDACTIONS.md) is the complete record of what was taken out
> and why.** Read it before you conclude something is missing or broken.
>
> Licensed for reading only. See [LICENSE](LICENSE).

---

## What it does

Three capabilities sit on one pipeline:

- **Video to music.** Analyse a video's cuts, pacing, and content; plan a
  musical arc against them; generate candidate tracks; mix the chosen one under
  the original audio.
- **Images to music video.** Assemble a set of images into a timed sequence with
  transitions, and score it.
- **Sound design and narration.** Place sound effects against specific moments,
  and write and synthesise a voice-over that fits the cuts rather than talking
  over them.

Generation itself is bought from upstream providers. This codebase is the part
around them: deciding what to ask for, placing the result against the picture,
recovering when a provider is slow or wrong or when the machine running the job
disappears, and never letting a provider's name or a raw error reach a client.

## 1 — Visual to audio: three kinds of audio, one picture

Generating a track, a voice or an effect is bought from a provider. What is
not bought, and what this part of the codebase is, is landing three different
kinds of audio on one picture at three different time scales, and keeping each
event individually addressable afterwards so a person can move one of them
without regenerating the rest.

Each modality meets the picture at a different granularity, and each has a
different bridging problem.

- **Music meets structure.** A generated track is not a cue. The best
  video-length *window* of it is chosen by scoring candidate start offsets
  against the footage's motion peaks and the song's own structure, instead of
  trimming from zero.
  → `MusicMatchingStage/`: `candidate_generator.py` proposes,
  `window_scorer.py` decides.

- **Narration meets shots.** Lines land between cut boundaries and clear of the
  footage's own speech, with per-line speed bounds and a retake ladder. Timing
  is measured off the rendered speech, never read from provider metadata. The
  ladder declines a retake that still would not clear the shot, because a
  rushed straddle is worse than a visible overrun. The planner's first question
  is whether a moment wants narration at all; coverage is not a goal.
  → `VideoVoiceOverWorkflow/`, `AgenticAudio/tools/narration_render.py`.

- **Effects meet instants.** A multimodal model spots moments to about a
  second. A second stage, with no model call, snaps each event to the nearest
  motion onset, because an effect needs roughly 100 ms accuracy to read as
  intentional. It also knows when not to snap: continuous motion has no onset,
  and a long sustained action has no single instant.
  → `TimingRefinementStage/timing_refinement_stage.py`: `find_motion_onset` is
  the whole idea in fifty lines.

Planned independently, the three collide on the same second. One shared moment
list carries one owner per moment, which is the film-post spotting session made
explicit. Every event also carries a timing authority, so a detector never
moves a moment a person placed, and that guard fails closed: an unrecognised
authority is treated as a human placement and left alone.

→ `AgenticAudio/tools/spotting.py`, `VideoSoundEffectWorkflow/planned_run.py`.

The picture is understood once. All three modalities read that one observation.

> **Note.** A beat- and cut-aware scoring path sits beside the music matcher
> behind `BEAT_AWARE_ENABLED`. With the flag unset, the scorer runs on motion
> and lyric structure alone; read `matching.py:30-38` before assuming which
> signals a given run used.

## 2 — Distributed serving: the queue is a lease, not a message

Video jobs take minutes and the machines running them are ephemeral. Everything
below follows from that, and all of it is one Postgres table and a `WHERE`
clause rather than a broker.

- **Claiming is a row lock, not a dequeue.** Every replica polls the same table
  and takes work with `FOR UPDATE SKIP LOCKED`, so N workers contend without
  blocking each other and without a second piece of infrastructure to operate.
  → `postgres_queue.py:154`.

- **A claim is a lease, and it has to be renewed.** The owner heartbeats at
  `min(60s, lease/3)`. Stop renewing — crash, OOM, scale-in — and the row
  becomes claimable again on its own.
  → `worker_heartbeat.py:50`.

- **A worker that lost its lease physically cannot write.** `complete`, `fail`
  and `heartbeat` all carry `AND lease_owner = %s`, so a process declared dead
  that then wakes up is a no-op instead of a double-write.
  → `postgres_queue.py:188, 217, 262`.

- **"Redelivered" and "tried too often" are kept as different facts.**
  `attempt` is incremented in exactly one place, the claim itself; the reaper
  then branches on the owning job's status and on the attempt count as separate
  axes. Conflating the two is how a finished result gets thrown away.
  → `postgres_queue.py:161`.

- **The reaper will not touch a result it did not produce.** Its dead-letter
  write names `status, error_json, updated_at, finished_at` and never
  `result_json`: a worker that wrote its result and then crashed must not have
  that result clobbered by the thing sent to clean up after it.
  → `postgres_queue.py::requeue_expired_leases`.

- **The queue shares a database with the domain store, deliberately.** The
  caller's connection is threaded through `enqueue` / `complete` / `fail`, so
  the task transition and the job's terminal write land in one commit. A broker
  puts them in two systems and makes that property unreachable without an
  outbox, a relay and a reconciler.
  → `monolith_worker.py:373`.

The invariant in the fifth bullet has a test that runs against a real database
and forces lease expiry with the database clock, so it skips on a plain
`pytest`. To watch it hold rather than take its word for it:

```bash
docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres --name edenn-pg postgres:16
RUN_ASYNC_V2_POSTGRES_INTEGRATION=1 DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres \
  pytest EdennCode/Deployment/async_pipeline_v2/Testing/test_reaper_durability.py -v
```

## 3 — The agentic layer: one manifest, nothing rendered in the loop

A conversational director that plans a soundtrack, gates on a plan the user has
seen, enqueues the work and hydrates results back.

- **One table generates four surfaces.** The model's decision schema
  (`models.py`), the argument validator (`tools/arg_specs.py`), the prompt's own
  tool documentation (`agent/prompts.py`) and the console's controls are all
  derived from a single tool manifest, so a capability cannot exist at one end
  and not the other.

- **The registry fails closed in both directions.** A tool with no spec will not
  build, and neither will a spec with no implementation, which is the half most
  codebases omit. The comment says why: a spec with no implementation is "a tool
  the model can name, the schema will accept, and the dispatcher cannot run — a
  promise the product does not keep."
  → `tools/base.py:260`.

- **Nothing is rendered inside the conversation loop.** Heavy tools enqueue and
  the result hydrates back on a later turn, so a dropped socket costs a reload
  rather than a take.
  → `agent/loop.py:699`.

- **It measures rather than asserts.** A rendered take is probed and the report
  the agent reads is arithmetic over those measurements, not a restatement of
  what was requested. The critics also declare what they could *not* check,
  instead of reporting clean by omission.

- **Editing reuses what was already made.** Re-reading one narration line
  re-records that line and keeps the rest; a composed master knows the identity
  and render epoch of every stem it was built from, and refuses to call itself
  current once one of them moves.

## Layout

| Path | What lives there |
|---|---|
| `EdennCode/Deployment/` | The HTTP API, authentication, billing, and the durable job pipeline. |
| `EdennCode/Deployment/async_pipeline_v2/` | The queue, workers, leases, and heartbeats. The most reusable thing here. |
| `EdennCode/EdennAgent/AgenticAudio/` | The conversational audio director: agent loop, tools, session state, and its browser console. |
| `EdennCode/WorkflowFactory/` | The generation workflows, stage by stage: scene segmentation, music planning, sound effects, voice-over, mixing. |
| `EdennCode/MusicGenerationCore/` | The provider abstraction: one model-spec vocabulary, one registry, one strategy per tier. |
| `EdennCode/ModelFactory/` | Adapters onto upstream providers. Every model-provider SDK import lives here (one speech-recognition package is imported lazily in `EdennAgent/Creation/`). |
| `EdennCode/Annotation/` | Event and telemetry capture across a pipeline run. |
| `EdennCode/Database/` | Schema migrations. |
| `EdennCode/TestSuites/`, `**/Testing/` | Tests. |
| `Frontend/console/` | The developer console: keys, usage, billing, and API documentation. |
| `terraform/modules/` | Reusable infrastructure modules. The environments that instantiated them were removed. |

## One more, because it shapes every response

**A vendor's name is a leak.** Nothing a client receives may identify which
upstream produced it: not a response field, not an error message, not a
filename. Two scrubbers enforce that at the boundary
(`Deployment/error_codes.py`, `Deployment/response_guardrail.py`), and their
vocabulary comes from configuration rather than from source — see
`Deployment/provider_vocabulary.py`. The collision rules there are more careful
than they look: a token must not match inside an ordinary word, and a model
family without a version digit is prose, not a model.

Read the narrowness honestly: the vocabulary comes from `PROVIDER_SCRUB_TOKENS`
and friends, so with nothing configured the free-text and URL passes match
nothing and only the field-name check is left. `guard_response_model` is wired
into the video-generation and multi-image response builders; the other APIs rely
on `error_codes` instead. The boundary is a mechanism plus a deployment
configuration, not a mechanism alone.

## Running the tests

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pytest
```

The default run makes **no network calls and spends nothing**: `pytest.ini`
deselects the `remote_integration` marker. That default is deliberate — a suite
that goes red on a laptop for want of a credential teaches everyone to ignore a
red suite.

Expect some failures. Media fixtures were removed from this snapshot
(REDACTIONS.md, rule `R2`), so tests that need real audio or video cannot pass
here. The unit and contract tests, which are the bulk of the suite, do.

## Configuration

`.env.example` lists every variable the code reads, with every value blank.
Nothing in it is real and nothing in it ever was. There is no configuration in
this repository that points at a running system.

## Checks

```bash
python tools/scan_secrets.py
python tools/check_provider_names.py
python tools/check_identifiers.py
```

These are the gates that produced this snapshot, committed so they keep running.
`check_provider_names.py` is the exception: its token list is deliberately not
committed, because writing the names down is the thing the rule forbids. It exits
non-zero and prints `NOT RUN` until `tools/provider-denylist.txt` is supplied —
refusing to run rather than reporting a pass it did not earn. See CONTRIBUTING.md.
`MANIFEST.tsv` records every file of the source repository and whether it was
kept or which rule excluded it.
