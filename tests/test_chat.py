"""Routing, answering, and the benchmarks that describe both.

The behaviour under test is the one that was wrong before: the plan must come
from the question. A test that only checked "a plan was produced" would have
passed against the hardcoded version, so these assert that different questions
produce different plans.
"""

import pytest

from backend.chat import heuristic_plan, run_turn
from backend.chat.answer import CONTRIBUTION_SCHEMA, _format_contributions, stream_direct
from backend.chat.planner import (
    Plan,
    Specialist,
    _coerce,
    obviously_simple,
    pick_planner_model,
)
from backend.chat.session import _mig, chat_graph
from backend.handoff import validate
from backend.providers.base import ProviderError

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------- routing


@pytest.mark.parametrize(
    "question,expected",
    [
        ("hi", "simple"),
        ("what is a mutex?", "simple"),
        ("thanks!", "simple"),
        ("compare REST versus GraphQL", "moderate"),
        ("should i use postgres or mongo", "moderate"),
        ("audit my authentication flow", "deep"),
        ("review the tradeoffs of event sourcing", "deep"),
    ],
)
def test_heuristic_routes_by_what_was_asked(question, expected):
    assert heuristic_plan(question).complexity == expected


def test_cheap_questions_skip_the_planner_call():
    """The planner is a full round trip; short unambiguous text must not pay it."""
    assert obviously_simple("hi")
    assert obviously_simple("what is a semaphore?")
    # Breadth markers outrank brevity - "audit my auth" is 3 words and deep.
    assert not obviously_simple("audit my auth")
    assert not obviously_simple("compare a and b")


def test_simple_never_convenes_a_council():
    p = heuristic_plan("hi")
    assert p.specialists == [] and not p.needs_council


def test_deep_gets_more_branches_than_moderate():
    assert len(heuristic_plan("audit my payment flow").specialists) > len(
        heuristic_plan("compare a and b").specialists
    )


def test_planner_prefers_a_fast_model_for_routing():
    assert pick_planner_model(["ollama:qwen2.5:7b", "groq:llama-3.3-70b-versatile"]) == (
        "groq:llama-3.3-70b-versatile"
    )
    assert pick_planner_model(["ollama:a:1"]) == "ollama:a:1"
    assert pick_planner_model([]) is None


# ------------------------------------------------------- coercing model JSON


def test_specialists_come_from_the_question_not_a_fixed_list():
    plan = _coerce(
        {
            "intent": "compare",
            "complexity": "moderate",
            "specialists": [
                {"id": "Locking Strategies", "role": "concurrency control options"},
                {"id": "ledger_architecture", "role": "write path design"},
            ],
        },
        "compare locking for a ledger",
    )
    ids = [s.id for s in plan.specialists]
    assert ids == ["locking_strategies", "ledger_architecture"]
    assert not ({"security", "quality", "docs"} & set(ids))


def test_contradictory_plan_believes_the_specialist_list():
    """'deep' with one branch is not a council. The list is what executes."""
    plan = _coerce(
        {"complexity": "deep", "specialists": [{"id": "only", "role": "x"}]},
        "audit my authentication flow for problems",
    )
    assert len(plan.specialists) >= 2


def test_simple_plan_is_stripped_of_council_machinery():
    plan = _coerce(
        {"complexity": "simple", "specialists": [{"id": "a", "role": "x"}],
         "clarifying_questions": ["really?"]},
        "hi",
    )
    assert plan.specialists == [] and plan.clarifying_questions == []


def test_garbage_specialists_are_dropped_not_crashed_on():
    plan = _coerce(
        {"complexity": "moderate",
         "specialists": ["not a dict", {"id": "!!!", "role": "x"}, {"id": "ok", "role": ""}]},
        "compare a and b",
    )
    # None survive, so the heuristic backfills rather than leaving zero branches.
    assert len(plan.specialists) >= 2


# ------------------------------------------------------------- contributions


def test_contribution_schema_accepts_a_real_contribution():
    assert validate(
        {"points": [{"claim": "Locks serialize writers", "support": "only one holder at a time",
                     "confidence": "high"}]},
        CONTRIBUTION_SCHEMA,
    ).ok


def test_contribution_schema_rejects_an_unsupported_claim():
    assert not validate({"points": [{"claim": "it is faster"}]}, CONTRIBUTION_SCHEMA).ok


def test_formatting_skips_empty_specialists():
    text = _format_contributions([
        {"aspect": "a", "points": [{"claim": "C1", "support": "S1"}]},
        {"aspect": "b", "points": []},
    ])
    assert "C1" in text and "Notes on b" not in text


# -------------------------------------------------------------- benchmarks


def test_mig_needs_two_specialists_to_mean_anything():
    assert _mig([{"aspect": "a", "points": [{"claim": "x", "support": "y"}]}])["available"] is False


def test_mig_flags_specialists_that_said_the_same_thing():
    same = "Optimistic locking detects conflicts at commit time using a version column"
    out = _mig([
        {"aspect": "a", "points": [{"claim": same, "support": same}]},
        {"aspect": "b", "points": [{"claim": same, "support": same}]},
    ])
    if not out["available"]:
        pytest.skip("embeddings unavailable")
    assert out["overlapping_pairs"], "identical contributions must be reported as overlapping"
    assert out["per_agent"]["a"] < 0.3


# ------------------------------------------------------------ the turn shape


