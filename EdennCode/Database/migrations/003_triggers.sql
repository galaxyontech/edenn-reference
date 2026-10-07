-- Migration 003: updated_at triggers for both tables.
-- Joint. Depends on 001a_requests and 001b_pipeline.
-- Spec: §3.4.

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- requests: bump only on changes the agent / backfill worker / ops queries care about.
CREATE TRIGGER requests_set_updated_at
  BEFORE UPDATE ON requests
  FOR EACH ROW
  WHEN (
    OLD.user_prompt        IS DISTINCT FROM NEW.user_prompt       OR
    OLD.options            IS DISTINCT FROM NEW.options           OR
    OLD.extracted_intent   IS DISTINCT FROM NEW.extracted_intent  OR
    OLD.status             IS DISTINCT FROM NEW.status            OR
    OLD.output_url         IS DISTINCT FROM NEW.output_url        OR
    OLD.input_kind         IS DISTINCT FROM NEW.input_kind        OR
    OLD.input_duration_s   IS DISTINCT FROM NEW.input_duration_s
  )
  EXECUTE FUNCTION set_updated_at();

-- pipeline_runs: same pattern.
CREATE TRIGGER pipeline_runs_set_updated_at
  BEFORE UPDATE ON pipeline_runs
  FOR EACH ROW
  WHEN (
    OLD.status              IS DISTINCT FROM NEW.status              OR
    OLD.modelspec           IS DISTINCT FROM NEW.modelspec           OR
    OLD.video_summary       IS DISTINCT FROM NEW.video_summary       OR
    OLD.music_prompt        IS DISTINCT FROM NEW.music_prompt        OR
    OLD.music_provider      IS DISTINCT FROM NEW.music_provider      OR
    -- duration_ms omitted: it is a GENERATED column and cannot be referenced
    -- via NEW in a BEFORE trigger WHEN clause. Changes to it imply changes
    -- to started_at/finished_at, which we'd add here if/when those become
    -- agent-mutable.
    OLD.total_input_tokens  IS DISTINCT FROM NEW.total_input_tokens  OR
    OLD.total_output_tokens IS DISTINCT FROM NEW.total_output_tokens
  )
  EXECUTE FUNCTION set_updated_at();
