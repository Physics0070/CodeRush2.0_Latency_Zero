"""Block 4 verification.

Uses a controlled completer with a known per-call delay rather than a live
model: parallelism is the property under test, and a real model's variable
latency would make the Parallel Efficiency assertion flaky. The same engine is
driven by real models in backend/demo.py.
"""

import asyncio
import json
import uuid

import pytest

from backend.config import settings
from backend.engine import AgentSpec, Edge, Executor, GraphSpec, Node
from backend.events import EventStore, EventType
from backend.providers.base import Completion

pytestmark = pytest.mark.skipif(
    not (settings.supabase_url and settings.supabase_service_key), reason="supabase not configured"
)

# Functional tests below only need a branch to take *some* time, so keep this
# short - the suite runs these eight times.
BRANCH_DELAY = 0.3

# Parallel Efficiency is measured separately, at a realistic branch duration.
#
# Every event append is a network round trip to Supabase (~150-400ms from here),
# and a node writes five of them. That fixed cost lands in wall clock but not in
# node latency, so it dilutes PE for short nodes: 0.6s branches measured 1.89 and
# 2.0s branches measured 1.976 - both under 2.0 despite the branches provably
# overlapping by timestamp. Real agent calls take 6-13s, where the overhead is
# noise and PE approaches the branch count. 4.0s reproduces that regime without
# making the suite unbearable.
PE_BRANCH_DELAY = 4.0
SCHEMA = {
    "type": "object",
    "required": ["findings"],
    "properties": {"findings": {"type": "array"}},
}


def completer(fail_for: set[str] | None = None, delay: float = BRANCH_DELAY):
    fail_for = fail_for or set()

    async def _complete(model, messages, **kw):
        agent_id = kw.get("agent_id") or ""
        await asyncio.sleep(delay)
        if agent_id in fail_for:
            return Completion(text="not json at all", latency_ms=int(delay * 1000), model=model)
        return Completion(
            text=json.dumps({"findings": [{"title": f"{agent_id} finding", "severity": "low"}]}),
            tokens_in=10, tokens_out=10, latency_ms=int(delay * 1000), model=model,
        )

    return _complete


def agent(aid: str, **kw) -> AgentSpec:
    return AgentSpec(
        id=aid, role=f"{aid} analyst",
        system_contract="Output JSON only.",
        model="ollama:llama3.2:3b", output_schema=SCHEMA, **kw,
    )


def fanout_graph() -> GraphSpec:
    """5 nodes: fanout -> 3 concurrent analysts -> join. Plus a compensator."""
    return GraphSpec(
        nodes=[
            Node(id="start", type="fanout"),
            Node(id="security", type="agent", agent=agent("security")),
            Node(id="quality", type="agent", agent=agent("quality")),
            Node(id="docs", type="agent", agent=agent("docs")),
            Node(id="merge", type="join"),
            Node(id="comp", type="compensate", compensates="security"),
        ],
        edges=[
            Edge(from_node="start", to_node="security", handoff_schema=SCHEMA),
            Edge(from_node="start", to_node="quality", handoff_schema=SCHEMA),
            Edge(from_node="start", to_node="docs", handoff_schema=SCHEMA),
            Edge(from_node="security", to_node="merge"),
            Edge(from_node="quality", to_node="merge"),
            Edge(from_node="docs", to_node="merge"),
        ],
    ).finalize()


@pytest.fixture
async def store():
    async with EventStore() as s:
        yield s


async def _run(store, graph, **kw):
    rid = str(uuid.uuid4())
    await store.create_run(rid, "engine test", graph.config_hash, 42, graph.model_dump(), "pending")
    ex = Executor(store, graph, rid, seed=42, **kw)
    return rid, await ex.run()


# ------------------------------------------------------------------ topology


def test_levels_put_independent_nodes_together():
    levels = fanout_graph().levels()
    assert levels[0] == ["start"]
    assert set(levels[1]) == {"docs", "quality", "security"}
    assert levels[2] == ["merge"]


def test_cycle_is_rejected():
    g = GraphSpec(
        nodes=[Node(id="a", type="fanout"), Node(id="b", type="fanout")],
        edges=[Edge(from_node="a", to_node="b"), Edge(from_node="b", to_node="a")],
    )
    with pytest.raises(ValueError, match="cycle"):
        g.levels()


def test_config_hash_is_stable_and_sensitive():
    a, b = fanout_graph(), fanout_graph()
    assert a.config_hash == b.config_hash
    b.nodes[1].agent.model = "groq:llama-3.3-70b-versatile"
    assert b.finalize().config_hash != a.config_hash


