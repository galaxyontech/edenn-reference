-- 004_video_music.sql
-- Canonical recommendation entities for the discovery/feed agent.
-- Schema source of truth: EdennCode/Deployment/RECOMMENDATION_SCHEMA_DESIGN.md.
-- Additive only — does not modify legacy tables.

-- pgvector is provisioned by 000_extensions.sql; no need to repeat here.

CREATE TABLE IF NOT EXISTS video_asset (
    video_id            TEXT PRIMARY KEY,
    job_id              TEXT NOT NULL,
    source_video_blob   TEXT,
    source_video_url    TEXT,
    source_video_path   TEXT,
    duration_s          DOUBLE PRECISION,
    width               INTEGER,
    height              INTEGER,
    fps                 DOUBLE PRECISION,
    content_type        TEXT,
    scene_summary_json  JSONB NOT NULL DEFAULT '[]'::jsonb,
    visual_feature_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS music_asset (
    music_id              TEXT PRIMARY KEY,
    job_id                TEXT NOT NULL,
    variant_label         TEXT NOT NULL,
    provider_task_id      TEXT,
    provider_audio_id     TEXT,
    full_audio_blob       TEXT,
    full_audio_url        TEXT,
    full_audio_path       TEXT,
    matched_audio_blob    TEXT,
    matched_audio_url     TEXT,
    matched_audio_path    TEXT,
    lyrics_text           TEXT,
    lyrics_timestamp_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    music_feature_json    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS generation_job (
    job_id                  TEXT PRIMARY KEY,
    video_id                TEXT NOT NULL,
    creative_id             TEXT NOT NULL,
    primary_music_id        TEXT,
    secondary_music_id      TEXT,
    selected_music_id       TEXT NOT NULL,
    alignment_id            TEXT NOT NULL,
    model_spec              TEXT NOT NULL,
    include_vocals          BOOLEAN NOT NULL DEFAULT false,
    vocal_gender            TEXT NOT NULL DEFAULT '',
    user_prompt             TEXT NOT NULL DEFAULT '',
    user_requested_language TEXT NOT NULL DEFAULT '',
    preserve_original_audio BOOLEAN NOT NULL DEFAULT false,
    music_volume            DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    status                  TEXT NOT NULL DEFAULT 'completed',
    token_usage_json        JSONB NOT NULL DEFAULT '{}'::jsonb,
    job_received_timestamp  BIGINT,
    job_finished_timestamp  BIGINT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS creative (
    creative_id        TEXT PRIMARY KEY,
    job_id             TEXT NOT NULL,
    video_id           TEXT NOT NULL,
    selected_music_id  TEXT NOT NULL,
    alignment_id       TEXT NOT NULL,
    title              TEXT NOT NULL DEFAULT '',
    description        TEXT NOT NULL DEFAULT '',
    creator_user_id    TEXT,
    visibility         TEXT NOT NULL DEFAULT 'private',
    result_video_blob  TEXT,
    result_video_url   TEXT,
    result_video_path  TEXT,
    thumbnail_blob     TEXT,
    thumbnail_url      TEXT,
    thumbnail_path     TEXT,
    legacy_work_id     TEXT,
    render_status      TEXT NOT NULL DEFAULT 'completed',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS music_video_alignment (
    alignment_id              TEXT PRIMARY KEY,
    job_id                    TEXT NOT NULL,
    creative_id               TEXT NOT NULL,
    video_id                  TEXT NOT NULL,
    music_id                  TEXT NOT NULL,
    alignment_score           DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    selected_clip_start_s     DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    selected_clip_duration_s  DOUBLE PRECISION,
    matching_used_track       TEXT,
    alignment_reason_json     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS creative_feature_snapshot (
    creative_id          TEXT PRIMARY KEY,
    video_id             TEXT NOT NULL,
    selected_music_id    TEXT NOT NULL,
    alignment_id         TEXT NOT NULL,
    job_id               TEXT NOT NULL,
    language             TEXT NOT NULL DEFAULT '',
    music_model_spec     TEXT NOT NULL DEFAULT '',
    include_vocals       BOOLEAN NOT NULL DEFAULT false,
    vocal_gender         TEXT NOT NULL DEFAULT '',
    genre_level1         TEXT,
    content_type         TEXT,
    tempo_bpm            DOUBLE PRECISION,
    energy_level         TEXT,
    overall_mood         TEXT,
    platform_hint        TEXT,
    alignment_score      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    visual_feature_json  JSONB NOT NULL DEFAULT '{}'::jsonb,
    music_feature_json   JSONB NOT NULL DEFAULT '{}'::jsonb,
    scene_summary_json   JSONB NOT NULL DEFAULT '[]'::jsonb,
    music_prompt_json    JSONB NOT NULL DEFAULT '{}'::jsonb,
    visual_embedding     vector,
    music_embedding      vector,
    refreshed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_generation_job_video
    ON generation_job (video_id);

CREATE INDEX IF NOT EXISTS idx_music_asset_job
    ON music_asset (job_id);

CREATE INDEX IF NOT EXISTS idx_creative_feature_snapshot_coarse
    ON creative_feature_snapshot (
        language,
        genre_level1,
        content_type,
        energy_level,
        include_vocals
    );

CREATE INDEX IF NOT EXISTS idx_creative_feature_snapshot_alignment
    ON creative_feature_snapshot (alignment_score DESC);
