# Threat Model

The problem statement's safety boundary: *"Every agent and tool must have
explicit capabilities, budgets, and approval semantics. API keys belong in a
secret broker; local-model execution must remain inside the declared workspace.
The orchestrator must never turn a user goal into uncontrolled parallel side
effects."*

Each clause below maps to a control, a test, and an event type.

---

## 1. Capability enforcement

**Threat.** A compromised or confused agent calls a tool it was never granted —
`file_delete`, a network egress, a write.

**Control.** Every tool call goes through `CapabilityGuard.call()`. The agent's
declared `tools` list is the entire allow-list. Anything else is refused, and a
`TOOL_BLOCKED` event records the attempted tool name, the agent id, the
arguments and the declared list.

**Why orchestrator-side.** Asking a model not to call a tool is a prompt.
Refusing to execute it is a control. Prompts are advisory; the guard is not.

A tool that is *declared but not registered* is also refused — a declaration is
not an implementation.

**Verified.** `tests/test_permissions.py`, and live via `POST /api/demo/red-agent`:

```json
{
  "attempted_tool": "file_delete",
  "agent_id": "red_agent",
  "declared_tools": ["read_file", "list_files"],
  "arguments": {"path": "backend/config.py"},
  "reason": "outside declared capabilities"
}
```

---

## 2. Prompt-injection screening on handoffs

**Threat.** Agent A's output is untrusted input to agent B. A poisoned payload
carries instructions that hijack B — "ignore previous instructions", a fake
`system:` turn, a request to reveal the system prompt or an API key.

**Control.** Payloads are screened **before** entering the downstream context,
not after the model has read them. Detection triggers `INJECTION_BLOCKED` and
quarantine.

Quarantine is **per upstream node**: one poisoned branch is dropped and healthy
siblings still reach the consumer. Failing the whole join would turn an
injection attempt into a denial of service.

Patterns covered: instruction override, role markers, chat-template tokens,
tool-call syntax, exfiltration phrasing, privilege escalation, destructive
imperatives.

**Stated limitation.** This is pattern matching. It is a demonstrable control,
not a solution to prompt injection — a determined attacker gets past regexes.
What *is* guaranteed is that a detected attempt is blocked, quarantined, and
visible in the trace. We say this out loud rather than overclaiming.

**Verified.** Live, three rules caught in one payload:

```json
{
  "from_node": "red", "into_node": "writer", "action": "quarantined",
  "rules": ["override_instruction", "exfiltration", "privilege_escalation"],
  "hits": [
    {"rule": "override_instruction", "match": "Ignore all previous instruction"},
    {"rule": "exfiltration", "match": "reveal your system prompt"},
    {"rule": "privilege_escalation", "match": "You are now"}
  ]
}
```

---

## 3. Secret broker

**Threat.** An API key reaches a log line, an event payload, a model context, or
the frontend bundle.

**Control.** `backend/providers/secret_broker.py` is the only module that reads
a key. Grep any key name and one file comes back.

- Agents receive a `ProviderHandle`, never a raw key.
- The secret is a `repr=False` dataclass field, so printing a handle or hitting
  a traceback cannot surface it. `str(handle)` gives
  `<ProviderHandle groq configured=True>`.
- Auth headers are built at call time and handed straight to httpx.
- `config.py` deliberately does **not** declare provider keys, so there is no
  second place they could leak from.
- `assert_no_key_in()` guards the artifact write path: a payload that somehow
  contains a live key crashes rather than being persisted.
- The service key is server-side only; the browser talks to our API, never to
  Supabase.

---

## 4. Budgets

**Threat.** A runaway agent burns the token budget or hangs the run.

**Control.** `budget_tokens` is counted per node by the engine and the call is
cut off. `timeout_s` is an `asyncio.timeout` around the whole node. Exceeded →
`BUDGET_EXCEEDED` → branch fails → compensation.

Enforced in code, never suggested to the model.

---

## 5. No uncontrolled parallel side effects

**Threat.** "Clean up this repo" fans out into eight concurrent agents that all
delete files.

**Control.** Any node with a non-empty `allowed_side_effects` must have an
`approval` node upstream. `GraphSpec.validate_side_effects()` walks the
ancestors, and the executor **refuses to start** the run if any side-effecting
node is unguarded. Read-only nodes fan out freely.

The compiler emits read-only agents by default, so the default graph is safe by
construction.

---

## 6. Web surface

| Area | Control |
|---|---|
| Input validation | Pydantic on every endpoint — type, length, format. Non-conforming input is **rejected** (422), never sanitised-and-continued |
| Rate limiting | `slowapi`, all thresholds from env vars, nothing hardcoded |
| Error handling | Global handler returns `{"error":"internal_error"}`. Stack traces, file paths and raw DB errors never reach a client; full detail to server logs |
| CORS | Explicit allow-list; `*` raises at startup |
| Prod boot | Refuses to start without `SECRET_KEY` and Supabase config, so a shipped default can never silently become the live one |
| API docs | Disabled when `APP_ENV=prod` |
| Dependencies | `pip-audit -r requirements.txt --strict` clean; 25 CVEs across 5 packages were found and fixed during the build |
| Secrets in git | `.env` gitignored and verified absent; CI fails if it is ever tracked |
| Auth library | `PyJWT`, not `python-jose` |

---

## 7. Data integrity

- `events` is append-only, enforced by trigger.
- Replay refuses to run if the stored graph no longer hashes to the stored
  `config_hash` — a replay that silently accepted a mutated spec would report a
  zero diff that means nothing.

---

## Known gaps, stated

1. **Injection screening is pattern-based** (§2). Not a solved problem.
2. **No authentication.** The demo is single-tenant. Rate limiting is per-IP;
   there are no accounts, so there is no per-account limiting yet.
3. **Artifacts over 200 KB** record a truncation marker rather than uploading to
   Supabase Storage. No demo artifact approaches that size, and an untested
   upload path is worse than a stated gap. Marked `TODO(block-12)`.
4. **Tool registry is minimal.** The guard is proven; the library of real tools
   behind it is small.
