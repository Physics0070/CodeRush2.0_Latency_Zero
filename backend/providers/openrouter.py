"""OpenRouter adapter - OpenAI-compatible chat completions.

One key, hundreds of underlying models across many labs - which is what makes
it a good fit for the council's diversity requirement (different model
families genuinely have different blind spots; same-family members do not).
Defaults are chosen from OpenRouter's free tier: verified live, one at a time,
against real completions before being wired in as defaults - several
`:free` models were tried and rejected (see backend/api/routes.py) for
truncating mid-answer, taking 100s+ on hidden reasoning tokens, or erroring
under shared free-tier load. This adapter itself has no opinion on which
model string it's given; that judgment lives in _default_models().
"""

import json
import time
from collections.abc import AsyncIterator

import httpx

from backend.providers.base import Completion, ProviderAdapter, ProviderError, ToolCall

# `:free`-suffixed models cost nothing; OpenRouter's catalog is too large to
# hardcode paid rates for arbitrary models, so an unrecognised paid model
# reports 0 rather than guessing - same convention as the Groq/Gemini adapters.
RATES: dict[str, tuple[float, float]] = {}


class OpenRouterAdapter(ProviderAdapter):
    provider = "openrouter"

    def _cost(self, model: str, tin: int, tout: int) -> float:
        if model.endswith(":free"):
            return 0.0
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
            raise ProviderError("openrouter is not configured (no key)")

        body: dict = {"model": model, "messages": messages, "temperature": temperature}
        if seed is not None:
            body["seed"] = seed
        if max_tokens:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = tools
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
                model=f"openrouter:{model}",
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
