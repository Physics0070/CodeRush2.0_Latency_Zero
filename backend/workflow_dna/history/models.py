"""
workflow_dna/history/models.py

A typed representation of one Workflow DNA history row — the "genome +
outcome" record for a single workflow execution (or, before it's
executed, a candidate whose outcome fields are still None).
"""

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional


@dataclass
class WorkflowExecutionRecord:
    workflow_id: str
    generation: int
    parent_workflow: Optional[str]
    mutation_type: str  # one of feature_engineering.MUTATION_TYPE_VOCABULARY

    # Structural DNA — always known, even before execution
    agent_count: int
    parallel_execution: int  # 0 or 1
    workflow_depth: int
    workflow_length: int

    # Outcome fields — None until the workflow has actually run
    execution_time: Optional[float] = None
    retry_count: Optional[int] = None
    cost: Optional[float] = None
    quality: Optional[float] = None
    success: Optional[int] = None
    fitness: Optional[float] = None

    # What the model predicted BEFORE execution, kept alongside the real
    # outcome so prediction accuracy can be reviewed later.
    predicted_fitness: Optional[float] = None

    timestamp: str = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, row: dict) -> "WorkflowExecutionRecord":
        return cls(**row)
