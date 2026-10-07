-- Migration 006: align `creative` with the canonical schema in
-- RECOMMENDATION_SCHEMA_DESIGN.md. The original `creative` table predated
-- migration 004 and was created without `creator_user_id` / `visibility`,
-- so 004's CREATE TABLE IF NOT EXISTS was a no-op for those columns.
-- This migration is additive only.

ALTER TABLE creative
    ADD COLUMN IF NOT EXISTS creator_user_id TEXT;

ALTER TABLE creative
    ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'private';

CREATE INDEX IF NOT EXISTS idx_creative_creator_user_id
    ON creative (creator_user_id);

CREATE INDEX IF NOT EXISTS idx_creative_visibility_created_at
    ON creative (visibility, created_at DESC);
