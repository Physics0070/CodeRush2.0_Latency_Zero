"""
tests/test_workflow_dna_integration.py

Coverage for backend/workflow_dna/orchestrator_hooks.py — the translation
layer between the real orchestrator's GraphSpec/EventStore/RunResult and
Workflow DNA's prediction + history services.

Uses a FakeEventStore (in-memory) rather than a real Turso-backed
EventStore, since predict_and_log/record_and_log only touch four of its
methods (append, aggregate_cost, aggregate_latency, read). This keeps
the suite fast and independent of Turso credentials being configured,
matching this repo's own preference for deterministic tests that don't
hit a live service unless the property under test actually requires it
(see test_engine.py's docstring on why it avoids a live model call).

Workflow DNA's own model/history artifacts (the trained .joblib, the
SQLite history db) are real and are genuinely exercised here — only the
orchestrator's event store is faked, since that boundary is what this
file is actually responsible for translating across.
"""

import pytest

from backend.engine import AgentSpec, Edge, GraphSpec, Node
from backend.engine.executor import NodeResult, RunResult
from backend.events.types import EventType
from backend.workflow_dna.orchestrator_hooks import (
    _dna_from_graph,
    predict_and_log,
    record_and_log,
)


class FakeEventStore:
    """In-memory stand-in for the four EventStore methods orchestrator_hooks.py uses."""

    def __init__(self):
        self.appended = []
        self.cost_usd = 0.0
        self.wall_clock_ms = 1000
        self.events = []

    async def append(self, run_id, event_type, payload=None, **kwargs):
        self.appended.append((run_id, event_type, payload))

    async def aggregate_cost(self, run_id):
        class _Cost:
            pass
        c = _Cost()
        c.cost_usd = self.cost_usd
        return c

    async def aggregate_latency(self, run_id):
        class _Latency:
            pass
        latency = _Latency()
        latency.wall_clock_ms = self.wall_clock_ms
        return latency

    async def read(self, run_id, from_seq=0):
        return self.events


def _agent_node(node_id: str) -> Node:
    return Node(id=node_id, type="agent", agent=AgentSpec(id=f"{node_id}_agent", role="r", system_contract="x"))


def test_dna_from_graph_counts_real_parallelism():
    """Two independent agent nodes feeding a third -> depth 2, parallel=1, straight from GraphSpec.levels()."""
    graph = GraphSpec(
        nodes=[_agent_node("a"), _agent_node("b"), _agent_node("c")],
        edges=[Edge(from_node="a", to_node="c"), Edge(from_node="b", to_node="c")],
    ).finalize()

    dna = _dna_from_graph(graph, "wf_test_1")

    assert dna.agent_count == 3
    assert dna.workflow_length == 3
    assert dna.workflow_depth == 2  # level 1: [a, b], level 2: [c]
    assert dna.parallel_execution == 1  # level 1 has 2 nodes


def test_dna_from_graph_sequential_has_no_parallelism():
    """A straight chain a -> b -> c has depth 3 and no parallel level."""
    graph = GraphSpec(
        nodes=[_agent_node("a"), _agent_node("b"), _agent_node("c")],
        edges=[Edge(from_node="a", to_node="b"), Edge(from_node="b", to_node="c")],
    ).finalize()

    dna = _dna_from_graph(graph, "wf_test_2")

    assert dna.workflow_depth == 3
    assert dna.parallel_execution == 0


async def test_predict_and_log_appends_dna_predicted_event():
    graph = GraphSpec(nodes=[_agent_node("a")], edges=[]).finalize()
    store = FakeEventStore()

    decision = await predict_and_log(store, graph, "wf_test_3")

    assert decision is not None
    assert len(store.appended) == 1
    run_id, event_type, payload = store.appended[0]
    assert run_id == "wf_test_3"
    assert event_type == EventType.DNA_PREDICTED
    assert "predicted_fitness" in payload


async def test_record_and_log_appends_dna_recorded_event():
    graph = GraphSpec(nodes=[_agent_node("a")], edges=[]).finalize()
    store = FakeEventStore()
    store.cost_usd = 0.05
    store.wall_clock_ms = 5000

    run_result = RunResult(
        run_id="wf_test_4", ok=True,
        results={"a": NodeResult(node_id="a", ok=True, latency_ms=4800, cost_usd=0.05)},
        wall_clock_ms=5000, parallel_efficiency=1.0, failed_nodes=[], compensated=[],
    )

    await record_and_log(store, graph, "wf_test_4", run_result, predicted_fitness=70.0)

    assert len(store.appended) == 1
    run_id, event_type, payload = store.appended[0]
    assert event_type == EventType.DNA_RECORDED
    assert "fitness" in payload
    assert 0 <= payload["fitness"] <= 100


async def test_workflow_dna_failure_never_raises(monkeypatch):
    """Advisory-only contract: if the model/service throws, hooks swallow it
    and return None rather than failing or blocking the real run."""
    from backend.workflow_dna import orchestrator_hooks as hooks

    class ExplodingService:
        def evaluate_workflow(self, dna):
            raise RuntimeError("simulated model failure")

    monkeypatch.setattr(hooks, "_get_service", lambda: ExplodingService())

    graph = GraphSpec(nodes=[_agent_node("a")], edges=[]).finalize()
    store = FakeEventStore()

    result = await predict_and_log(store, graph, "wf_test_5")

    assert result is None
    assert store.appended == []  # no event logged, no exception propagated
