"""Event vocabulary and row shape.

`EventType` is a closed set on purpose: a typo in an event name would silently
create an event class nothing reads, and every metric in this system is a query
over these names.
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class EventType(StrEnum):
    RUN_START = "RUN_START"
    CLARIFY_ASKED = "CLARIFY_ASKED"
    CLARIFY_ANSWERED = "CLARIFY_ANSWERED"
    GRAPH_PROPOSED = "GRAPH_PROPOSED"
    GRAPH_EDITED = "GRAPH_EDITED"
    GRAPH_LOCKED = "GRAPH_LOCKED"
    GRAPH_APPROVED = "GRAPH_APPROVED"
    NODE_START = "NODE_START"
    NODE_END = "NODE_END"
    HANDOFF_EMITTED = "HANDOFF_EMITTED"
    HANDOFF_VALIDATED = "HANDOFF_VALIDATED"
    HANDOFF_REJECTED = "HANDOFF_REJECTED"
    REPAIR_ATTEMPT = "REPAIR_ATTEMPT"
    TOOL_CALL = "TOOL_CALL"
    TOOL_RESULT = "TOOL_RESULT"
    TOOL_BLOCKED = "TOOL_BLOCKED"
    INJECTION_BLOCKED = "INJECTION_BLOCKED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    RETRY = "RETRY"
    COMPENSATE = "COMPENSATE"
    COUNCIL_PROPOSAL = "COUNCIL_PROPOSAL"
    COUNCIL_RANKING = "COUNCIL_RANKING"
    COUNCIL_VERDICT = "COUNCIL_VERDICT"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    CANCEL = "CANCEL"
    BRANCH_FAILED = "BRANCH_FAILED"
    RUN_END = "RUN_END"
    MEMORY_WRITE = "MEMORY_WRITE"
    MEMORY_READ = "MEMORY_READ"
    MEMORY_EVICTED = "MEMORY_EVICTED"


class Event(BaseModel):
    run_id: str
    seq: int
    event_type: EventType
    node_id: str | None = None
    agent_id: str | None = None
    payload: dict | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int | None = None
    ts: str | None = None


class RunRow(BaseModel):
    run_id: str
    goal: str
    config_hash: str
    seed: int
    graph_spec: dict
    status: str
    replay_of: str | None = None
    last_seq: int = 0
    started_at: str | None = None
    ended_at: str | None = None


class CostTotals(BaseModel):
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    events: int = 0


class LatencyTotals(BaseModel):
    """`sum_ms` over `wall_clock_ms` is the Parallel Efficiency ratio."""

    sum_ms: int = 0
    wall_clock_ms: int = 0
    measured_nodes: int = 0

    @property
    def parallel_efficiency(self) -> float:
        return round(self.sum_ms / self.wall_clock_ms, 3) if self.wall_clock_ms else 0.0


class Budget(BaseModel):
    """Engine-enforced ceilings. Not suggested to the model - checked in code."""

    budget_tokens: int = Field(gt=0)
    timeout_s: int = Field(gt=0)
