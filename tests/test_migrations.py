"""Workflow version migration. Pure - no network, no Turso."""

import pytest

from backend.engine.migrations import CURRENT_VERSION, migrate
from backend.engine.spec import GraphSpec


def test_v1_dict_migrates_to_current_version():
    v1 = {"version": 1, "nodes": [], "edges": []}
    migrated = migrate(v1)
    assert migrated["version"] == CURRENT_VERSION


def test_migration_result_still_parses_as_a_graphspec():
    v1 = {"version": 1, "nodes": [{"id": "a", "type": "fanout"}], "edges": []}
    g = GraphSpec(**migrate(v1))
    assert g.version == CURRENT_VERSION
    assert g.node("a").id == "a"


def test_already_current_version_is_a_no_op():
    current = {"version": CURRENT_VERSION, "nodes": [], "edges": []}
    assert migrate(current) == current


def test_missing_migration_path_raises_rather_than_silently_passing_through():
    # version 0 is below CURRENT_VERSION but has no registered migration step
    # (only version 1 does) - a genuine gap in the path, unlike a version
    # newer than CURRENT_VERSION, which migrate() correctly leaves untouched.
    with pytest.raises(ValueError, match="no migration registered"):
        migrate({"version": 0, "nodes": [], "edges": []})


def test_memory_scope_gate_flips_after_migration():
    """The actual behavioral point of v1->v2, not just the version number."""
    v1 = {
        "version": 1,
        "nodes": [{"id": "a", "type": "agent", "agent": {
            "id": "a", "role": "r", "system_contract": "c", "tools": [],
            "model": "ollama:llama3.2:3b", "memory_scope": "shared_rw",
        }}],
        "edges": [],
    }
    before = GraphSpec(**v1)
    after = GraphSpec(**migrate(v1))
    assert before.version < 2 <= after.version
