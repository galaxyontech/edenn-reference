CREATE TABLE IF NOT EXISTS async_v2_jobs (
  job_id TEXT PRIMARY KEY,
  session_id TEXT,
  creator_user_id TEXT,
  job_type TEXT NOT NULL,
  status TEXT NOT NULL,
  current_stage TEXT,
  progress_percent DOUBLE PRECISION NOT NULL DEFAULT 0,
  priority INTEGER NOT NULL DEFAULT 0,
  request_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  result_json JSONB,
  error_json JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS async_v2_jobs_status_idx
ON async_v2_jobs(status, priority DESC, created_at);

CREATE TABLE IF NOT EXISTS async_v2_tasks (
  task_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES async_v2_jobs(job_id) ON DELETE CASCADE,
  queue_name TEXT NOT NULL,
  task_type TEXT NOT NULL,
  status TEXT NOT NULL,
  payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  priority INTEGER NOT NULL DEFAULT 0,
  attempt INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  lease_owner TEXT,
  lease_until TIMESTAMPTZ,
  not_before TIMESTAMPTZ NOT NULL DEFAULT now(),
  idempotency_key TEXT,
  last_error_json JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS async_v2_tasks_ready_idx
ON async_v2_tasks(queue_name, status, not_before, priority DESC, created_at);

CREATE INDEX IF NOT EXISTS async_v2_tasks_job_idx
ON async_v2_tasks(job_id, created_at);

CREATE TABLE IF NOT EXISTS async_v2_stage_runs (
  stage_run_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES async_v2_jobs(job_id) ON DELETE CASCADE,
  task_id TEXT REFERENCES async_v2_tasks(task_id) ON DELETE SET NULL,
  stage_name TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  input_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  output_json JSONB,
  error_json JSONB,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  heartbeat_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS async_v2_stage_runs_job_idx
ON async_v2_stage_runs(job_id, started_at);

CREATE TABLE IF NOT EXISTS async_v2_artifacts (
  artifact_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES async_v2_jobs(job_id) ON DELETE CASCADE,
  artifact_type TEXT NOT NULL,
  role TEXT,
  container TEXT,
  blob_name TEXT,
  url TEXT,
  content_type TEXT,
  local_path TEXT,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  payload_json JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE async_v2_artifacts
ADD COLUMN IF NOT EXISTS payload_json JSONB;

CREATE INDEX IF NOT EXISTS async_v2_artifacts_job_idx
ON async_v2_artifacts(job_id, artifact_type, role, created_at);

CREATE TABLE IF NOT EXISTS async_v2_job_events (
  event_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES async_v2_jobs(job_id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  stage_name TEXT,
  message TEXT,
  payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS async_v2_job_events_job_idx
ON async_v2_job_events(job_id, created_at);

-- Content-addressed cache shared across jobs so identical source videos are not
-- recompressed or re-analyzed. Keyed by content sha + params + key_version, not by
-- job or URL. `status` supports single-flight: a 'pending' row reserves the compute
-- while one worker produces the result, and 'ready' rows are servable cache hits.
CREATE TABLE IF NOT EXISTS async_v2_cache (
  cache_key TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  content_sha TEXT,
  key_version TEXT,
  status TEXT NOT NULL DEFAULT 'ready',
  payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  size_bytes BIGINT,
  hit_count BIGINT NOT NULL DEFAULT 0,
  lease_owner TEXT,
  lease_until TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_accessed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS async_v2_cache_lru_idx
ON async_v2_cache(kind, last_accessed_at);

CREATE INDEX IF NOT EXISTS async_v2_cache_expiry_idx
ON async_v2_cache(expires_at);
