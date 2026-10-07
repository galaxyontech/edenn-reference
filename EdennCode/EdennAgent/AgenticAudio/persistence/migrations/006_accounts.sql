-- The account a principal belongs to, and the person a job was actually run by.
--
-- Two separate holes, both of which have to be closed before anything can
-- charge for a render.
--
-- 1. WHO PAYS. The studio knows a principal (a verified uid). Money lives on a
--    platform ACCOUNT. Nothing connected the two, so there was no answer to
--    "whose balance does this come out of" — not a wrong answer, an absent one.
--    `account_id` is nullable because it genuinely is unknown for a uid that
--    has never signed up, and because the lookup is allowed to be unavailable;
--    a NULL here means "not established", never "free".
--
-- 2. WHO SPENT. Every job row was stamped with the session's OWNER, whoever
--    actually pressed the button. A collaborator invited to iterate spends,
--    and the row says the owner did — while the rate limiter and the audit log
--    (which see the real actor) say something else. Attribution that disagrees
--    with itself cannot become a bill.

ALTER TABLE agentic_audio_users
  ADD COLUMN IF NOT EXISTS account_id TEXT;

-- Partial: most rows have no account while the link is being rolled out, and
-- the question asked of this column is always "which users belong to account
-- X", never "which have none".
CREATE INDEX IF NOT EXISTS agentic_audio_users_account_idx
ON agentic_audio_users(account_id)
WHERE account_id IS NOT NULL;

ALTER TABLE agentic_audio_jobs
  ADD COLUMN IF NOT EXISTS actor_user_id TEXT;

-- Existing rows: the owner is the only id the row carries, so it is the best
-- available answer and an honest one for the single-user sessions that are
-- almost all of them. Re-runnable by the WHERE clause, and it never overwrites
-- an actor a newer row already recorded.
UPDATE agentic_audio_jobs
SET actor_user_id = creator_user_id
WHERE actor_user_id IS NULL AND creator_user_id IS NOT NULL;

-- The foreign key 003 deliberately postponed.
--
-- 003 backfilled a user row for every principal that owned a session or held a
-- membership, then stopped: adding the constraint in the same migration as the
-- backfill would have failed on any historical value it could not account for.
-- The order it wrote down is backfill, verify, constrain — this is the third
-- step, and it repeats the first because principals have arrived since.

INSERT INTO agentic_audio_users (user_id, auth_source)
SELECT DISTINCT creator_user_id, 'legacy_token'
FROM agentic_audio_sessions
WHERE creator_user_id IS NOT NULL AND creator_user_id <> ''
ON CONFLICT (user_id) DO NOTHING;

INSERT INTO agentic_audio_users (user_id, auth_source)
SELECT DISTINCT user_id, 'legacy_token'
FROM agentic_audio_participants
WHERE user_id IS NOT NULL AND user_id <> ''
ON CONFLICT (user_id) DO NOTHING;

-- A session created while auth was off carries an EMPTY creator, not a NULL
-- one, and the backfill skips empties on purpose (there is no such user). The
-- two already mean the same thing to every reader — ownership compares
-- `creator_user_id or ""` — so this changes no behaviour and lets the
-- constraint hold.
UPDATE agentic_audio_sessions SET creator_user_id = NULL WHERE creator_user_id = '';

-- Postgres has no ADD CONSTRAINT IF NOT EXISTS, and a migration that cannot be
-- run twice is a migration that takes the service down when a database is
-- restored from a backup whose ledger predates it.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'agentic_audio_sessions_creator_fkey'
  ) THEN
    ALTER TABLE agentic_audio_sessions
      ADD CONSTRAINT agentic_audio_sessions_creator_fkey
      FOREIGN KEY (creator_user_id) REFERENCES agentic_audio_users(user_id)
      ON DELETE SET NULL;
  END IF;
END
$$;
