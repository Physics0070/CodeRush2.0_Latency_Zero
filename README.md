# Agent Council Orchestrator

**CodeRush 2.0 · Problem statement AE-03 — Unified Agent Form Orchestrator**
Team **Latency Zero** · VIT Pune

**Ask it anything and it answers, like any assistant. Then it shows you what
the answer cost.**

Simple questions go straight to one model and stream back in about two seconds.
Harder ones are split across specialists that run in parallel, and the answers
are merged into one reply. You get prose, not a findings table — and under every
answer, the measured truth about how it was produced.

For heavier work there is a second surface: type a goal, and the system asks
clarifying questions, asks permission, designs an agent team as a visual graph,
shows it for approval, executes it parallel-first, streams live state, and
replays any past run with a provably identical result.

Runs at **₹0** — local models via Ollama, Supabase free tier, local embeddings.

---

## What makes it different

**0. It decides how hard your question is, and spends accordingly.**
A council of four is not free. The router reads what you actually asked and
picks the cheapest shape that answers it well — most messages skip the council
entirely. It also derives the specialists *from your question*: ask about
locking in a ledger and you get `locking_strategies` and `ledger_architecture`,
not a generic security/quality/docs template.

**1. Agents pass typed forms, not chat messages.**
Every edge in the graph carries a JSON Schema. A wrong shape is rejected, the
*exact* validator error goes back to the producing agent, and it retries. Like a
hospital handover chart with required fields, not "the patient seems unwell."

**2. A council, not a single boss.**
Several models each propose, then **peer-review each other anonymously** and
rank. A chairman merges. Authorship is stripped before ranking — a reviewer
shown "proposal by qwen2.5" rates the label, not the content.

**3. It reports when it is overkill.**
We measure whether more agents actually helped, and say so when they did not.
An agent scoring MIG below 0.1 produced nothing the others did not already have,
and the system names it out loud.

---

## Architecture in one paragraph

One append-only Postgres table is the single source of truth. Observability,
replay, every metric, the trace viewer, pause/resume and the reproducibility
report are all *reads* of that table. No state change happens anywhere without
an event row.

```
                        ┌─ simple ──▶ one model ─────────────────▶ answer
question ─▶ router ─────┤
                        └─ moderate/deep ─▶ fanout ─┬─▶ specialist ─┐
                                                    ├─▶ specialist ─┼─▶ merge ─▶ answer
                                                    └─▶ specialist ─┘
                                                      (concurrent)

goal ─▶ clarify ─▶ approval ─▶ fanout ─┬─▶ analyst ─┐
                                       ├─▶ analyst ─┼─▶ join ─▶ verify ─▶ report
                                       └─▶ analyst ─┘
                                        (concurrent)     (council)
```

The specialists are named by the router from your question, not from a fixed
list. Both paths write to the same event log, so a chat turn is as replayable
and as measurable as a full orchestrated run.

Full detail: [docs/architecture.md](docs/architecture.md).

---

## Setup

