"""Provider registry. Instances are cached per backend name so HTTP clients
(and their connection pools) are reused across passes and turns."""

from __future__ import annotations

from ..config import SETTINGS, BackendConfig, Settings
from .base import (
    GenRequest,
    GenResult,
    Provider,
    ProviderError,
    ReasoningDelta,
    estimate_tokens,
)
from .echo import EchoProvider
from .horde import HordeProvider
from .ollama import LlamaCppProvider, OllamaProvider
from .openai_compat import OpenAICompatProvider

PROVIDERS: dict[str, type[Provider]] = {
    "echo": EchoProvider,
    "ollama": OllamaProvider,
    "llamacpp": LlamaCppProvider,
    "openai": OpenAICompatProvider,
    "horde": HordeProvider,
}

_cache: dict[str, Provider] = {}


def build(config: BackendConfig) -> Provider:
    cls = PROVIDERS.get(config.kind)
    if cls is None:
        raise ProviderError(f"unknown backend kind {config.kind!r}")
    return cls(config)


def get_provider(name: str, settings: Settings | None = None) -> Provider:
    settings = settings or SETTINGS
    config = settings.backend(name)
    cached = _cache.get(name)
    if cached is not None and cached.config == config:
        return cached
    provider = build(config)
    _cache[name] = provider
    return provider


def provider_for_tier(tier: str, settings: Settings | None = None) -> Provider:
    settings = settings or SETTINGS
    return get_provider(settings.backend_for_tier(tier).name, settings)


async def close_all() -> None:
    for provider in list(_cache.values()):
        await provider.aclose()
    _cache.clear()


__all__ = [
    "GenRequest",
    "GenResult",
    "Provider",
    "ProviderError",
    "ReasoningDelta",
    "build",
    "close_all",
    "estimate_tokens",
    "get_provider",
    "provider_for_tier",
]
