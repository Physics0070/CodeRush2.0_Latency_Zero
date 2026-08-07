"""Block 2 verification. Run: python -m backend.providers.smoke_test

Prints one real completion per configured provider with real token counts and
cost, then forces a failure on a bad model to show the fallback firing and
landing a RETRY row in the event log.
"""

import asyncio
import uuid

from backend.events import EventStore, EventType
from backend.providers import complete, configured_providers, get_handle
from backend.providers.ollama import OllamaAdapter

PROMPT = [{"role": "user", "content": "Reply with exactly: contract ok"}]


async def main() -> None:
    print("=" * 66)
    print("SECRET BROKER")
    print("=" * 66)
    for p in ("ollama", "groq", "gemini"):
        h = get_handle(p)
        # Printing the handle must never print a key - that is the point.
        print(f"  {p:8} {h}  base_url={h.base_url}")
    print(f"  configured: {configured_providers()}")

    local = await OllamaAdapter(get_handle("ollama")).list_models()
    print(f"  ollama models: {local}")

    print()
    print("=" * 66)
    print("REAL COMPLETIONS")
    print("=" * 66)

    specs = [f"ollama:{m}" for m in local if not m.endswith(":cloud")][:2]
    if "groq" in configured_providers():
        specs.append("groq:llama-3.3-70b-versatile")
    if "gemini" in configured_providers():
        specs.append("gemini:gemini-2.0-flash")

    async with EventStore() as store:
        run_id = str(uuid.uuid4())
        await store.create_run(run_id, "provider smoke test", "smoke", 42, {}, "running")

        for spec in specs:
            try:
                c = await complete(
                    spec, PROMPT, seed=42, max_tokens=32,
                    store=store, run_id=run_id, agent_id="smoke",
                )
                print(f"  {spec}")
                print(f"    text        : {c.text.strip()[:60]!r}")
                print(f"    tokens      : in={c.tokens_in} out={c.tokens_out}")
                print(f"    cost_usd    : {c.cost_usd}")
                print(f"    latency_ms  : {c.latency_ms}")
            except Exception as e:
                print(f"  {spec} FAILED: {type(e).__name__}: {e}")

        print()
        print("=" * 66)
        print("FALLBACK: primary model does not exist -> falls back to a real one")
        print("=" * 66)
        good = specs[0]
        c = await complete(
            "ollama:this-model-does-not-exist",
            PROMPT,
            fallback_model=good,
            seed=42,
            max_tokens=32,
            store=store,
            run_id=run_id,
            agent_id="fallback_demo",
        )
        print(f"  answered by  : {c.model}")
        print(f"  used_fallback: {c.used_fallback}")

        print()
        print("=" * 66)
        print("EVENT LOG")
        print("=" * 66)
        for e in await store.read(run_id):
            extra = ""
            if e.event_type == EventType.RETRY:
                extra = f"  {e.payload['primary']} -> {e.payload['fallback']}"
            elif e.event_type == EventType.TOOL_RESULT:
                extra = f"  {e.payload['model']} fallback={e.payload['used_fallback']}"
            print(
                f"  seq={e.seq:<3} {e.event_type:<14} tin={e.tokens_in:<5} "
                f"tout={e.tokens_out:<5} ${e.cost_usd:<10} {e.latency_ms}ms{extra}"
            )

        cost = await store.aggregate_cost(run_id)
        print(f"\n  totals: {cost.tokens_in} in / {cost.tokens_out} out / ${cost.cost_usd}")
        await store.set_run_status(run_id, "done", ended=True)


if __name__ == "__main__":
    asyncio.run(main())
