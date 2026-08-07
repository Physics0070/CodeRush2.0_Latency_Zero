# Example runs

Three saved runs, exported straight from the event log with
`python -m backend.demo --export`. Each file is the complete, unedited event
sequence for one run — not a summary, and not hand-written.

| File | What it shows | Events |
|---|---|---|
| `run_clean.json` | Three parallel branches → join → verify, all handoffs valid | 30 |
| `run_recovered.json` | One branch fails schema validation, repairs are attempted, then compensation runs | 33 |
| `run_adversarial.json` | Undeclared tool call blocked, smuggled instruction quarantined | 4 |

## Reading them

Each file has three keys:

```json
{
  "run":    { "run_id": …, "config_hash": …, "seed": 42, "status": … },
  "cost":   { "tokens_in": …, "tokens_out": …, "cost_usd": …, "events": … },
  "events": [ { "seq": 1, "event_type": "RUN_START", … }, … ]
}
```

`seq` is gapless and monotonic per run — that is enforced in SQL, not in Python.

## What to look for

**`run_clean.json`** — find the `NODE_START` rows for `security`, `quality` and
`docs`. Their `ts` values overlap: those three branches ran concurrently. The
`RUN_END` payload carries `parallel_efficiency`.

**`run_recovered.json`** — follow one node through the contract:

```
HANDOFF_EMITTED    → the producer's raw output
HANDOFF_REJECTED   → the exact jsonschema errors
REPAIR_ATTEMPT     → those same errors sent back to the producer
HANDOFF_EMITTED    → its corrected attempt
HANDOFF_REJECTED   → still wrong
BRANCH_FAILED      → repair budget exhausted
COMPENSATE         → the compensation node runs
```

The whole graded execution contract is legible in seven rows.

**`run_adversarial.json`** — two rows matter:

- `TOOL_BLOCKED` — `attempted_tool: "file_delete"`, `agent_id: "red_agent"`,
  `declared_tools: ["read_file", "list_files"]`
- `INJECTION_BLOCKED` — three rules fired on one payload
  (`override_instruction`, `exfiltration`, `privilege_escalation`),
  `action: "quarantined"`

## A note on how these were produced

The clean and recovered runs use a **fixture completer** with fixed outputs, so
the committed files are stable artefacts that do not change on every export.
The agent outputs in them are therefore illustrative, not model-generated.

Everything else in the files is real: real event rows from the real Supabase
event log, real sequence numbers, real timestamps, real schema validation, real
compensation. The adversarial run is entirely real — the guard and the screen
are the actual code paths.

To generate a run with real model output instead:

```bash
python -m backend.demo --live
```
