CREATE TABLE IF NOT EXISTS agentic_audio_sessions (
  session_id TEXT PRIMARY KEY,
  source_video_artifact_id TEXT NOT NULL,
  creator_user_id TEXT,
  status TEXT NOT NULL,
  phase TEXT NOT NULL,
  selected_candidate_id TEXT,
  linked_job_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS agentic_audio_sessions_status_idx
ON agentic_audio_sessions(status, phase, created_at);

CREATE TABLE IF NOT EXISTS agentic_audio_messages (
  message_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES agentic_audio_sessions(session_id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agentic_audio_messages_session_idx
ON agentic_audio_messages(session_id, created_at, message_id);

CREATE TABLE IF NOT EXISTS agentic_audio_tool_calls (
  tool_call_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES agentic_audio_sessions(session_id) ON DELETE CASCADE,
  tool_name TEXT NOT NULL,
  status TEXT NOT NULL,
  input_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  output_json JSONB,
  error_json JSONB,
  linked_job_id TEXT,
  linked_artifact_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS agentic_audio_tool_calls_session_idx
ON agentic_audio_tool_calls(session_id, created_at, tool_call_id);

CREATE TABLE IF NOT EXISTS agentic_audio_choices (
  choice_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES agentic_audio_sessions(session_id) ON DELETE CASCADE,
  choice_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agentic_audio_choices_session_idx
ON agentic_audio_choices(session_id, created_at, choice_id);
