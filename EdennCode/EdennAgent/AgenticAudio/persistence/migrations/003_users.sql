-- The studio's own users.
--
-- Deliberately separate from the platform's accounts/account_identities: the
-- studio ships as its own app, and coupling its schema to billing's would make
-- every future change to either one a change to both. What the two share is the
-- CREDENTIAL, not the table — a caller presents an ID token, the uid is the
-- principal, and that principal is this table's key.
--
-- `user_id` therefore holds whatever `resolve_caller` returned: a verified uid
-- in normal operation, or a legacy static-token user id during the transition.
-- No CHECK narrows it, because a row that cannot be written is a login that
-- cannot happen.

CREATE TABLE IF NOT EXISTS agentic_audio_users (
  user_id TEXT PRIMARY KEY,
  -- What collaborators see. Empty until the person sets one; the console falls
  -- back to the participant row and then to the principal itself, which is why
  -- an owner used to render as a raw uid.
  display_name TEXT NOT NULL DEFAULT '',
  -- The source that vouched for this principal ('identity' | 'legacy_token'),
  -- so a migration can find every account still resting on a static token.
  auth_source TEXT NOT NULL DEFAULT 'identity',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- "Who has not been back since?" — for retention and for finding dormant
-- accounts to clean up. Cheap to add now, painful to add later on a big table.
CREATE INDEX IF NOT EXISTS agentic_audio_users_last_seen_idx
ON agentic_audio_users(last_seen_at);

-- The sessions list is the most-hit query in the product and had no index for
-- the column it filters on: every "my sessions" was a sequential scan over
-- every session anyone had ever created.
CREATE INDEX IF NOT EXISTS agentic_audio_sessions_creator_idx
ON agentic_audio_sessions(creator_user_id, created_at DESC);

-- NOTE ON THE FOREIGN KEY.
--
-- `agentic_audio_sessions.creator_user_id` is intentionally NOT constrained to
-- this table yet. Existing rows carry creator ids that predate any users table
-- — request-body strings, static-token names, and NULL for sessions created
-- while auth was off — so adding the constraint now would either fail the
-- migration or require inventing a user row for every historical value.
--
-- The order that works is: backfill (below), verify nothing is orphaned, then
-- add the constraint in its own migration. Splitting it means the risky step is
-- reversible on its own.
--
--   ALTER TABLE agentic_audio_sessions
--     ADD CONSTRAINT agentic_audio_sessions_creator_fkey
--     FOREIGN KEY (creator_user_id) REFERENCES agentic_audio_users(user_id)
--     ON DELETE SET NULL;
--
-- ON DELETE SET NULL, not CASCADE: deleting an account must not silently
-- destroy the work, and what happens to a deleted user's sessions is a product
-- decision (see the account-deletion endpoint), not a database default.

-- Backfill a user row for every principal that already owns a session, so the
-- table is complete before anything depends on it being complete.
INSERT INTO agentic_audio_users (user_id, auth_source)
SELECT DISTINCT creator_user_id, 'legacy_token'
FROM agentic_audio_sessions
WHERE creator_user_id IS NOT NULL AND creator_user_id <> ''
ON CONFLICT (user_id) DO NOTHING;

-- Same for anyone who was only ever a collaborator.
INSERT INTO agentic_audio_users (user_id, auth_source)
SELECT DISTINCT user_id, 'legacy_token'
FROM agentic_audio_participants
WHERE user_id IS NOT NULL AND user_id <> ''
ON CONFLICT (user_id) DO NOTHING;
