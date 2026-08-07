"""Ollama adapter - local models, genuinely zero cost.

This is the provider the demo runs on: no key, no quota, no network, and the
same model file every time, which is one less source of replay drift.
"""

import json
import time
from collections.abc import AsyncIterator

import httpx

from backend.providers.base import Completion, ProviderAdapter


class OllamaAdapter(ProviderAdapter):
    provider = "ollama"

    async def complete(
        self,
        messages: list[dict],
        model: str,
        *,
        temperature: float = 0.0,
        seed: int | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
    ) -> Completion:
        options: dict = {"temperature": temperature}
        if seed is not None:
            options["seed"] = seed
        if max_tokens:
            options["num_predict"] = max_tokens

        body: dict = {"model": model, "messages": messages, "stream": False, "options": options}
        if tools:
            body["tools"] = tools

        async def call() -> Completion:
            started = time.perf_counter()
            async with httpx.AsyncClient(base_url=self.handle.base_url, timeout=300.0) as c:
                r = await c.post("/api/chat", json=body)
                self._classify(r)
                data = r.json()
            elapsed = int((time.perf_counter() - started) * 1000)
            msg = data.get("message", {})
            return Completion(
                text=msg.get("content", ""),
                tokens_in=data.get("prompt_eval_count", 0),
                tokens_out=data.get("eval_count", 0),
                # Local inference has no marginal rupee cost. Recorded as a real
                # zero, not as a placeholder - see docs/reproducibility.md.
                cost_usd=0.0,
                latency_ms=elapsed,
                model=f"ollama:{model}",
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
        options: dict = {"temperature": temperature}
        if seed is not None:
            options["seed"] = seed
        body = {"model": model, "messages": messages, "stream": True, "options": options}
        async with httpx.AsyncClient(base_url=self.handle.base_url, timeout=300.0) as c:
            async with c.stream("POST", "/api/chat", json=body) as r:
                self._classify(r)
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    piece = chunk.get("message", {}).get("content")
                    if piece:
                        yield piece

    async def list_models(self) -> list[str]:
        async with httpx.AsyncClient(base_url=self.handle.base_url, timeout=30.0) as c:
            r = await c.get("/api/tags")
            self._classify(r)
            return [m["name"] for m in r.json().get("models", [])]
