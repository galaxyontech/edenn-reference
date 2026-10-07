# Async Pipeline V2 Workspace

This directory is intentionally separate from the existing deployed video endpoints.

The current endpoints remain untouched:

- `POST /api/v1/jobs/video`
- `POST /api/v1/jobs/async_video_music_gen`
- `GET /api/v1/jobs/async_video_music_gen/{job_id}`

Purpose of this workspace:

- design and implement a durable async pipeline runner
- support horizontal worker scaling
- recover long-running jobs after API/worker restarts
- prepare the backend for future agentic orchestration across music, voice-over, SFX, and final composition

Start with:

- `IMPLEMENTATION_PLAN.md`
- `docs/async-v2-split-data-flow.md`
- `docs/async-v2-container-app-topology.md`

Current implemented foundation:

- typed v2 job/task/stage/artifact/event models
- idempotent Postgres schema migration
- repository methods for jobs, stage runs, artifacts, events, and status view
- Postgres pull queue with priority leasing, heartbeat, retry, dead-letter, cancellation, and stale lease requeue
- artifact staging service for local paths, uploaded bytes, and remote video URLs
- source-video artifact metadata with SHA-256, ffprobe media metadata, content type, blob name, optional URL, and original request fields
- monolithic video-music worker that leases `video_music_monolith` tasks, calls the existing `VideoGenerationOrchestrator.run()` unchanged, records output artifacts, and completes/fails durable job state
- opt-in v2 API router for staging source videos, creating durable video-music jobs, reading status/events, and canceling queued work
- four-stage split worker path with typed stage input/output contracts:
  - `video_preprocess`
  - `analysis_and_planning`
  - `provider_candidate_generation`
  - `selection_ranking_remix_finalize`
- provider-specific generation queues:
  - `music-basic`
  - `music-enhanced`
  - `music-studio`
- split-stage retry hardening for cancellation checks and artifact reuse on retries
- focused tests with opt-in real Postgres, storage, and provider integration coverage

Target no-redownload deployment topology:

- `worker-media` owns `video-preprocess`, `analysis-and-planning`, and `selection-ranking-remix-finalize`
- `worker-provider` owns `music-basic`, `music-enhanced`, and `music-studio`
- `worker-provider` must not resolve, download, or open video bytes

Queue isolation for canaries and e2e tests:

- default queue names stay unchanged in normal deployments
- set `ASYNC_V2_QUEUE_NAMESPACE=<name>` to make the API enqueue and workers lease namespaced queues such as `<name>:video-preprocess`
- use a unique namespace for local real-provider e2e tests that share a dev Postgres database with deployed workers

Test commands:

```bash
.venv/bin/python -m pytest EdennCode/Deployment/async_pipeline_v2/Testing -q
RUN_ASYNC_V2_POSTGRES_INTEGRATION=1 .venv/bin/python -m pytest EdennCode/Deployment/async_pipeline_v2/Testing -q
RUN_ASYNC_V2_POSTGRES_INTEGRATION=1 RUN_ASYNC_V2_STORAGE_INTEGRATION=1 .venv/bin/python -m pytest EdennCode/Deployment/async_pipeline_v2/Testing -q
RUN_ASYNC_V2_REAL_PROVIDER_INTEGRATION=1 RUN_ASYNC_V2_POSTGRES_INTEGRATION=1 RUN_ASYNC_V2_STORAGE_INTEGRATION=1 .venv/bin/python -m pytest EdennCode/Deployment/async_pipeline_v2/Testing/test_async_pipeline_v2_monolith_worker.py -q -k real_provider
RUN_ASYNC_V2_REAL_PROVIDER_INTEGRATION=1 RUN_ASYNC_V2_POSTGRES_INTEGRATION=1 RUN_ASYNC_V2_STORAGE_INTEGRATION=1 .venv/bin/python -m pytest EdennCode/Deployment/async_pipeline_v2/Testing/test_async_pipeline_v2_split_stage_workers.py -q -k real_provider_split_pipeline_from_api_for_modelspec
ASYNC_V2_QUEUE_NAMESPACE=local-e2e RUN_ASYNC_V2_REAL_PROVIDER_CONCURRENT=1 RUN_ASYNC_V2_REAL_PROVIDER_INTEGRATION=1 RUN_ASYNC_V2_POSTGRES_INTEGRATION=1 RUN_ASYNC_V2_STORAGE_INTEGRATION=1 .venv/bin/python -m pytest EdennCode/Deployment/async_pipeline_v2/Testing/test_async_pipeline_v2_split_stage_workers.py -q -k three_concurrent
```
