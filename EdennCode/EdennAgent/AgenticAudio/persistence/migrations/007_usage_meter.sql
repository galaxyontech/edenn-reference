-- What a render actually consumed. Counts only; no money.
--
-- The studio's spend is currently recorded nowhere. A failed paid generation,
-- an interrupted one and one that was never started are indistinguishable
-- after the fact, and there is no answer to "what did this customer use".
--
-- Two rules shape every column below.
--
-- 1. THE BILL OUTLIVES THE FOOTAGE. agentic_audio_jobs cascades away with its
--    session (005_jobs.sql:27) and the retention sweep is live and now deletes
--    the stored media too (retention.py:219,233 -- 7 days, retention.py:48).
--    So the session link here is ON DELETE SET NULL: deleting a session takes
--    everything that describes the video and leaves the fact that money was
--    spent. The database guarantees it so no deletion path has to remember.
--
-- 2. A ROW MUST NEVER DESCRIBE THE FOOTAGE. That is what makes rule 1 safe.
--    No prompt, script, lyric, label, title, scene text, filename, URL, blob
--    name, artifact id, detected language or error string. No vendor, product
--    or model name -- models.provider_for_modelspec (models.py:544) returns
--    upstream company names and must never be called on the way in here. A
--    count of seconds is not a description of a video; a scene summary is.
--    If a column cannot be printed on an invoice, it does not belong here.

