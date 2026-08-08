"""
workflow_dna/history/repository.py

All reads and writes to the Workflow DNA history database go through
this class — nothing else in the package should open a sqlite3
connection directly. Responsibilities:

  - record_execution(): write a finished (or in-progress) workflow row
  - get_by_id() / get_lineage(): look up a workflow and walk its
    parent chain back to generation 1
  - get_similar_workflows(): find past EXECUTED workflows with a
    similar structural shape — this is what lets the prediction
    service estimate execution_time/cost/retry_count/success for a
    workflow that hasn't run yet
  - next_generation_number(): generation counting for new candidates
"""

import sqlite3
from pathlib import Path
from typing import List, Optional

from backend.workflow_dna.history.models import WorkflowExecutionRecord
from backend.workflow_dna.history.schema import init_db, DEFAULT_DB_PATH

STRUCTURAL_FIELDS = ["agent_count", "parallel_execution", "workflow_depth", "workflow_length"]
OUTCOME_FIELDS = ["execution_time", "retry_count", "cost", "quality", "success"]

ROW_COLUMNS = [
    "workflow_id", "generation", "parent_workflow", "mutation_type",
    "agent_count", "parallel_execution", "workflow_depth", "workflow_length",
    "execution_time", "retry_count", "cost", "quality", "success",
    "fitness", "predicted_fitness", "timestamp",
]


class WorkflowHistoryRepository:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or str(DEFAULT_DB_PATH)
        init_db(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def record_execution(self, record: WorkflowExecutionRecord) -> None:
        """Inserts a new row, or overwrites it if the same workflow_id is recorded again
        (e.g. a candidate row created pre-execution gets updated with real outcomes after execution)."""
        d = record.to_dict()
        placeholders = ", ".join(["?"] * len(ROW_COLUMNS))
        columns = ", ".join(ROW_COLUMNS)
        values = [d[c] for c in ROW_COLUMNS]

        conn = self._connect()
        try:
            conn.execute(
                f"INSERT OR REPLACE INTO workflow_executions ({columns}) VALUES ({placeholders})",
                values,
            )
            conn.commit()
        finally:
            conn.close()

    def get_by_id(self, workflow_id: str) -> Optional[WorkflowExecutionRecord]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM workflow_executions WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
            return WorkflowExecutionRecord.from_row(dict(row)) if row else None
        finally:
            conn.close()

    def get_lineage(self, workflow_id: str) -> List[WorkflowExecutionRecord]:
        """Walks parent_workflow pointers back to generation 1. Returns oldest-first."""
        lineage = []
        current = self.get_by_id(workflow_id)
        seen = set()
        while current is not None and current.workflow_id not in seen:
            lineage.append(current)
            seen.add(current.workflow_id)
            current = self.get_by_id(current.parent_workflow) if current.parent_workflow else None
        return list(reversed(lineage))

    def next_generation_number(self, parent_workflow: Optional[str]) -> int:
        """Generation 1 if there's no parent, otherwise parent's generation + 1."""
        if parent_workflow is None:
            return 1
        parent = self.get_by_id(parent_workflow)
        return (parent.generation + 1) if parent else 1

    def get_similar_workflows(
        self, agent_count: int, parallel_execution: int, workflow_depth: int,
        workflow_length: int, k: int = 5,
    ) -> List[WorkflowExecutionRecord]:
        """
        Finds the k EXECUTED (fitness IS NOT NULL) historical workflows
        whose structural shape is closest to the given candidate, by
        normalized Euclidean distance over the 4 structural fields.
        This is what powers cold-start-free feature estimation once
        real orchestrator history accumulates.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM workflow_executions WHERE fitness IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return []

        candidates = [dict(r) for r in rows]

        # Normalize each structural field by the observed range in history
        # so no single field (e.g. workflow_length, which can be much
        # larger in magnitude than parallel_execution) dominates distance.
        ranges = {}
        for field in STRUCTURAL_FIELDS:
            values = [c[field] for c in candidates]
            lo, hi = min(values), max(values)
            ranges[field] = (lo, hi if hi > lo else lo + 1)

        target = {
            "agent_count": agent_count, "parallel_execution": parallel_execution,
            "workflow_depth": workflow_depth, "workflow_length": workflow_length,
        }

        def distance(row: dict) -> float:
            total = 0.0
            for field in STRUCTURAL_FIELDS:
                lo, hi = ranges[field]
                a = (row[field] - lo) / (hi - lo)
                b = (target[field] - lo) / (hi - lo)
                total += (a - b) ** 2
            return total ** 0.5

        candidates.sort(key=distance)
        top_k = candidates[:k]
        return [WorkflowExecutionRecord.from_row(row) for row in top_k]

    def count(self) -> int:
        conn = self._connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM workflow_executions").fetchone()[0]
        finally:
            conn.close()

    def list_recent(self, limit: int = 20) -> List[WorkflowExecutionRecord]:
        """Most recent EXECUTED workflows (fitness IS NOT NULL), newest first.
        Powers the frontend's Evolution History panel."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM workflow_executions WHERE fitness IS NOT NULL "
                "ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [WorkflowExecutionRecord.from_row(dict(r)) for r in rows]
        finally:
            conn.close()
