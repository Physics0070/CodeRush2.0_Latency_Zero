-- Agent Council Orchestrator - single source of truth.
-- One append-only table. Observability, replay, metrics, the trace viewer,
-- pause/resume and the reproducibility report are all reads of it.
--
-- Apply in the Supabase SQL editor. Idempotent - safe to re-run.

-- ---------------------------------------------------------------------------
-- runs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS runs (
  run_id       UUID PRIMARY KEY,
  goal         TEXT NOT NULL,
  config_hash  TEXT NOT NULL,
  seed         INT  NOT NULL,
  graph_spec   JSONB NOT NULL,
  status       TEXT NOT NULL CHECK (status IN
                 ('pending','running','paused','done','failed','cancelled')),
  replay_of    UUID REFERENCES runs(run_id),
  -- Sequence allocator. Bumping this row is what serialises concurrent appends
  -- for a run; see append_event below.
  last_seq     BIGINT NOT NULL DEFAULT 0,
  started_at   TIMESTAMPTZ DEFAULT now(),
  ended_at     TIMESTAMPTZ
);

-- ---------------------------------------------------------------------------
-- events - append only. No UPDATE, no DELETE, ever.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
  run_id      UUID NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  seq         BIGINT NOT NULL,
  node_id     TEXT,
  agent_id    TEXT,
  event_type  TEXT NOT NULL,
  payload     JSONB,
  tokens_in   INT DEFAULT 0,
  tokens_out  INT DEFAULT 0,
  cost_usd    NUMERIC(12,6) DEFAULT 0,
  latency_ms  INT,
  ts          TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (run_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_events_run  ON events(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(run_id, event_type);

-- ---------------------------------------------------------------------------
-- artifacts - every validated output carries provenance (block 3)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id       UUID NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  seq          BIGINT NOT NULL,          -- event seq that produced it
  written_by   TEXT NOT NULL,            -- agent id
  node_id      TEXT,
  content      JSONB NOT NULL,
  storage_path TEXT,                     -- set when the blob went to Storage
  ts           TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id, seq);

-- ---------------------------------------------------------------------------
-- append_event - the only way a row enters `events`.
--
-- Why a function and not a client-side INSERT: `SELECT MAX(seq)+1` races.
-- Two concurrent appends read the same max and one loses on the primary key.
-- `UPDATE ... RETURNING` takes a row lock on the run, so concurrent callers
-- queue behind it and every append gets a distinct, gapless seq. One round
-- trip, one transaction.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION append_event(
  p_run_id     UUID,
  p_event_type TEXT,
  p_node_id    TEXT    DEFAULT NULL,
  p_agent_id   TEXT    DEFAULT NULL,
  p_payload    JSONB   DEFAULT NULL,
  p_tokens_in  INT     DEFAULT 0,
  p_tokens_out INT     DEFAULT 0,
  p_cost_usd   NUMERIC DEFAULT 0,
  p_latency_ms INT     DEFAULT NULL
) RETURNS BIGINT
LANGUAGE plpgsql
AS $$
DECLARE
  v_seq BIGINT;
BEGIN
  UPDATE runs
     SET last_seq = last_seq + 1
   WHERE run_id = p_run_id
  RETURNING last_seq INTO v_seq;

  IF v_seq IS NULL THEN
    RAISE EXCEPTION 'unknown run_id %, create the run before appending', p_run_id;
  END IF;

  INSERT INTO events (run_id, seq, node_id, agent_id, event_type, payload,
                      tokens_in, tokens_out, cost_usd, latency_ms)
  VALUES (p_run_id, v_seq, p_node_id, p_agent_id, p_event_type, p_payload,
          p_tokens_in, p_tokens_out, p_cost_usd, p_latency_ms);

  RETURN v_seq;
END;
$$;

-- ---------------------------------------------------------------------------
-- Enforce append-only at the database, not by convention.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION events_are_immutable() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'events is append-only; % is not permitted', TG_OP;
END;
$$;

DROP TRIGGER IF EXISTS trg_events_immutable ON events;
CREATE TRIGGER trg_events_immutable
  BEFORE UPDATE OR DELETE ON events
  FOR EACH ROW EXECUTE FUNCTION events_are_immutable();

-- ---------------------------------------------------------------------------
-- RLS on with no policies: anon/authenticated get nothing. The backend uses
-- the service key, which bypasses RLS. Keys never reach the browser.
-- ---------------------------------------------------------------------------
ALTER TABLE runs      ENABLE ROW LEVEL SECURITY;
ALTER TABLE events    ENABLE ROW LEVEL SECURITY;
ALTER TABLE artifacts ENABLE ROW LEVEL SECURITY;
