"""Gemini Flash adapter."""

import json
import time
from collections.abc import AsyncIterator

import httpx

from backend.providers.base import Completion, ProviderAdapter, ProviderError

# Published Gemini Flash rates, USD per 1M tokens.
RATES = {"gemini-2.0-flash": (0.10, 0.40), "gemini-1.5-flash": (0.075, 0.30)}


def _to_gemini(messages: list[dict]) -> tuple[list[dict], str | None]:
    """Gemini splits the system prompt out and calls the assistant role 'model'."""
    system = None
    contents = []
    for m in messages:
        if m["role"] == "system":
            system = m["content"]
            continue
        role = "model" if m["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})
    return contents, system


class GeminiAdapter(ProviderAdapter):
    provider = "gemini"

    def _cost(self, model: str, tin: int, tout: int) -> float:
        rate = RATES.get(model)
        return round((tin * rate[0] + tout * rate[1]) / 1_000_000, 8) if rate else 0.0

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
        if not self.handle.configured:
            raise ProviderError("gemini is not configured (no key)")

        contents, system = _to_gemini(messages)
        gen: dict = {"temperature": temperature}
        if max_tokens:
            gen["maxOutputTokens"] = max_tokens
        if seed is not None:
            gen["seed"] = seed
        body: dict = {"contents": contents, "generationConfig": gen}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        async def call() -> Completion:
            started = time.perf_counter()
            async with httpx.AsyncClient(base_url=self.handle.base_url, timeout=120.0) as c:
                r = await c.post(
                    f"/models/{model}:generateContent",
                    json=body,
                    headers=self.handle.auth_headers(),
                )
                self._classify(r)
                data = r.json()
            elapsed = int((time.perf_counter() - started) * 1000)
            cands = data.get("candidates") or [{}]
            parts = cands[0].get("content", {}).get("parts") or [{}]
            usage = data.get("usageMetadata", {})
            tin = usage.get("promptTokenCount", 0)
            tout = usage.get("candidatesTokenCount", 0)
            return Completion(
                text=parts[0].get("text", ""),
                tokens_in=tin,
                tokens_out=tout,
                cost_usd=self._cost(model, tin, tout),
                latency_ms=elapsed,
                model=f"gemini:{model}",
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
        contents, system = _to_gemini(messages)
        body: dict = {"contents": contents, "generationConfig": {"temperature": temperature}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        async with httpx.AsyncClient(base_url=self.handle.base_url, timeout=120.0) as c:
            async with c.stream(
                "POST",
                f"/models/{model}:streamGenerateContent?alt=sse",
                json=body,
                headers=self.handle.auth_headers(),
            ) as r:
                self._classify(r)
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    chunk = json.loads(line[6:])
                    for cand in chunk.get("candidates", []):
                        for part in cand.get("content", {}).get("parts", []):
                            if part.get("text"):
                                yield part["text"]
