-- Agent Council Orchestrator - single source of truth.
-- One append-only table. Observability, replay, metrics, the trace viewer,
-- pause/resume and the reproducibility report are all reads of it.
--
-- Turso / libSQL edition. Apply with:
--   turso db shell <db-name> < migrations/001_events.sql
--
-- SQLite has no server-side functions (no plpgsql), so the atomic sequence
-- allocation that used to live in a Postgres function now lives in
-- backend/events/store.py as an UPDATE ... RETURNING followed by a dependent
-- INSERT, both committed as one transaction and serialised by an
-- in-process lock. What triggers still enforce here is append-only-ness,
-- because that is a database guarantee no amount of application code can
-- fake convincingly. Idempotent - safe to re-run.

-- ---------------------------------------------------------------------------
-- runs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS runs (
  run_id       TEXT PRIMARY KEY,
  goal         TEXT NOT NULL,
  config_hash  TEXT NOT NULL,
  seed         INTEGER NOT NULL,
  graph_spec   TEXT NOT NULL,               -- JSON, stored as text
  status       TEXT NOT NULL CHECK (status IN
                 ('pending','running','paused','done','failed','cancelled')),
  replay_of    TEXT REFERENCES runs(run_id),
  -- Sequence allocator. Bumping this row via UPDATE ... RETURNING is what
  -- serialises concurrent appends for a run; see backend/events/store.py.
  last_seq     INTEGER NOT NULL DEFAULT 0,
  started_at   TEXT NOT NULL,
  ended_at     TEXT
);

-- ---------------------------------------------------------------------------
-- events - append only. No UPDATE, no DELETE, ever.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
  run_id      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  seq         INTEGER NOT NULL,
  node_id     TEXT,
  agent_id    TEXT,
  event_type  TEXT NOT NULL,
  payload     TEXT,                         -- JSON, stored as text
  tokens_in   INTEGER DEFAULT 0,
  tokens_out  INTEGER DEFAULT 0,
  cost_usd    REAL DEFAULT 0,
  latency_ms  INTEGER,
  ts          TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_events_run  ON events(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(run_id, event_type);

-- ---------------------------------------------------------------------------
-- artifacts - every validated output carries provenance (block 3)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id  TEXT PRIMARY KEY,
  run_id       TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  seq          INTEGER NOT NULL,             -- event seq that produced it
  written_by   TEXT NOT NULL,                -- agent id
  node_id      TEXT,
  content      TEXT NOT NULL,                -- JSON, stored as text
  storage_path TEXT,
  ts           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id, seq);

-- ---------------------------------------------------------------------------
-- Enforce append-only at the database, not by convention.
-- ---------------------------------------------------------------------------
DROP TRIGGER IF EXISTS trg_events_no_update;
CREATE TRIGGER trg_events_no_update
BEFORE UPDATE ON events
BEGIN
  SELECT RAISE(ABORT, 'events is append-only; UPDATE is not permitted');
END;

DROP TRIGGER IF EXISTS trg_events_no_delete;
CREATE TRIGGER trg_events_no_delete
BEFORE DELETE ON events
BEGIN
  SELECT RAISE(ABORT, 'events is append-only; DELETE is not permitted');
END;

-- No RLS equivalent here: Turso has no row-level security layer. Access
-- control is the auth token itself (full read/write) - the backend is the
-- only holder of it, same trust boundary as the Supabase service key before.
