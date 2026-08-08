"""Workflow (GraphSpec) version migration.

`GraphSpec.version` was, until now, an inert field - nothing read it. This
registry is what makes it real: a graph dict persisted under an older schema
version is walked forward, one version at a time, before it is used to start a
new run.

Not used by replay (backend/replay/replay.py): replay's whole contract is
"reconstruct exactly what was stored and verify the hash matches what ran" -
migrating the dict first would make it verify against a different object than
what actually executed. Migration only applies to *starting a new run* from an
old graph dict (see backend/api/routes.py::start_run).
"""

from collections.abc import Callable

CURRENT_VERSION = 2


def _v1_to_v2(raw: dict) -> dict:
    """v1 -> v2 is a behavioral epoch, not a shape change.

    `subgraph` (on Node) and `memory_ttl_s` (on AgentSpec) are additive,
    None-default fields - a v1 dict already parses correctly under the v2
    schema with nothing to rewrite, and hashes identically (see
    backend/engine/spec.py's CURRENT_VERSION docstring). What actually changes
    at v2 is that the executor starts honoring AgentSpec.memory_scope
    (declared long before this module existed, previously always inert) and
    dispatches "subgraph" nodes - both gated on `graph.version >= 2` so
    replaying an old run never silently changes behavior.

    This step exists to make that gate explicit and version-checked, and to
    give the next migration that DOES need a real reshape a tested slot to
    land in - not to manufacture a field rewrite this version doesn't need.
    """
    raw = dict(raw)
    raw["version"] = 2
    return raw


MIGRATIONS: dict[int, Callable[[dict], dict]] = {1: _v1_to_v2}


def migrate(graph_dict: dict) -> dict:
    """Walk `graph_dict` forward to CURRENT_VERSION, one registered step at a
    time. Raises if a step in the path is missing - no silent pass-through."""
    version = graph_dict.get("version", 1)
    while version < CURRENT_VERSION:
        step = MIGRATIONS.get(version)
        if step is None:
            raise ValueError(f"no migration registered from graph version {version}")
        graph_dict = step(graph_dict)
        if graph_dict.get("version") != version + 1:
            raise ValueError(
                f"migration from version {version} did not stamp version {version + 1}"
            )
        version += 1
    return graph_dict
