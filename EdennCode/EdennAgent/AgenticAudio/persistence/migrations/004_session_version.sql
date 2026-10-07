-- A version on every session row.
--
-- `state_json` is one document holding everything about a session, and every
-- writer does read-modify-write on it. Two writers that overlap do not merge:
-- the second writes a document built from a copy that predates the first, and
-- the first's change disappears with no error anywhere.
--
-- The version does not prevent that on its own — a row lock does, and
-- `mutate_session_state` takes one. What the version buys is DETECTION: a
-- caller that knows which version it read can ask for compare-and-set and fail
-- loudly, and after the fact a stalled version is the evidence that says
-- "something overwrote this" instead of leaving you guessing.
--
-- Starts at 1 for existing rows so "never written since the upgrade" and
-- "written once" are distinguishable.

ALTER TABLE agentic_audio_sessions
  ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 1;
