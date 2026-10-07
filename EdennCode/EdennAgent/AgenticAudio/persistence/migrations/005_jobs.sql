-- The studio's own job store.
--
-- The standalone deployment completes render jobs in-process, and until this
-- migration those jobs lived only in a Python dict. Sessions were durable; the
-- work they pointed at was not. Every scale-from-zero therefore left a session
-- holding a job id that existed nowhere, which the UI shows as a spinner that
-- never resolves, on a render the customer has already paid for.
--
-- These tables are deliberately NOT the fleet's async_v2_* tables even though
-- they carry the same rows. The studio and the worker fleet can be pointed at
-- one database, and the studio's completer claims any unfinished job it finds.
-- Sharing a table would mean a studio replica picking up a fleet job and
-- "completing" it in-process — the same class of cross-deployment theft that
-- an un-namespaced queue has already caused here once. Separate names make it
-- impossible rather than unlikely.

CREATE TABLE IF NOT EXISTS agentic_audio_jobs (
  job_id TEXT PRIMARY KEY,
  -- Cascading is not tidiness, it is the retention promise. Deleting a session
  -- already takes its messages, tool calls and comments, because "a partial
  -- delete leaves a person believing their footage is gone while the
  -- transcript that describes it is still there". A job row carries the prompt
  -- the user wrote, the analysis of their footage, and the URL of what was
  -- made from it — so it has to go the same way, and the database is a better
  -- place to guarantee that than a sweep someone has to remember to extend.
  -- NULL is allowed and is what a staged upload has, before any session.
  session_id TEXT REFERENCES agentic_audio_sessions(session_id) ON DELETE CASCADE,
  creator_user_id TEXT,
  job_type TEXT NOT NULL,
  status TEXT NOT NULL,
  current_stage TEXT,
  progress_percent DOUBLE PRECISION NOT NULL DEFAULT 0,
  priority INTEGER NOT NULL DEFAULT 0,
  request_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  result_json JSONB,
  error_json JSONB,
  -- Which process is running this, and when it last said so. Together they are
  -- the difference between "a sibling replica is generating this right now" and
  -- "the container that was generating this died". The first must be left
  -- alone; the second must never be silently generated again, because the
  -- provider call it was making may well have completed and been billed.
  runner_id TEXT,
  heartbeat_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ
);

-- The completer polls for open work on a short timer, so the index it uses is
-- partial: finished jobs accumulate forever and must not be walked.
CREATE INDEX IF NOT EXISTS agentic_audio_jobs_open_idx
ON agentic_audio_jobs (created_at)
WHERE status NOT IN ('completed', 'failed', 'canceled');

CREATE INDEX IF NOT EXISTS agentic_audio_jobs_session_idx
ON agentic_audio_jobs (session_id, created_at);

CREATE TABLE IF NOT EXISTS agentic_audio_job_artifacts (
  artifact_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES agentic_audio_jobs(job_id) ON DELETE CASCADE,
  artifact_type TEXT NOT NULL,
  role TEXT,
  container TEXT,
  blob_name TEXT,
  url TEXT,
  content_type TEXT,
  -- A path on whichever container wrote it. The row surviving a recycle does
  -- not make the file survive one; the media store is what restores the bytes.
  local_path TEXT,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  payload_json JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agentic_audio_job_artifacts_job_idx
ON agentic_audio_job_artifacts (job_id, artifact_type, role, created_at);

CREATE TABLE IF NOT EXISTS agentic_audio_job_events (
  event_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES agentic_audio_jobs(job_id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  stage_name TEXT,
  message TEXT,
  payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agentic_audio_job_events_job_idx
ON agentic_audio_job_events (job_id, created_at);
