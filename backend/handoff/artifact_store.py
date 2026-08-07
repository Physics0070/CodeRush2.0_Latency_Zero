"""Validated outputs, stored with provenance.

Every artifact records who wrote it, in which run, at which event seq. That
chain is what makes the trace viewer able to answer "where did this claim come
from" and what lets replay prove an output was not silently regenerated.
"""

import json

from pydantic import BaseModel

from backend.events import EventStore
from backend.providers.secret_broker import assert_no_key_in

# Above this, the row holds a pointer and the blob goes to Supabase Storage.
INLINE_LIMIT_BYTES = 200_000


class Artifact(BaseModel):
    artifact_id: str | None = None
    run_id: str
    seq: int
    written_by: str
    node_id: str | None = None
    content: dict | list
    storage_path: str | None = None
    ts: str | None = None


async def put(
    store: EventStore,
    run_id: str,
    *,
    seq: int,
    written_by: str,
    content: dict | list,
    node_id: str | None = None,
) -> Artifact:
    body = json.dumps(content)
    # Cheap guard on the persistence boundary: a payload that somehow picked up
    # a live key crashes here rather than being written down.
    assert_no_key_in(body)

    storage_path = None
    if len(body) > INLINE_LIMIT_BYTES:
        # TODO(block-12): push oversized blobs to object storage and set
        # storage_path. Not yet exercised - no artifact in the demo graph gets
        # near 200KB, and an untested upload path is worse than an honest gap.
        content = {"truncated": True, "bytes": len(body)}

    row = await store.put_artifact(
        run_id, seq=seq, written_by=written_by, content=content,
        node_id=node_id, storage_path=storage_path,
    )
    return Artifact(**row)


async def for_run(store: EventStore, run_id: str) -> list[Artifact]:
    rows = await store.list_artifacts(run_id)
    return [Artifact(**x) for x in rows]
