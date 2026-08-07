"""Graph data model and config hashing.

`config_hash` is what makes replay provable: it is a SHA256 over the nodes,
edges, agent specs and model identifiers, so an identical hash plus an identical
seed means an identical execution surface.
"""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field

NodeType = Literal[
    "clarify",
    "agent",
    "fanout",
    "join",
    "conditional",
    "verify",
    "council",
    "approval",
    "compensate",
    "subgraph",
]

MemoryScope = Literal["shared_rw", "shared_ro", "isolated"]


class AgentSpec(BaseModel):
    id: str
    role: str
    system_contract: str
    input_schema: dict = Field(default_factory=dict)
    output_schema: dict = Field(default_factory=dict)
    # Declared capabilities. Enforced by the orchestrator at call time, not
    # trusted from the model's own claims.
    tools: list[str] = Field(default_factory=list)
    model: str = "ollama:llama3.2:3b"
    fallback_model: str | None = None
    memory_scope: MemoryScope = "shared_ro"
    budget_tokens: int = 8000
    timeout_s: int = 120
    # Non-empty means this node needs an upstream approval gate.
    allowed_side_effects: list[str] = Field(default_factory=list)


class Node(BaseModel):
    id: str
    type: NodeType
    agent: AgentSpec | None = None
    # conditional: python-free predicate over upstream outputs
    condition: str | None = None
    # compensate: which node this compensates for
    compensates: str | None = None
    council_members: list[str] = Field(default_factory=list)
    chairman_policy: str = "strongest"
    max_retries: int = 1


class Edge(BaseModel):
    from_node: str
    to_node: str
    # The graded contract. Empty means "no payload crosses here" (control edge).
    handoff_schema: dict = Field(default_factory=dict)


class GraphSpec(BaseModel):
    version: int = 1
    config_hash: str = ""
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    locked: bool = False

    def compute_hash(self) -> str:
        """Stable across dict ordering; excludes the hash field itself."""
        material = {
            "nodes": [n.model_dump(exclude_none=True) for n in self.nodes],
            "edges": [e.model_dump() for e in self.edges],
            "models": sorted(n.agent.model for n in self.nodes if n.agent),
        }
        blob = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    def finalize(self) -> "GraphSpec":
        self.config_hash = self.compute_hash()
        return self

    # ---------- topology ----------

    def node(self, node_id: str) -> Node:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(f"no node {node_id!r}")

    def parents(self, node_id: str) -> list[str]:
        return [e.from_node for e in self.edges if e.to_node == node_id]

    def children(self, node_id: str) -> list[str]:
        return [e.to_node for e in self.edges if e.from_node == node_id]

    def edge(self, from_node: str, to_node: str) -> Edge | None:
        for e in self.edges:
            if e.from_node == from_node and e.to_node == to_node:
                return e
        return None

    def levels(self) -> list[list[str]]:
        """Kahn layering. Everything in one level is genuinely independent and
        runs concurrently; a node drops to the next level only when it actually
        consumes an upstream output. This is the parallel-first claim, and
        Parallel Efficiency is the measurement of it.
        """
        compensators = {n.id for n in self.nodes if n.type == "compensate"}
        indegree = {
            n.id: len([p for p in self.parents(n.id) if p not in compensators])
            for n in self.nodes
            if n.id not in compensators
        }
        out: list[list[str]] = []
        remaining = dict(indegree)
        while remaining:
            ready = sorted(k for k, v in remaining.items() if v == 0)
            if not ready:
                raise ValueError(f"cycle detected among {sorted(remaining)}")
            out.append(ready)
            for r in ready:
                del remaining[r]
                for c in self.children(r):
                    if c in remaining:
                        remaining[c] -= 1
        return out

    def validate_side_effects(self) -> list[str]:
        """PS safety boundary: a node that can cause a side effect must have an
        approval gate somewhere upstream. Read-only nodes fan out freely.
        """
        problems = []
        for n in self.nodes:
            if not (n.agent and n.agent.allowed_side_effects):
                continue
            if not self._has_upstream_approval(n.id):
                problems.append(
                    f"node {n.id!r} declares side effects "
                    f"{n.agent.allowed_side_effects} with no upstream approval node"
                )
        return problems

    def _has_upstream_approval(self, node_id: str, seen: set[str] | None = None) -> bool:
        seen = seen or set()
        for p in self.parents(node_id):
            if p in seen:
                continue
            seen.add(p)
            if self.node(p).type == "approval":
                return True
            if self._has_upstream_approval(p, seen):
                return True
        return False
