"""The event log. Every other module reads from here.

Talks to PostgREST over httpx rather than through supabase-py: the hot path is
one RPC call and three selects, httpx is already a dependency, and it is
async-native. supabase-py stays in requirements for Storage (large artifact
blobs, block 3).

Appends go through the `append_event` SQL function, never a client-side INSERT,
because sequence allocation must be atomic. See migrations/001_events.sql.
"""

import logging
from typing import Any, Self

import httpx

from backend.config import settings
from backend.events.types import CostTotals, Event, EventType, LatencyTotals, RunRow

log = logging.getLogger("aco.events")


class EventStoreError(RuntimeError):
    """Persistence failed. Message is safe to log; it never contains the key."""


class EventStore:
    def __init__(self, url: str | None = None, key: str | None = None) -> None:
        self._base = (url or settings.supabase_url).rstrip("/")
        self._key = key or settings.supabase_service_key
        if not self._base or not self._key:
            raise EventStoreError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        self._client: httpx.AsyncClient | None = None

    # ---------- lifecycle ----------

    async def __aenter__(self) -> Self:
        self._client = httpx.AsyncClient(
            base_url=f"{self._base}/rest/v1",
            headers={
                "apikey": self._key,
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0),
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise EventStoreError("EventStore used outside its async context")
        return self._client

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        r = await self._http.request(method, path, **kw)
        if r.status_code >= 400:
            # PostgREST echoes the failing SQL, not credentials, but keep the
            # surface tight anyway: detail to server logs, short message up.
            log.error("postgrest %s %s -> %s %s", method, path, r.status_code, r.text[:500])
            raise EventStoreError(f"event store rejected {method} {path} ({r.status_code})")
        return r

    # ---------- runs ----------

    async def create_run(
        self,
        run_id: str,
        goal: str,
        config_hash: str,
        seed: int,
        graph_spec: dict,
        status: str = "pending",
        replay_of: str | None = None,
    ) -> RunRow:
        """A run row must exist before any event: it owns the seq allocator."""
        body = {
            "run_id": run_id,
            "goal": goal,
            "config_hash": config_hash,
            "seed": seed,
            "graph_spec": graph_spec,
            "status": status,
            "replay_of": replay_of,
        }
        r = await self._request(
            "POST", "/runs", json=body, headers={"Prefer": "return=representation"}
        )
        return RunRow(**r.json()[0])

    async def get_run(self, run_id: str) -> RunRow | None:
        r = await self._request("GET", "/runs", params={"run_id": f"eq.{run_id}", "select": "*"})
        rows = r.json()
        return RunRow(**rows[0]) if rows else None

    async def set_run_status(self, run_id: str, status: str, ended: bool = False) -> None:
        patch: dict[str, Any] = {"status": status}
        if ended:
            patch["ended_at"] = "now()"
        await self._request("PATCH", "/runs", params={"run_id": f"eq.{run_id}"}, json=patch)

    # ---------- events ----------

    async def append(
        self,
        run_id: str,
        event_type: EventType | str,
        *,
        node_id: str | None = None,
        agent_id: str | None = None,
        payload: dict | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        latency_ms: int | None = None,
    ) -> int:
        """Append one event, return its seq. Atomic and gapless per run."""
        r = await self._request(
            "POST",
            "/rpc/append_event",
            json={
                "p_run_id": run_id,
                "p_event_type": str(event_type),
                "p_node_id": node_id,
                "p_agent_id": agent_id,
                "p_payload": payload,
                "p_tokens_in": tokens_in,
                "p_tokens_out": tokens_out,
                "p_cost_usd": cost_usd,
                "p_latency_ms": latency_ms,
            },
        )
        return int(r.json())

    async def read(self, run_id: str, from_seq: int = 0) -> list[Event]:
        r = await self._request(
            "GET",
            "/events",
            params={
                "run_id": f"eq.{run_id}",
                "seq": f"gt.{from_seq}",
                "select": "*",
                "order": "seq.asc",
            },
        )
        return [Event(**row) for row in r.json()]

    # ---------- aggregates (metrics read these, they compute nothing new) ----------

    async def aggregate_cost(self, run_id: str) -> CostTotals:
        r = await self._request(
            "GET",
            "/events",
            params={"run_id": f"eq.{run_id}", "select": "tokens_in,tokens_out,cost_usd"},
        )
        rows = r.json()
        return CostTotals(
            tokens_in=sum(x["tokens_in"] or 0 for x in rows),
            tokens_out=sum(x["tokens_out"] or 0 for x in rows),
            cost_usd=round(sum(float(x["cost_usd"] or 0) for x in rows), 6),
            events=len(rows),
        )

    async def aggregate_latency(self, run_id: str) -> LatencyTotals:
        """Sum of node latencies vs wall clock - the Parallel Efficiency inputs."""
        r = await self._request(
            "GET",
            "/events",
            params={
                "run_id": f"eq.{run_id}",
                "select": "event_type,latency_ms,ts",
                "order": "seq.asc",
            },
        )
        rows = r.json()
        if not rows:
            return LatencyTotals()

        node_latencies = [
            x["latency_ms"]
            for x in rows
            if x["event_type"] == EventType.NODE_END and x["latency_ms"]
        ]
        stamps = sorted(x["ts"] for x in rows if x["ts"])
        wall = 0
        if len(stamps) >= 2:
            from datetime import datetime

            first = datetime.fromisoformat(stamps[0])
            last = datetime.fromisoformat(stamps[-1])
            wall = int((last - first).total_seconds() * 1000)

        return LatencyTotals(
            sum_ms=sum(node_latencies),
            wall_clock_ms=max(wall, 1),
            measured_nodes=len(node_latencies),
        )
