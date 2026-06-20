"""Tests for the MCP HTTP auth scheme provider registry (BDP-2362).

The registry replaces the old sequential if-chain in
``mcp._resolve_http_headers``. These tests pin:

- the two built-in schemes resolve the same headers as before;
- precedence: explicit header > databricks-profile > oauth;
- a newly registered (fake) scheme applies.
"""

from __future__ import annotations

import omnigent.tools.mcp as mcp_mod
from omnigent.spec.types import MCPOAuthConfig, MCPServerConfig
from omnigent.tools.mcp_auth import (
    DatabricksProfileAuthScheme,
    McpAuthRegistry,
    McpAuthScheme,
    OAuthAuthScheme,
    _build_default_registry,
)


def _cfg(**kw) -> MCPServerConfig:
    return MCPServerConfig(name="p", url="http://x/mcp", **kw)


def test_default_registry_schemes_satisfy_protocol() -> None:
    assert isinstance(DatabricksProfileAuthScheme(), McpAuthScheme)
    assert isinstance(OAuthAuthScheme(), McpAuthScheme)


def test_empty_config_resolves_to_none() -> None:
    reg = _build_default_registry()
    assert reg.resolve_headers(_cfg()) is None


def test_explicit_headers_only_pass_through() -> None:
    reg = _build_default_registry()
    headers = reg.resolve_headers(_cfg(headers={"X-Custom": "v"}))
    assert headers == {"X-Custom": "v"}


def test_databricks_profile_injects_bearer(monkeypatch) -> None:
    monkeypatch.setattr(mcp_mod, "_resolve_databricks_token", lambda profile: "dbx-tok")
    reg = _build_default_registry()
    headers = reg.resolve_headers(_cfg(databricks_profile="prod"))
    assert headers == {"Authorization": "Bearer dbx-tok"}


def test_oauth_injects_bearer(monkeypatch) -> None:
    monkeypatch.setattr(mcp_mod, "_resolve_oauth_token", lambda oauth: "oauth-tok")
    reg = _build_default_registry()
    headers = reg.resolve_headers(
        _cfg(oauth=MCPOAuthConfig(token_url="http://t", client_id="c"))
    )
    assert headers == {"Authorization": "Bearer oauth-tok"}


def test_precedence_databricks_wins_over_oauth(monkeypatch) -> None:
    monkeypatch.setattr(mcp_mod, "_resolve_databricks_token", lambda profile: "dbx-tok")
    monkeypatch.setattr(mcp_mod, "_resolve_oauth_token", lambda oauth: "oauth-tok")
    reg = _build_default_registry()
    headers = reg.resolve_headers(
        _cfg(
            databricks_profile="prod",
            oauth=MCPOAuthConfig(token_url="http://t", client_id="c"),
        )
    )
    # Databricks registered first → its setdefault wins.
    assert headers == {"Authorization": "Bearer dbx-tok"}


def test_precedence_explicit_header_wins_over_both(monkeypatch) -> None:
    monkeypatch.setattr(mcp_mod, "_resolve_databricks_token", lambda profile: "dbx-tok")
    monkeypatch.setattr(mcp_mod, "_resolve_oauth_token", lambda oauth: "oauth-tok")
    reg = _build_default_registry()
    headers = reg.resolve_headers(
        _cfg(
            headers={"Authorization": "Bearer explicit"},
            databricks_profile="prod",
            oauth=MCPOAuthConfig(token_url="http://t", client_id="c"),
        )
    )
    assert headers == {"Authorization": "Bearer explicit"}


def test_new_scheme_registers_and_applies() -> None:
    class ApiKeyScheme:
        def apply(self, config: MCPServerConfig, headers: dict[str, str]) -> None:
            headers.setdefault("X-Api-Key", "secret-key")

    reg = McpAuthRegistry()
    reg.register(ApiKeyScheme())
    headers = reg.resolve_headers(_cfg())
    assert headers == {"X-Api-Key": "secret-key"}


def test_registration_order_determines_precedence() -> None:
    class FirstScheme:
        def apply(self, config: MCPServerConfig, headers: dict[str, str]) -> None:
            headers.setdefault("Authorization", "Bearer first")

    class SecondScheme:
        def apply(self, config: MCPServerConfig, headers: dict[str, str]) -> None:
            headers.setdefault("Authorization", "Bearer second")

    reg = McpAuthRegistry([FirstScheme(), SecondScheme()])
    assert reg.resolve_headers(_cfg()) == {"Authorization": "Bearer first"}
