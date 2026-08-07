"""Groq adapter - OpenAI-compatible chat completions."""

import json
import time
from collections.abc import AsyncIterator

import httpx

from backend.providers.base import Completion, ProviderAdapter, ProviderError, ToolCall

# Published Groq rates, USD per 1M tokens. Used only to report real spend; if a
# model is missing we record 0 rather than guessing a number.
RATES = {
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
}


class GroqAdapter(ProviderAdapter):
    provider = "groq"

    def _cost(self, model: str, tin: int, tout: int) -> float:
        rate = RATES.get(model)
        if not rate:
            return 0.0
        return round((tin * rate[0] + tout * rate[1]) / 1_000_000, 8)

    async def complete(
        self,
        messages: list[dict],
        model: str,
        *,
        temperature: float = 0.0,
        seed: int | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        json_mode: bool = False,
    ) -> Completion:
        if not self.handle.configured:
            raise ProviderError("groq is not configured (no key)")

        body: dict = {"model": model, "messages": messages, "temperature": temperature}
        if seed is not None:
            body["seed"] = seed
        if max_tokens:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = tools
        # Groq rejects response_format alongside tools, and tool calls are
        # already JSON, so json_mode only applies to plain completions.
        if json_mode and not tools:
            body["response_format"] = {"type": "json_object"}

        async def call() -> Completion:
            started = time.perf_counter()
            async with httpx.AsyncClient(base_url=self.handle.base_url, timeout=120.0) as c:
                r = await c.post(
                    "/chat/completions", json=body, headers=self.handle.auth_headers()
                )
                self._classify(r)
                data = r.json()
            elapsed = int((time.perf_counter() - started) * 1000)
            choice = data["choices"][0]["message"]
            usage = data.get("usage", {})
            tin = usage.get("prompt_tokens", 0)
            tout = usage.get("completion_tokens", 0)
            calls = [
                ToolCall(
                    name=t["function"]["name"],
                    arguments=json.loads(t["function"]["arguments"]),
                )
                for t in choice.get("tool_calls") or []
            ]
            return Completion(
                text=choice.get("content") or "",
                tokens_in=tin,
                tokens_out=tout,
                cost_usd=self._cost(model, tin, tout),
                latency_ms=elapsed,
                model=f"groq:{model}",
                tool_calls=calls,
            )

        return await self._with_backoff(call)

    async def stream(
        self,
        messages: list[dict],
        model: str,
        *,
        temperature: float = 0.0,
        seed: int | None = None,
    ) -> AsyncIterator[str]:
        body: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if seed is not None:
            body["seed"] = seed
        async with httpx.AsyncClient(base_url=self.handle.base_url, timeout=120.0) as c:
            async with c.stream(
                "POST", "/chat/completions", json=body, headers=self.handle.auth_headers()
            ) as r:
                self._classify(r)
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:].strip()
                    if raw == "[DONE]":
                        break
                    delta = json.loads(raw)["choices"][0].get("delta", {}).get("content")
                    if delta:
                        yield delta
