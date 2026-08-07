"""Blocks 8 and 9: replay must diff to zero, metrics must be able to say 'overkill'."""

import json
import uuid

import pytest

from backend.config import settings
from backend.council.compiler import build_graph
from backend.engine import Executor
from backend.events import EventStore, EventType
from backend.metrics import compute
from backend.providers.base import Completion
from backend.replay import replay_run

pytestmark = pytest.mark.skipif(
    not (settings.supabase_url and settings.supabase_service_key), reason="supabase not configured"
)

MODELS = ["ollama:llama3.2:3b", "ollama:qwen2.5:7b"]

# Two agents saying the same thing in different words, one saying something new.
# This is what lets the redundancy assertions be real rather than contrived.
SCRIPTED = {
    "security": {"findings": [{"title": "Hardcoded database credentials in settings",
                               "severity": "critical",
                               "recommendation": "Move credentials to environment variables"}]},
    "quality": {"findings": [{"title": "Database password is hardcoded in the settings file",
                              "severity": "critical",
                              "recommendation": "Load the credential from the environment"}]},
    "docs": {"findings": [{"title": "No installation instructions in the README",
                           "severity": "low",
                           "recommendation": "Add a setup section"}]},
    "tests": {"findings": [{"title": "No tests cover the auth module",
                            "severity": "medium",
                            "recommendation": "Add auth unit tests"}]},
    "verifier": {"summary": "Credential exposure is the top issue in this repository.",
                 "prioritized": [{"rank": 1, "title": "Hardcoded credentials",
                                  "severity": "critical"}]},
}


def scripted_completer(delay: float = 0.05):
    import asyncio

    async def _complete(model, messages, **kw):
        await asyncio.sleep(delay)
        agent_id = kw.get("agent_id") or ""
        body = SCRIPTED.get(agent_id, {"findings": [
            {"title": f"{agent_id} generic finding", "severity": "low",
             "recommendation": "review"}]})
        return Completion(text=json.dumps(body), tokens_in=120, tokens_out=60,
                          cost_usd=0.0001, latency_ms=int(delay * 1000), model=model)

    return _complete


@pytest.fixture
async def store():
    async with EventStore() as s:
        yield s


async def _run(store, branches, completer=None):
    graph = build_graph(branches, MODELS, verifier_model=MODELS[-1])
    rid = str(uuid.uuid4())
    await store.create_run(rid, "Audit this repository", graph.config_hash, 42,
                           graph.model_dump(), "pending")
    approvals = {n.id for n in graph.nodes if n.type == "approval"}
    result = await Executor(
        store, graph, rid, seed=42,
        completer=completer or scripted_completer(), approvals=approvals,
    ).run()
    return rid, graph, result


BRANCHES3 = [
    {"id": "security", "role": "sec", "tools": ["read_file"]},
    {"id": "quality", "role": "qual", "tools": ["read_file"]},
    {"id": "docs", "role": "docs", "tools": ["read_file"]},
]


# ------------------------------------------------------------------- replay


async def test_replay_diff_is_zero(store):
    rid, _, result = await _run(store, BRANCHES3)
    assert result.ok, result.failed_nodes

    replay_result, diff = await replay_run(store, rid)

    assert diff.identical, f"expected zero diff, got {diff.output_diffs}"
    assert diff.output_diffs == []
    assert diff.nodes_compared > 0
    # Replay calls nothing, so it costs nothing.
    assert diff.replay_cost_usd == 0.0
    assert diff.original_cost_usd > 0
    assert replay_result.ok


async def test_replay_row_links_back_to_the_original(store):
    rid, _, _ = await _run(store, BRANCHES3)
    replay_result, _ = await replay_run(store, rid)
    row = await store.get_run(replay_result.run_id)
    assert row.replay_of == rid
    assert row.config_hash == (await store.get_run(rid)).config_hash


async def test_replay_serves_from_the_log_not_the_provider(store):
    rid, _, _ = await _run(store, BRANCHES3)
    replay_result, _ = await replay_run(store, rid)
    events = await store.read(replay_result.run_id)
    served = [e for e in events if e.event_type == EventType.TOOL_RESULT
              and (e.payload or {}).get("replayed")]
    assert served, "replay did not serve any completion from the log"


async def test_tampered_graph_spec_is_refused(store):
    """A replay that silently accepted a mutated spec would fake a zero diff."""
    rid, graph, _ = await _run(store, BRANCHES3)
    await store._request(
        "PATCH", "/runs", params={"run_id": f"eq.{rid}"},
        json={"config_hash": "0" * 64},
    )
    with pytest.raises(ValueError, match="config_hash mismatch"):
        await replay_run(store, rid)


# ------------------------------------------------------------------ metrics


async def test_metrics_detect_the_duplicate_pair(store):
    rid, _, _ = await _run(store, BRANCHES3)
    m = await compute(store, rid)

    assert len(m.agents) >= 3
    by_node = {a.node_id: a for a in m.agents}
    # security and quality report the same issue in different words.
    assert m.duplicate_work >= 1, f"redundancy not detected: {m.redundancy_index}"
    pair = {frozenset((p["a"], p["b"])) for p in m.duplicate_pairs}
    assert frozenset(("security", "quality")) in pair

    # Assert the relationship, not a magic number: the agent that said something
    # genuinely different must score materially higher than the pair that
    # restated each other. An absolute threshold here would just encode whatever
    # all-MiniLM-L6-v2 happens to return today.
    dup_mig = max(by_node["security"].marginal_information_gain,
                  by_node["quality"].marginal_information_gain)
    unique_mig = by_node["docs"].marginal_information_gain
    assert unique_mig > 2 * dup_mig, (
        f"MIG failed to separate unique from duplicate work: "
        f"docs={unique_mig} vs duplicates={dup_mig}"
    )
    # And the duplicate pair must sit below the midpoint of the scale.
    assert dup_mig < 0.5


async def test_metrics_deduplicate_findings(store):
    rid, _, _ = await _run(store, BRANCHES3)
    m = await compute(store, rid)
    total = sum(a.findings for a in m.agents)
    assert m.unique_findings < total, "identical findings were counted twice"
    assert m.cost_per_unique_finding is not None


async def test_metrics_report_validity_and_efficiency(store):
    rid, _, result = await _run(store, BRANCHES3)
    m = await compute(store, rid)
    assert m.handoff_validity == 1.0
    assert m.parallel_efficiency == result.parallel_efficiency
    assert m.approval_frequency >= 1
    assert 0 < m.branch_utilization <= 1.0


async def test_verdict_names_the_least_valuable_agent(store):
    rid, _, _ = await _run(store, BRANCHES3)
    m = await compute(store, rid)
    assert m.least_valuable_agent in {"security", "quality"}
    assert m.verdict
    assert "duplicat" in m.verdict.lower() or "MIG" in m.verdict


async def test_recovery_rate_when_a_branch_fails(store):
    async def failing(model, messages, **kw):
        if kw.get("agent_id") == "docs":
            return Completion(text="not json", model=model)
        return await scripted_completer()(model, messages, **kw)

    rid, _, result = await _run(store, BRANCHES3, completer=failing)
    m = await compute(store, rid)
    assert "docs" in result.failed_nodes
    assert m.recovery_rate is not None and m.recovery_rate > 0
    assert m.handoff_validity is not None and m.handoff_validity < 1.0
