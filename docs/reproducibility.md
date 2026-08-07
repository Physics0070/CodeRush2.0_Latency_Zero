# Reproducibility

> "Most systems *replay* by re-running and getting a different answer. We replay
> from the event log — the diff is zero, and we show it."

Replay is only worth claiming if it is provable. Three things make it provable.

## 1. Determinism inputs are recorded on the run

Every run row carries `seed` and `config_hash`. Every model call is made with
`temperature=0` and that seed. Both are written before the first node executes,
so there is no way to retrofit them afterwards.

## 2. `config_hash` pins the execution surface

```python
material = {
  "nodes":  [n.model_dump(exclude_none=True) for n in self.nodes],
  "edges":  [e.model_dump() for e in self.edges],
  "models": sorted(n.agent.model for n in self.nodes if n.agent),
}
config_hash = sha256(json.dumps(material, sort_keys=True, separators=(",", ":")))
```

Sorted keys and fixed separators mean dict ordering cannot change the hash.
Changing one agent's model changes it — `tests/test_engine.py` asserts both
directions.

**Replay refuses to run on a hash mismatch.** If the stored `graph_spec` no
longer hashes to the stored `config_hash`, the spec was modified and a "zero
diff" would be meaningless:

```
ValueError: config_hash mismatch: stored 4f2a91c8de11 but graph hashes
9b7e03aa5c42; the saved spec was modified
```

## 3. Every completion is recorded, and replay serves from the log

This is the mechanism. `LoggedCompleter` walks the original run's `TOOL_RESULT`
rows and returns what was recorded instead of calling a provider.

Two details that are easy to get wrong:

**Recording lives in the engine, not the provider layer.** It was originally in
`registry.complete`, which meant any injected completer produced a run with no
recording — replaying it returned empty output for every node while still
reporting a diff. The zero-diff test caught this; it is now
`executor._agent_node`, so recording is guaranteed regardless of what produced
the completion.

**Recordings are keyed by `(node_id, agent_id)`, not a single queue.** A global
FIFO would be order-dependent, and concurrent branches would steal each other's
responses. Order-independence is the entire point.

Approvals are replayed from the log rather than re-requested from a human, so
replay is non-interactive.

## Worked example

A 3-branch audit graph (`security`, `quality`, `docs` → `join` → `verify`), run
and then replayed:

```
POST /api/runs/{run_id}/replay
```

```json
{
  "replay_run_id": "…",
  "original_run_id": "…",
  "diff": {
    "identical": true,
    "output_diffs": [],
    "nodes_compared": 4,
    "original_cost_usd": 0.0005,
    "replay_cost_usd": 0.0,
    "original_wall_ms": 2709,
    "replay_wall_ms": 2119,
    "original_tokens": 900,
    "replay_tokens": 900
  }
}
```

Read it as:

- `identical: true`, `output_diffs: []` — every validated node output is
  byte-identical after `json.dumps(sort_keys=True)`.
- `replay_cost_usd: 0.0` — replay calls nothing, so it costs nothing. The
  difference against the original is money not spent.
- `replay_wall_ms < original_wall_ms` — the remaining time is engine and event
  log, not inference.
- Token counts match because they are read back from the recording, not
  recomputed.

The replay run's row carries `replay_of` pointing at the original, so the
lineage is queryable.

## What the tests assert

`tests/test_replay_metrics.py`:

| Test | Asserts |
|---|---|
| `test_replay_diff_is_zero` | `identical` is true and `output_diffs` is empty |
| `test_replay_row_links_back_to_the_original` | `replay_of` set, `config_hash` matches |
| `test_replay_serves_from_the_log_not_the_provider` | replayed `TOOL_RESULT` rows exist |
| `test_tampered_graph_spec_is_refused` | a mutated `config_hash` raises |

```
4 passed
```

## Honest limits

1. **Determinism of the *original* run is not guaranteed.** `temperature=0` plus
   a seed makes a local model near-deterministic, but GPU non-determinism and
   provider-side changes mean two *fresh* runs can differ. What we guarantee is
   that a **replay** of a recorded run is identical — which is the requirement,
   and is why replay reads the log instead of re-calling.
2. **Replay reproduces recorded behaviour, not recorded wall-clock timing.**
   Latency is deliberately reported as 0 for served calls, and the comparison is
   against the original.
3. **A run with no recording cannot be replayed.** `LoggedCompleter` raises
   rather than inventing a response, so an unreplayable run fails loudly.
