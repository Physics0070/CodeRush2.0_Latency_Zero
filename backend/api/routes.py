"""HTTP surface. Everything the UI needs is a read of the event log."""

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from backend.api.schemas import (
    ApprovalRequest,
    ChatRequest,
    CompileRequest,
    GoalRequest,
    MarginalValueRequest,
    RunRequest,
)
from backend.config import settings
from backend.council import clarifying_questions, compile_graph, permission_prompt
from backend.engine import Executor, GraphSpec
from backend.events import EventStore, EventType
from backend.permissions.red_agent import red_agent_graph
from backend.providers.secret_broker import configured_providers

log = logging.getLogger("aco.api")
router = APIRouter(prefix="/api")

# In-flight runs, so cancel/pause can reach them. State of record is still the
# event log; this is only a handle to the asyncio task.
_TASKS: dict[str, asyncio.Task] = {}

# spec -> (provider, model-tags-from-config) builder, keyed by LLM_PROVIDER.
# Only "ollama" is implemented; adding a second provider is one more entry
# here, not a rewrite of this function - the rest of the registry (groq,
# gemini, openrouter adapters) already speaks the same "provider:model" spec
# shape and stays reachable via an explicit req.models override even though
# it is not the default.
_MODEL_BUILDERS = {
    "ollama": lambda: [
        f"ollama:{m.strip()}" for m in settings.ollama_models.split(",") if m.strip()
    ],
}


async def _default_models() -> list[str]:
    """Model specs for the configured LLM_PROVIDER, fastest/cheapest first.

    `models[0]` answers, is preferred by the planner, and is the council
    chairman by default; the rest widen the council. Model names themselves
    live in config (OLLAMA_MODELS) rather than here, so a local tag and a
    :cloud tag are interchangeable without touching this function - see
    backend/config.py.
    """
    provider = settings.llm_provider or "ollama"
    build = _MODEL_BUILDERS.get(provider)
    if build is None or provider not in configured_providers():
        return []
    return build()


NO_MODELS = (
    "No model provider is configured. Set OLLAMA_BASE_URL (and OLLAMA_API_KEY "
    "for Ollama Cloud) as environment variables."
)


def _require_models(models: list[str]) -> list[str]:
    """Fail loudly and early rather than deep inside a turn.

    With no hosted key there is nothing to answer with, and the old local
    fallback is gone. A 503 naming the missing variable is far easier to act on
    than an IndexError on `models[0]` three frames down.
    """
    if not models:
        raise HTTPException(status_code=503, detail=NO_MODELS)
    return models


@router.get("/models")
async def models() -> dict:
    return {"models": await _default_models(), "providers": configured_providers()}


@router.post("/chat")
async def chat(req: ChatRequest, request: Request) -> EventSourceResponse:
    """Ask anything, get an answer. SSE so the answer streams like a chat should.

    The turn also reports what it cost to produce - route taken, specialists
    consulted, overlap between them - which is the whole point of orchestrating
    rather than forwarding the question to one model.
    """
    from backend.chat import run_turn

    models_ = _require_models(req.models or await _default_models())
    history = [m.model_dump() for m in req.history]

    async def gen():
        store: EventStore | None = None
        try:
            store = EventStore()
            await store.__aenter__()
        except Exception as e:
            log.warning("chat running without the event log: %s", e)
            store = None
        try:
            async for evt in run_turn(
                req.message, history=history, models=models_, seed=req.seed, store=store
            ):
                if await request.is_disconnected():
                    return
                yield {"event": evt["event"], "data": json.dumps(evt["data"], default=str)}
        except Exception as e:
            log.exception("chat turn failed")
            yield {"event": "error", "data": json.dumps({"detail": str(e)[:300]})}
        finally:
            if store is not None:
                await store.__aexit__(None, None, None)

    return EventSourceResponse(gen())


@router.post("/chat/plan")
async def chat_plan(req: ChatRequest) -> dict:
    """The routing decision on its own, without answering.

    Exposed because "why did this question get four agents and that one get
    none" is the first thing anyone asks about the router.
    """
    from backend.chat import plan_for

    models_ = _require_models(req.models or await _default_models())
    plan = await plan_for(req.message, models_[0] if models_ else None)
    return {
        **plan.model_dump(),
        "route": "council" if plan.needs_council else "fast",
    }


