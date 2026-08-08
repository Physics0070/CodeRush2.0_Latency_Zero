"""Parallel-first execution engine.

Nodes are layered by real data dependency and every node in a layer runs
concurrently. Sequential, conditional, retry, join, approval and compensation
all still exist - the problem statement lists them explicitly - but they are the
exception, not the default shape.

Budgets are enforced here, in code. The model is never asked to respect a token
ceiling; it is cut off.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from backend.engine.spec import GraphSpec, Node
from backend.events import EventStore, EventType
from backend.handoff import repair_loop
from backend.memory import DEFAULT_MEMORY_TTL_S, memory_store
from backend.permissions import CapabilityGuard, screen_payload
from backend.providers import complete

log = logging.getLogger("aco.engine")

# Injected so replay can serve calls from the log instead of the network.
Completer = Callable[..., Awaitable]

# A subgraph node's nested GraphSpec can itself contain a subgraph node,
# recursively. Bounded so a malformed or self-referential graph fails loudly
# with a clear error instead of exhausting the call stack.
MAX_SUBGRAPH_DEPTH = 3


class NodeResult(BaseModel):
    node_id: str
    ok: bool
    output: dict | list | None = None
    error: str | None = None
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    blocked: bool = False
    skipped: bool = False


class RunResult(BaseModel):
    run_id: str
    ok: bool
    results: dict[str, NodeResult] = {}
    wall_clock_ms: int = 0
    parallel_efficiency: float = 0.0
    failed_nodes: list[str] = []
    compensated: list[str] = []


class BudgetExceeded(RuntimeError):
    pass


class Executor:
    def __init__(
        self,
        store: EventStore,
        graph: GraphSpec,
        run_id: str,
        *,
        seed: int = 42,
        completer: Completer | None = None,
        approvals: set[str] | None = None,
        clarifications: dict | None = None,
        depth: int = 0,
    ) -> None:
        self.store = store
        self.graph = graph
        self.run_id = run_id
        self.seed = seed
        self.completer = completer or complete
        # Approval gates already granted for this run (the UI posts these).
        self.approvals = approvals or set()
        self.clarifications = clarifications or {}
        self.outputs: dict[str, dict | list] = {}
        self.results: dict[str, NodeResult] = {}
        self.guard = CapabilityGuard(store, run_id)
        # 0 for the top-level executor of a run; a subgraph node's child
        # executor is constructed with depth+1. A constructor param, not a
        # class-level counter - separate runs execute as concurrent asyncio
        # Tasks in the same process, so a shared counter would leak between
        # unrelated runs.
        self.depth = depth

    # ------------------------------------------------------------------ run

    async def run(self) -> RunResult:
        problems = self.graph.validate_side_effects()
        all_ids = self.graph.all_node_ids()
        if len(all_ids) != len(set(all_ids)):
            dupes = sorted({i for i in all_ids if all_ids.count(i) > 1})
            problems.append(
                f"duplicate node id(s) across the graph and its nested subgraphs: {dupes}"
            )
        if problems:
            await self.store.append(
                self.run_id, EventType.BRANCH_FAILED,
                payload={"reason": "unapproved_side_effects", "problems": problems},
            )
            await self.store.set_run_status(self.run_id, "failed", ended=True)
            return RunResult(run_id=self.run_id, ok=False, failed_nodes=["<graph>"])

        await self.store.append_and_set_status(
            self.run_id, EventType.RUN_START,
            {"config_hash": self.graph.config_hash, "seed": self.seed},
            status="running",
        )

        started = time.perf_counter()
        failed, compensated = await self._run_graph()
        wall = max(int((time.perf_counter() - started) * 1000), 1)
        latency_sum = sum(r.latency_ms for r in self.results.values())

        pe = round(latency_sum / wall, 3)
        await self.store.append_and_set_status(
            self.run_id, EventType.RUN_END,
            {
                "ok": not failed,
                "failed_nodes": failed,
                "compensated": compensated,
                "wall_clock_ms": wall,
                "latency_sum_ms": latency_sum,
                "parallel_efficiency": pe,
            },
            status="done" if not failed else "failed", ended=True,
        )
        return RunResult(
            run_id=self.run_id,
            ok=not failed,
            results=self.results,
            wall_clock_ms=wall,
            parallel_efficiency=pe,
            failed_nodes=failed,
            compensated=compensated,
        )

    async def _run_graph(self) -> tuple[list[str], list[str]]:
        """The level loop + compensation, with no run-status bookkeeping.

        Split out of `run()` so a subgraph node can execute a nested GraphSpec
        under the *same* run_id without marking the whole run ended the moment
        the nested graph finishes - see `_subgraph_node`. Populates
        `self.results`/`self.outputs` exactly as `run()` used to inline.
        """
        failed: list[str] = []

        for level in self.graph.levels():
            runnable = [n for n in level if self._should_run(n)]
            for n in level:
                if n not in runnable:
                    self.results[n] = NodeResult(node_id=n, ok=True, skipped=True)

            if not runnable:
                continue

            # The whole point: one gather per level, not one await per node.
            settled = await asyncio.gather(
                *(self._run_node(self.graph.node(n)) for n in runnable),
                return_exceptions=True,
            )
            for node_id, outcome in zip(runnable, settled, strict=True):
                if isinstance(outcome, BaseException):
                    log.exception("node %s raised", node_id)
                    res = NodeResult(node_id=node_id, ok=False, error=str(outcome))
                else:
                    res = outcome
                self.results[node_id] = res
                if res.ok and res.output is not None:
                    self.outputs[node_id] = res.output
                if not res.ok:
                    failed.append(node_id)

        compensated = await self._compensate(failed)
        return failed, compensated

    # ------------------------------------------------------------- per node

    def _should_run(self, node_id: str) -> bool:
        """A node is skipped when a parent failed or a conditional excluded it."""
        for p in self.graph.parents(node_id):
            r = self.results.get(p)
            if r and (not r.ok or r.skipped):
                return False
        node = self.graph.node(node_id)
        if node.type == "conditional" and node.condition:
            return self._evaluate(node.condition)
        return True

    def _evaluate(self, condition: str) -> bool:
        """Conditions are `<node_id>.<key> <op> <value>` over prior outputs.

        Deliberately not eval(): a graph is model-authored, and eval on
        model-authored text is remote code execution.
        """
        try:
            left, op, right = condition.split(maxsplit=2)
            node_id, _, key = left.partition(".")
            value = (self.outputs.get(node_id) or {})
            actual = value.get(key) if isinstance(value, dict) else None
            right = right.strip().strip("'\"")
            if op == "==":
                return str(actual) == right
            if op == "!=":
                return str(actual) != right
            if op in (">", "<", ">=", "<="):
                a, b = float(actual), float(right)
                return {">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}[op]
        except Exception:
            log.warning("unparsable condition %r, treating as false", condition)
        return False

    async def _run_node(self, node: Node) -> NodeResult:
        if node.type == "approval":
            return await self._approval(node)
        if node.type in ("fanout", "join", "clarify"):
            return await self._passthrough(node)
        if node.type in ("agent", "verify", "council", "conditional"):
            return await self._agent_node(node)
        if node.type == "subgraph":
            return await self._subgraph_node(node)
        return NodeResult(node_id=node.id, ok=True, skipped=True)

    async def _subgraph_node(self, node: Node) -> NodeResult:
        """Run a nested GraphSpec as one unit of the parent graph.

        Reuses the same store/run_id (nested events land in the same run's
        trace) but a fresh outputs/results scope, so the child's node ids -
        already guaranteed unique across the whole nested tree by run()'s
        duplicate check - never collide with the parent's.
        """
        if node.subgraph is None:
            return NodeResult(node_id=node.id, ok=True, skipped=True)
        if self.depth >= MAX_SUBGRAPH_DEPTH:
            return NodeResult(
                node_id=node.id, ok=False,
                error=f"max subgraph nesting depth {MAX_SUBGRAPH_DEPTH} exceeded",
            )
        problems = node.subgraph.validate_side_effects()
        if problems:
            return NodeResult(
                node_id=node.id, ok=False,
                error=f"unapproved side effects in subgraph: {problems}",
            )

        child = Executor(
            self.store, node.subgraph, self.run_id,
            seed=self.seed, completer=self.completer,
            approvals=self.approvals, clarifications=self.clarifications,
            depth=self.depth + 1,
        )
        parent_ctx = self._upstream_context(node.id)
        if parent_ctx:
            child.outputs["__parent__"] = parent_ctx

        started = time.perf_counter()
        failed, _compensated = await child._run_graph()
        elapsed = int((time.perf_counter() - started) * 1000)

        # Sink nodes (nothing downstream inside the subgraph) are its outputs -
        # what the parent graph sees this node as having produced.
        sinks = [
            n.id for n in node.subgraph.nodes
            if n.type != "compensate" and not node.subgraph.children(n.id)
        ]
        output = {s: child.outputs[s] for s in sinks if s in child.outputs}
        return NodeResult(node_id=node.id, ok=not failed, output=output, latency_ms=elapsed)

    async def _approval(self, node: Node) -> NodeResult:
        await self.store.append(
            self.run_id, EventType.APPROVAL_REQUESTED, node_id=node.id,
            payload={"node": node.id},
        )
        if node.id not in self.approvals:
            return NodeResult(
                node_id=node.id, ok=False, error="approval not granted", blocked=True
            )
        await self.store.append(
            self.run_id, EventType.APPROVAL_GRANTED, node_id=node.id, payload={"node": node.id}
        )
        return NodeResult(node_id=node.id, ok=True, output={"approved": True})

    async def _passthrough(self, node: Node) -> NodeResult:
        """fanout/join/clarify carry no model call; join merges its parents.

        Nothing but wall-clock separates start from end here, so both events
        go through one append_many round trip instead of two - see its
        docstring for why this doesn't apply to the agent-node path below.
        """
        started = time.perf_counter()
        merged = {p: self.outputs.get(p) for p in self.graph.parents(node.id)}
        if node.type == "clarify" and self.clarifications:
            merged["answers"] = self.clarifications
        elapsed = int((time.perf_counter() - started) * 1000)
        await self.store.append_many(
            self.run_id,
            [
                {"event_type": EventType.NODE_START, "payload": {"type": node.type}},
                {"event_type": EventType.NODE_END, "payload": {"type": node.type},
                 "latency_ms": elapsed},
            ],
            node_id=node.id,
        )
        return NodeResult(node_id=node.id, ok=True, output=merged, latency_ms=elapsed)

    async def _agent_node(self, node: Node) -> NodeResult:
        agent = node.agent
        if agent is None:
            return NodeResult(node_id=node.id, ok=True, skipped=True)

        started = time.perf_counter()
        await self.store.append(
            self.run_id, EventType.NODE_START, node_id=node.id, agent_id=agent.id,
            payload={"model": agent.model, "budget_tokens": agent.budget_tokens},
        )

        upstream = self._upstream_context(node.id)
        if self.graph.version >= 2:
            upstream = await self._merge_memory(node, agent, upstream)
        # Injection screening happens before the payload enters the context,
        # not after the model has already read it - memory contents flow
        # through here too, since they were merged into `upstream` above.
        clean, blocked = await screen_payload(
            self.store, self.run_id, upstream, node_id=node.id, agent_id=agent.id
        )
        if blocked:
            elapsed = int((time.perf_counter() - started) * 1000)
            await self.store.append(
                self.run_id, EventType.NODE_END, node_id=node.id, agent_id=agent.id,
                payload={"blocked": True}, latency_ms=elapsed,
            )
            return NodeResult(
                node_id=node.id, ok=False, error="handoff quarantined",
                blocked=True, latency_ms=elapsed,
            )

        spent = {"tokens": 0}

        async def produce(feedback: str | None):
            messages = [
                {"role": "system", "content": agent.system_contract},
                {"role": "user", "content": _render(clean, agent)},
            ]
            if feedback:
                messages.append({"role": "user", "content": feedback})

            remaining = agent.budget_tokens - spent["tokens"]
            if remaining <= 0:
                raise BudgetExceeded(f"{agent.id} exhausted {agent.budget_tokens} tokens")

            c = await self.completer(
                agent.model, messages,
                fallback_model=agent.fallback_model,
                seed=self.seed, temperature=0.0, max_tokens=min(remaining, 1024),
                store=self.store, run_id=self.run_id, node_id=node.id, agent_id=agent.id,
            )
            # The engine records every completion, whatever produced it. Replay
            # reads exactly these rows, so this must not depend on which
            # completer was injected.
            await self.store.append(
                self.run_id, EventType.TOOL_RESULT, node_id=node.id, agent_id=agent.id,
                payload={
                    "model": c.model,
                    "used_fallback": c.used_fallback,
                    "replayed": c.replayed,
                    "text": c.text,
                },
                tokens_in=c.tokens_in, tokens_out=c.tokens_out,
                cost_usd=c.cost_usd, latency_ms=c.latency_ms,
            )
            spent["tokens"] += c.tokens_in + c.tokens_out
            if spent["tokens"] > agent.budget_tokens:
                await self.store.append(
                    self.run_id, EventType.BUDGET_EXCEEDED, node_id=node.id, agent_id=agent.id,
                    payload={"spent": spent["tokens"], "budget": agent.budget_tokens},
                )
                raise BudgetExceeded(f"{agent.id} exceeded {agent.budget_tokens} tokens")
            return c.text

        schema = self._outgoing_schema(node.id) or agent.output_schema or {"type": "object"}

        try:
            async with asyncio.timeout(agent.timeout_s):
                outcome = await self._with_retries(node, produce, schema, agent)
        except TimeoutError:
            elapsed = int((time.perf_counter() - started) * 1000)
            await self.store.append(
                self.run_id, EventType.BUDGET_EXCEEDED, node_id=node.id, agent_id=agent.id,
                payload={"reason": "timeout_s", "timeout_s": agent.timeout_s},
            )
            return NodeResult(node_id=node.id, ok=False, error="timeout", latency_ms=elapsed)
        except BudgetExceeded as e:
            elapsed = int((time.perf_counter() - started) * 1000)
            return NodeResult(node_id=node.id, ok=False, error=str(e), latency_ms=elapsed)

        elapsed = int((time.perf_counter() - started) * 1000)
        await self.store.append(
            self.run_id, EventType.NODE_END, node_id=node.id, agent_id=agent.id,
            payload={"ok": outcome.ok, "repair_attempts": outcome.attempts},
            latency_ms=elapsed,
        )
        if not outcome.ok:
            return NodeResult(
                node_id=node.id, ok=False, error="; ".join(outcome.errors), latency_ms=elapsed
            )

        from backend.handoff import artifact_store

        await artifact_store.put(
            self.store, self.run_id, seq=outcome.final_seq or 0,
            written_by=agent.id, content=outcome.payload, node_id=node.id,
        )
        if self.graph.version >= 2:
            await self._write_memory(node, agent, outcome.payload)
        return NodeResult(node_id=node.id, ok=True, output=outcome.payload, latency_ms=elapsed)

    async def _with_retries(self, node: Node, produce, schema: dict, agent):
        """Node-level retry sits outside the repair loop: repair fixes shape,
        retry covers a transient provider failure."""
        last = None
        for attempt in range(node.max_retries + 1):
            try:
                return await repair_loop(
                    self.store, self.run_id, produce=produce, schema=schema,
                    agent_id=agent.id, node_id=node.id,
                )
            except BudgetExceeded:
                raise
            except Exception as e:
                last = e
                if attempt == node.max_retries:
                    raise
                await self.store.append(
                    self.run_id, EventType.RETRY, node_id=node.id, agent_id=agent.id,
                    payload={"attempt": attempt + 1, "of": node.max_retries, "error": str(e)},
                )
                await asyncio.sleep(0.5 * (attempt + 1))
        raise last  # pragma: no cover

    # --------------------------------------------------------- compensation

    async def _compensate(self, failed: list[str]) -> list[str]:
        done = []
        for node in self.graph.nodes:
            if node.type != "compensate" or node.compensates not in failed:
                continue
            await self.store.append(
                self.run_id, EventType.COMPENSATE, node_id=node.id,
                payload={"compensates": node.compensates},
            )
            res = await self._run_node(node) if node.agent else NodeResult(node_id=node.id, ok=True)
            self.results[node.id] = res
            done.append(node.id)
        return done

    # ------------------------------------------------------------- helpers

    def _upstream_context(self, node_id: str) -> dict:
        ctx = {p: self.outputs.get(p) for p in self.graph.parents(node_id) if p in self.outputs}
        # A subgraph's child executor seeds outputs["__parent__"] with the
        # parent graph's context for the subgraph node (see _subgraph_node).
        # No-op for the top-level executor, which never sets this key.
        if node_id != "__parent__" and "__parent__" in self.outputs:
            ctx.setdefault("__parent__", self.outputs["__parent__"])
        return ctx

    def _outgoing_schema(self, node_id: str) -> dict | None:
        for e in self.graph.edges:
            if e.from_node == node_id and e.handoff_schema:
                return e.handoff_schema
        return None

    # --------------------------------------------------------------- memory

    async def _merge_memory(self, node: Node, agent, upstream: dict) -> dict:
        """Fold live shared/scratch memory into this node's upstream context.

        Direct-parent output wins on key collision (it is the more specific,
        freshly-produced value); memory fills in what a node without a direct
        edge to the source still needs to see.
        """
        live, evicted = await memory_store.read_for(
            self.run_id, agent.id, include_shared=agent.memory_scope != "isolated"
        )
        if evicted:
            await self.store.append(
                self.run_id, EventType.MEMORY_EVICTED, node_id=node.id, agent_id=agent.id,
                payload={"keys": evicted},
            )
        if not live:
            return upstream
        await self.store.append(
            self.run_id, EventType.MEMORY_READ, node_id=node.id, agent_id=agent.id,
            payload={"keys": list(live)},
        )
        return {**live, **upstream}

    async def _write_memory(self, node: Node, agent, payload) -> None:
        ttl = agent.memory_ttl_s if agent.memory_ttl_s is not None else DEFAULT_MEMORY_TTL_S
        await memory_store.write(
            self.run_id, agent.id, node.id, payload,
            ttl_s=ttl, shared=agent.memory_scope == "shared_rw",
        )
        await self.store.append(
            self.run_id, EventType.MEMORY_WRITE, node_id=node.id, agent_id=agent.id,
            payload={"ttl_s": ttl, "shared": agent.memory_scope == "shared_rw"},
        )


def _render(context: dict, agent) -> str:
    import json

    if not context:
        return f"Task: {agent.role}\n\nProduce output matching your declared schema."
    return (
        f"Task: {agent.role}\n\nUpstream results:\n"
        f"{json.dumps(context, indent=2)[:4000]}\n\n"
        "Produce output matching your declared schema."
    )
