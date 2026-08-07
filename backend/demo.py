"""End-to-end demo driver, and the exporter for examples/.

Run: python -m backend.demo            # full sequence against real models
     python -m backend.demo --export   # also write examples/*.json

Produces the three saved runs the deliverables ask for: one clean, one with a
failed-and-recovered branch, one with a blocked adversarial agent.
"""

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from backend.council.compiler import build_graph
from backend.engine import Executor
from backend.events import EventStore, EventType
from backend.permissions import CapabilityGuard, screen_payload
from backend.permissions.red_agent import POISONED_HANDOFF, RED_AGENT
from backend.providers.base import Completion
from backend.replay import replay_run

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
GOAL = "Audit this repository and produce a prioritized remediation report."

BRANCHES = [
    {"id": "security", "role": "Find security weaknesses.", "tools": ["read_file"]},
    {"id": "quality", "role": "Find code quality issues.", "tools": ["read_file"]},
    {"id": "docs", "role": "Find documentation gaps.", "tools": ["read_file"]},
]

# Deterministic stand-in so the exported examples are stable artefacts rather
# than a different story on every run. The live demo uses real models; this is
# only for reproducible example files.
FIXTURES = {
    "security": {"findings": [{"title": "Service key readable from the client bundle",
                               "severity": "critical",
                               "recommendation": "Move all Supabase calls behind the API"}]},
    "quality": {"findings": [{"title": "Duplicated retry logic across adapters",
                              "severity": "medium",
                              "recommendation": "Lift retry into the shared base adapter"}]},
    "docs": {"findings": [{"title": "No setup instructions for a fresh clone",
                           "severity": "low",
                           "recommendation": "Document the venv and migration steps"}]},
    "verifier": {"summary": "One critical credential-exposure issue dominates.",
                 "prioritized": [{"rank": 1, "title": "Service key exposure",
                                  "severity": "critical"}]},
}


def fixture_completer(fail_for: set[str] | None = None):
    fail_for = fail_for or set()

    async def _complete(model, messages, **kw):
        await asyncio.sleep(0.05)
        aid = kw.get("agent_id") or ""
        if aid in fail_for:
            return Completion(text="the repository looks fine", model=model)
        body = FIXTURES.get(aid, {"findings": [
            {"title": f"{aid} finding", "severity": "low", "recommendation": "review"}]})
        return Completion(text=json.dumps(body), tokens_in=140, tokens_out=70,
                          cost_usd=0.00012, latency_ms=50, model=model)

    return _complete


async def _run(store, name: str, *, fail_for=None, live=False):
    graph = build_graph(BRANCHES, ["ollama:llama3.2:3b", "ollama:qwen2.5:7b"],
                        verifier_model="ollama:qwen2.5:7b")
    rid = str(uuid.uuid4())
    await store.create_run(rid, GOAL, graph.config_hash, 42, graph.model_dump(), "pending")
    approvals = {n.id for n in graph.nodes if n.type == "approval"}
    result = await Executor(
        store, graph, rid, seed=42, approvals=approvals,
        completer=None if live else fixture_completer(fail_for),
    ).run()
    print(f"  {name:24} ok={result.ok!s:5} PE={result.parallel_efficiency:<6} "
          f"failed={result.failed_nodes} compensated={result.compensated}")
    return rid, graph, result


async def red_agent_run(store) -> str:
    rid = str(uuid.uuid4())
    await store.create_run(rid, "Red Agent demonstration", "red-demo", 42, {}, "running")
    await store.append(rid, EventType.RUN_START, payload={"demo": "red_agent"})
    guard = CapabilityGuard(store, rid)

    async def file_delete(path: str) -> str:
        return f"deleted {path}"

    guard.register("file_delete", file_delete)
    blocked = await guard.call(
        agent_id=RED_AGENT.id, declared_tools=RED_AGENT.tools,
        tool="file_delete", arguments={"path": "backend/config.py"}, node_id="red",
    )
    _, quarantined = await screen_payload(
        store, rid, {"red": POISONED_HANDOFF}, node_id="writer", agent_id="report_writer"
    )
    await store.append(rid, EventType.RUN_END,
                       payload={"tool_blocked": blocked.blocked,
                                "injection_blocked": quarantined})
    await store.set_run_status(rid, "done", ended=True)
    print(f"  {'adversarial (red agent)':24} tool_blocked={blocked.blocked} "
          f"injection_blocked={quarantined}")
    return rid


def _write(path: Path, payload: dict, count: int) -> None:
    """Sync on purpose: blocking file IO does not belong in the async path."""
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"    wrote {path.name}  ({count} events)")


async def export(store, run_id: str, path: Path) -> None:
    row = await store.get_run(run_id)
    events = await store.read(run_id)
    cost = await store.aggregate_cost(run_id)
    payload = {
        "run": row.model_dump() if row else None,
        "cost": cost.model_dump(),
        "events": [e.model_dump() for e in events],
    }
    await asyncio.to_thread(_write, path, payload, len(events))


async def main(do_export: bool, live: bool) -> int:
    EXAMPLES.mkdir(exist_ok=True)
    async with EventStore() as store:
        print("=" * 70)
        print("DEMO SEQUENCE")
        print("=" * 70)

        clean_id, _, clean = await _run(store, "clean run", live=live)
        recovered_id, _, rec = await _run(store, "failed + recovered", fail_for={"docs"})
        red_id = await red_agent_run(store)

        print()
        print("=" * 70)
        print("REPLAY (step 9)")
        print("=" * 70)
        _, diff = await replay_run(store, clean_id)
        print(f"  identical      : {diff.identical}")
        print(f"  output_diffs   : {diff.output_diffs}")
        print(f"  nodes compared : {diff.nodes_compared}")
        print(f"  cost           : ${diff.original_cost_usd} -> ${diff.replay_cost_usd}")
        print(f"  wall clock     : {diff.original_wall_ms}ms -> {diff.replay_wall_ms}ms")

        ok = clean.ok and diff.identical and not rec.ok and rec.compensated

        if do_export:
            print()
            print("=" * 70)
            print("EXPORT")
            print("=" * 70)
            await export(store, clean_id, EXAMPLES / "run_clean.json")
            await export(store, recovered_id, EXAMPLES / "run_recovered.json")
            await export(store, red_id, EXAMPLES / "run_adversarial.json")

        print()
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", action="store_true", help="write examples/*.json")
    ap.add_argument("--live", action="store_true", help="use real models for the clean run")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.export, a.live)))