@router.post("/clarify")
async def clarify(req: GoalRequest) -> dict:
    """Ask about THIS goal, then ask permission. Nothing runs yet.

    The questions come from a planner that reads the goal. The fixed list is
    still the fallback, because a clarify step that fails because the planner
    model is down is worse than three generic questions.
    """
    from backend.chat import plan_for

    models_ = _require_models(req.models or await _default_models())
    plan = await plan_for(req.goal, models_[0] if models_ else None)

    questions = plan.clarifying_questions or clarifying_questions(req.goal)
    branches = [s.id for s in plan.specialists] or ["security", "quality", "docs"]
    return {
        "questions": [{"id": f"q{i}", "text": q} for i, q in enumerate(questions)],
        "permission": permission_prompt(branches),
        "plan": {"intent": plan.intent, "complexity": plan.complexity,
                 "rationale": plan.rationale, "source": plan.source,
                 "specialists": [{"id": s.id, "role": s.role} for s in plan.specialists]},
    }


@router.post("/compile")
async def compile_endpoint(req: CompileRequest) -> dict:
    """Council designs the graph. Not executed until /runs is called."""
    models_ = _require_models(req.models or await _default_models())
    async with EventStore() as store:
        run_id = str(uuid.uuid4())
        await store.create_run(run_id, req.goal, "pending", req.seed, {}, "pending")
        for i, (q, a) in enumerate(req.answers.items()):
            await store.append(run_id, EventType.CLARIFY_ASKED, payload={"index": i, "question": q})
            await store.append(run_id, EventType.CLARIFY_ANSWERED, payload={"question": q,
                                                                            "answer": a})
        graph, verdict = await compile_graph(
            req.goal, models_, store=store, run_id=run_id, seed=req.seed,
            chairman_policy=req.chairman_policy,
        )
        return {
            "run_id": run_id,
            "graph": graph.model_dump(),
            "council": None if not verdict else {
                "chairman": verdict.chairman,
                "borda": verdict.borda,
                "winner_label": verdict.winner_label,
                "winner_author": verdict.winner_author,
                "disagreement": verdict.disagreement,
                "escalated": verdict.escalated,
                "proposals": [
                    {"label": p.label, "author_model": p.author_model,
                     "tokens": p.tokens_in + p.tokens_out}
                    for p in verdict.proposals
                ],
                "rankings": [{"ranker": r.ranker_model, "order": r.order}
                             for r in verdict.rankings],
            },
        }


@router.post("/runs")
async def start_run(req: RunRequest) -> dict:
    graph = GraphSpec(**req.graph.model_dump()).finalize()
    if graph.locked and req.graph.locked is False:
        raise HTTPException(status_code=409, detail="graph is locked")

    problems = graph.validate_side_effects()
    if problems:
        raise HTTPException(status_code=400, detail={"unapproved_side_effects": problems})

    run_id = str(uuid.uuid4())
    async with EventStore() as store:
        await store.create_run(
            run_id, req.goal, graph.config_hash, req.seed, graph.model_dump(), "pending"
        )
        await store.append(run_id, EventType.GRAPH_APPROVED,
                           payload={"config_hash": graph.config_hash})

    async def _execute():
        async with EventStore() as s:
            try:
                await Executor(
                    s, graph, run_id, seed=req.seed, approvals=set(req.approvals)
                ).run()
            except Exception:
                log.exception("run %s crashed", run_id)
                await s.set_run_status(run_id, "failed", ended=True)
            finally:
                _TASKS.pop(run_id, None)

    _TASKS[run_id] = asyncio.create_task(_execute())
    return {"run_id": run_id, "config_hash": graph.config_hash, "status": "running"}


@router.post("/runs/{run_id}/approve")
async def approve(run_id: str, req: ApprovalRequest) -> dict:
    async with EventStore() as store:
        if await store.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="unknown run")
        await store.append(
            run_id,
            EventType.APPROVAL_GRANTED if req.granted else EventType.CANCEL,
            node_id=req.node_id, payload={"granted": req.granted},
        )
    return {"ok": True}


@router.post("/runs/{run_id}/cancel")
async def cancel(run_id: str) -> dict:
    task = _TASKS.get(run_id)
    if task:
        task.cancel()
    async with EventStore() as store:
        await store.append(run_id, EventType.CANCEL, payload={"by": "user"})
        await store.set_run_status(run_id, "cancelled", ended=True)
    return {"ok": True}


@router.post("/runs/{run_id}/pause")
async def pause(run_id: str) -> dict:
    async with EventStore() as store:
        await store.append(run_id, EventType.PAUSE, payload={"by": "user"})
        await store.set_run_status(run_id, "paused")
    return {"ok": True}


@router.post("/runs/{run_id}/resume")
async def resume(run_id: str) -> dict:
    async with EventStore() as store:
        await store.append(run_id, EventType.RESUME, payload={"by": "user"})
        await store.set_run_status(run_id, "running")
    return {"ok": True}


