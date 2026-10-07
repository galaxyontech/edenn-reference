-- Collab mode: comment threads anchored to lineage nodes, per-session
-- participants (share roles), and per-user read receipts. Additive; 001 must
-- run first (sessions FK). All statements idempotent.

CREATE TABLE IF NOT EXISTS agentic_audio_comment_threads (
  thread_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES agentic_audio_sessions(session_id) ON DELETE CASCADE,
  anchor_node_id TEXT NOT NULL,
  anchor_label TEXT,
  anchor_start_s DOUBLE PRECISION,
  anchor_end_s DOUBLE PRECISION,
  status TEXT NOT NULL DEFAULT 'open',
  resolved_by TEXT,
  resolved_at TIMESTAMPTZ,
  created_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agentic_audio_comment_threads_session_idx
ON agentic_audio_comment_threads(session_id, created_at, thread_id);

CREATE TABLE IF NOT EXISTS agentic_audio_comments (
  comment_id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL REFERENCES agentic_audio_comment_threads(thread_id) ON DELETE CASCADE,
  session_id TEXT NOT NULL REFERENCES agentic_audio_sessions(session_id) ON DELETE CASCADE,
  author_id TEXT NOT NULL,
  author_name TEXT,
  author_kind TEXT NOT NULL DEFAULT 'user',
  body TEXT NOT NULL,
  mentions JSONB NOT NULL DEFAULT '[]'::jsonb,
  attachments JSONB NOT NULL DEFAULT '[]'::jsonb,
  reactions JSONB NOT NULL DEFAULT '{}'::jsonb,
  edited_at TIMESTAMPTZ,
  deleted_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agentic_audio_comments_thread_idx
ON agentic_audio_comments(thread_id, created_at, comment_id);

CREATE INDEX IF NOT EXISTS agentic_audio_comments_session_idx
ON agentic_audio_comments(session_id, created_at);

CREATE TABLE IF NOT EXISTS agentic_audio_participants (
  session_id TEXT NOT NULL REFERENCES agentic_audio_sessions(session_id) ON DELETE CASCADE,
  user_id TEXT NOT NULL,
  display_name TEXT,
  role TEXT NOT NULL DEFAULT 'comment',
  added_by TEXT,
  added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, user_id)
);

CREATE TABLE IF NOT EXISTS agentic_audio_thread_reads (
  thread_id TEXT NOT NULL REFERENCES agentic_audio_comment_threads(thread_id) ON DELETE CASCADE,
  user_id TEXT NOT NULL,
  last_read_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (thread_id, user_id)
);
