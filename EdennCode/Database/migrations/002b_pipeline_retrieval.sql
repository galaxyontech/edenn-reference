-- Migration 002b: retrieval columns and indexes for pipeline_runs.
-- Owned by Track B. Depends on 000_extensions and 001b_pipeline.
-- Spec: §3.2 retrieval columns.

ALTER TABLE pipeline_runs
  ADD COLUMN summary_tsv TSVECTOR GENERATED ALWAYS AS (
    to_tsvector('english',
      coalesce(video_summary, '') || ' ' ||
      coalesce(music_prompt->>'global_music_prompt', '') || ' ' ||
      coalesce(music_prompt->>'style_prompt', ''))
  ) STORED,
  ADD COLUMN video_summary_embedding VECTOR(1536),
  ADD COLUMN music_prompt_embedding  VECTOR(1536);

CREATE INDEX pipeline_runs_summary_tsv_idx           ON pipeline_runs USING GIN (summary_tsv);
CREATE INDEX pipeline_runs_video_summary_embedding_idx
  ON pipeline_runs USING hnsw (video_summary_embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
CREATE INDEX pipeline_runs_music_prompt_embedding_idx
  ON pipeline_runs USING hnsw (music_prompt_embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