@router.get("/runs")
async def list_runs(limit: int = 25) -> dict:
    limit = max(1, min(limit, 100))
    async with EventStore() as store:
        rows = await store.list_runs(limit)
        return {"runs": [r.model_dump() for r in rows]}


@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict:
    async with EventStore() as store:
        row = await store.get_run(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown run")
        cost = await store.aggregate_cost(run_id)
        lat = await store.aggregate_latency(run_id)
        return {
            "run": row.model_dump(),
            "cost": cost.model_dump(),
            "latency": {**lat.model_dump(), "parallel_efficiency": lat.parallel_efficiency},
        }


@router.get("/runs/{run_id}/events")
async def events(run_id: str) -> dict:
    async with EventStore() as store:
        return {"events": [e.model_dump() for e in await store.read(run_id)]}


@router.get("/runs/{run_id}/stream")
async def stream(run_id: str, request: Request) -> EventSourceResponse:
    """SSE tail of the event log. No polling from the browser - the browser
    holds one connection and the server does the polling, once."""

    async def gen():
        last = 0
        idle = 0
        async with EventStore() as store:
            while not await request.is_disconnected():
                rows = await store.read(run_id, from_seq=last)
                if rows:
                    idle = 0
                    for e in rows:
                        last = e.seq
                        yield {"event": "log", "data": json.dumps(e.model_dump(), default=str)}
                        if e.event_type == EventType.RUN_END:
                            yield {"event": "done", "data": json.dumps({"run_id": run_id})}
                            return
                else:
                    idle += 1
                    if idle > 600:  # ~5 min of silence
                        return
                await asyncio.sleep(0.5)

    # ping: an SSE comment every 15s so a proxy or load balancer sees traffic
    # on an otherwise-quiet long poll and doesn't time the connection out.
    # X-Accel-Buffering: nginx-family proxies (which Render's edge is) buffer
    # a response by default, which would hold the whole stream until it ends -
    # exactly what a live tail must not do.
    return EventSourceResponse(gen(), ping=15, headers={"X-Accel-Buffering": "no"})


@router.get("/runs/{run_id}/metrics")
async def metrics(run_id: str) -> dict:
    from backend.metrics import compute

    async with EventStore() as store:
        if await store.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="unknown run")
        return (await compute(store, run_id)).model_dump()


@router.post("/runs/{run_id}/replay")
async def replay(run_id: str) -> dict:
    from backend.replay import replay_run

    async with EventStore() as store:
        try:
            result, diff = await replay_run(store, run_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {
            "replay_run_id": result.run_id,
            "original_run_id": run_id,
            "diff": diff.model_dump(),
            "cost_saved_usd": diff.cost_saved_usd,
        }


@router.post("/marginal-value")
async def marginal_value(req: MarginalValueRequest) -> dict:
    from backend.metrics import marginal_value_report

    models_ = _require_models(req.models or await _default_models())
    async with EventStore() as store:
        report = await marginal_value_report(
            store, req.goal, models_, depths=tuple(req.depths), seed=req.seed
        )
        return report.model_dump()


@router.post("/demo/red-agent")
async def red_agent_demo() -> dict:
    """Demo step 7: undeclared tool call blocked, smuggled instruction quarantined."""
    from backend.permissions import CapabilityGuard, screen_payload
    from backend.permissions.red_agent import POISONED_HANDOFF, RED_AGENT

    graph = red_agent_graph()
    run_id = str(uuid.uuid4())
    async with EventStore() as store:
        await store.create_run(run_id, "Red Agent demonstration", graph.config_hash, 42,
                               graph.model_dump(), "running")
        await store.append(run_id, EventType.RUN_START, payload={"demo": "red_agent"})

        guard = CapabilityGuard(store, run_id)

        async def file_delete(path: str) -> str:
            return f"deleted {path}"  # registered, never reached

        guard.register("file_delete", file_delete)
        blocked = await guard.call(
            agent_id=RED_AGENT.id, declared_tools=RED_AGENT.tools,
            tool="file_delete", arguments={"path": "backend/config.py"}, node_id="red",
        )
        _, quarantined = await screen_payload(
            store, run_id, {"red": POISONED_HANDOFF},
            node_id="writer", agent_id="report_writer",
        )
        await store.append(run_id, EventType.RUN_END,
                           payload={"tool_blocked": blocked.blocked,
                                    "injection_blocked": quarantined})
        await store.set_run_status(run_id, "done", ended=True)

        rows = await store.read(run_id)
        return {
            "run_id": run_id,
            "tool_blocked": blocked.blocked,
            "injection_blocked": quarantined,
            "events": [
                e.model_dump() for e in rows
                if e.event_type in (EventType.TOOL_BLOCKED, EventType.INJECTION_BLOCKED)
            ],
        }
