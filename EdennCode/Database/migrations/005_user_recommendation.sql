-- Migration 005: user-side recommendation tables in the Video-Music DB.
-- Mirrors the schema created by
-- EdennCode/Deployment/Testing/populate_recommendation_tables.py
-- in the legacy User DB (DB B). Going forward writes land in DB A; reads
-- still fall back to DB B for legacy users until backfill is complete.

CREATE TABLE IF NOT EXISTS user_profile (
    user_id           VARCHAR(64) PRIMARY KEY,
    locale            VARCHAR(20),
    language          VARCHAR(30),
    timezone          VARCHAR(50),
    platform          VARCHAR(20),
    signup_date       TIMESTAMP,
    subscription_tier VARCHAR(20) NOT NULL DEFAULT 'free',
    is_active         BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMP   NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_creation_summary (
    user_id                VARCHAR(64) PRIMARY KEY REFERENCES user_profile(user_id),
    total_creations        INTEGER     NOT NULL DEFAULT 0,
    first_creation_time    TIMESTAMP,
    last_creation_time     TIMESTAMP,
    preferred_language     VARCHAR(30),
    preferred_model        VARCHAR(50),
    preferred_voice        VARCHAR(30),
    language_distribution  JSONB,
    model_distribution     JSONB,
    voice_distribution     JSONB,
    avg_duration_seconds   NUMERIC(8,2),
    total_tokens_used      BIGINT      NOT NULL DEFAULT 0,
    music_taxonomy         JSONB,
    semantic_style_summary TEXT,
    updated_at             TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_interaction_event (
    id                       BIGSERIAL   PRIMARY KEY,
    user_id                  VARCHAR(64) NOT NULL,
    work_id                  VARCHAR(64) NOT NULL,
    event_type               VARCHAR(30) NOT NULL,
    watch_duration_ms        INTEGER,
    watch_completion_pct     NUMERIC(5,2),
    audio_listen_duration_ms INTEGER,
    session_id               VARCHAR(64),
    platform                 VARCHAR(20),
    event_time               TIMESTAMP   NOT NULL DEFAULT NOW(),
    metadata                 JSONB
);

CREATE INDEX IF NOT EXISTS idx_uie_user_time ON user_interaction_event (user_id, event_time);
CREATE INDEX IF NOT EXISTS idx_uie_work_type ON user_interaction_event (work_id, event_type);
