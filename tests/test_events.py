"""Block 1 verification against the real Turso database.

The load-bearing assertion is the concurrency one: five appends fired at once
must produce seq 1..5 with no gaps and no duplicates. A client-side
`SELECT MAX(seq)+1` passes this test serially and fails it concurrently, which
is exactly why append goes through the lock-guarded UPDATE...RETURNING path in
backend/events/store.py rather than a naive read-then-write.
"""

import asyncio
import uuid

import pytest

from backend.config import settings
from backend.events import EventStore, EventType

pytestmark = pytest.mark.skipif(
    not (settings.turso_database_url and settings.turso_auth_token),
    reason="TURSO_DATABASE_URL / TURSO_AUTH_TOKEN not configured",
)

GRAPH = {"version": 1, "nodes": [], "edges": []}


@pytest.fixture
async def store():
    async with EventStore() as s:
        yield s


@pytest.fixture
async def run_id(store):
    rid = str(uuid.uuid4())
    await store.create_run(rid, "test goal", "hash-" + rid[:8], 42, GRAPH, "running")
    return rid


async def test_concurrent_appends_are_gapless(store, run_id):
    seqs = await asyncio.gather(
        *(
            store.append(run_id, EventType.NODE_START, node_id=f"n{i}", payload={"i": i})
            for i in range(5)
        )
    )
    assert sorted(seqs) == [1, 2, 3, 4, 5], f"expected 1..5, got {sorted(seqs)}"
    assert len(set(seqs)) == 5, "duplicate seq allocated"

    rows = await store.read(run_id)
    assert [e.seq for e in rows] == [1, 2, 3, 4, 5]


async def test_read_from_seq(store, run_id):
    for i in range(3):
        await store.append(run_id, EventType.NODE_START, node_id=f"n{i}")
    assert [e.seq for e in await store.read(run_id, from_seq=1)] == [2, 3]


async def test_aggregates(store, run_id):
    await store.append(run_id, EventType.NODE_END, tokens_in=100, tokens_out=50,
                       cost_usd=0.0012, latency_ms=400)
    await store.append(run_id, EventType.NODE_END, tokens_in=200, tokens_out=25,
                       cost_usd=0.0008, latency_ms=600)

    cost = await store.aggregate_cost(run_id)
    assert cost.tokens_in == 300
    assert cost.tokens_out == 75
    assert cost.cost_usd == pytest.approx(0.002)

    lat = await store.aggregate_latency(run_id)
    assert lat.sum_ms == 1000
    assert lat.measured_nodes == 2


async def test_events_are_append_only(store, run_id):
    """The immutability trigger must reject UPDATE and DELETE at the database."""
    await store.append(run_id, EventType.RUN_START)
    from backend.events import EventStoreError

    with pytest.raises(EventStoreError):
        await store._execute(
            "UPDATE events SET event_type = ? WHERE run_id = ? AND seq = ?",
            ["TAMPERED", run_id, 1],
        )

    with pytest.raises(EventStoreError):
        await store._execute(
            "DELETE FROM events WHERE run_id = ? AND seq = ?",
            [run_id, 1],
        )


async def test_append_to_unknown_run_is_rejected(store):
    """No orphan events: the run row owns the sequence."""
    from backend.events import EventStoreError

    with pytest.raises(EventStoreError):
        await store.append(str(uuid.uuid4()), EventType.RUN_START)