def test_side_effect_without_approval_is_rejected():
    g = fanout_graph()
    g.node("security").agent.allowed_side_effects = ["write_artifact"]
    assert g.validate_side_effects(), "a side-effecting node needs an upstream approval"


def test_side_effect_with_upstream_approval_is_allowed():
    g = fanout_graph()
    g.nodes.append(Node(id="gate", type="approval"))
    g.edges.append(Edge(from_node="gate", to_node="security"))
    g.node("security").agent.allowed_side_effects = ["write_artifact"]
    assert g.validate_side_effects() == []


# ----------------------------------------------------------------- execution


async def test_three_branches_start_before_any_finishes(store):
    """The load-bearing proof of concurrency: overlap in wall-clock timestamps.

    This is independent of logging overhead, so unlike the ratio below it cannot
    be diluted by a slow database.
    """
    from datetime import datetime

    # Branch work must outlast the time taken to write three NODE_START rows.
    # Each append is a round trip (~150-400ms), so at 0.3s branches a slow
    # network can finish branch one before branch three has even been logged -
    # a race in the test, not in the engine.
    rid, result = await _run(store, fanout_graph(), completer=completer(delay=2.0))
    assert result.ok, result.failed_nodes

    events = await store.read(rid)
    branches = {"security", "quality", "docs"}
    starts = {
        e.node_id: e.ts for e in events
        if e.event_type == EventType.NODE_START and e.node_id in branches
    }
    ends = {
        e.node_id: e.ts for e in events
        if e.event_type == EventType.NODE_END and e.node_id in branches
    }
    assert len(starts) == 3 and len(ends) == 3

    last_start = max(datetime.fromisoformat(t) for t in starts.values())
    first_end = min(datetime.fromisoformat(t) for t in ends.values())
    # Serial execution would put every start after some other branch's end.
    assert last_start < first_end, (
        f"branches did not overlap: last start {last_start} >= first end {first_end}"
    )


async def test_parallel_efficiency_exceeds_two(store):
    """Σ(node latencies) / wall_clock > 2 for three concurrent branches."""
    _, result = await _run(
        store, fanout_graph(), completer=completer(delay=PE_BRANCH_DELAY)
    )
    assert result.ok, result.failed_nodes
    assert result.parallel_efficiency > 2.0, (
        f"PE={result.parallel_efficiency} wall={result.wall_clock_ms}ms"
    )


async def test_failed_branch_triggers_compensation(store):
    rid, result = await _run(
        store, fanout_graph(), completer=completer(fail_for={"security"})
    )
    assert not result.ok
    assert "security" in result.failed_nodes
    assert "comp" in result.compensated

    kinds = [e.event_type for e in await store.read(rid)]
    assert EventType.BRANCH_FAILED in kinds
    assert EventType.COMPENSATE in kinds
    assert kinds.count(EventType.REPAIR_ATTEMPT) == settings.max_repair_attempts


async def test_downstream_of_failure_is_skipped_not_run(store):
    _, result = await _run(store, fanout_graph(), completer=completer(fail_for={"security"}))
    assert result.results["merge"].skipped


async def test_budget_exceeded_fails_the_branch(store):
    g = fanout_graph()
    g.node("quality").agent.budget_tokens = 5  # one call spends 20

    rid, result = await _run(store, g, completer=completer())
    assert "quality" in result.failed_nodes
    kinds = [e.event_type for e in await store.read(rid)]
    assert EventType.BUDGET_EXCEEDED in kinds


async def test_timeout_is_enforced_by_the_engine(store):
    g = fanout_graph()
    g.node("docs").agent.timeout_s = 1

    rid, result = await _run(store, g, completer=completer(delay=3.0))
    assert "docs" in result.failed_nodes
    assert result.results["docs"].error == "timeout"


async def test_approval_gate_blocks_until_granted(store):
    g = fanout_graph()
    g.nodes.append(Node(id="gate", type="approval"))
    g.edges.append(Edge(from_node="gate", to_node="security"))
    g.node("security").agent.allowed_side_effects = ["write_artifact"]
    g = g.finalize()

    _, denied = await _run(store, g, completer=completer())
    assert denied.results["gate"].blocked

    _, granted = await _run(store, g, completer=completer(), approvals={"gate"})
    assert granted.results["gate"].ok
    assert granted.ok, granted.failed_nodes