Prerequisites: **Python 3.11+**, **Node 20+**, and [Ollama](https://ollama.com)
for local models.

```bash
git clone https://github.com/Physics0070/CodeRush2.0_Latency_Zero.git
cd CodeRush2.0_Latency_Zero

# 1. Backend
python3.11 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Optional: semantic metrics (marginal information gain, duplicate detection).
# ~1GB. Skip it and the app runs fully, reporting embeddings_available: false.
pip install -r requirements-embeddings.txt \
    --extra-index-url https://download.pytorch.org/whl/cpu

# 2. Local models (free, no API key)
ollama pull llama3.2:3b
ollama pull qwen2.5:7b

# 3. Configuration
cp .env.example .env                 # then fill in SUPABASE_URL + SUPABASE_SERVICE_KEY

# 4. Database — paste migrations/001_events.sql into the Supabase SQL editor

# 5. Frontend
cd frontend && npm install && npm run build && cd ..

# 6. Run
uvicorn backend.main:app --host 0.0.0.0 --port 7860
```

Open <http://localhost:7860>.

> Blank values in `.env` fall back to defaults, so `cp .env.example .env` boots
> without editing anything except the two Supabase keys.

### Troubleshooting

**`pip install` succeeded but the import still fails.** Almost always the wrong
interpreter. If a newer Python is first on `PATH`, a bare `pip`/`python` installs
there instead of into the venv. Check which one you are actually using:

```bash
.venv/Scripts/python -c "import sys; print(sys.executable, sys.version)"
```

Always install with `.venv/Scripts/python -m pip install …` rather than bare
`pip`, so the interpreter and the target are the same by construction.

**The torch install hangs.** `download.pytorch.org` is slow or blocked on some
networks — we measured zero bytes in 120 seconds, and separately a 50-minute
stall. Symptom: pip sits at "Collecting torch" with no progress bar movement.

This is why torch is **not** in `requirements.txt`. It is optional:

```bash
# add explicit timeouts so pip gives up and retries instead of hanging
pip install --retries 8 --timeout 60 -r requirements-embeddings.txt \
    --extra-index-url https://download.pytorch.org/whl/cpu
```

Torch is needed **only** for the two semantic metrics. Everything else — the
chat, the event log, typed handoffs, the engine, the council, permissions,
replay, the UI, and every timing and cost number — runs without it. Skip it and
`/api/runs/{id}/metrics` still returns, with `embeddings_available: false` and
marginal information gain and duplicate pairs omitted rather than guessed.

Note the CPU index carries no cp311 linux wheel above 2.6.0, so
`requirements-embeddings.txt` uses a range rather than an exact pin — a pin
that resolves on Windows can be unsatisfiable in a linux container.

**`ModuleNotFoundError: No module named 'backend'`.** Run from the repository
root, not from inside `backend/`. `pytest` reads `pythonpath = ["."]` from
`pyproject.toml`.

**Ollama connection refused.** Start it (`ollama serve`) and confirm the models
are present with `ollama list`.

**Tests skip with "supabase not configured".** `.env` is missing
`SUPABASE_URL` / `SUPABASE_SERVICE_KEY`, or the migration has not been applied.

### Environment

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SUPABASE_URL` | yes | — | Event log, runs, artifacts |
| `SUPABASE_SERVICE_KEY` | yes | — | Server-side only, never sent to the browser |
| `OLLAMA_BASE_URL` | no | `http://localhost:11434` | Local models |
| `GROQ_API_KEY` | no | — | Optional faster council member |
| `GEMINI_API_KEY` | no | — | Optional council member |
| `MAX_REPAIR_ATTEMPTS` | no | `2` | Repair retries before a branch fails |
| `DEFAULT_SEED` | no | `42` | Recorded per run; required for replay |
| `DEFAULT_BUDGET_TOKENS` | no | `8000` | Engine-enforced, per agent |
| `DEFAULT_TIMEOUT_S` | no | `60` | Engine-enforced, per agent |
| `SECRET_KEY` | prod only | — | Prod refuses to boot without it |
| `CORS_ORIGINS` | no | `http://localhost:5173` | Explicit allow-list; `*` is rejected |
| `RATE_LIMIT_*` | no | see `.env.example` | No threshold is hardcoded |

Provider keys are read by **one module**, `backend/providers/secret_broker.py`.
Nowhere else. They never enter a log line, an event payload, or the frontend
bundle.

---

## Running the demo

### Chat (the default surface)

1. Ask *"What is a race condition?"* → answers in ~2s, routed `simple`,
   no council convened
2. Ask *"Compare optimistic and pessimistic locking for a high-write ledger"* →
   routed `moderate`, two specialists named from the question run in parallel,
   answers merge into one reply
3. Click **benchmarks** under either answer → route, timings, tokens, cost, and
   the measured overlap between specialists

The second question is the one to show: the system reports when its own
specialists said the same thing, which is a claim nobody else's demo makes
about itself.

### Orchestrator (the `orchestrator` tab)

1. Type: *"Audit this repository and produce a prioritized remediation report."*
2. Answer the clarifying questions → approve
3. Council compiles a graph — parallel branches and a verify node appear
4. Inspect a node → lock → **Approve & run**
5. Watch typed handoffs validate live in the trace
6. Click **Red Agent** → `TOOL_BLOCKED` and `INJECTION_BLOCKED` in red
7. Click **Replay** → diff is zero, cost compared side by side
8. Click **Marginal value** → the system recommends a graph size

---

## Verification

```bash
pytest -v                    # full suite
pytest -m "not slow"         # skips the live-model tests
python -m backend.providers.smoke_test    # real completions + fallback
python -m backend.benchmarks.show         # real SWE-bench + BFCL rows
ruff check .
pip-audit -r requirements.txt --strict
```

---

## Data

| Source | Licence | Access |
|---|---|---|
| [SWE-bench Verified](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified) | CC BY 4.0 | datasets-server API, real rows |
| [BFCL](https://github.com/ShishirPatil/gorilla) | Apache 2.0 | JSONL — `load_dataset()` does **not** work |

No sample record, finding, or metric in this repository is fabricated. Where a
code path needs data we do not have, it raises rather than inventing.

---

## Documentation

- [docs/architecture.md](docs/architecture.md) — event sourcing, node types, data model
- [docs/threat-model.md](docs/threat-model.md) — capability enforcement, injection screening, secret broker
- [docs/reproducibility.md](docs/reproducibility.md) — config_hash, seed, zero-diff worked example
- [examples/](examples/) — saved runs: clean, recovered, adversarial

## Licence

[Apache 2.0](LICENSE).
