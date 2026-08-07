"""One chat turn, start to finish.

Emits a stream of events the UI renders as it goes: the routing decision, each
specialist as it lands, the answer token by token, then the measured benchmarks.

The benchmarks are computed from what this turn actually did - wall clock,
token counts and embedding distances between the specialists' contributions.
Nothing here is estimated, and a turn that took the fast path reports that it
took the fast path rather than inventing council numbers.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator

from backend.chat.answer import (
    CONTRIBUTION_SCHEMA,
    contribute,
    stream_direct,
    stream_synthesis,
)
from backend.chat.planner import Plan, pick_planner_model, plan_for
from backend.engine.spec import AgentSpec, Edge, GraphSpec, Node
from backend.events import EventStore, EventType

log = logging.getLogger("aco.chat")

# A specialist that has not landed by now is a failed branch, not a reason to
# hold the whole turn. The Ollama client's own timeout is 300s, so without this
# one slow local model stalls the answer for five minutes and the user watches
# an empty screen - which is indistinguishable from the app being broken.
SPECIALIST_TIMEOUT_S = 45.0


def specialist_models(models: list[str]) -> list[str]:
    """The models the fanout is allowed to use.

    Round-robining across the whole list hands specialists local Ollama models,
    which generate at well under one token per second in a memory-capped
    container - far slower than any timeout worth waiting for. When a hosted
    model is configured the council runs on hosted models only. With no key at
    all the local list is still the zero-cost fallback, as before.
    """
    hosted = [m for m in models if not m.startswith("ollama:")]
    return hosted or models


async def _bounded(coro, aspect_id: str) -> dict:
    """Run one specialist under the timeout, degrading instead of raising."""
    try:
        return await asyncio.wait_for(coro, SPECIALIST_TIMEOUT_S)
    except TimeoutError:
        log.warning("specialist %s timed out after %ss", aspect_id, SPECIALIST_TIMEOUT_S)
        return {"aspect": aspect_id, "points": [],
                "error": f"timed out after {int(SPECIALIST_TIMEOUT_S)}s"}


def chat_graph(plan: Plan, models: list[str]) -> GraphSpec:
    """The shape this turn will actually run, as a real GraphSpec.

    Not decoration. Replay recomputes the config hash from the stored spec and
    refuses a run whose spec was modified, so a chat turn needs a genuine graph
    to be replayable at all. It also means a chat turn renders in the same
    canvas as an orchestrated run, because it is the same kind of object.
    """
    nodes: list[Node] = []
    edges: list[Edge] = []

    if plan.needs_council:
        nodes.append(Node(id="fanout", type="fanout"))
        for i, s in enumerate(plan.specialists):
            nodes.append(Node(id=s.id, type="agent", agent=AgentSpec(
                id=s.id, role=s.role,
                system_contract=f"Examine one aspect of the question: {s.role}",
                tools=[], model=models[i % len(models)],
                output_schema=CONTRIBUTION_SCHEMA,
                budget_tokens=900, timeout_s=180,
            )))
            edges.append(Edge(from_node="fanout", to_node=s.id,
                              handoff_schema=CONTRIBUTION_SCHEMA))
        nodes.append(Node(id="join", type="join"))
        for s in plan.specialists:
            edges.append(Edge(from_node=s.id, to_node="join"))

    # A passthrough, not an agent node. The answer is prose, and agent nodes
    # are schema-validated JSON handoffs - modelling it as one makes replay try
    # to parse an English paragraph as a typed contract and fail every time.
    # The answer text is still recorded as a TOOL_RESULT row.
    nodes.append(Node(id="answer", type="join"))
    if plan.needs_council:
        edges.append(Edge(from_node="join", to_node="answer"))

    return GraphSpec(version=1, nodes=nodes, edges=edges).finalize()


def _mig(contributions: list[dict]) -> dict:
    """How much each specialist added that the others did not.

    1 - max cosine to any other specialist. A branch that restates its
    neighbour scores near zero, which is the honest signal that the council was
    wider than the question needed.
    """
    from backend.metrics.metrics import DUPLICATE_THRESHOLD, cosine_matrix, embeddings_available

    texts, ids = [], []
    for c in contributions:
        body = " ".join(
            f"{p.get('claim', '')} {p.get('support', '')}"
            for p in c.get("points") or []
            if isinstance(p, dict)
        ).strip()
        if body:
            texts.append(body)
            ids.append(c["aspect"])

    if len(texts) < 2 or not embeddings_available():
        return {"available": False, "per_agent": {}, "overlapping_pairs": []}

    try:
        sim = cosine_matrix(texts)
    except Exception as e:
        log.warning("embedding failed: %s", e)
        return {"available": False, "per_agent": {}, "overlapping_pairs": []}

    per_agent, pairs = {}, []
    for i, aid in enumerate(ids):
        others = [float(sim[i][j]) for j in range(len(ids)) if j != i]
        per_agent[aid] = round(1.0 - max(others), 4)
        for j in range(i + 1, len(ids)):
            if float(sim[i][j]) >= DUPLICATE_THRESHOLD:
                pairs.append({"a": aid, "b": ids[j], "similarity": round(float(sim[i][j]), 4)})
    return {"available": True, "per_agent": per_agent, "overlapping_pairs": pairs}


async def run_turn(
    question: str,
    *,
    history: list[dict] | None = None,
    models: list[str],
    seed: int = 42,
    store: EventStore | None = None,
) -> AsyncIterator[dict]:
    """Yield `{"event": ..., "data": ...}` for the whole turn.

    `store` is optional: the chat works without a database, it just loses the
    replayable trace. A Supabase outage should not take the product down.
    """
    run_id = str(uuid.uuid4())
    answer_model = models[0]
    t0 = time.perf_counter()
    first_token_ms: int | None = None

    yield {"event": "start", "data": {"run_id": run_id}}

    # ---------- plan ----------
    yield {"event": "status", "data": {"stage": "planning"}}
    plan: Plan = await plan_for(question, pick_planner_model(models))
    plan_ms = int((time.perf_counter() - t0) * 1000)

    # The run is created after planning, not before: the graph is a product of
    # the plan, and replay validates the stored spec against its own hash.
    graph = chat_graph(plan, models)
    if store:
        try:
            await store.create_run(run_id, question[:2000], graph.config_hash, seed,
                                   graph.model_dump(), "running")
            await store.append(run_id, EventType.RUN_START,
                               payload={"mode": "chat", "question": question[:2000]})
            await store.append(run_id, EventType.GRAPH_PROPOSED, payload={
                "intent": plan.intent, "complexity": plan.complexity,
                "rationale": plan.rationale, "source": plan.source,
                "config_hash": graph.config_hash,
                "branches": [s.id for s in plan.specialists],
            })
        except Exception as e:
            log.warning("event log unavailable, continuing without it: %s", e)
            store = None

    yield {"event": "plan", "data": {
        "intent": plan.intent, "complexity": plan.complexity,
        "rationale": plan.rationale, "source": plan.source,
        "route": "fast" if not plan.needs_council else "council",
        "clarifying_questions": plan.clarifying_questions,
        "specialists": [{"id": s.id, "role": s.role} for s in plan.specialists],
        "plan_ms": plan_ms,
    }}

    # ---------- specialists ----------
    contributions: list[dict] = []
    fanout_ms = 0
    if plan.needs_council:
        yield {"event": "status", "data": {"stage": "consulting",
                                           "count": len(plan.specialists)}}
        if store:
            for s in plan.specialists:
                await store.append(run_id, EventType.NODE_START, node_id=s.id,
                                   payload={"role": s.role})

        fan_t0 = time.perf_counter()
        # One gather: every specialist starts before any of them finishes, and
        # each is bounded so the slowest cannot decide how long the turn takes.
        fan_models = specialist_models(models)
        contributions = await asyncio.gather(*[
            _bounded(
                contribute(question, s.id, s.role,
                           fan_models[i % len(fan_models)], seed=seed),
                s.id,
            )
            for i, s in enumerate(plan.specialists)
        ])
        fanout_ms = int((time.perf_counter() - fan_t0) * 1000)

        for c in contributions:
            if store:
                # TOOL_RESULT is what replay serves from, keyed by
                # (node_id, agent_id). Without this row a chat turn records
                # that it happened but cannot be replayed, which would make
                # the drawer's replay claim false for exactly the turns most
                # people will look at.
                if not c.get("error"):
                    await store.append(
                        run_id, EventType.TOOL_RESULT,
                        node_id=c["aspect"], agent_id=c["aspect"],
                        tokens_in=c.get("tokens_in", 0),
                        tokens_out=c.get("tokens_out", 0),
                        cost_usd=c.get("cost_usd", 0.0),
                        latency_ms=c.get("latency_ms"),
                        payload={"model": c.get("model", ""),
                                 "used_fallback": c.get("used_fallback", False),
                                 "replayed": False,
                                 "text": c.get("text", "")},
                    )
                    # The validated handoff is what a replay diff compares.
                    # Without it the original run has no recorded outputs and
                    # every node reads as a difference.
                    await store.append(
                        run_id, EventType.HANDOFF_VALIDATED,
                        node_id=c["aspect"], agent_id=c["aspect"],
                        payload={"attempt": 1 if c.get("repaired") else 0,
                                 "payload": {"points": c.get("points") or []}},
                    )
                await store.append(
                    run_id,
                    EventType.NODE_END if not c.get("error") else EventType.BRANCH_FAILED,
                    node_id=c["aspect"], agent_id=c["aspect"],
                    payload={k: v for k, v in c.items() if k != "text"},
                )
            yield {"event": "specialist", "data": {
                "id": c["aspect"], "points": len(c.get("points") or []),
                "model": c.get("model"), "latency_ms": c.get("latency_ms"),
                "tokens": c.get("tokens"), "error": c.get("error"),
            }}

    # ---------- answer ----------
    yield {"event": "status", "data": {"stage": "answering"}}
    chunks: list[str] = []
    gen = (
        stream_synthesis(question, contributions, answer_model, history=history)
        if plan.needs_council
        else stream_direct(question, answer_model, history=history)
    )
    try:
        async for piece in gen:
            if first_token_ms is None:
                first_token_ms = int((time.perf_counter() - t0) * 1000)
            chunks.append(piece)
            yield {"event": "token", "data": piece}
    except Exception as e:
        log.exception("answer stream failed")
        msg = f"\n\n_The answer stream failed: {e}_"
        chunks.append(msg)
        yield {"event": "token", "data": msg}

    answer = "".join(chunks)
    total_ms = int((time.perf_counter() - t0) * 1000)

    if store and answer:
        # The answer is a completion too. Recording it under a stable node id
        # means a replayed turn reproduces the reply itself, not just the
        # specialist notes that fed it.
        await store.append(
            run_id, EventType.TOOL_RESULT, node_id="answer", agent_id="answer",
            latency_ms=total_ms,
            payload={"model": answer_model, "used_fallback": False,
                     "replayed": False, "text": answer},
        )

    # ---------- benchmarks ----------
    spec_latency = sum(c.get("latency_ms") or 0 for c in contributions)
    tokens = sum(c.get("tokens") or 0 for c in contributions)
    cost = sum(c.get("cost_usd") or 0.0 for c in contributions)
    bench = {
        "run_id": run_id,
        "route": "fast" if not plan.needs_council else "council",
        "intent": plan.intent,
        "complexity": plan.complexity,
        "planner_source": plan.source,
        "models_used": sorted(
            {answer_model, *(c.get("model") or "" for c in contributions)} - {""}
        ),
        "timing": {
            "plan_ms": plan_ms,
            "fanout_ms": fanout_ms,
            "first_token_ms": first_token_ms,
            "total_ms": total_ms,
        },
        "specialists": [
            {"id": c["aspect"], "points": len(c.get("points") or []),
             "latency_ms": c.get("latency_ms"), "tokens": c.get("tokens"),
             "failed": bool(c.get("error"))}
            for c in contributions
        ],
        # Sequential cost divided by wall clock. >1 means the fanout genuinely
        # overlapped; 0 branches means the fast path, reported as null not 1.0.
        "parallel_efficiency": (
            round(spec_latency / fanout_ms, 2) if fanout_ms > 0 and spec_latency else None
        ),
        "tokens": tokens,
        "cost_usd": round(cost, 6),
        "answer_chars": len(answer),
        "marginal_information_gain": _mig(contributions) if contributions else {
            "available": False, "per_agent": {}, "overlapping_pairs": []},
    }

    if store:
        try:
            await store.append(run_id, EventType.RUN_END, payload={"benchmarks": bench})
            await store.set_run_status(run_id, "done", ended=True)
        except Exception as e:
            log.warning("could not close run in log: %s", e)

    yield {"event": "benchmarks", "data": bench}
    yield {"event": "done", "data": {"run_id": run_id, "answer": answer}}
