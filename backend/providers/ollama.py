"""Ollama adapter - one code path for Ollama Cloud and a local/tunnelled
instance. Which one is in play is entirely a matter of `self.handle`: base URL
and optional bearer key come from secret_broker, this class has no opinion on
where the model actually runs. Verified against Ollama's own docs: both cloud
and local serve the same native `/api/chat` path (no `/v1` OpenAI-compat
suffix needed here), cloud requires `Authorization: Bearer <key>`, local
typically has no key and must see no Authorization header at all.
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
        json_mode: bool = False,
    ) -> Completion:
        options: dict = {"temperature": temperature}
        if seed is not None:
            options["seed"] = seed
        if max_tokens:
            options["num_predict"] = max_tokens

        body: dict = {"model": model, "messages": messages, "stream": False, "options": options}
        if tools:
            body["tools"] = tools
        if json_mode:
            # Grammar-constrained decoding. A 3B model asked politely for JSON
            # returns prose about half the time; asked this way it cannot emit
            # anything else.
            body["format"] = "json"

        async def call() -> Completion:
            started = time.perf_counter()
            async with httpx.AsyncClient(base_url=self.handle.base_url, timeout=300.0) as c:
                r = await c.post("/api/chat", json=body, headers=self.handle.auth_headers())
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
            async with c.stream(
                "POST", "/api/chat", json=body, headers=self.handle.auth_headers()
            ) as r:
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
            r = await c.get("/api/tags", headers=self.handle.auth_headers())
            self._classify(r)
            return [m["name"] for m in r.json().get("models", [])]
