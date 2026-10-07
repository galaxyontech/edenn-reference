-- Asset Understanding Layer (UNDERSTANDING_LAYER.md §4): three tables.
-- Refs are content + coordinates; annotations are append-only and versioned;
-- lineage is edges between refs. Everything project-scoped from day one.

CREATE TABLE IF NOT EXISTS aul_assets (
  asset_id TEXT PRIMARY KEY,              -- "asset_" + sha256[:12] (content-addressed)
  project_id TEXT NOT NULL DEFAULT 'default',
  kind TEXT NOT NULL,                     -- video | image | audio
  sha256 TEXT NOT NULL,
  name TEXT NOT NULL,
  uri TEXT NOT NULL,                      -- local path now; blob url when platform-wired
  duration_s DOUBLE PRECISION,
  width INTEGER,
  height INTEGER,
  generated BOOLEAN NOT NULL DEFAULT FALSE,  -- made-with-Edenn badge
  meta_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS aul_assets_sha_idx ON aul_assets(project_id, sha256);
CREATE INDEX IF NOT EXISTS aul_assets_project_idx ON aul_assets(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS aul_annotations (
  annotation_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL DEFAULT 'default',
  asset_id TEXT NOT NULL REFERENCES aul_assets(asset_id) ON DELETE CASCADE,
  span_start_s DOUBLE PRECISION,          -- NULL = whole asset
  span_end_s DOUBLE PRECISION,
  layer TEXT NOT NULL,                    -- L0 | L1 | L2 | L3 | outcome
  kind TEXT NOT NULL,                     -- e.g. scene, speech_span, beat_grid, signals, outcome_daily
  producer TEXT NOT NULL,                 -- "scene_detector@v1", "tiktok_ads@v1", ...
  inputs_hash TEXT,
  payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS aul_ann_asset_idx
ON aul_annotations(asset_id, layer, kind, span_start_s);
CREATE INDEX IF NOT EXISTS aul_ann_kind_idx
ON aul_annotations(project_id, kind, created_at DESC);

CREATE TABLE IF NOT EXISTS aul_edges (
  edge_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL DEFAULT 'default',
  src_ref TEXT NOT NULL,                  -- e.g. "asset_ab12#t=193.2-203.5"
  dst_ref TEXT NOT NULL,                  -- e.g. "asset_cd34" (an output is an asset too)
  operation TEXT NOT NULL,                -- slot_cut | music_for | narration_for | published_as | ...
  params_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  session_id TEXT,
  job_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS aul_edges_src_idx ON aul_edges(project_id, src_ref);
CREATE INDEX IF NOT EXISTS aul_edges_dst_idx ON aul_edges(project_id, dst_ref);
CREATE INDEX IF NOT EXISTS aul_edges_op_idx ON aul_edges(project_id, operation, created_at DESC);
