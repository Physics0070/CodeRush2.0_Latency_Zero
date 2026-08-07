"""Model-string routing and the fallback path.

An agent declares `model: "ollama:qwen2.5:7b"`. Swapping that string to
`"groq:llama-3.3-70b-versatile"` is the entire change needed to move an agent
between providers - the graph, the schemas and the engine are untouched.
"""

import logging

from backend.events import EventStore, EventType
from backend.providers.base import Completion, ProviderAdapter, ProviderError
from backend.providers.gemini import GeminiAdapter
from backend.providers.groq import GroqAdapter
from backend.providers.ollama import OllamaAdapter
from backend.providers.secret_broker import get_handle

log = logging.getLogger("aco.providers")

_ADAPTERS: dict[str, type[ProviderAdapter]] = {
    "ollama": OllamaAdapter,
    "groq": GroqAdapter,
    "gemini": GeminiAdapter,
}


def parse_model(spec: str) -> tuple[str, str]:
    """'ollama:qwen2.5:7b' -> ('ollama', 'qwen2.5:7b').

    Split once only: Ollama tags contain colons, so splitting on every colon
    would turn 'qwen2.5:7b' into 'qwen2.5'.
    """
    provider, sep, model = spec.partition(":")
    if not sep or not model:
        raise ValueError(f"model must be 'provider:model', got {spec!r}")
    if provider not in _ADAPTERS:
        raise ValueError(f"unknown provider {provider!r} in {spec!r}")
    return provider, model


def adapter_for(spec: str) -> tuple[ProviderAdapter, str]:
    provider, model = parse_model(spec)
    return _ADAPTERS[provider](get_handle(provider)), model


async def complete(
    spec: str,
    messages: list[dict],
    *,
    fallback_model: str | None = None,
    temperature: float = 0.0,
    seed: int | None = None,
    max_tokens: int | None = None,
    tools: list[dict] | None = None,
    store: EventStore | None = None,
    run_id: str | None = None,
    node_id: str | None = None,
    agent_id: str | None = None,
) -> Completion:
    """Call `spec`; on failure fall back once to `fallback_model`.

    Both the attempt and the fallback are written to the event log with real
    token counts, so cost and latency in the dashboard are measured, not
    estimated.
    """
    adapter, model = adapter_for(spec)
    try:
        result = await adapter.complete(
            messages,
            model,
            temperature=temperature,
            seed=seed,
            max_tokens=max_tokens,
            tools=tools,
        )
    except ProviderError as primary_error:
        if not fallback_model:
            if store and run_id:
                await store.append(
                    run_id,
                    EventType.BRANCH_FAILED,
                    node_id=node_id,
                    agent_id=agent_id,
                    payload={"model": spec, "error": str(primary_error), "fallback": None},
                )
            raise

        log.warning(
            "primary %s failed (%s), falling back to %s", spec, primary_error, fallback_model
        )
        if store and run_id:
            await store.append(
                run_id,
                EventType.RETRY,
                node_id=node_id,
                agent_id=agent_id,
                payload={
                    "reason": "provider_failure",
                    "primary": spec,
                    "fallback": fallback_model,
                    "status": primary_error.status,
                    "error": str(primary_error),
                },
            )
        fb_adapter, fb_model = adapter_for(fallback_model)
        result = await fb_adapter.complete(
            messages,
            fb_model,
            temperature=temperature,
            seed=seed,
            max_tokens=max_tokens,
            tools=tools,
        )
        result.used_fallback = True

    # Deliberately does NOT write TOOL_RESULT. Replay depends on those rows, so
    # recording them is the engine's guarantee (see engine/executor.py) rather
    # than the provider layer's - otherwise swapping in a different completer
    # would silently make a run unreplayable.
    return result
