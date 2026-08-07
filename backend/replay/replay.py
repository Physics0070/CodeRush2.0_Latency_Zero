"""Deterministic replay.

Most systems "replay" by re-running and getting a different answer. This one
serves every model and tool call out of the event log, so the diff is provably
zero rather than approximately zero.

The mechanism is a completer that, instead of calling a provider, walks the
original run's TOOL_RESULT rows in order and hands back what was recorded. Same
graph, same config_hash, same seed, same responses - so any difference in output
is a real engine bug, which is exactly what the diff is for.
"""

import json
import logging
import uuid
from collections import defaultdict

from pydantic import BaseModel, Field

from backend.engine import Executor, GraphSpec, RunResult
from backend.events import EventStore, EventType
from backend.providers.base import Completion

log = logging.getLogger("aco.replay")


class Diff(BaseModel):
    identical: bool
    output_diffs: list[dict] = Field(default_factory=list)
    original_cost_usd: float = 0.0
    replay_cost_usd: float = 0.0
    original_wall_ms: int = 0
    replay_wall_ms: int = 0
    original_tokens: int = 0
    replay_tokens: int = 0
    nodes_compared: int = 0

    @property
    def cost_saved_usd(self) -> float:
        return round(self.original_cost_usd - self.replay_cost_usd, 8)


class LoggedCompleter:
    """Serves recorded completions in the order the original run made them.

    Keyed by (node_id, agent_id) so concurrent branches cannot steal each
    other's responses - a single global queue would be order-dependent and the
    whole point here is that order is not.
    """

    def __init__(self, recorded: dict[tuple[str | None, str | None], list[dict]]):
        self._q = {k: list(v) for k, v in recorded.items()}
        self.served = 0
        self.missing = 0

    async def __call__(self, model, messages, **kw):
        key = (kw.get("node_id"), kw.get("agent_id"))
        queue = self._q.get(key)
        if not queue:
            self.missing += 1
            raise RuntimeError(f"no recorded completion for {key}; run is not replayable")
        row = queue.pop(0)
        self.served += 1
        # The executor writes the TOOL_RESULT row; `replayed` marks it so a
        # replay of a replay reads the original recording, not this one.
        return Completion(
            text=row.get("text", ""),
            tokens_in=row.get("tokens_in", 0),
            tokens_out=row.get("tokens_out", 0),
            # Replay costs nothing: nothing is called.
            cost_usd=0.0,
            latency_ms=0,
            model=row.get("model", model),
            used_fallback=row.get("used_fallback", False),
            replayed=True,
        )


async def recorded_completions(
    store: EventStore, run_id: str
) -> dict[tuple[str | None, str | None], list[dict]]:
    out: dict[tuple, list[dict]] = defaultdict(list)
    for e in await store.read(run_id):
        if e.event_type != EventType.TOOL_RESULT or not e.payload:
            continue
        if e.payload.get("replayed"):
            continue
        out[(e.node_id, e.agent_id)].append({
            "text": e.payload.get("text", ""),
            "model": e.payload.get("model", ""),
            "used_fallback": e.payload.get("used_fallback", False),
            "tokens_in": e.tokens_in,
            "tokens_out": e.tokens_out,
        })
    return dict(out)


async def final_outputs(store: EventStore, run_id: str) -> dict[str, object]:
    """Validated node outputs, which is what a diff should compare."""
    out: dict[str, object] = {}
    for e in await store.read(run_id):
        if e.event_type == EventType.HANDOFF_VALIDATED and e.node_id and e.payload:
            out[e.node_id] = e.payload.get("payload")
    return out


async def replay_run(store: EventStore, original_run_id: str) -> tuple[RunResult, Diff]:
    original = await store.get_run(original_run_id)
    if original is None:
        raise ValueError(f"unknown run {original_run_id}")

    graph = GraphSpec(**original.graph_spec)
    # If the stored graph hashes differently, the saved spec was mutated and the
    # replay would be meaningless. Fail loudly instead of producing a fake zero.
    if graph.compute_hash() != original.config_hash:
        raise ValueError(
            f"config_hash mismatch: stored {original.config_hash[:12]} but graph hashes "
            f"{graph.compute_hash()[:12]}; the saved spec was modified"
        )

    recorded = await recorded_completions(store, original_run_id)
    completer = LoggedCompleter(recorded)

    replay_id = str(uuid.uuid4())
    await store.create_run(
        replay_id, original.goal, original.config_hash, original.seed,
        original.graph_spec, "pending", replay_of=original_run_id,
    )

    # Approvals are replayed from the log, not re-requested from a human.
    approvals = {
        e.node_id for e in await store.read(original_run_id)
        if e.event_type == EventType.APPROVAL_GRANTED and e.node_id
    }

    result = await Executor(
        store, graph, replay_id, seed=original.seed,
        completer=completer, approvals=approvals,
    ).run()

    diff = await build_diff(store, original_run_id, replay_id)
    log.info(
        "replay %s of %s: identical=%s served=%d",
        replay_id, original_run_id, diff.identical, completer.served,
    )
    return result, diff


async def build_diff(store: EventStore, original_id: str, replay_id: str) -> Diff:
    before, after = await final_outputs(store, original_id), await final_outputs(store, replay_id)
    oc, rc = await store.aggregate_cost(original_id), await store.aggregate_cost(replay_id)
    ol, rl = await store.aggregate_latency(original_id), await store.aggregate_latency(replay_id)

    diffs = []
    for node in sorted(set(before) | set(after)):
        a = json.dumps(before.get(node), sort_keys=True)
        b = json.dumps(after.get(node), sort_keys=True)
        if a != b:
            diffs.append({"node": node, "original": before.get(node), "replay": after.get(node)})

    return Diff(
        identical=not diffs and set(before) == set(after),
        output_diffs=diffs,
        original_cost_usd=oc.cost_usd, replay_cost_usd=rc.cost_usd,
        original_wall_ms=ol.wall_clock_ms, replay_wall_ms=rl.wall_clock_ms,
        original_tokens=oc.tokens_in + oc.tokens_out,
        replay_tokens=rc.tokens_in + rc.tokens_out,
        nodes_compared=len(set(before) | set(after)),
    )