CREATE TABLE IF NOT EXISTS agentic_audio_usage (
  -- The row's own handle, for a support conversation and a log line. Separate
  -- from meter_key below so a minted uuid is never mistaken for the identity
  -- of the spend unit.
  usage_id         TEXT PRIMARY KEY,

  -- THE IDENTITY OF THE SPEND UNIT. Every double-write path in section 5 is
  -- made safe by the unique index on this column and nothing else. Shapes:
  --   job:{job_id}:a{attempt}:{site}    one render attempt, one site
  --   turn:{session_id}:{uuid4}         one reasoning turn
  --   analysis:{tool_call_id}           one understanding run with a tool call
  --   analysis:{session_id}:{uuid4}     one without (the bootstrap path)
  --   boot:{runner_id}:probe            the startup reachability call
  -- NOT the turn ordinal: loop.py:933 writes len(turns)+1 while loop.py:957
  -- caps the list at 100, so from turn 101 the ordinal is permanently 101 and
  -- a key built from it silently stops recording.
  meter_key        TEXT NOT NULL,

  -- The invoice line's category, and the only axis pricing needs. The studio's
  -- own vocabulary, never a vendor's. music_edit is deliberately NOT here: it
  -- is the same job type through the same enqueue at the same cost, and the
  -- payload carries no marker to tell them apart (tools/media.py:1774-1800).
  site             TEXT NOT NULL CHECK (site IN (
                     'reasoning_turn','understanding','music','music_restyle',
                     'narration','sfx_events','sfx_bed')),

  -- Which process metered this. The same job type is metered by two processes
  -- with two different guarantees -- one can join the job's own transaction,
  -- one cannot -- and a reconciler has to know which applied to the row it is
  -- reading. It also explains why a standalone row always has attempt 1.
  deployment       TEXT NOT NULL CHECK (deployment IN ('standalone','fleet')),

  -- The render this belongs to. Plain text, no FK: it cascades away with the
  -- session, and under api/router.py:343's default it lives in another
  -- database entirely. NULL for the three sites that are not jobs.
  job_id           TEXT,

  -- The worker fleet re-leases a task up to max_attempts (3 for music, edit
  -- and narration; 1 for SFX -- tools/media.py:1849,1946,2074,2170) and each
  -- lease can submit to the provider again. A job-level row records one spend
  -- for three. Always 1 in the standalone, which refuses to re-dispatch a
  -- terminal job (devserver.py:2132-2136) and has no attempt column.
  attempt          INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),

  -- THE ONE USER ACTION this row belongs to. generate_candidates runs
  -- `for index in range(count)` (tools/media.py:857) with a default of 2 on
  -- the two premium tiers (models.py:616-624), so one approval is two
  -- independent jobs. Without this a customer sees two charges for one click
  -- and nothing can explain it. Holds the proposal_id for takes, the
  -- voice-over id for narration, the sfx variant id for effects.
  group_ref        TEXT,

  -- The retention answer. SET NULL, not CASCADE -- see rule 1 at the top.
  session_id       TEXT REFERENCES agentic_audio_sessions(session_id)
                   ON DELETE SET NULL,

  -- Who pressed the button, which is not always whose session it is
  -- (006_accounts.sql). No FK: account deletion is a bare DELETE on
  -- agentic_audio_users and a money record must neither block it nor vanish
  -- with it.
  actor_user_id    TEXT,
  creator_user_id  TEXT,
  -- The account link as it stood at the moment of the spend, stamped rather
  -- than looked up later because the link can change and a bill may not.
  -- NULL means "not established" and never "free" (006_accounts.sql:9-11).
  account_id       TEXT,

  -- Which process wrote this. Two rows naming two runners for one job is the
  -- only visible trace of a swallowed lease loss (worker_heartbeat.py:89).
  runner_id        TEXT,

  -- The tier that ACTUALLY rendered, from rendered_modelspec (devserver.py:122)
  -- -- never request_json["modelspec"], because devserver.py:1975-1990
  -- substitutes a tier when the requested provider has no key on the box and
  -- billing the requested one would be a lie. tier_requested is stored beside
  -- it so an inequality is a support answer instead of a mystery.
  tier             TEXT CHECK (tier IS NULL OR tier IN
                     ('edenn_basic','edenn_enhanced','edenn_studio')),
  tier_requested   TEXT CHECK (tier_requested IS NULL OR tier_requested IN
                     ('edenn_basic','edenn_enhanced','edenn_studio')),

  -- Which synthesis route actually ran, where more than one exists and they
  -- are charged differently. Today only the ambience bed: 'text' (per call) or
  -- 'video_native' (per second). Recorded AFTER the runtime fallback at
  -- sound_effect_generation_stage.py:244, never as requested.
  route            TEXT CHECK (route IS NULL OR route IN ('text','video_native')),

  -- Which measuring rules produced this row. A later correction to HOW a
  -- quantity is derived must be distinguishable from a change in what it
  -- costs, or re-pricing history silently re-measures it too.
  meter_version    SMALLINT NOT NULL DEFAULT 1,

  -- Which quantity column a pricing pass should read for this row. A label,
  -- not a constraint: every dimension that was free to measure is stored
  -- anyway, because a count can be re-priced and a dimension never written
  -- cannot be re-measured after the media is deleted on day 7.
  primary_unit     TEXT CHECK (primary_unit IS NULL OR primary_unit IN
                     ('delivered_ms','items','source_ms','lm_input_tokens')),

  -- open   : claimed; something may be spending right now, or a process died
  --          holding this.
  -- closed : nothing further will be learned by waiting.
  state            TEXT NOT NULL CHECK (state IN ('open','closed')),

  -- delivered          spent, and the customer got it
  -- failed_after_spend spent, and the customer got nothing
  -- spend_unknown      claimed, then silence. RECONCILE against the vendor.
  -- no_spend           reached terminal without calling a provider
  outcome          TEXT CHECK (outcome IS NULL OR outcome IN
                     ('delivered','failed_after_spend','spend_unknown','no_spend')),

  -- ---- the quantities --------------------------------------------------
  -- Every one NULLABLE, and NULL means UNMEASURED, never zero: a render that
  -- died after paying must not look free. Zero is reserved for "we know it
  -- made none", and is written only when a render REPORTED making none. This
  -- mirrors the rule the reasoning accumulator already follows at
  -- loop.py:246-250. Integer milliseconds, never floats: the platform's own
  -- duration path ceilings a float and buys a spare block at the boundary
  -- (Deployment/billing/engine.py), and an integer cannot do that.

  -- Milliseconds of media actually handed over. The number a customer
  -- recognises, and the difference between "we paid and delivered" and "we
  -- paid and delivered nothing" (NULL on a failure).
  delivered_ms     BIGINT CHECK (delivered_ms IS NULL OR delivered_ms >= 0),
  -- Milliseconds the provider produced before our cut. A 3-minute track
  -- delivered as a 20-second window is two different numbers and both matter.
  produced_ms      BIGINT CHECK (produced_ms IS NULL OR produced_ms >= 0),
  -- Milliseconds ASKED OF the provider. edenn_basic is charged on this, not on
  -- what was delivered: to_eleven_ms floors at 10 000 ms
  -- (music_generation_stage.py:244), so a 4-second clip buys 10 seconds.
  requested_ms     BIGINT CHECK (requested_ms IS NULL OR requested_ms >= 0),
  -- Milliseconds of the customer's own footage the work was done over. The
  -- understanding unit, and the only quantity a customer fully controls.
  source_ms        BIGINT CHECK (source_ms IS NULL OR source_ms >= 0),

  -- Paid upstream generation operations. For music this is
  -- generation_api_call_count = 1 + extension_count
  -- (music_generation_stage.py:927), which counts TRACK generations only --
  -- the lyric and timestamped-lyric round-trips beside it (:785, :908) are
  -- real spend and are not in this number. Recorded; see note_code.
  provider_calls   INTEGER CHECK (provider_calls IS NULL OR provider_calls >= 0),

  -- Discrete deliverables, and the ones that cost nothing because a previous
  -- render already bought them. The reuse paths exist precisely so fixing one
  -- hit in a bed of twelve does not re-charge for eleven; a meter that ignores
  -- them re-charges for eleven.
  items            INTEGER CHECK (items IS NULL OR items >= 0),
  items_reused     INTEGER CHECK (items_reused IS NULL OR items_reused >= 0),

  -- Characters handed to a speech engine, counted at the synthesize boundary
  -- so a retake counts its line twice and a kept line not at all. Provenance,
  -- not the price basis: the expressive model prepends a delivery tag that is
  -- billed and is not in this number (hosted_speech_synthesizer.py:188 vs :211).
  text_chars       INTEGER CHECK (text_chars IS NULL OR text_chars >= 0),

  -- Prompt and completion are priced differently and are never summed.
  lm_input_tokens  BIGINT CHECK (lm_input_tokens IS NULL OR lm_input_tokens >= 0),
  lm_output_tokens BIGINT CHECK (lm_output_tokens IS NULL OR lm_output_tokens >= 0),
  -- Upstream ATTEMPTS, including ones that raised. RetryingLLM
  -- (devserver.py:257-278) retries only on exception and raises on exhaustion,
  -- so an attempt above the reported call may or may not have been billed.
  -- This is deliberately NOT provider_calls: it is the signal that a degraded
  -- endpoint is multiplying round-trips, and nothing more.
  lm_attempts      INTEGER CHECK (lm_attempts IS NULL OR lm_attempts >= 0),

  -- A short code from a frozen catalogue in persistence/usage.py. NEVER free
  -- text and never an exception string: those carry vendor identity and
  -- sometimes the user's own prompt (devserver.py:2016 already has to scrub
  -- error text before it reaches a client).
  note_code        TEXT CHECK (note_code IS NULL OR length(note_code) <= 64),

  -- Provenance that explains a number but is never multiplied by a price:
  -- num_variants, spotting mode, edit_kind, scene_count, group size, reuse
  -- misses, over_budget, the per-stage token breakdown. The content ban at the
  -- top of this file applies to every key AND every value in here.
  detail_json      JSONB NOT NULL DEFAULT '{}'::jsonb,

  opened_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  closed_at        TIMESTAMPTZ,

  -- A closed row has said what happened; an open one has not. Without this the
  -- sweep's worklist and the invoice query can disagree about the same row.
  CONSTRAINT agentic_audio_usage_closed_ck CHECK (
    (state = 'closed') = (outcome IS NOT NULL)
    AND (state = 'closed') = (closed_at IS NOT NULL)
  ),
  -- "Nothing was bought" has to mean nothing was bought. A quantity on an
  -- unspent row is an invented charge, and the database is a better place to
  -- refuse that than a code review.
  CONSTRAINT agentic_audio_usage_no_spend_ck CHECK (
    outcome <> 'no_spend' OR (
      COALESCE(provider_calls,0)   = 0 AND
      COALESCE(lm_input_tokens,0)  = 0 AND
      COALESCE(lm_output_tokens,0) = 0 AND
      COALESCE(items,0)            = 0 AND
      COALESCE(text_chars,0)       = 0
    )
  ),
  -- A render row with no job cannot be traced back to a render.
  CONSTRAINT agentic_audio_usage_render_has_job_ck CHECK (
    site IN ('reasoning_turn','understanding') OR job_id IS NOT NULL
  ),
  -- The in-process sites have no open/close lifecycle: they are written once,
  -- at the end, by the same frame that made the calls.
  CONSTRAINT agentic_audio_usage_inprocess_ck CHECK (
    site NOT IN ('reasoning_turn','understanding') OR state = 'closed'
  ),
  -- Only the bed has a route today, and a route on anything else is a bug
  -- heading for an invoice.
  CONSTRAINT agentic_audio_usage_route_ck CHECK (
    route IS NULL OR site = 'sfx_bed'
  )
);

