-- Migration 002a: retrieval columns and indexes for requests.
-- Owned by Track A. Depends on 000_extensions (vector) and 001a_requests.
-- Spec: §3.1 retrieval columns, Q12 GIN index.

ALTER TABLE requests
  ADD COLUMN user_prompt_tsv TSVECTOR GENERATED ALWAYS AS (
    to_tsvector('english', coalesce(user_prompt, ''))
  ) STORED,
  ADD COLUMN user_prompt_embedding VECTOR(1536);

CREATE INDEX requests_user_prompt_tsv_idx       ON requests USING GIN (user_prompt_tsv);
CREATE INDEX requests_extracted_intent_gin      ON requests USING GIN (extracted_intent);
CREATE INDEX requests_user_prompt_embedding_idx
  ON requests USING hnsw (user_prompt_embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
