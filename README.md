# Agent Council Orchestrator

**CodeRush 2.0 · Problem statement AE-03 — Unified Agent Form Orchestrator**
Team **Latency Zero** · VIT Pune

A control room for teams of AI agents. You type one goal in plain English. The
system asks clarifying questions, asks permission, designs an agent team as a
visual graph, shows it for approval, executes it parallel-first, streams live
state, and replays any past run with a provably identical result.

Human input is three things: the goal, answers to clarifying questions, and one
approve click. Everything else is automatic.

Runs at **₹0** — local models via Ollama, Supabase free tier, local embeddings.

---

## What makes it different

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
goal ─▶ clarify ─▶ approval ─▶ fanout ─┬─▶ security ─┐
                                       ├─▶ quality  ─┼─▶ join ─▶ verify ─▶ report
                                       └─▶ docs     ─┘
                                       (concurrent)      (council)
```

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
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt

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
