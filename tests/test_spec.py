"""GraphSpec/Node: hash stability across the subgraph/memory_ttl_s addition,
recursive validation, and node-id collection. Pure - no network, no Turso.
"""

from backend.engine.spec import AgentSpec, CURRENT_VERSION, Edge, GraphSpec, Node

SCHEMA = {"type": "object"}


def agent(aid: str, **kw) -> AgentSpec:
    return AgentSpec(
        id=aid, role=f"{aid} role", system_contract="c",
        tools=[], model="ollama:llama3.2:3b", **kw,
    )


# --------------------------------------------------------- hash stability


def test_v1_shaped_dict_hashes_identically_to_before_this_change():
    """The exact regression this repo cannot afford: a graph dict with no
    `subgraph`/`memory_ttl_s` keys (i.e. anything persisted before this batch
    shipped) must hash to the value it hashed to before these fields existed,
    or replay_run() starts rejecting every pre-existing run as tampered.
    """
    old_dict = {
        "version": 1,
        "nodes": [{"id": "a", "type": "agent", "agent": {
            "id": "a", "role": "r", "system_contract": "c", "tools": [],
            "model": "ollama:llama3.2:3b",
        }}],
        "edges": [],
    }
    g = GraphSpec(**old_dict)
    assert g.version == 1
    # Locked to the exact hash computed against the pre-change code (verified
    # via `git stash` A/B comparison while implementing this).
    assert g.compute_hash() == (
        "4f0c13fdd6615c6f7a54ba658a042d20a15a837f737765836a472335831d1031"
    )


def test_fresh_graph_defaults_to_current_version():
    g = GraphSpec(nodes=[Node(id="x", type="fanout")], edges=[])
    assert g.version == CURRENT_VERSION


def test_absent_new_fields_do_not_change_the_hash():
    """None-valued subgraph/memory_ttl_s must not appear in hash material -
    compute_hash uses model_dump(exclude_none=True)."""
    base = GraphSpec(nodes=[Node(id="a", type="agent", agent=agent("a"))], edges=[])
    explicit_none = GraphSpec(
        nodes=[Node(id="a", type="agent", agent=agent("a", memory_ttl_s=None),
                     subgraph=None)],
        edges=[],
    )
    assert base.compute_hash() == explicit_none.compute_hash()


def test_setting_the_new_fields_does_change_the_hash():
    base = GraphSpec(nodes=[Node(id="a", type="agent", agent=agent("a"))], edges=[])
    with_ttl = GraphSpec(
        nodes=[Node(id="a", type="agent", agent=agent("a", memory_ttl_s=60))], edges=[],
    )
    assert base.compute_hash() != with_ttl.compute_hash()


# ------------------------------------------------------------- subgraphs


def test_node_subgraph_forward_ref_resolves():
    """Node.model_rebuild() must have run - constructing a Node with a nested
    GraphSpec must not raise a PydanticUndefinedAnnotation error."""
    inner = GraphSpec(nodes=[Node(id="inner", type="fanout")], edges=[])
    outer = Node(id="sub", type="subgraph", subgraph=inner)
    assert outer.subgraph is inner


def test_all_node_ids_recurses_into_nested_subgraphs():
    inner = GraphSpec(nodes=[Node(id="inner1", type="fanout"),
                              Node(id="inner2", type="fanout")], edges=[])
    outer = GraphSpec(nodes=[
        Node(id="outer1", type="fanout"),
        Node(id="sub", type="subgraph", subgraph=inner),
    ], edges=[])
    assert sorted(outer.all_node_ids()) == ["inner1", "inner2", "outer1", "sub"]


def test_validate_side_effects_recurses_into_nested_subgraphs():
    """A side-effecting node hidden inside a subgraph, with no approval gate
    inside that subgraph, must not slip past the top-level check."""
    unsafe_inner = GraphSpec(nodes=[
        Node(id="writer", type="agent", agent=agent("writer", allowed_side_effects=["write"])),
    ], edges=[])
    outer = GraphSpec(nodes=[Node(id="sub", type="subgraph", subgraph=unsafe_inner)], edges=[])
    problems = outer.validate_side_effects()
    assert problems, "an unapproved side effect nested in a subgraph must be caught"
    assert "sub.writer" in problems[0] or "writer" in problems[0]


def test_validate_side_effects_allows_a_subgraph_with_its_own_approval_gate():
    safe_inner = GraphSpec(nodes=[
        Node(id="gate", type="approval"),
        Node(id="writer", type="agent", agent=agent("writer", allowed_side_effects=["write"])),
    ], edges=[Edge(from_node="gate", to_node="writer")])
    outer = GraphSpec(nodes=[Node(id="sub", type="subgraph", subgraph=safe_inner)], edges=[])
    assert outer.validate_side_effects() == []


def test_never_finalize_a_nested_subgraph_independently():
    """Documented rule, checked here: a subgraph embedded without its own
    config_hash must not carry a stray hash into the parent's hash material."""
    inner = GraphSpec(nodes=[Node(id="inner", type="fanout")], edges=[])
    outer_a = GraphSpec(
        nodes=[Node(id="sub", type="subgraph", subgraph=inner)], edges=[]
    ).finalize()

    finalized_inner = GraphSpec(nodes=[Node(id="inner", type="fanout")], edges=[]).finalize()
    outer_b = GraphSpec(
        nodes=[Node(id="sub", type="subgraph", subgraph=finalized_inner)], edges=[]
    ).finalize()

    assert outer_a.config_hash != outer_b.config_hash, (
        "a finalized (non-empty config_hash) nested subgraph changes the "
        "parent's hash even though nothing about the executable shape "
        "changed - confirms why subgraphs must stay unfinalized when embedded"
    )
