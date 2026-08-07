"""Task-to-graph compiler.

The council proposes GraphSpecs, peer-ranks them anonymously, and the chairman
merges. The merged plan is then structurally validated and repaired here, in
code, because a model-authored graph is untrusted: it can name a node twice,
point an edge at nothing, or omit the verification node the PS requires.

The compiler guarantees the invariants; the council only supplies the shape.
"""

import json
import logging

from backend.council.council import Verdict, deliberate
from backend.engine.spec import AgentSpec, Edge, GraphSpec, Node
from backend.events import EventStore, EventType

log = logging.getLogger("aco.compiler")

PLAN_SCHEMA_HINT = """Return JSON of this exact shape:
{
  "branches": [
    {"id":"security","role":"what this specialist examines","tools":["read_file"]},
    {"id":"quality","role":"...","tools":["read_file"]}
  ]
}
Rules: 2 to 4 branches. Each id is lowercase, one word, unique. Branches must be
independent of each other so they can run in parallel. tools may only contain
"read_file" and "list_files"."""

FINDINGS_SCHEMA = {
    "type": "object",
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["title", "severity", "recommendation"],
                "properties": {
                    "title": {"type": "string", "minLength": 3},
                    "severity": {"enum": ["low", "medium", "high", "critical"]},
                    "recommendation": {"type": "string", "minLength": 3},
                },
            },
        }
    },
}

REPORT_SCHEMA = {
    "type": "object",
    "required": ["summary", "prioritized"],
    "properties": {
        "summary": {"type": "string", "minLength": 10},
        "prioritized": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["rank", "title", "severity"],
                "properties": {
                    "rank": {"type": "integer", "minimum": 1},
                    "title": {"type": "string"},
                    "severity": {"enum": ["low", "medium", "high", "critical"]},
                },
            },
        },
    },
}

ALLOWED_TOOLS = {"read_file", "list_files"}


def _branch_agent(bid: str, role: str, tools: list[str], model: str) -> AgentSpec:
    return AgentSpec(
        id=bid,
        role=role,
        system_contract=(
            f"You are the {bid} specialist. {role}\n"
            "Report only what you can support from the material given to you. "
            "Output JSON only, matching the required schema. No prose, no markdown fence."
        ),
        tools=[t for t in tools if t in ALLOWED_TOOLS],
        model=model,
        output_schema=FINDINGS_SCHEMA,
        budget_tokens=4000,
        timeout_s=180,
    )


def build_graph(branches: list[dict], models: list[str], verifier_model: str) -> GraphSpec:
    """Assemble the canonical shape: fanout -> N parallel analysts -> verify -> report.

    The verify node and at least one parallel branch are invariants, not
    suggestions - PS requirement 64 names both.
    """
    seen: set[str] = set()
    clean: list[dict] = []
    for b in branches:
        bid = str(b.get("id", "")).strip().lower().replace(" ", "_")
        if not bid or bid in seen or not bid.isidentifier():
            continue
        seen.add(bid)
        clean.append({
            "id": bid,
            "role": str(b.get("role") or f"Examine the {bid} aspects of the repository."),
            "tools": list(b.get("tools") or ["read_file"]),
        })
    if len(clean) < 2:
        # The council failed to give us a parallel plan. Fall back to the shape
        # the PS requires rather than emitting a single-branch graph.
        log.warning("council returned %d usable branches, using default set", len(clean))
        clean = [
            {"id": "security", "role": "Find security weaknesses.", "tools": ["read_file"]},
            {"id": "quality", "role": "Find code quality and maintainability issues.",
             "tools": ["read_file"]},
            {"id": "docs", "role": "Find documentation gaps.", "tools": ["read_file"]},
        ]
    clean = clean[:4]

    nodes = [
        Node(id="clarify", type="clarify"),
        Node(id="approve", type="approval"),
        Node(id="fanout", type="fanout"),
    ]
    edges = [
        Edge(from_node="clarify", to_node="approve"),
        Edge(from_node="approve", to_node="fanout"),
    ]

    for i, b in enumerate(clean):
        model = models[i % len(models)]
        nodes.append(Node(id=b["id"], type="agent", agent=_branch_agent(
            b["id"], b["role"], b["tools"], model), max_retries=1))
        edges.append(Edge(from_node="fanout", to_node=b["id"], handoff_schema=FINDINGS_SCHEMA))

    nodes.append(Node(id="join", type="join"))
    for b in clean:
        edges.append(Edge(from_node=b["id"], to_node="join", handoff_schema=FINDINGS_SCHEMA))

    nodes.append(Node(
        id="verify", type="verify",
        agent=AgentSpec(
            id="verifier",
            role="Independent verification of the merged findings",
            system_contract=(
                "You verify findings produced by other agents. Drop anything unsupported, "
                "merge duplicates, and rank by severity. Output JSON only."
            ),
            tools=[], model=verifier_model, output_schema=REPORT_SCHEMA,
            budget_tokens=4000, timeout_s=180,
        ),
    ))
    edges.append(Edge(from_node="join", to_node="verify", handoff_schema=REPORT_SCHEMA))

    # Compensation for each analyst: a failed branch degrades the report rather
    # than killing the run.
    for b in clean:
        nodes.append(Node(id=f"comp_{b['id']}", type="compensate", compensates=b["id"]))

    return GraphSpec(version=1, nodes=nodes, edges=edges).finalize()


async def compile_graph(
    goal: str,
    members: list[str],
    *,
    store: EventStore | None = None,
    run_id: str | None = None,
    seed: int = 42,
    chairman_policy: str = "strongest",
) -> tuple[GraphSpec, Verdict | None]:
    task = (
        f"Design a team of independent specialist agents for this goal:\n\n{goal}\n\n"
        "Each specialist must be able to work in parallel without waiting for the others."
    )
    verdict = None
    branches: list[dict] = []
    try:
        verdict = await deliberate(
            task, members, schema_hint=PLAN_SCHEMA_HINT,
            policy=chairman_policy, store=store, run_id=run_id, seed=seed,
        )
        content = verdict.content
        if isinstance(content, dict):
            branches = content.get("branches") or []
        elif isinstance(content, list):
            branches = content
    except Exception as e:
        log.warning("council compile failed, using default graph: %s", e)

    graph = build_graph(branches, members, verifier_model=members[-1])

    if store and run_id:
        await store.append(
            run_id, EventType.GRAPH_PROPOSED,
            payload={
                "config_hash": graph.config_hash,
                "node_count": len(graph.nodes),
                "levels": graph.levels(),
                "branches": [n.id for n in graph.nodes if n.type == "agent"],
                "council_used": verdict is not None,
            },
        )
    return graph, verdict


def clarifying_questions(goal: str) -> list[str]:
    """Up to 3 questions, then an explicit permission request.

    Deterministic rather than model-generated: the demo must ask the same
    questions every time, and these are the axes that actually change the graph.
    """
    return [
        "Which areas should the audit prioritise - security, code quality, "
        "documentation, or tests?",
        "Should findings be limited to what is provable from the code, or may they "
        "include advisory suggestions?",
        "What severity floor should appear in the final report?",
    ][:3]


def permission_prompt(branches: list[str]) -> str:
    return (
        "I can run "
        + ", ".join(branches)
        + " as parallel read-only agents, verify their merged output with an independent "
        "council node, and produce a prioritized remediation report. "
        "No files will be written. Shall I proceed?"
    )


def schema_json(schema: dict) -> str:
    return json.dumps(schema, indent=2)
