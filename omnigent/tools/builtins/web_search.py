"""Built-in tool: unified web search.

Backend selection is fully determined by the agent spec:

- **OpenAI model** → passthrough to OpenAI's native
  ``web_search_preview`` (server-side, uses the LLM API key).
- **Other models** → requires ``search_provider`` in config
  (``"google"`` or ``"perplexity"``) with the appropriate
  credentials. No env var fallbacks — the spec is self-contained.

Usage in config.yaml::

    # OpenAI model — web search is built-in, no config needed:
    tools:
      builtins:
        - web_search

    # Non-OpenAI model — must specify search_provider + credentials:
    tools:
      builtins:
        - name: web_search
          search_provider: perplexity
          api_key: ${PERPLEXITY_API_KEY}
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from omnigent.tools.base import Tool, ToolContext

if TYPE_CHECKING:
    from omnigent.pluggable import PluggableRegistry

_logger = logging.getLogger(__name__)


@runtime_checkable
class WebSearchProvider(Protocol):
    """One web-search backend (the ``web_search`` pluggable seam, BDP-2348).

    A provider runs a query using credentials carried in the agent's spec
    config and returns a formatted, human-readable result string. Providers
    are selected by name from ``config["search_provider"]`` via the seam's
    :class:`~omnigent.pluggable.PluggableRegistry`; new backends (Brave,
    SearXNG, Tavily, Bing, self-hosted) plug in by registration alone.
    """

    def search(self, query: str, config: dict[str, str]) -> str:
        """Run *query* using *config* credentials; return a result string."""
        ...


class WebSearchTool(Tool):
    """
    Unified web search tool with backend determined by the agent spec.

    When the agent uses an OpenAI model, this emits the native
    ``web_search_preview`` passthrough schema. For other models,
    the spec must set ``search_provider`` to ``"google"`` or
    ``"perplexity"`` with credentials — there is no env var
    fallback because the agent spec must be self-contained.

    :param config: Spec-level config from config.yaml, e.g.
        ``{"search_provider": "perplexity", "api_key": "pplx-..."}``.
    :param llm_provider: The LLM provider name extracted from
        the model string, e.g. ``"openai"`` or ``"anthropic"``.
        When ``None``, falls back to function-tool mode.
    """

    def __init__(
        self,
        config: dict[str, str] | None = None,
        llm_provider: str | None = None,
    ) -> None:
        """
        Create a unified web search tool.

        :param config: Spec-level config with ``search_provider``
            and credentials. Required for non-OpenAI models.
        :param llm_provider: The agent's LLM provider, e.g.
            ``"openai"``. Determines whether to use passthrough
            or function-tool mode.
        """
        self._config = config or {}
        self._is_openai = llm_provider == "openai"

    @classmethod
    def name(cls) -> str:
        """
        :returns: ``"web_search"``.
        """
        return "web_search"

    @classmethod
    def description(cls) -> str:
        """
        :returns: Human-readable description of the tool.
        """
        return (
            "Quick web search — returns a comprehensive "
            "list of result links and snippets from a "
            "search engine. Good for broad discovery and "
            "finding URLs, but results may be slightly "
            "delayed vs. live web. For reading full page "
            "content or fetching the latest info from a "
            "specific URL, use web_fetch instead."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the tool schema, varying by provider.

        For OpenAI, returns the native ``web_search_preview``
        passthrough. For others, returns a function schema.

        :returns: OpenAI-format tool schema dict.
        """
        if self._is_openai:
            return {"type": "web_search_preview"}

        return {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": (
                    "Quick web search — returns a comprehensive "
                    "list of result links and snippets from a "
                    "search engine. Good for broad discovery and "
                    "finding URLs, but results may be slightly "
                    "delayed vs. live web. For reading full page "
                    "content or fetching the latest info from a "
                    "specific URL, use web_fetch instead."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The search query.",
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    def is_async(self, arguments: str | None = None) -> bool:
        """
        Run web_search synchronously in the parent's tool loop.

        :param arguments: Ignored — async-ness is a property of
            this tool, not the per-call arguments.
        :returns: ``False`` — web_search always runs synchronously.
        """
        del arguments
        return False

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Execute a web search query.

        For OpenAI, this should never be called (passthrough).
        For others, delegates to the backend specified by
        ``search_provider`` in the spec config.

        :param arguments: JSON-encoded dict with a ``query`` key.
        :param ctx: Tool execution context.
        :returns: Search results or an error message.
        """
        if self._is_openai:
            raise RuntimeError(
                "web_search in OpenAI mode is a passthrough — "
                "the provider handles execution server-side. "
                "invoke() should never be called."
            )

        parsed: dict[str, Any] = json.loads(arguments)
        query = parsed.get("query")
        if not query:
            return "Error: 'query' parameter is required."

        return _search(query, self._config)


_UNCONFIGURED_HELP = (
    "web_search requires configuration for non-OpenAI models. "
    "(For OpenAI models, web_search works automatically with no "
    "config needed.)\n\n"
    "Set search_provider and credentials in config.yaml:\n"
    "  tools:\n"
    "    builtins:\n"
    "      - name: web_search\n"
    "        search_provider: perplexity  # or google\n"
    "        api_key: ${PERPLEXITY_API_KEY}\n\n"
    "Supported backends:\n"
    "  - google (requires api_key + engine_id)\n"
    "  - perplexity (requires api_key)"
)


class _GoogleProvider:
    """Built-in ``google`` provider — Google Custom Search."""

    def search(self, query: str, config: dict[str, str]) -> str:
        """
        Run a Google Custom Search query using spec config credentials.

        :param query: The search query.
        :param config: Must contain ``api_key`` and ``engine_id``.
        :returns: Formatted results or an error message.
        """
        from omnigent.tools.builtins.web_search_google import (
            _search_google,
        )

        api_key = config.get("api_key")
        engine_id = config.get("engine_id")
        if not api_key or not engine_id:
            return "Google web search requires api_key and engine_id in the web_search config."

        return _search_google(query, config)


class _PerplexityProvider:
    """Built-in ``perplexity`` provider — Perplexity online search."""

    def search(self, query: str, config: dict[str, str]) -> str:
        """
        Run a Perplexity search query using spec config credentials.

        :param query: The search query.
        :param config: Must contain ``api_key``.
        :returns: Answer with citations or an error message.
        """
        from omnigent.tools.builtins.web_search_perplexity import (
            _search_perplexity,
        )

        api_key = config.get("api_key")
        if not api_key:
            return "Perplexity web search requires api_key in the web_search config."

        return _search_perplexity(query, config)


def _build_provider_registry() -> PluggableRegistry[WebSearchProvider]:
    """Build the ``web_search`` provider seam registry, keyed by provider name.

    The two built-in providers (``google``, ``perplexity``) are registered;
    selection happens by name from ``config["search_provider"]``. There is no
    registered default — selection is always explicit (an empty
    ``search_provider`` is handled by :func:`_search` as the unconfigured help
    path, preserving the historical message). Extensions can contribute
    providers (Brave/SearXNG/Tavily/Bing/self-hosted) via a
    ``web_search_providers`` hook.

    :returns: The provider registry for the ``web_search`` seam.
    """
    from omnigent.pluggable import PluggableRegistry

    registry: PluggableRegistry[WebSearchProvider] = PluggableRegistry("web_search")
    registry.register("google", _GoogleProvider)
    registry.register("perplexity", _PerplexityProvider)
    # Extension discovery deferred to server startup (Wave-2 composition root):
    # it loads FastAPI-heavy entry-point extensions; keep off the import hot path.
    # Hook: 'web_search_providers'.
    return registry


def _search(query: str, config: dict[str, str]) -> str:
    """
    Run a web search using the backend specified in config.

    The ``search_provider`` key in config selects a registered
    :class:`WebSearchProvider` via the seam's
    :class:`~omnigent.pluggable.PluggableRegistry`. No env var fallbacks —
    the spec must be self-contained.

    :param query: The search query string.
    :param config: Spec-level config. Required keys:

        - ``search_provider``: a registered provider name (e.g. ``"google"``
          or ``"perplexity"``)
        - ``api_key``: API key for the chosen backend
        - ``engine_id``: Required for Google only

    :returns: Search results, or an error message.
    :raises ProviderNotRegistered: if ``search_provider`` names an unknown
        provider (an empty/missing value returns the unconfigured help
        message instead).
    """
    backend = config.get("search_provider")
    if not backend:
        return _UNCONFIGURED_HELP

    provider = _build_provider_registry().get(backend)
    return provider.search(query, config)
