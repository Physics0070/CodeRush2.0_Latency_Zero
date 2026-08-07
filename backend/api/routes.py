"""HTTP surface. Everything the UI needs is a read of the event log."""

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from backend.api.schemas import (
    ApprovalRequest,
    CompileRequest,
    GoalRequest,
    MarginalValueRequest,
    RunRequest,
)
from backend.council import clarifying_questions, compile_graph, permission_prompt
from backend.engine import Executor, GraphSpec
from backend.events import EventStore, EventType
from backend.permissions.red_agent import red_agent_graph
from backend.providers.ollama import OllamaAdapter
from backend.providers.secret_broker import configured_providers, get_handle

log = logging.getLogger("aco.api")
router = APIRouter(prefix="/api")

# In-flight runs, so cancel/pause can reach them. State of record is still the
# event log; this is only a handle to the asyncio task.
_TASKS: dict[str, asyncio.Task] = {}


async def _default_models() -> list[str]:
    """Prefer two distinct local models so the council is a real council."""
    try:
        local = [
            f"ollama:{m}"
            for m in await OllamaAdapter(get_handle("ollama")).list_models()
            if not m.endswith(":cloud")
        ]
    except Exception:
        local = []
    if "groq" in configured_providers():
        local.append("groq:llama-3.3-70b-versatile")
    return local[:3] or ["ollama:llama3.2:3b"]


@router.get("/models")
async def models() -> dict:
    return {"models": await _default_models(), "providers": configured_providers()}


@router.post("/clarify")
async def clarify(req: GoalRequest) -> dict:
    """Demo step 2: ask, then ask permission. Nothing runs yet."""
    questions = clarifying_questions(req.goal)
    return {
        "questions": [{"id": f"q{i}", "text": q} for i, q in enumerate(questions)],
        "permission": permission_prompt(["security", "quality", "docs"]),
    }


@router.post("/compile")
async def compile_endpoint(req: CompileRequest) -> dict:
    """Council designs the graph. Not executed until /runs is called."""
    models_ = req.models or await _default_models()
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
        r = await store._request(
            "GET", "/runs",
            params={"select": "run_id,goal,status,config_hash,seed,replay_of,started_at,ended_at",
                    "order": "started_at.desc", "limit": str(limit)},
        )
        return {"runs": r.json()}


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

    return EventSourceResponse(gen())


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

    models_ = req.models or await _default_models()
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
