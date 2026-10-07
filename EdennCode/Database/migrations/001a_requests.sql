-- Migration 001a: requests table.
-- Owned by Track A (user intent).
-- Spec: §3.1.

CREATE TABLE requests (
  request_id           UUID PRIMARY KEY,
  endpoint             TEXT NOT NULL,
  api_key_id           TEXT,                                       -- reserved for future tenant tracking; NULL today
  received_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  responded_at         TIMESTAMPTZ,
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  http_status          INT,
  latency_ms           INT GENERATED ALWAYS AS (
                          (EXTRACT(EPOCH FROM (responded_at - received_at)) * 1000)::INT
                       ) STORED,

  user_prompt          TEXT,
  options              JSONB NOT NULL DEFAULT '{}',

  input_kind           TEXT,
  input_size_bytes     BIGINT,
  input_duration_s     NUMERIC(10,3),
  input_image_count    INT,
  input_source         TEXT,

  status               TEXT NOT NULL DEFAULT 'received'
                       CHECK (status IN ('received','succeeded','failed','timeout')),
  error_code           TEXT,
  error_message        TEXT,
  output_url           TEXT,

  -- Q12 decision: dedicated column for user-intent agent output
  extracted_intent     JSONB,

  metadata             JSONB NOT NULL DEFAULT '{}',

  CONSTRAINT requests_responded_after_received
    CHECK (responded_at IS NULL OR responded_at >= received_at),
  CONSTRAINT requests_succeeded_has_url
    CHECK (status <> 'succeeded' OR output_url IS NOT NULL)
);

CREATE INDEX requests_received_at_idx       ON requests (received_at DESC);
CREATE INDEX requests_updated_at_idx        ON requests (updated_at);
CREATE INDEX requests_endpoint_status_idx   ON requests (endpoint, status, received_at DESC);
CREATE INDEX requests_options_modelspec_idx ON requests ((options->>'modelspec'));
