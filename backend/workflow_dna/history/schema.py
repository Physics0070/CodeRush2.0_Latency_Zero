"""
workflow_dna/history/schema.py

Database schema for Workflow DNA's execution memory.

This is the "memory" the spec talks about: every executed workflow
becomes one row here, carrying its generation number, its parent (if it
was born from a mutation), which mutation produced it, its DNA, and how
it actually performed. This is what makes "which workflow historically
performs best?" an answerable question instead of a rhetorical one.

Uses SQLite via Python's built-in sqlite3 module — no external database
server, no extra dependency, single portable .db file. That matches the
project's "lightweight, hackathon-scoped" framing while still being a
real, queryable, ACID-compliant database.

Field list note: the spec suggested workflow_id, generation,
parent_workflow, mutation_type, agent_count, parallel_execution,
workflow_depth, execution_time, retry_count, cost, quality, success,
fitness, timestamp — and called these "suggested fields," not an
exhaustive contract. This schema adds ONE extra column,
`workflow_length`, because it's one of the 9 features the trained model
requires as input, and without storing it we couldn't re-run a
prediction on a historical row later. Flagging the addition explicitly
rather than silently changing the suggested schema.
"""

import sqlite3
from pathlib import Path

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS workflow_executions (
    workflow_id         TEXT PRIMARY KEY,
    generation          INTEGER NOT NULL,
    parent_workflow     TEXT,
    mutation_type       TEXT NOT NULL DEFAULT 'none',
    agent_count         INTEGER NOT NULL,
    parallel_execution  INTEGER NOT NULL,      -- 0 or 1
    workflow_depth      INTEGER NOT NULL,
    workflow_length     INTEGER NOT NULL,      -- beyond the spec's list; required ML input
    execution_time      REAL,                  -- NULL until the workflow has actually executed
    retry_count         INTEGER,
    cost                REAL,
    quality             REAL,                  -- continuous 0-1 quality score
    success              INTEGER,               -- 0 or 1, genuine boolean completion outcome
    fitness              REAL,                  -- actual fitness, from FitnessCalculator
    predicted_fitness    REAL,                  -- what the model predicted BEFORE execution
    timestamp             TEXT NOT NULL,
    FOREIGN KEY (parent_workflow) REFERENCES workflow_executions(workflow_id)
);
"""

CREATE_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_parent ON workflow_executions(parent_workflow);",
    "CREATE INDEX IF NOT EXISTS idx_generation ON workflow_executions(generation);",
    "CREATE INDEX IF NOT EXISTS idx_fitness ON workflow_executions(fitness);",
]

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "artifacts" / "workflow_dna_history.db"


def init_db(db_path: str = str(DEFAULT_DB_PATH)) -> None:
    """Creates the database file and table/indexes if they don't already exist.

    Safe to call repeatedly.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(CREATE_TABLE_SQL)
        for stmt in CREATE_INDEX_SQL:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()
