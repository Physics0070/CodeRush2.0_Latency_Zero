"""Process-local shared + scratch memory, with TTL-based staleness eviction.

Not the replayable record - the event log (MEMORY_WRITE/READ/EVICTED, see
backend/events/types.py) is, same as everywhere else in this codebase. This is
working state, matching the existing accepted pattern for per-run runtime data
(backend/api/routes.py's module-level `_TASKS` dict) - a single-instance
deploy has no need for a second Turso table just to hold a handful of
short-TTL entries per run.

Two pools per run, keyed by AgentSpec.memory_scope:
- "shared": written by shared_rw agents, readable by shared_rw/shared_ro agents.
- per-agent scratch: every agent, regardless of scope, always writes and reads
  its own private scratch entry too - personal working memory nothing else
  ever sees, which is what an "isolated" agent gets instead of the shared pool.

Wired into backend/engine/executor.py::_agent_node, gated on
`graph.version >= 2` so a v1 graph replayed under this code behaves exactly as
it originally did (memory_scope was declared before this module existed and
was always inert - see backend/engine/migrations.py for the version story).
"""

import time

DEFAULT_MEMORY_TTL_S = 300.0

_SHARED = "shared"


def _scratch_bucket(agent_id: str) -> str:
    return f"agent:{agent_id}"


class MemoryStore:
    def __init__(self) -> None:
        # (run_id, bucket, key) -> (value, written_at_monotonic, ttl_s)
        self._data: dict[tuple[str, str, str], tuple[object, float, float]] = {}

    async def write(
        self,
        run_id: str,
        agent_id: str,
        key: str,
        value: object,
        *,
        ttl_s: float,
        shared: bool,
    ) -> None:
        """Always writes the agent's own scratch entry; additionally writes the
        shared pool when `shared` is True (AgentSpec.memory_scope == "shared_rw")."""
        now = time.monotonic()
        self._sweep(now)
        self._data[(run_id, _scratch_bucket(agent_id), key)] = (value, now, ttl_s)
        if shared:
            self._data[(run_id, _SHARED, key)] = (value, now, ttl_s)

    async def read_for(
        self, run_id: str, agent_id: str, *, include_shared: bool
    ) -> tuple[dict[str, object], list[str]]:
        """Live entries visible to this agent: its own scratch always, plus the
        shared pool when `include_shared` (memory_scope != "isolated").

        Returns (live_entries, evicted_keys) - an entry older than its TTL is
        removed and reported as evicted rather than silently omitted, so
        eviction is observable in the trace, not just invisible.
        """
        now = time.monotonic()
        buckets = {_scratch_bucket(agent_id)}
        if include_shared:
            buckets.add(_SHARED)

        live: dict[str, object] = {}
        evicted: list[str] = []
        for (rid, bucket, key), (value, written_at, ttl_s) in list(self._data.items()):
            if rid != run_id or bucket not in buckets:
                continue
            if now - written_at > ttl_s:
                # A shared_rw write lands in both the scratch and shared
                # buckets under the same key - report the key once even if
                # both copies expired in this same read.
                if key not in evicted:
                    evicted.append(key)
                del self._data[(rid, bucket, key)]
            else:
                live[key] = value
        return live, evicted

    def _sweep(self, now: float) -> None:
        """Opportunistic, cheap: entry counts per run stay tiny (a handful of
        nodes' worth), so a full scan on write is not worth avoiding."""
        for k, (_, written_at, ttl_s) in list(self._data.items()):
            if now - written_at > ttl_s:
                del self._data[k]


memory_store = MemoryStore()
