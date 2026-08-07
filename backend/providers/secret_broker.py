"""The only module in this repo that reads a provider API key.

Grep any key name and exactly one file comes back. Agents receive a
`ProviderHandle`, never a raw key: the handle carries the base URL and knows how
to build an auth header, but the key itself never crosses a return boundary,
never enters an event payload, never reaches a log line, never ships to the
frontend.

Keys are read from os.environ here rather than through backend.config on
purpose - config.py deliberately does not declare them, so there is no second
place they could leak from.
"""

import os
from dataclasses import dataclass, field

from backend.config import settings


# `repr=False` on the secret field: a stray f-string or traceback that prints a
# handle cannot print the key.
@dataclass(frozen=True)
class ProviderHandle:
    provider: str
    base_url: str
    _secret: str | None = field(default=None, repr=False)

    @property
    def configured(self) -> bool:
        """Local providers need no key; remote ones do."""
        return self.provider == "ollama" or bool(self._secret)

    def auth_headers(self) -> dict[str, str]:
        """Built at call time, handed straight to httpx, never stored.

        Ollama is deliberately included here, not defaulted to it: a local or
        tunnelled instance has no key and must see no Authorization header at
        all (some dev proxies reject an empty/garbage bearer token), while
        Ollama Cloud requires one. `not self._secret` above already covers
        "no key configured" for both cases.
        """
        if not self._secret:
            return {}
        if self.provider in ("groq", "openrouter", "ollama"):
            return {"Authorization": f"Bearer {self._secret}"}
        if self.provider == "gemini":
            return {"x-goog-api-key": self._secret}
        return {}

    def __str__(self) -> str:
        return f"<ProviderHandle {self.provider} configured={self.configured}>"

    __repr__ = __str__


_SPEC = {
    "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1"),
    "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1"),
    "gemini": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta"),
    # OLLAMA_API_KEY is optional (see config.py) - present for Ollama Cloud,
    # absent for a local/tunnelled instance.
    "ollama": ("OLLAMA_API_KEY", "http://localhost:11434"),
}


def get_handle(provider: str) -> ProviderHandle:
    if provider not in _SPEC:
        raise ValueError(f"unknown provider {provider!r}")
    env_key, default_url = _SPEC[provider]

    if provider == "ollama":
        # Base URL is not a secret, so it comes from settings (backend.config
        # is the single source for it) rather than a second os.environ read
        # here. Falls back to localhost only when unset - config.py's prod
        # validator refuses to boot with it blank in prod, so this fallback
        # only ever fires in dev.
        base_url = settings.ollama_base_url or default_url
        return ProviderHandle(provider, base_url, os.environ.get(env_key) or None)

    return ProviderHandle(provider, default_url, os.environ.get(env_key) or None)


def configured_providers() -> list[str]:
    """Which providers can actually be called right now."""
    return [p for p in _SPEC if get_handle(p).configured]


def assert_no_key_in(text: str) -> None:
    """Guard for the event-log write path: refuse to persist a payload that
    contains a live key. Cheap, and it turns a silent leak into a crash."""
    for env_key, _ in _SPEC.values():
        if not env_key:
            continue
        secret = os.environ.get(env_key)
        if secret and len(secret) > 8 and secret in text:
            raise RuntimeError(f"refusing to persist payload containing {env_key}")