async def test_turn_emits_an_answer_and_benchmarks(monkeypatch):
    """No provider is called: the point is the event contract the UI depends on."""

    async def fake_stream(question, model, *, history=None, temperature=0.3, fallback_model=None):
        for piece in ("Hello", " there"):
            yield piece

    monkeypatch.setattr("backend.chat.session.stream_direct", fake_stream)

    events = [e async for e in run_turn("hi", models=["ollama:x:1"], store=None)]
    kinds = [e["event"] for e in events]

    assert kinds[0] == "start" and kinds[-1] == "done"
    assert "plan" in kinds and "benchmarks" in kinds
    assert "".join(e["data"] for e in events if e["event"] == "token") == "Hello there"

    bench = next(e["data"] for e in events if e["event"] == "benchmarks")
    assert bench["route"] == "fast"
    # The fast path ran no branches, so it must not report a parallel speedup.
    assert bench["parallel_efficiency"] is None
    assert bench["specialists"] == []
    assert bench["timing"]["first_token_ms"] is not None


async def test_turn_survives_a_broken_answer_stream(monkeypatch):
    """A provider dying mid-answer must still close the turn cleanly."""

    async def boom(question, model, *, history=None, temperature=0.3, fallback_model=None):
        yield "partial"
        raise RuntimeError("provider died")

    monkeypatch.setattr("backend.chat.session.stream_direct", boom)

    events = [e async for e in run_turn("hi", models=["ollama:x:1"], store=None)]
    assert events[-1]["event"] == "done"
    assert "partial" in events[-1]["data"]["answer"]


async def test_stream_direct_sends_history_and_a_system_prompt(monkeypatch):
    seen: dict = {}

    class FakeAdapter:
        async def stream(self, messages, model, *, temperature=0.0, seed=None):
            seen["messages"] = messages
            yield "ok"

    monkeypatch.setattr(
        "backend.chat.answer.adapter_for", lambda spec: (FakeAdapter(), "m")
    )
    out = [p async for p in stream_direct(
        "and then?", "ollama:x:1", history=[{"role": "user", "content": "hi"}]
    )]

    assert out == ["ok"]
    assert seen["messages"][0]["role"] == "system"
    assert seen["messages"][-1]["content"] == "and then?"
    assert any(m["content"] == "hi" for m in seen["messages"])


async def test_stream_direct_falls_back_before_any_token_is_yielded(monkeypatch):
    """A 429 (or any provider failure) before the first token must be
    invisible to the user - retried once against fallback_model."""

    class FailingAdapter:
        async def stream(self, messages, model, *, temperature=0.0, seed=None):
            raise ProviderError("provider returned 429", status=429, retryable=True)
            yield  # pragma: no cover - unreachable; keeps this an async generator

    class FallbackAdapter:
        async def stream(self, messages, model, *, temperature=0.0, seed=None):
            yield "ok"

    def fake_adapter_for(spec):
        return (FailingAdapter(), spec) if spec == "primary:model" else (FallbackAdapter(), spec)

    monkeypatch.setattr("backend.chat.answer.adapter_for", fake_adapter_for)
    out = [p async for p in stream_direct(
        "q", "primary:model", fallback_model="fallback:model",
    )]
    assert out == ["ok"]


async def test_stream_direct_does_not_retry_after_a_token_was_already_sent(monkeypatch):
    """A failure mid-answer must be reported, not silently restarted - a
    retry here would splice two partial answers together."""

    class FlakyAdapter:
        async def stream(self, messages, model, *, temperature=0.0, seed=None):
            yield "partial"
            raise ProviderError("provider returned 500", status=500, retryable=True)

    class NeverCalledAdapter:
        async def stream(self, messages, model, *, temperature=0.0, seed=None):
            raise AssertionError("fallback must not run once a token has been sent")
            yield  # pragma: no cover

    def fake_adapter_for(spec):
        return (FlakyAdapter(), spec) if spec == "primary:model" else (NeverCalledAdapter(), spec)

    monkeypatch.setattr("backend.chat.answer.adapter_for", fake_adapter_for)
    got: list[str] = []
    with pytest.raises(ProviderError):
        async for piece in stream_direct("q", "primary:model", fallback_model="fallback:model"):
            got.append(piece)
    assert got == ["partial"]


def test_plan_route_is_reported_honestly():
    assert Plan(complexity="simple").needs_council is False
    assert Plan(complexity="deep", specialists=[]).needs_council is False


# ------------------------------------------------------- replayable turns


def test_chat_turn_has_a_real_graph_and_hash():
    """Replay recomputes the hash from the stored spec and refuses a mismatch,
    so a placeholder config_hash makes a turn unreplayable."""
    plan = Plan(complexity="moderate", specialists=[
        Specialist(id="a", role="x"), Specialist(id="b", role="y"),
    ])
    g = chat_graph(plan, ["ollama:m:1"])
    assert g.config_hash and g.config_hash != "chat"
    assert len(g.config_hash) >= 8
    # Every specialist runs in one level, which is what makes the fanout
    # parallel rather than a chain.
    assert sorted(g.levels()[1]) == ["a", "b"]


def test_the_answer_node_is_not_schema_validated():
    """The answer is prose. Modelling it as an agent node makes replay try to
    parse an English paragraph as a typed handoff, which fails every time."""
    g = chat_graph(Plan(complexity="simple"), ["ollama:m:1"])
    assert g.node("answer").type == "join"


def test_fast_path_graph_is_a_single_node():
    g = chat_graph(Plan(complexity="simple"), ["ollama:m:1"])
    assert [n.id for n in g.nodes] == ["answer"]


def test_council_graph_wires_fanout_to_every_specialist():
    plan = Plan(complexity="deep", specialists=[
        Specialist(id="a", role="x"), Specialist(id="b", role="y"),
        Specialist(id="c", role="z"),
    ])
    g = chat_graph(plan, ["ollama:m:1"])
    fanned = {e.to_node for e in g.edges if e.from_node == "fanout"}
    assert fanned == {"a", "b", "c"}
    assert g.node("a").agent.output_schema == CONTRIBUTION_SCHEMA
