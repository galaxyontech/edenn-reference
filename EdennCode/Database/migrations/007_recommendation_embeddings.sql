-- Migration 007: RAG embeddings for the recommendation read path.
-- Additive only. Embedding model assumed: embed-standard (1536-dim),
-- consistent with requests / pipeline_runs from migrations 002a / 002b.

-- 1. Ensure + pin vector dims on creative_feature_snapshot.
--    `DROP EXTENSION vector CASCADE` removes vector-typed columns from existing
--    tables. Repair runs can then skip 004's CREATE TABLE IF NOT EXISTS while
--    leaving these columns absent, so 007 must restore them before casting.
ALTER TABLE creative_feature_snapshot
    ADD COLUMN IF NOT EXISTS music_embedding  vector,
    ADD COLUMN IF NOT EXISTS visual_embedding vector;

ALTER TABLE creative_feature_snapshot
    ALTER COLUMN music_embedding  TYPE vector(1536) USING music_embedding::vector(1536);

ALTER TABLE creative_feature_snapshot
    ALTER COLUMN visual_embedding TYPE vector(1536) USING visual_embedding::vector(1536);

-- 2. Generation-side: per-job user-prompt embedding + creator_user_id.
ALTER TABLE generation_job
    ADD COLUMN IF NOT EXISTS user_prompt_embedding vector(1536);

ALTER TABLE generation_job
    ADD COLUMN IF NOT EXISTS creator_user_id TEXT;

-- 3. HNSW cosine indexes.
CREATE INDEX IF NOT EXISTS generation_job_user_prompt_embedding_idx
    ON generation_job USING hnsw (user_prompt_embedding vector_cosine_ops)
    WITH (m='16', ef_construction='64');

CREATE INDEX IF NOT EXISTS creative_feature_snapshot_music_embedding_idx
    ON creative_feature_snapshot USING hnsw (music_embedding vector_cosine_ops)
    WITH (m='16', ef_construction='64');

-- 4. B-tree on creator_user_id for fast filter + join.
CREATE INDEX IF NOT EXISTS idx_generation_job_creator_user_id
    ON generation_job (creator_user_id);
