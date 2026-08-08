"""
workflow_dna/history/models.py

A typed representation of one Workflow DNA history row — the "genome +
outcome" record for a single workflow execution (or, before it's
executed, a candidate whose outcome fields are still None).
"""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime


@dataclass
class WorkflowExecutionRecord:
    workflow_id: str
    generation: int
    parent_workflow: str | None
    mutation_type: str  # one of feature_engineering.MUTATION_TYPE_VOCABULARY

    # Structural DNA — always known, even before execution
    agent_count: int
    parallel_execution: int  # 0 or 1
    workflow_depth: int
    workflow_length: int

    # Outcome fields — None until the workflow has actually run
    execution_time: float | None = None
    retry_count: int | None = None
    cost: float | None = None
    quality: float | None = None
    success: int | None = None
    fitness: float | None = None

    # What the model predicted BEFORE execution, kept alongside the real
    # outcome so prediction accuracy can be reviewed later.
    predicted_fitness: float | None = None

    timestamp: str = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, row: dict) -> "WorkflowExecutionRecord":
        return cls(**row)
