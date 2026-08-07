"""Marginal-value report: the same task at graph depth 1, 2, 3, 4.

Produces the Pareto curve of quality-per-rupee against graph size, and is
allowed to conclude that a smaller graph won. That conclusion is the point - a
report that can only ever say "more agents is better" is marketing.
"""

import logging
import uuid

from pydantic import BaseModel, Field

from backend.council.compiler import build_graph
from backend.engine import Executor
from backend.events import EventStore
from backend.metrics.metrics import compute

log = logging.getLogger("aco.metrics.marginal")

BRANCH_POOL = [
    {"id": "security", "role": "Find security weaknesses.", "tools": ["read_file"]},
    {"id": "quality", "role": "Find code quality and maintainability issues.",
     "tools": ["read_file"]},
    {"id": "docs", "role": "Find documentation gaps.", "tools": ["read_file"]},
    {"id": "tests", "role": "Find gaps in test coverage.", "tools": ["read_file"]},
]


class DepthPoint(BaseModel):
    depth: int
    agents: list[str] = Field(default_factory=list)
    run_id: str = ""
    unique_findings: int = 0
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    wall_clock_ms: int = 0
    redundancy_index: float = 0.0
    duplicate_work: int = 0
    parallel_efficiency: float = 0.0
    lowest_mig: float = 0.0
    lowest_mig_agent: str = ""
    # Findings per 1k tokens. Used instead of per-rupee when the run is local
    # and genuinely free, so the curve is not a flat line of zeros.
    quality_per_1k_tokens: float = 0.0
    quality_per_rupee: float | None = None


class MarginalValueReport(BaseModel):
    goal: str
    points: list[DepthPoint] = Field(default_factory=list)
    best_depth: int = 0
    recommendation: str = ""
    cost_basis: str = "tokens"


async def run_at_depth(
    store: EventStore, goal: str, depth: int, models: list[str], *, seed: int = 42, completer=None
) -> DepthPoint:
    branches = BRANCH_POOL[:depth]
    # allow_degenerate: a depth-1 graph is the point of the measurement, not a
    # malformed council response to be repaired.
    graph = build_graph(branches, models, verifier_model=models[-1], allow_degenerate=True)
    run_id = str(uuid.uuid4())
    await store.create_run(run_id, goal, graph.config_hash, seed, graph.model_dump(), "pending")

    approvals = {n.id for n in graph.nodes if n.type == "approval"}
    result = await Executor(
        store, graph, run_id, seed=seed, completer=completer, approvals=approvals
    ).run()
    m = await compute(store, run_id)

    point = DepthPoint(
        depth=depth,
        agents=[a.node_id for a in m.agents],
        run_id=run_id,
        unique_findings=m.unique_findings,
        total_cost_usd=m.total_cost_usd,
        total_tokens=m.total_tokens,
        wall_clock_ms=result.wall_clock_ms,
        redundancy_index=m.redundancy_index,
        duplicate_work=m.duplicate_work,
        parallel_efficiency=m.parallel_efficiency,
    )
    if m.agents:
        weakest = min(m.agents, key=lambda a: a.marginal_information_gain)
        point.lowest_mig = weakest.marginal_information_gain
        point.lowest_mig_agent = weakest.node_id
    if m.total_tokens:
        point.quality_per_1k_tokens = round(m.unique_findings / (m.total_tokens / 1000), 4)
    if m.total_cost_usd > 0:
        point.quality_per_rupee = round(m.unique_findings / (m.total_cost_usd * 83), 4)
    return point


async def marginal_value_report(
    store: EventStore,
    goal: str,
    models: list[str],
    *,
    depths: tuple[int, ...] = (1, 2, 3, 4),
    seed: int = 42,
    completer=None,
) -> MarginalValueReport:
    report = MarginalValueReport(goal=goal)
    for d in depths:
        try:
            report.points.append(
                await run_at_depth(store, goal, d, models, seed=seed, completer=completer)
            )
        except Exception as e:
            log.warning("depth %d failed: %s", d, e)

    if not report.points:
        report.recommendation = "No depth completed; nothing to compare."
        return report

    paid = any(p.total_cost_usd > 0 for p in report.points)
    report.cost_basis = "rupees" if paid else "tokens"

    def efficiency(p: DepthPoint) -> float:
        return p.quality_per_rupee if (paid and p.quality_per_rupee) else p.quality_per_1k_tokens

    peak = max(efficiency(p) for p in report.points)
    # Depths often tie on efficiency because each extra agent contributes
    # proportionally. Among ties, more findings for the same efficiency is
    # strictly better - reporting the smallest tied depth would tell a team to
    # drop agents that were costing them nothing.
    tied = [p for p in report.points if efficiency(p) >= peak - 1e-9]
    best = max(tied, key=lambda p: (p.unique_findings, -p.depth))
    report.best_depth = best.depth
    largest = max(report.points, key=lambda p: p.depth)

    if best.depth < largest.depth:
        gain = largest.unique_findings - best.unique_findings
        extra = largest.total_tokens - best.total_tokens
        gain_text = (
            f"added nothing at all for {extra} extra tokens"
            if gain <= 0
            else f"added {gain} unique finding(s) for {extra} extra tokens"
        )
        report.recommendation = (
            f"Depth {best.depth} is the efficient point. Going to depth {largest.depth} "
            f"{gain_text}. For this task class our own orchestrator recommends "
            f"{best.depth} agent(s), not {largest.depth}."
        )
        if len(tied) > 1:
            report.recommendation += (
                f" Depths {', '.join(str(p.depth) for p in sorted(tied, key=lambda x: x.depth))} "
                f"tie on efficiency; {best.depth} is chosen because it finds the most."
            )
        if largest.duplicate_work:
            report.recommendation += (
                f" Depth {largest.depth} also produced {largest.duplicate_work} "
                f"duplicate branch pair(s)."
            )
        if largest.lowest_mig < 0.1:
            report.recommendation += (
                f" Agent '{largest.lowest_mig_agent}' scored MIG {largest.lowest_mig:.2f} "
                f"at depth {largest.depth} - it added almost nothing."
            )
    else:
        report.recommendation = (
            f"Depth {best.depth} remained the most efficient and every added agent still "
            f"contributed; the larger graph is justified for this task class."
        )
    return report
