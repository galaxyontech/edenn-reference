-- Migration 001b: pipeline_runs + pipeline_stages tables.
-- Owned by Track B (pipeline metadata).
-- Spec: §3.2, §3.3.
-- Depends on 001a_requests (FK target).

CREATE TABLE pipeline_runs (
  run_id               UUID PRIMARY KEY,
  request_id           UUID NOT NULL REFERENCES requests(request_id) ON DELETE CASCADE,
  workflow_type        TEXT NOT NULL,
  workflow_version     TEXT NOT NULL,
  modelspec            TEXT,

  started_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at          TIMESTAMPTZ,
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  duration_ms          INT GENERATED ALWAYS AS (
                          (EXTRACT(EPOCH FROM (finished_at - started_at)) * 1000)::INT
                       ) STORED,

  status               TEXT NOT NULL DEFAULT 'running'
                       CHECK (status IN ('running','succeeded','failed','cancelled')),
  failed_at_stage      TEXT,
  error_code           TEXT,
  error_message        TEXT,

  total_input_tokens   INT NOT NULL DEFAULT 0,
  total_output_tokens  INT NOT NULL DEFAULT 0,

  video_summary        TEXT,
  music_prompt         JSONB,
  music_provider       TEXT,

  metadata             JSONB NOT NULL DEFAULT '{}',

  CONSTRAINT pipeline_runs_token_totals_nonneg
    CHECK (total_input_tokens >= 0 AND total_output_tokens >= 0)
);

CREATE INDEX pipeline_runs_request_idx     ON pipeline_runs (request_id);
CREATE INDEX pipeline_runs_updated_at_idx  ON pipeline_runs (updated_at);
CREATE INDEX pipeline_runs_workflow_idx    ON pipeline_runs (workflow_type, started_at DESC);
CREATE INDEX pipeline_runs_modelspec_idx   ON pipeline_runs (modelspec, started_at DESC);


CREATE TABLE pipeline_stages (
  stage_id             BIGSERIAL PRIMARY KEY,
  run_id               UUID NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
  stage_name           TEXT NOT NULL,
  stage_index          SMALLINT NOT NULL,

  started_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at          TIMESTAMPTZ,
  duration_ms          INT GENERATED ALWAYS AS (
                          (EXTRACT(EPOCH FROM (finished_at - started_at)) * 1000)::INT
                       ) STORED,

  status               TEXT NOT NULL DEFAULT 'running'
                       CHECK (status IN ('running','succeeded','failed','skipped')),
  error_code           TEXT,
  error_message        TEXT,

  provider             TEXT,
  model                TEXT,
  input_tokens         INT,
  output_tokens        INT,

  output               JSONB,
  metadata             JSONB NOT NULL DEFAULT '{}',

  CONSTRAINT pipeline_stages_output_size_cap
    CHECK (output IS NULL OR octet_length(output::text) <= 262144),
  CONSTRAINT pipeline_stages_tokens_nonneg
    CHECK ((input_tokens IS NULL OR input_tokens >= 0)
       AND (output_tokens IS NULL OR output_tokens >= 0))
);

CREATE UNIQUE INDEX pipeline_stages_run_index_unique  ON pipeline_stages (run_id, stage_index);
CREATE INDEX pipeline_stages_name_status_idx          ON pipeline_stages (stage_name, status, started_at DESC);
CREATE INDEX pipeline_stages_provider_idx             ON pipeline_stages (provider, model, started_at DESC);
CREATE INDEX pipeline_stages_output_gin               ON pipeline_stages USING GIN (output);
