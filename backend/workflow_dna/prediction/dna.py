"""
workflow_dna/prediction/dna.py

WorkflowDNA is the "genome" of a candidate workflow — the structural
shape that's genuinely known BEFORE it ever executes. This is
deliberately a small object: only the fields the orchestrator can
supply at workflow-generation time. Everything about how it actually
performs (execution_time, cost, retry_count, success) is unknown until
it runs, and is estimated by the prediction service rather than stored
here.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class WorkflowDNA:
    workflow_id: str
    agent_count: int
    parallel_execution: int  # 0 or 1
    workflow_depth: int
    workflow_length: int
    mutation_type: str = "none"
    parent_workflow: Optional[str] = None
    generation: int = 1
