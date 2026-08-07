"""The event log. Every other module reads from here.

Talks to Turso (libSQL) over its serverless HTTP driver. SQLite has no
server-side functions, so the atomic, gapless sequence allocation that used to
live in a Postgres `append_event` function (see migrations/001_events.sql) now
lives here: `UPDATE ... RETURNING` bumps `last_seq` and hands back the new
value, a dependent `INSERT` uses it, both commit as one transaction. An
in-process `asyncio.Lock` serialises that read-modify-write across concurrent
callers in this process - proven gapless under real concurrent writers against
the live database before this was wired in. `turso_serverless` is a sync,
DB-API 2.0 driver with no persistent connection, so every call opens its own
short-lived one and runs on a worker thread via `asyncio.to_thread`.
"""

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Self

import turso_serverless

from backend.config import settings
from backend.events.types import CostTotals, Event, EventType, LatencyTotals, RunRow

log = logging.getLogger("aco.events")


class EventStoreError(RuntimeError):
    """Persistence failed. Message is safe to log; it never contains the key."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class EventStore:
    def __init__(self, url: str | None = None, token: str | None = None) -> None:
        self._url = url or settings.turso_database_url
        self._token = token or settings.turso_auth_token
        if not self._url or not self._token:
            raise EventStoreError("TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be set")
        # Guards the UPDATE-RETURNING-then-INSERT sequence in `append`. Every
        # other method is a single statement and needs no lock.
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def _conn(self):
        c = turso_serverless.connect(self._url, auth_token=self._token)
        c.execute("PRAGMA foreign_keys = ON")
        return c

    def _run(self, fn) -> Any:
        """Execute a synchronous DB-API sequence on a worker thread.

        `fn` receives an open connection and must commit before returning, or
        its writes are lost - the connection closes right after.
        """

        def _call():
            c = self._conn()
            try:
                result = fn(c)
                return result
            except turso_serverless.dbapi.Error as e:
                log.error("turso error: %s: %s", type(e).__name__, e)
                raise EventStoreError(f"event store rejected the operation: {e}") from e
            finally:
                c.close()

        return asyncio.to_thread(_call)

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
        started_at = _now()

        def _do(c):
            c.execute(
                "INSERT INTO runs (run_id, goal, config_hash, seed, graph_spec, status, "
                "replay_of, last_seq, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
                [run_id, goal, config_hash, seed, json.dumps(graph_spec), status,
                 replay_of, started_at],
            )
            c.commit()

        await self._run(_do)
        return RunRow(
            run_id=run_id, goal=goal, config_hash=config_hash, seed=seed,
            graph_spec=graph_spec, status=status, replay_of=replay_of,
            last_seq=0, started_at=started_at, ended_at=None,
        )

    async def get_run(self, run_id: str) -> RunRow | None:
        def _do(c):
            cur = c.execute(
                "SELECT run_id, goal, config_hash, seed, graph_spec, status, replay_of, "
                "last_seq, started_at, ended_at FROM runs WHERE run_id = ?",
                [run_id],
            )
            row = list(cur)
            c.commit()
            return row[0] if row else None

        row = await self._run(_do)
        if row is None:
            return None
        return RunRow(
            run_id=row[0], goal=row[1], config_hash=row[2], seed=row[3],
            graph_spec=json.loads(row[4]), status=row[5], replay_of=row[6],
            last_seq=row[7], started_at=row[8], ended_at=row[9],
        )

    async def list_runs(self, limit: int = 25) -> list[RunRow]:
        def _do(c):
            cur = c.execute(
                "SELECT run_id, goal, config_hash, seed, graph_spec, status, replay_of, "
                "last_seq, started_at, ended_at FROM runs ORDER BY started_at DESC LIMIT ?",
                [limit],
            )
            rows = list(cur)
            c.commit()
            return rows

        rows = await self._run(_do)
        return [
            RunRow(
                run_id=r[0], goal=r[1], config_hash=r[2], seed=r[3],
                graph_spec=json.loads(r[4]), status=r[5], replay_of=r[6],
                last_seq=r[7], started_at=r[8], ended_at=r[9],
            )
            for r in rows
        ]

    async def set_run_status(self, run_id: str, status: str, ended: bool = False) -> None:
        def _do(c):
            if ended:
                c.execute(
                    "UPDATE runs SET status = ?, ended_at = ? WHERE run_id = ?",
                    [status, _now(), run_id],
                )
            else:
                c.execute("UPDATE runs SET status = ? WHERE run_id = ?", [status, run_id])
            c.commit()

        await self._run(_do)

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
        """Append one event, return its seq. Atomic and gapless per run.

        The lock is process-local, which matches this app's deployment: one
        backend instance, no cross-process writers. The `UPDATE ... RETURNING`
        row-level effect on `runs` still holds even without it - the lock
        exists so the read of the new seq and the dependent INSERT can never
        interleave with another append from this same process.
        """
        ts = _now()

        def _do(c):
            cur = c.execute(
                "UPDATE runs SET last_seq = last_seq + 1 WHERE run_id = ? RETURNING last_seq",
                [run_id],
            )
            row = list(cur)
            if not row:
                raise EventStoreError(f"unknown run_id {run_id}, create the run before appending")
            new_seq = row[0][0]

            c.execute(
                "INSERT INTO events (run_id, seq, node_id, agent_id, event_type, payload, "
                "tokens_in, tokens_out, cost_usd, latency_ms, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [run_id, new_seq, node_id, agent_id, str(event_type),
                 json.dumps(payload) if payload is not None else None,
                 tokens_in, tokens_out, cost_usd, latency_ms, ts],
            )
            c.commit()
            return new_seq

        async with self._lock:
            return await self._run(_do)

    async def read(self, run_id: str, from_seq: int = 0) -> list[Event]:
        def _do(c):
            cur = c.execute(
                "SELECT run_id, seq, node_id, agent_id, event_type, payload, tokens_in, "
                "tokens_out, cost_usd, latency_ms, ts FROM events "
                "WHERE run_id = ? AND seq > ? ORDER BY seq ASC",
                [run_id, from_seq],
            )
            rows = list(cur)
            c.commit()
            return rows

        rows = await self._run(_do)
        return [
            Event(
                run_id=r[0], seq=r[1], node_id=r[2], agent_id=r[3], event_type=r[4],
                payload=json.loads(r[5]) if r[5] is not None else None,
                tokens_in=r[6] or 0, tokens_out=r[7] or 0, cost_usd=r[8] or 0.0,
                latency_ms=r[9], ts=r[10],
            )
            for r in rows
        ]

    # ---------- artifacts ----------

    async def put_artifact(
        self,
        run_id: str,
        *,
        seq: int,
        written_by: str,
        content: dict | list,
        node_id: str | None = None,
        storage_path: str | None = None,
    ) -> dict:
        artifact_id = str(uuid.uuid4())
        ts = _now()

        def _do(c):
            c.execute(
                "INSERT INTO artifacts (artifact_id, run_id, seq, written_by, node_id, "
                "content, storage_path, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [artifact_id, run_id, seq, written_by, node_id, json.dumps(content),
                 storage_path, ts],
            )
            c.commit()

        await self._run(_do)
        return {
            "artifact_id": artifact_id, "run_id": run_id, "seq": seq,
            "written_by": written_by, "node_id": node_id, "content": content,
            "storage_path": storage_path, "ts": ts,
        }

    async def list_artifacts(self, run_id: str) -> list[dict]:
        def _do(c):
            cur = c.execute(
                "SELECT artifact_id, run_id, seq, written_by, node_id, content, "
                "storage_path, ts FROM artifacts WHERE run_id = ? ORDER BY seq ASC",
                [run_id],
            )
            rows = list(cur)
            c.commit()
            return rows

        rows = await self._run(_do)
        return [
            {
                "artifact_id": r[0], "run_id": r[1], "seq": r[2], "written_by": r[3],
                "node_id": r[4], "content": json.loads(r[5]), "storage_path": r[6], "ts": r[7],
            }
            for r in rows
        ]

    # ---------- internal (test use only) ----------

    async def _execute(self, sql: str, params: list | None = None) -> None:
        """Run one raw statement and commit. Used by tests that probe database
        guarantees (e.g. the append-only trigger) directly."""

        def _do(c):
            c.execute(sql, params or [])
            c.commit()

        await self._run(_do)

    # ---------- aggregates (metrics read these, they compute nothing new) ----------

    async def aggregate_cost(self, run_id: str) -> CostTotals:
        def _do(c):
            cur = c.execute(
                "SELECT tokens_in, tokens_out, cost_usd FROM events WHERE run_id = ?",
                [run_id],
            )
            rows = list(cur)
            c.commit()
            return rows

        rows = await self._run(_do)
        return CostTotals(
            tokens_in=sum(r[0] or 0 for r in rows),
            tokens_out=sum(r[1] or 0 for r in rows),
            cost_usd=round(sum(float(r[2] or 0) for r in rows), 6),
            events=len(rows),
        )

    async def aggregate_latency(self, run_id: str) -> LatencyTotals:
        """Sum of node latencies vs wall clock - the Parallel Efficiency inputs."""

        def _do(c):
            cur = c.execute(
                "SELECT event_type, latency_ms, ts FROM events WHERE run_id = ? "
                "ORDER BY seq ASC",
                [run_id],
            )
            rows = list(cur)
            c.commit()
            return rows

        rows = await self._run(_do)
        if not rows:
            return LatencyTotals()

        node_latencies = [
            r[1] for r in rows if r[0] == EventType.NODE_END and r[1]
        ]
        stamps = sorted(r[2] for r in rows if r[2])
        wall = 0
        if len(stamps) >= 2:
            first = datetime.fromisoformat(stamps[0])
            last = datetime.fromisoformat(stamps[-1])
            wall = int((last - first).total_seconds() * 1000)

        return LatencyTotals(
            sum_ms=sum(node_latencies),
            wall_clock_ms=max(wall, 1),
            measured_nodes=len(node_latencies),
        )