-- THE idempotency guarantee. Without this every path in section 5 is only
-- "unlikely" rather than safe.
CREATE UNIQUE INDEX IF NOT EXISTS agentic_audio_usage_key_uniq
ON agentic_audio_usage (meter_key);

-- The alarm, and the sweep's worklist. A row still open long after it was
-- claimed is money that was spent and never accounted for. Partial for the
-- same reason agentic_audio_jobs_open_idx (005_jobs.sql:51) is: closed rows
-- accumulate forever and this index must never walk them. The predicate must
-- stay character-identical to the one the sweep writes.
CREATE INDEX IF NOT EXISTS agentic_audio_usage_open_idx
ON agentic_audio_usage (opened_at)
WHERE state = 'open';

-- Reconciling one render; the close path's lookup.
CREATE INDEX IF NOT EXISTS agentic_audio_usage_job_idx
ON agentic_audio_usage (job_id, attempt)
WHERE job_id IS NOT NULL;

-- Not for reading: this is what keeps ON DELETE SET NULL cheap. Without it
-- every session deletion sequentially scans this table, and retention deletes
-- in batches.
CREATE INDEX IF NOT EXISTS agentic_audio_usage_session_idx
ON agentic_audio_usage (session_id);

-- "Show me everything my one click made" -- the query support runs when a
-- customer asks why they were charged twice.
CREATE INDEX IF NOT EXISTS agentic_audio_usage_group_idx
ON agentic_audio_usage (group_ref, opened_at)
WHERE group_ref IS NOT NULL;

-- The invoicing read. Partial because account_id is NULL for every row until
-- an account directory is wired.
CREATE INDEX IF NOT EXISTS agentic_audio_usage_account_idx
ON agentic_audio_usage (account_id, closed_at)
WHERE account_id IS NOT NULL;
