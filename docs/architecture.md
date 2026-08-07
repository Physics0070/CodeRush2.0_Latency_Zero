# Architecture

## Why event sourcing

One append-only Postgres table is the single source of truth. This is the
decision the rest of the system falls out of.

Six requirements in the problem statement are, structurally, the same
requirement — they are all *reads* of one ordered log:

| Requirement | Implementation |
|---|---|
| Observability dashboard (62) | `SELECT * FROM events WHERE run_id = ?` |
| Replay a completed run (67) | Re-execute, serving completions from the log |
| All metrics (69) | Aggregate over event types |
| Trace viewer, pause/resume/cancel (74) | Tail the log via SSE; controls append events |
| Reproducibility report (77) | Compare two runs' logs |
| Marginal value of each agent (70) | Embed the validated outputs recorded in the log |

Building one structure well beat building six features shallowly.

**The rule:** no state change happens anywhere without an event row. If a value
exists only in a Python variable, it does not exist.

## Sequence allocation is atomic, in SQL

The obvious implementation of "next seq for this run" is
`SELECT MAX(seq)+1 FROM events WHERE run_id = ?`. It is wrong under concurrency:
two branches of a fanout read the same maximum and one loses on the primary key.

Instead `runs` carries a `last_seq` column and appends go through a function:

```sql
UPDATE runs SET last_seq = last_seq + 1 WHERE run_id = p_run_id
RETURNING last_seq INTO v_seq;
```

`UPDATE ... RETURNING` takes a row lock, so concurrent appends for one run queue
behind it and each gets a distinct, gapless number. One round trip, one
transaction. `tests/test_events.py` fires five appends concurrently and asserts
`[1,2,3,4,5]` — that test fails against the naive implementation.

Append-only is enforced by a trigger that raises on `UPDATE` and `DELETE`, not
by convention. RLS is on with no policies: the anon key sees nothing, and the
service key stays server-side.

## Schema

```
runs        run_id, goal, config_hash, seed, graph_spec, status,
            replay_of, last_seq, started_at, ended_at

events      run_id, seq, node_id, agent_id, event_type, payload,
            tokens_in, tokens_out, cost_usd, latency_ms, ts
            PRIMARY KEY (run_id, seq)

artifacts   artifact_id, run_id, seq, written_by, node_id,
            content, storage_path, ts
```

Provenance lives on every artifact: `written_by`, `run_id`, `seq`, `ts`. That
chain is what lets the trace viewer answer "where did this claim come from."

## Node types

| Type | Purpose |
|---|---|
| `clarify` | Ask up to 3 questions, then ask permission |
| `approval` | Human gate. Nothing downstream runs until granted |
| `fanout` | Split into independent branches |
| `agent` | One specialist, one model, one output schema |
| `join` | Fan-in, including partial failure |
| `conditional` | Branch on a prior node's output |
| `verify` | Council reviews merged output; escalates on disagreement |
| `council` | Propose → anonymous peer-rank → chairman merge |
| `compensate` | Runs when the node it compensates for fails |
| `subgraph` | One level of nesting (deliberately not recursive) |

## The execution contract

The problem statement's closing line says it grades *"the quality of the
execution contract between agents."* So this is the most important code in the
repository, and it was built before the compiler and before the UI.

On every handoff:

1. Producer emits output
2. Validate against `edge.handoff_schema` with `jsonschema`
3. On failure the **exact validator text** goes back to the producer, up to
   `MAX_REPAIR_ATTEMPTS`
4. Still failing → `BRANCH_FAILED` → the compensate node runs
5. Validated output is stored with provenance

Two design points worth defending:

- **All errors are collected, not just the first.** Fixing one field per round
  trip would exhaust the repair budget on a payload with three missing keys.
- **A markdown fence is stripped before judging.** Models wrap JSON in
  ```` ```json ````. That is a formatting artefact, not a contract violation,
  and rejecting it would burn repairs on nothing.

## Parallel-first execution

The DAG is layered by real data dependency (Kahn). Everything in a layer runs in
one `asyncio.gather`. A node drops to the next layer only when it genuinely
consumes an upstream output. Parallelism is the default; sequencing is the
exception.

**Parallel Efficiency** = Σ(node latencies) / wall clock.

An honest caveat we state rather than hide: every event append is a network
round trip charged to wall clock but *not* to node latency, so PE understates
parallelism for very fast nodes. At 0.6 s per branch it measured 1.89 despite
the branches provably overlapping by timestamp. At realistic agent latencies
(6–13 s) the overhead is noise and PE approaches the branch count.

Budgets are enforced in code. `budget_tokens` is counted per node and the call
is cut off; `timeout_s` is an `asyncio.timeout` around the node. The model is
never *asked* to respect a ceiling.

Node-level retry sits **outside** the repair loop deliberately: repair fixes
payload shape, retry covers a transient provider failure. Different faults,
different budgets.

## Conditions are parsed, never `eval`'d

A graph is model-authored. `eval()` on model-authored text is remote code
execution. Conditions are a fixed grammar: `<node_id>.<key> <op> <value>`.

## The council

`propose` → `peer_rank` (anonymous) → `chairman_merge`.

Anonymity is load-bearing. Labels are shuffled with the run seed, so the
assignment is reproducible for replay but does not track member order.
Authorship is re-attached only in the verdict.

Ranking is Borda, so second place still scores. A member that returns unusable
JSON abstains rather than corrupting the tally. Disagreement is the share of
rankers who disagreed on first place; above the threshold the verdict is flagged
`escalated` rather than silently passed.

Chairman is a **policy field**, not a constant: cheapest for a low-stakes merge,
strongest for verification.

The council appears in exactly three places: the graph compiler, the verify
node, and the conflict arbiter.

## The compiler treats the council as untrusted

A model-authored graph can name a node twice, point an edge at nothing, or omit
the verification node. The compiler de-duplicates branch ids, drops
non-identifiers, caps at four branches, and **guarantees** the fanout, the verify
node and per-branch compensators regardless of what the models returned. The
council supplies the shape; the compiler guarantees the invariants.

## Scope decisions

Deliberately minimal, and we say so:

- **Memory conflict resolution** is last-write-wins *with full provenance
  history*, so conflicts are visible and auditable. Not a CRDT.
- **Artifact store** is a Postgres table plus Supabase Storage, not a
  content-addressed blob system.

Deliberately deferred (the PS itself labels these "hard-mode"):

- Workflow version migration
- Nested subgraph recursion beyond one level
- Stale-memory eviction

We took the two hard-mode items with the highest demonstrable value —
adversarial blocking and conflict arbitration — and named the rest.
