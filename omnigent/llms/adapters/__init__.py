"""
Adapter registry — maps provider names to adapter instances.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.llms.adapters.base import BaseAdapter

# Lazy-initialized adapter cache. Each provider gets at most one
# adapter instance per process. The concrete connection-params shape
# varies per adapter, so the registry erases it to ``BaseAdapter[Any]``.
_adapter_cache: dict[str, BaseAdapter[Any]] = {}


def get_adapter(provider: str, **kwargs: Any) -> BaseAdapter[Any]:
    """
    Return an adapter instance for the given provider.

    Adapters are cached — the first call creates the instance and
    subsequent calls return the same one.

    :param provider: The provider identifier, e.g. ``"anthropic"``.
    :param kwargs: Extra keyword arguments forwarded to the adapter
        constructor (used by tests to override config).
    :returns: A :class:`BaseAdapter` subclass instance.
    :raises OmnigentError: If the provider is not supported.
    """
    if provider in _adapter_cache and not kwargs:
        return _adapter_cache[provider]

    adapter = _create_adapter(provider, **kwargs)
    if not kwargs:
        _adapter_cache[provider] = adapter
    return adapter


# OpenAI-compatible providers — default base URLs only. API keys come from
# connection_params at call time, not env vars. ``openai`` itself uses the
# Responses-API subclass; the rest share ``OpenAICompatibleAdapter``.
_OPENAI_COMPAT_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "xai": "https://api.x.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://localhost:11434/v1",
    "moonshot": "https://api.moonshot.cn/v1",
}


def _make_openai_compat_factory(
    provider: str, default_base_url: str
) -> Callable[..., BaseAdapter[Any]]:
    """Build the lazy factory for one OpenAI-compatible provider."""

    def factory(**kwargs: Any) -> BaseAdapter[Any]:
        resolved_url = kwargs.get("base_url", default_base_url)
        if provider == "openai":
            # OpenAI supports the Responses API natively; use the subclass
            # that calls /v1/responses directly so reasoning token events
            # (reasoning_summary_text.delta etc.) flow through.
            from omnigent.llms.adapters.openai import OpenAIAdapter

            return OpenAIAdapter(base_url=resolved_url)
        from omnigent.llms.adapters.openai import OpenAICompatibleAdapter

        return OpenAICompatibleAdapter(base_url=resolved_url)

    return factory


def _anthropic_factory(**kwargs: Any) -> BaseAdapter[Any]:
    from omnigent.llms.adapters.anthropic import AnthropicAdapter

    return AnthropicAdapter(**kwargs)


def _gemini_factory(**kwargs: Any) -> BaseAdapter[Any]:
    from omnigent.llms.adapters.gemini import GeminiAdapter

    return GeminiAdapter(**kwargs)


def _bedrock_factory(**kwargs: Any) -> BaseAdapter[Any]:
    from omnigent.llms.adapters.bedrock import BedrockAdapter

    return BedrockAdapter(**kwargs)


def _vertex_factory(**kwargs: Any) -> BaseAdapter[Any]:
    from omnigent.llms.adapters.vertex import VertexAdapter

    return VertexAdapter(**kwargs)


def _databricks_factory(**kwargs: Any) -> BaseAdapter[Any]:
    from omnigent.llms.adapters.databricks import DatabricksAdapter

    return DatabricksAdapter(**kwargs)


# Provider → lazy adapter factory. This dict is the single source of truth for
# "which providers are supported": ``get_adapter``'s error message and any
# supported-list consumer derive from its keys, so adding a provider can never
# drift out of sync with the supported set. Imports stay inside the factories
# so optional deps (boto3, google-auth) load only when their provider is used.
_ADAPTER_FACTORIES: dict[str, Callable[..., BaseAdapter[Any]]] = {
    **{
        provider: _make_openai_compat_factory(provider, base_url)
        for provider, base_url in _OPENAI_COMPAT_BASE_URLS.items()
    },
    "anthropic": _anthropic_factory,
    "gemini": _gemini_factory,
    "bedrock": _bedrock_factory,
    "vertex": _vertex_factory,
    "databricks": _databricks_factory,
}


def supported_providers() -> list[str]:
    """Return the sorted list of provider identifiers ``get_adapter`` accepts."""
    return sorted(_ADAPTER_FACTORIES)


def _create_adapter(provider: str, **kwargs: Any) -> BaseAdapter[Any]:
    """
    Instantiate the correct adapter for the provider.

    Dispatch is a registry lookup against :data:`_ADAPTER_FACTORIES`; each
    factory lazily imports its adapter so optional dependencies are not pulled
    in until their provider is requested.

    :param provider: The provider identifier.
    :param kwargs: Extra kwargs for the adapter constructor.
    :returns: A :class:`BaseAdapter` instance.
    :raises OmnigentError: If the provider is not registered.
    """
    factory = _ADAPTER_FACTORIES.get(provider)
    if factory is None:
        raise OmnigentError(
            f"Unknown provider {provider!r}. Supported: {supported_providers()}",
            code=ErrorCode.INVALID_INPUT,
        )
    return factory(**kwargs)


def clear_cache() -> None:
    """
    Clear the adapter cache. Useful for tests.
    """
    _adapter_cache.clear()
