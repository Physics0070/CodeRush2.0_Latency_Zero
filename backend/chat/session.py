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

from backend.chat.answer import contribute, stream_direct, stream_synthesis
from backend.chat.planner import Plan, pick_planner_model, plan_for
from backend.events import EventStore, EventType

log = logging.getLogger("aco.chat")


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

    if store:
        try:
            await store.create_run(run_id, question[:2000], "chat", seed, {}, "running")
            await store.append(run_id, EventType.RUN_START,
                               payload={"mode": "chat", "question": question[:2000]})
            await store.append(run_id, EventType.GRAPH_PROPOSED, payload={
                "intent": plan.intent, "complexity": plan.complexity,
                "rationale": plan.rationale, "source": plan.source,
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
        # One gather: every specialist starts before any of them finishes.
        contributions = await asyncio.gather(*[
            contribute(question, s.id, s.role, models[i % len(models)], seed=seed)
            for i, s in enumerate(plan.specialists)
        ])
        fanout_ms = int((time.perf_counter() - fan_t0) * 1000)

        for c in contributions:
            if store:
                await store.append(
                    run_id,
                    EventType.NODE_END if not c.get("error") else EventType.BRANCH_FAILED,
                    node_id=c["aspect"], agent_id=c["aspect"], payload=c,
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
