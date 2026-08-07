"""Provider abstraction: per-agent model selection, fallback, secrets isolation."""

from backend.providers.base import Completion, ProviderAdapter, ProviderError, ToolCall
from backend.providers.registry import adapter_for, complete, parse_model
from backend.providers.secret_broker import ProviderHandle, configured_providers, get_handle

__all__ = [
    "Completion",
    "ProviderAdapter",
    "ProviderError",
    "ProviderHandle",
    "ToolCall",
    "adapter_for",
    "complete",
    "configured_providers",
    "get_handle",
    "parse_model",
]
