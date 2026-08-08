"""
workflow_dna/prediction/estimator.py

Solves the core Phase 4 problem: the trained fitness model needs
execution_time, cost, retry_count, and success as inputs, but those are
only known AFTER a workflow executes. This class estimates them for a
not-yet-executed WorkflowDNA by:

  1. Looking up the k most structurally-similar PAST executions from
     Workflow DNA history (via WorkflowHistoryRepository), and
     averaging their real outcomes.
  2. If history doesn't have enough similar examples yet (cold start —
     e.g. right after the orchestrator is first deployed), falling
     back to the BPI2017 pretraining set's median outcomes instead.

Either way, the caller gets back a complete, usable estimate — it just
gets more accurate over time as real history accumulates.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from backend.workflow_dna.history.repository import WorkflowHistoryRepository
from backend.workflow_dna.prediction.dna import WorkflowDNA

COLD_START_PATH = Path(__file__).resolve().parent / "artifacts" / "cold_start_defaults.json"
MIN_SIMILAR_FOR_HISTORY_ESTIMATE = 3  # below this, prefer the cold-start prior


@dataclass
class EstimatedOutcome:
    execution_time: float
    cost: float
    retry_count: float
    success: float
    source: str  # "history" or "cold_start" — surfaced to the UI so it's honest about confidence
    similar_workflow_count: int


class HistoricalFeatureEstimator:
    def __init__(
        self,
        repository: WorkflowHistoryRepository | None = None,
        cold_start_path: str | None = None,
    ):
        self.repository = repository or WorkflowHistoryRepository()
        path = Path(cold_start_path) if cold_start_path else COLD_START_PATH
        if not path.exists():
            raise FileNotFoundError(
                f"No cold-start defaults found at {path}. "
                "Run `python -m workflow_dna.prediction.fit_cold_start_defaults` first."
            )
        self.cold_start_defaults = json.loads(path.read_text())

    def estimate(self, dna: WorkflowDNA) -> EstimatedOutcome:
        similar = self.repository.get_similar_workflows(
            agent_count=dna.agent_count,
            parallel_execution=dna.parallel_execution,
            workflow_depth=dna.workflow_depth,
            workflow_length=dna.workflow_length,
            k=5,
        )

        if len(similar) >= MIN_SIMILAR_FOR_HISTORY_ESTIMATE:
            n = len(similar)
            return EstimatedOutcome(
                execution_time=sum(r.execution_time for r in similar) / n,
                cost=sum(r.cost for r in similar) / n,
                retry_count=sum(r.retry_count for r in similar) / n,
                success=sum(r.quality for r in similar) / n,
                source="history",
                similar_workflow_count=n,
            )

        d = self.cold_start_defaults
        return EstimatedOutcome(
            execution_time=d["execution_time"],
            cost=d["cost"],
            retry_count=d["retry_count"],
            success=d["success"],
            source="cold_start",
            similar_workflow_count=len(similar),
        )
