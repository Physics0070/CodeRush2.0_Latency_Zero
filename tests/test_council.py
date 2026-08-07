"""Block 5 verification: council mechanics + compiled graph invariants."""

import uuid

import pytest

from backend.config import settings
from backend.council import (
    Proposal,
    Ranking,
    borda,
    build_graph,
    clarifying_questions,
    compile_graph,
    disagreement,
    permission_prompt,
    pick_chairman,
)
from backend.events import EventStore, EventType

pytestmark = pytest.mark.skipif(
    not (settings.turso_database_url and settings.turso_auth_token), reason="turso not configured"
)

MEMBERS = ["ollama:llama3.2:3b", "ollama:qwen2.5:7b"]


# ------------------------------------------------------------ pure mechanics


def test_borda_rewards_higher_placement():
    r = [Ranking(ranker_model="m1", order=["A", "B", "C"]),
         Ranking(ranker_model="m2", order=["A", "C", "B"])]
    s = borda(r, ["A", "B", "C"])
    assert s["A"] > s["C"] > s["B"] or s["A"] > s["B"]
    assert s["A"] == 6  # first of three, twice


def test_disagreement_zero_when_unanimous():
    r = [Ranking(ranker_model="m1", order=["A", "B"]),
         Ranking(ranker_model="m2", order=["A", "B"])]
    assert disagreement(r) == 0.0


def test_disagreement_high_when_split():
    r = [Ranking(ranker_model="m1", order=["A", "B"]),
         Ranking(ranker_model="m2", order=["B", "A"])]
    assert disagreement(r) == 0.5


def test_chairman_is_a_policy_not_a_constant():
    members = ["ollama:llama3.2:3b", "groq:llama-3.3-70b-versatile"]
    assert pick_chairman("strongest", members) == "groq:llama-3.3-70b-versatile"
    assert pick_chairman("cheapest", members) == "ollama:llama3.2:3b"
    assert pick_chairman("ollama:llama3.2:3b", members) == "ollama:llama3.2:3b"


def test_ranking_is_anonymous():
    """Reviewers must see labels only - never an author model name."""
    import asyncio

    from backend.council.council import peer_rank

    props = [Proposal(author_model="ollama:llama3.2:3b", content={"branches": []}),
             Proposal(author_model="ollama:qwen2.5:7b", content={"branches": []})]

    captured = {}

    async def fake_complete(model, messages, **kw):
        captured[model] = messages[-1]["content"]
        from backend.providers.base import Completion
        return Completion(text='{"order":["A","B"],"reasons":{}}')

    import backend.council.council as mod

    original, mod.complete = mod.complete, fake_complete
    try:
        asyncio.run(peer_rank(props, MEMBERS, seed=42))
    finally:
        mod.complete = original

    for prompt in captured.values():
        assert "llama3.2" not in prompt and "qwen2.5" not in prompt, "authorship leaked"
        assert "Proposal A" in prompt


# ------------------------------------------------------- graph invariants


def test_compiled_graph_has_fanout_verify_and_parallelism():
    g = build_graph(
        [{"id": "security", "role": "sec", "tools": ["read_file"]},
         {"id": "quality", "role": "qual", "tools": ["read_file"]},
         {"id": "docs", "role": "docs", "tools": ["read_file"]}],
        MEMBERS, verifier_model=MEMBERS[-1],
    )
    types = {n.type for n in g.nodes}
    assert {"fanout", "verify", "join", "approval", "clarify", "compensate"} <= types
    assert g.config_hash

    levels = g.levels()
    parallel = max(len(x) for x in levels)
    assert parallel >= 3, f"expected a parallel level, got {levels}"


def test_malformed_council_output_falls_back_to_a_valid_graph():
    """A model-authored graph is untrusted; the compiler guarantees invariants."""
    g = build_graph([{"id": "", "role": "x"}, {"id": "!!bad", "role": "y"}],
                    MEMBERS, verifier_model=MEMBERS[-1])
    agents = [n.id for n in g.nodes if n.type == "agent"]
    assert len(agents) >= 2
    assert any(n.type == "verify" for n in g.nodes)


def test_duplicate_branch_ids_are_dropped():
    g = build_graph(
        [{"id": "security", "role": "a"}, {"id": "security", "role": "b"},
         {"id": "docs", "role": "c"}],
        MEMBERS, verifier_model=MEMBERS[-1],
    )
    agents = [n.id for n in g.nodes if n.type == "agent"]
    assert len(agents) == len(set(agents))


def test_every_analyst_edge_carries_a_schema():
    g = build_graph([{"id": "security", "role": "a"}, {"id": "docs", "role": "b"}],
                    MEMBERS, verifier_model=MEMBERS[-1])
    analyst_ids = {n.id for n in g.nodes if n.type == "agent" and n.id != "verifier"}
    for e in g.edges:
        if e.to_node in analyst_ids or e.from_node in analyst_ids:
            assert e.handoff_schema, f"untyped edge {e.from_node}->{e.to_node}"


def test_side_effects_are_clean_by_default():
    g = build_graph([{"id": "security", "role": "a"}, {"id": "docs", "role": "b"}],
                    MEMBERS, verifier_model=MEMBERS[-1])
    assert g.validate_side_effects() == []


def test_clarify_and_permission_text():
    qs = clarifying_questions("Audit this repository")
    assert 1 <= len(qs) <= 3
    assert "shall i proceed" in permission_prompt(["security", "docs"]).lower()


# ---------------------------------------------------- live council compile


@pytest.mark.slow
async def test_council_compiles_a_real_graph():
    async with EventStore() as store:
        rid = str(uuid.uuid4())
        await store.create_run(rid, "Audit this repository", "pending", 42, {}, "running")
        graph, verdict = await compile_graph(
            "Audit this repository and produce a prioritized remediation report.",
            MEMBERS, store=store, run_id=rid, seed=42,
        )
        assert graph.config_hash
        assert any(n.type == "fanout" for n in graph.nodes)
        assert any(n.type == "verify" for n in graph.nodes)

        kinds = [e.event_type for e in await store.read(rid)]
        assert EventType.COUNCIL_PROPOSAL in kinds
        assert EventType.GRAPH_PROPOSED in kinds
