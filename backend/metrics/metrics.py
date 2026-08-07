"""Honest metrics.

Every competitor claims more agents is better. This module measures whether that
was true for a given run and says so when it was not.

Embeddings are all-MiniLM-L6-v2 running locally, so computing them is free and
adds no provider cost to the thing being measured.
"""

import itertools
import json
import logging
from functools import lru_cache

from pydantic import BaseModel, Field

from backend.events import EventStore, EventType

log = logging.getLogger("aco.metrics")

DUPLICATE_THRESHOLD = 0.85


class EmbeddingsUnavailable(RuntimeError):
    """sentence-transformers is not installed. Every other capability still works."""


def embeddings_available() -> bool:
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        return False
    return True


@lru_cache(maxsize=1)
def _model():
    """Loaded once, lazily: importing sentence-transformers costs seconds and
    most requests never touch metrics."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as e:
        raise EmbeddingsUnavailable(
            "sentence-transformers is not installed; similarity metrics "
            "(MIG, redundancy, duplicate work) are unavailable. "
            "Install with: pip install torch --index-url "
            "https://download.pytorch.org/whl/cpu && pip install sentence-transformers"
        ) from e

    return SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")


def embed(texts: list[str]):
    import numpy as np

    if not texts:
        return np.zeros((0, 384))
    return _model().encode(texts, normalize_embeddings=True, show_progress_bar=False)


def cosine_matrix(texts: list[str]):
    import numpy as np

    vecs = embed(texts)
    if len(vecs) == 0:
        return np.zeros((0, 0))
    return np.clip(vecs @ vecs.T, -1.0, 1.0)


class AgentMetric(BaseModel):
    agent_id: str
    node_id: str
    marginal_information_gain: float
    tokens: int = 0
    cost_usd: float = 0.0
    findings: int = 0


class RunMetrics(BaseModel):
    run_id: str
    agents: list[AgentMetric] = Field(default_factory=list)
    redundancy_index: float = 0.0
    duplicate_pairs: list[dict] = Field(default_factory=list)
    duplicate_work: int = 0
    parallel_efficiency: float = 0.0
    recovery_rate: float | None = None
    handoff_validity: float | None = None
    branch_utilization: float = 0.0
    approval_frequency: int = 0
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    unique_findings: int = 0
    cost_per_unique_finding: float | None = None
    tokens_per_unique_finding: float | None = None
    least_valuable_agent: str | None = None
    verdict: str = ""
    # False when sentence-transformers is absent: the counted metrics are still
    # real, the similarity-derived ones are simply not reported.
    embeddings_available: bool = True


def _texts_by_node(events) -> dict[str, tuple[str, str]]:
    """node_id -> (agent_id, validated output as text)."""
    out: dict[str, tuple[str, str]] = {}
    for e in events:
        if e.event_type == EventType.HANDOFF_VALIDATED and e.node_id and e.payload:
            out[e.node_id] = (e.agent_id or e.node_id,
                              json.dumps(e.payload.get("payload"), sort_keys=True))
    return out


def _findings(payload_text: str) -> list[str]:
    try:
        value = json.loads(payload_text)
    except Exception:
        return []
    if not isinstance(value, dict):
        return []
    items = value.get("findings") or value.get("prioritized") or []
    out = []
    for i in items:
        if isinstance(i, dict):
            out.append(str(i.get("title") or i.get("summary") or json.dumps(i, sort_keys=True)))
        else:
            out.append(str(i))
    return out


async def compute(store: EventStore, run_id: str) -> RunMetrics:
    events = await store.read(run_id)
    by_node = _texts_by_node(events)
    m = RunMetrics(run_id=run_id)

    cost = await store.aggregate_cost(run_id)
    lat = await store.aggregate_latency(run_id)
    m.total_cost_usd = cost.cost_usd
    m.total_tokens = cost.tokens_in + cost.tokens_out

    run_end = next((e for e in reversed(events) if e.event_type == EventType.RUN_END), None)
    m.parallel_efficiency = (
        (run_end.payload or {}).get("parallel_efficiency", 0.0)
        if run_end else lat.parallel_efficiency
    )

    # ---- handoff validity: counted from rows, not asserted
    validated = sum(1 for e in events if e.event_type == EventType.HANDOFF_VALIDATED)
    rejected = sum(1 for e in events if e.event_type == EventType.HANDOFF_REJECTED)
    if validated + rejected:
        m.handoff_validity = round(validated / (validated + rejected), 3)

    # ---- recovery rate
    failed = sum(1 for e in events if e.event_type == EventType.BRANCH_FAILED)
    compensated = sum(1 for e in events if e.event_type == EventType.COMPENSATE)
    if failed:
        m.recovery_rate = round(min(compensated / failed, 1.0), 3)

    m.approval_frequency = sum(
        1 for e in events if e.event_type == EventType.APPROVAL_REQUESTED
    )

    if not by_node:
        m.verdict = "No validated agent output in this run; nothing to compare."
        return m

    nodes = sorted(by_node)
    texts = [by_node[n][1] for n in nodes]

    # Counted metrics above never needed embeddings. If the optional dependency
    # is absent, return what is genuinely measured rather than failing the whole
    # request or - worse - substituting a cheaper similarity and presenting it
    # as the same number.
    if not embeddings_available():
        m.embeddings_available = False
        m.verdict = (
            "Similarity metrics unavailable: sentence-transformers is not installed. "
            "Handoff validity, recovery rate, parallel efficiency and approval "
            "frequency above are measured; MIG, redundancy and duplicate work are not."
        )
        return m

    sim = cosine_matrix(texts)

    # ---- Marginal Information Gain: 1 - max similarity to any other agent
    per_node_cost: dict[str, float] = {}
    per_node_tokens: dict[str, int] = {}
    for e in events:
        if e.node_id:
            per_node_cost[e.node_id] = per_node_cost.get(e.node_id, 0.0) + float(e.cost_usd or 0)
            per_node_tokens[e.node_id] = (
                per_node_tokens.get(e.node_id, 0) + (e.tokens_in or 0) + (e.tokens_out or 0)
            )

    for i, node in enumerate(nodes):
        others = [sim[i][j] for j in range(len(nodes)) if j != i]
        mig = round(1.0 - max(others), 4) if others else 1.0
        m.agents.append(AgentMetric(
            agent_id=by_node[node][0], node_id=node,
            marginal_information_gain=mig,
            tokens=per_node_tokens.get(node, 0),
            cost_usd=round(per_node_cost.get(node, 0.0), 8),
            findings=len(_findings(by_node[node][1])),
        ))

    # ---- Redundancy Index and duplicate work
    pairs = list(itertools.combinations(range(len(nodes)), 2))
    if pairs:
        m.redundancy_index = round(sum(float(sim[i][j]) for i, j in pairs) / len(pairs), 4)
        for i, j in pairs:
            if float(sim[i][j]) > DUPLICATE_THRESHOLD:
                m.duplicate_pairs.append({
                    "a": nodes[i], "b": nodes[j], "cosine": round(float(sim[i][j]), 4)
                })
        m.duplicate_work = len(m.duplicate_pairs)

    # ---- Cost per Unique Finding: dedupe findings by embedding, not by string
    all_findings = [f for n in nodes for f in _findings(by_node[n][1])]
    m.unique_findings = _dedupe_count(all_findings)
    if m.unique_findings:
        m.cost_per_unique_finding = round(m.total_cost_usd / m.unique_findings, 8)
        m.tokens_per_unique_finding = round(m.total_tokens / m.unique_findings, 2)

    contributing = sum(1 for a in m.agents if a.findings > 0)
    m.branch_utilization = round(contributing / len(m.agents), 3) if m.agents else 0.0

    if m.agents:
        weakest = min(m.agents, key=lambda a: a.marginal_information_gain)
        m.least_valuable_agent = weakest.node_id
        m.verdict = _verdict(m, weakest)
    return m


def _dedupe_count(findings: list[str]) -> int:
    """Two agents phrasing the same issue differently is one finding, not two."""
    if not findings:
        return 0
    if len(findings) == 1:
        return 1
    sim = cosine_matrix(findings)
    keep: list[int] = []
    for i in range(len(findings)):
        if all(float(sim[i][j]) <= DUPLICATE_THRESHOLD for j in keep):
            keep.append(i)
    return len(keep)


def _verdict(m: RunMetrics, weakest: AgentMetric) -> str:
    """The sentence the system says out loud when it was overkill."""
    if weakest.marginal_information_gain < 0.1:
        return (
            f"Agent '{weakest.node_id}' scored MIG {weakest.marginal_information_gain} - it "
            f"produced almost nothing the other agents did not already have. For this task "
            f"class a smaller graph would have been better."
        )
    if m.duplicate_work:
        pairs = ", ".join(f"{p['a']}/{p['b']}" for p in m.duplicate_pairs)
        return (
            f"{m.duplicate_work} branch pair(s) exceeded cosine {DUPLICATE_THRESHOLD} ({pairs}). "
            f"Those agents largely duplicated each other."
        )
    if m.redundancy_index > 0.7:
        return (
            f"Redundancy index {m.redundancy_index} is high; the branches are covering "
            f"similar ground and could be merged."
        )
    return (
        f"All {len(m.agents)} agents contributed distinct information "
        f"(lowest MIG {weakest.marginal_information_gain}). The graph size is justified."
    )
