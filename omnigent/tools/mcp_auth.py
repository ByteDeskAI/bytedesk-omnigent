"""
MCP HTTP auth scheme provider.

Resolving HTTP headers for an MCP connection used to be a fixed
sequential if-chain in :mod:`omnigent.tools.mcp` (explicit ``headers``,
then a Databricks profile token, then an OAuth token). Adding a new
auth method (SigV4, mTLS, API key) meant editing that chain.

This module replaces the chain with an :class:`McpAuthScheme` Protocol
(ADR-0008 Strategy) and an ordered :class:`McpAuthRegistry`. Each scheme
inspects the :class:`~omnigent.spec.types.MCPServerConfig` and mutates a
shared, already-populated headers dict. The registry applies the schemes
in registration order, and every scheme uses ``setdefault`` so an
earlier (higher-precedence) writer always wins.

Precedence is preserved byte-for-byte from the old if-chain:

    explicit config ``headers``  >  databricks-profile token  >  oauth token

The explicit headers seed the dict before any scheme runs (mirroring the
old ``merged = dict(self.config.headers)`` start), so the two built-in
schemes only ever fill an absent ``Authorization``.

New schemes register against :data:`DEFAULT_MCP_AUTH_REGISTRY` (or a
private registry) without touching the call site.

Imports stay light (typing + the spec config + the two token resolvers)
because this runs on every MCP connect/reconnect.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnigent.spec.types import MCPServerConfig


@runtime_checkable
class McpAuthScheme(Protocol):
    """Strategy that contributes HTTP auth headers for an MCP server.

    A scheme is consulted once per header-resolution. It decides from
    *config* whether it applies and, if so, mutates *headers* in place
    using ``setdefault`` so a higher-precedence scheme that already wrote
    a key is never overwritten.
    """

    def apply(self, config: MCPServerConfig, headers: dict[str, str]) -> None:
        """Add this scheme's headers to *headers* when *config* enables it."""
        ...


class DatabricksProfileAuthScheme:
    """Add a Databricks-profile OAuth bearer token.

    Active when ``config.databricks_profile`` is set. Resolves a fresh
    token on each call so reconnects pick up rotated credentials, and
    only fills ``Authorization`` when no higher-precedence scheme (an
    explicit header) already set it.
    """

    def apply(self, config: MCPServerConfig, headers: dict[str, str]) -> None:
        if config.databricks_profile is None:
            return
        # Imported lazily: the Databricks SDK is heavy and only needed
        # when a profile is actually configured.
        from omnigent.tools.mcp import _resolve_databricks_token

        token = _resolve_databricks_token(config.databricks_profile)
        # Explicit Authorization header wins — don't overwrite.
        headers.setdefault("Authorization", f"Bearer {token}")


class OAuthAuthScheme:
    """Add a generic OAuth bearer token.

    Active when ``config.oauth`` is set. Resolves the token fresh on each
    call and only fills ``Authorization`` when no higher-precedence
    scheme (an explicit header or a Databricks token) already set it.
    """

    def apply(self, config: MCPServerConfig, headers: dict[str, str]) -> None:
        if config.oauth is None:
            return
        from omnigent.tools.mcp import _resolve_oauth_token

        token = _resolve_oauth_token(config.oauth)
        # Explicit Authorization header (or a databricks token) wins.
        headers.setdefault("Authorization", f"Bearer {token}")


class McpAuthRegistry:
    """Ordered registry of :class:`McpAuthScheme` strategies.

    Schemes apply in registration order; precedence flows from order +
    each scheme's ``setdefault``, so the first writer of a header wins.
    The explicit config ``headers`` are seeded before any scheme runs,
    making them the highest-precedence source.
    """

    def __init__(self, schemes: list[McpAuthScheme] | None = None) -> None:
        self._schemes: list[McpAuthScheme] = list(schemes) if schemes else []

    def register(self, scheme: McpAuthScheme) -> None:
        """Append *scheme* to the end of the ordered scheme list."""
        self._schemes.append(scheme)

    def resolve_headers(self, config: MCPServerConfig) -> dict[str, str] | None:
        """
        Build the HTTP headers for an MCP connection.

        Seeds the dict with the explicit config ``headers`` (highest
        precedence), then lets each registered scheme contribute via
        ``setdefault``.

        :param config: The MCP server config.
        :returns: Merged headers dict, or ``None`` if no headers are
            needed (empty config headers and no scheme contributed).
        """
        merged = dict(config.headers) if config.headers else {}
        for scheme in self._schemes:
            scheme.apply(config, merged)
        return merged or None


def _build_default_registry() -> McpAuthRegistry:
    """Build the registry with the two built-in schemes in precedence order."""
    return McpAuthRegistry(
        [
            DatabricksProfileAuthScheme(),
            OAuthAuthScheme(),
        ]
    )


#: Process-wide registry used by :meth:`mcp.McpServerConnection._resolve_http_headers`.
#: Register additional schemes (SigV4, mTLS, API key) here.
DEFAULT_MCP_AUTH_REGISTRY = _build_default_registry()
