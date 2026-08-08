"""
backend/workflow_dna/orchestrator_hooks.py

The ONLY file in this package that imports the orchestrator's own types
(GraphSpec, RunResult, EventStore). Everything else under
backend/workflow_dna/ is orchestrator-agnostic by design. Keeping the
translation logic in exactly one file means removing Workflow DNA later
is: delete backend/workflow_dna/, then remove the handful of call sites
in backend/api/routes.py that import from here. Nothing else changes.

Two entrypoints, matching the two integration points in
backend/api/routes.py's start_run():

  predict_and_log()   called BEFORE execution
  record_and_log()    called AFTER execution

Both are advisory and fail safe: if Workflow DNA raises for any reason,
the exception is logged and swallowed here — a prediction or scoring
failure must never block, slow, or fail a real run. This module is a
passenger, not a gatekeeper.
"""

import logging
from typing import Optional

from backend.engine import GraphSpec, RunResult
from backend.events import EventStore, EventType

from backend.workflow_dna.prediction.dna import WorkflowDNA
from backend.workflow_dna.service import WorkflowDNAService, DNADecision

log = logging.getLogger("aco.workflow_dna")

_service: Optional[WorkflowDNAService] = None


def _get_service() -> WorkflowDNAService:
    """Lazy singleton — the trained model + history DB connection only need to load once per process."""
    global _service
    if _service is None:
        _service = WorkflowDNAService()
    return _service


def _dna_from_graph(graph: GraphSpec, workflow_id: str) -> WorkflowDNA:
    """
    Maps a real GraphSpec to Workflow DNA's structural genome.

    agent_count         nodes that actually carry an AgentSpec — structural
                         nodes like fanout/join/conditional don't invoke a
                         model themselves, so they're not counted as agents
    workflow_length       total node count in the graph
    workflow_depth          len(graph.levels()) — this is REAL topological
                            depth already computed by GraphSpec, not a proxy
    parallel_execution        1 if any level has more than one node — also
                              real ground truth, not the BPI2017-era heuristic
    """
    levels = graph.levels()
    agent_count = sum(1 for n in graph.nodes if n.agent is not None)
    parallel = 1 if any(len(level) > 1 for level in levels) else 0
    return WorkflowDNA(
        workflow_id=workflow_id,
        agent_count=max(agent_count, 1),
        parallel_execution=parallel,
        workflow_depth=max(len(levels), 1),
        workflow_length=max(len(graph.nodes), 1),
        mutation_type="none",
        parent_workflow=None,
        generation=1,
    )


async def predict_and_log(store: EventStore, graph: GraphSpec, workflow_id: str) -> Optional[DNADecision]:
    """
    Predicts fitness for the workflow about to run, and — if predicted
    fitness is below threshold — generates ONE mutation suggestion for
    comparison. ADVISORY ONLY: never rewrites `graph` itself; a human
    or the existing approval-gate flow decides whether to act on the
    suggestion. Logs the result as a DNA_PREDICTED event.
    """
    service = _get_service()
    dna = _dna_from_graph(graph, workflow_id)

    try:
        decision = service.evaluate_workflow(dna)
    except Exception:
        log.exception("Workflow DNA prediction failed for %s; continuing without it", workflow_id)
        return None

    payload = {
        "predicted_fitness": decision.original.predicted_fitness,
        "estimate_source": decision.original.estimated_outcome.source,
        "similar_workflow_count": decision.original.estimated_outcome.similar_workflow_count,
        "threshold_met_without_mutation": decision.threshold_met_without_mutation,
    }
    if decision.mutated is not None:
        payload["mutation_suggestion"] = {
            "mutation_type": decision.mutated_dna.mutation_type,
            "predicted_fitness": decision.mutated.predicted_fitness,
            "note": "Advisory only — not automatically applied to the graph.",
        }

    await store.append(workflow_id, EventType.DNA_PREDICTED, payload=payload)
    return decision


async def record_and_log(
    store: EventStore,
    graph: GraphSpec,
    workflow_id: str,
    run_result: RunResult,
    predicted_fitness: Optional[float],
) -> None:
    """
    Called after a run completes — successfully or with failed nodes,
    but NOT after a hard crash (that path has no well-formed RunResult
    to score; a crashed run simply isn't added to Workflow DNA history,
    a scope boundary rather than an oversight). Computes the real
    Fitness Score and writes it to Workflow DNA's history database,
    then logs a DNA_RECORDED event so the outcome is visible in this
    system's own event log too, not just in a separate side database.
    """
    service = _get_service()
    dna = _dna_from_graph(graph, workflow_id)

    cost_totals = await store.aggregate_cost(workflow_id)
    latency_totals = await store.aggregate_latency(workflow_id)
    events = await store.read(workflow_id)
    retry_count = sum(1 for e in events if e.event_type == EventType.RETRY)

    abort_count = len(run_result.failed_nodes)
    start_count = max(len(run_result.results), 1)

    try:
        record = service.record_actual_execution(
            dna=dna,
            abort_count=abort_count,
            start_count=start_count,
            resume_count=retry_count,
            execution_time_seconds=latency_totals.wall_clock_ms / 1000.0,
            cost=cost_totals.cost_usd,
            predicted_fitness=predicted_fitness,
        )
    except Exception:
        log.exception("Workflow DNA recording failed for %s; run result is unaffected", workflow_id)
        return

    await store.append(
        workflow_id, EventType.DNA_RECORDED,
        payload={
            "fitness": record.fitness,
            "quality": record.quality,
            "predicted_fitness": predicted_fitness,
        },
    )
