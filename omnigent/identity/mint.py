"""Credential mint strategies + their registry (the *how* of acting-as).

Consolidates the three credential paths omnigent already uses for egress behind
one :class:`~omnigent.identity.ports.MintStrategy` Strategy registry — it is a
*relocation of live behaviour*, not new machinery:

- ``static`` — resolve a per-integration secret via
  :func:`omnigent.onboarding.secrets.load_secret` (the per-service API-key path).
- ``client_credentials`` — mint an OAuth 2.0 bearer via
  :func:`omnigent.tools.mcp._resolve_oauth_token` (the OpenIddict/MCP path).
- ``pass_through`` — resolve a Databricks profile token via
  :func:`omnigent.tools.mcp._resolve_databricks_token`.

The default provider (:class:`omnigent.identity.defaults.StaticSecretProvider`)
selects ``static`` unless a config names another strategy, so a tool that does
not pass an identity keeps minting exactly the credential it does today
(degrade-to-default). The ``identity`` argument is accepted by every strategy
but the in-box strategies ignore it — a future token-exchange/OBO strategy is
the first to read it.

Heavy egress helpers are imported lazily inside each ``mint`` so importing this
module (and therefore the registry / capability manifest) stays light.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from omnigent.identity.types import Credential
from omnigent.pluggable.registry import PluggableRegistry

if TYPE_CHECKING:
    from omnigent.identity.identity import ActingIdentity
    from omnigent.identity.ports import MintStrategy


def _bearer(value: str, scheme: str = "Bearer") -> str:
    """Format a credential header value (``"<scheme> <value>"``, or raw if no scheme)."""
    return f"{scheme} {value}" if scheme else value


class StaticMintStrategy:
    """Per-integration static secret via ``load_secret`` (today's API-key path).

    Config: ``secret_ref`` (required) — the ``load_secret`` name; optional
    ``scheme`` (default ``"Bearer"``) and ``header_name`` (default
    ``"Authorization"``).
    """

    name = "static"

    def mint(
        self,
        *,
        identity: ActingIdentity | None,
        integration: str,
        config: Mapping[str, Any],
    ) -> Credential:
        del identity  # interface arg; the static egress is identity-blind (degrade-to-default)
        from omnigent.onboarding.secrets import load_secret

        ref = config.get("secret_ref")
        if not ref:
            raise ValueError(f"static mint for {integration!r} requires config['secret_ref']")
        value = load_secret(ref)
        if value is None:
            raise RuntimeError(f"no secret stored under {ref!r} for {integration!r}")
        scheme = config.get("scheme", "Bearer")
        return Credential(
            header_value=_bearer(value, scheme),
            header_name=config.get("header_name", "Authorization"),
        )


class ClientCredentialsMintStrategy:
    """OAuth 2.0 ``client_credentials`` bearer (today's MCP/OpenIddict path).

    Config: ``oauth`` (required) — an :class:`omnigent.spec.types.MCPOAuthConfig`.
    """

    name = "client_credentials"

    def mint(
        self,
        *,
        identity: ActingIdentity | None,
        integration: str,
        config: Mapping[str, Any],
    ) -> Credential:
        del identity  # interface arg; client-credentials is identity-blind (degrade-to-default)
        from omnigent.tools.mcp import _resolve_oauth_token

        oauth = config.get("oauth")
        if oauth is None:
            raise ValueError(
                f"client_credentials mint for {integration!r} requires config['oauth']"
            )
        token = _resolve_oauth_token(oauth)
        return Credential(header_value=_bearer(token))


class PassThroughMintStrategy:
    """Databricks profile token (today's pass-through path).

    Config: ``profile`` (required) — the Databricks CLI profile name.
    """

    name = "pass_through"

    def mint(
        self,
        *,
        identity: ActingIdentity | None,
        integration: str,
        config: Mapping[str, Any],
    ) -> Credential:
        del identity  # interface arg; pass-through is identity-blind (degrade-to-default)
        from omnigent.tools.mcp import _resolve_databricks_token

        profile = config.get("profile")
        if not profile:
            raise ValueError(f"pass_through mint for {integration!r} requires config['profile']")
        token = _resolve_databricks_token(profile)
        return Credential(header_value=_bearer(token))


def build_mint_registry() -> PluggableRegistry[MintStrategy]:
    """Build the mint-strategy registry (``static`` default + the other two)."""
    registry: PluggableRegistry[MintStrategy] = PluggableRegistry(
        "mint_strategy", default=("static", StaticMintStrategy)
    )
    registry.register("client_credentials", ClientCredentialsMintStrategy)
    registry.register("pass_through", PassThroughMintStrategy)
    return registry


#: Module-level mint-strategy registry (singleton; strategies are stateless).
MINT_REGISTRY: PluggableRegistry[MintStrategy] = build_mint_registry()


__all__ = [
    "MINT_REGISTRY",
    "ClientCredentialsMintStrategy",
    "PassThroughMintStrategy",
    "StaticMintStrategy",
    "build_mint_registry",
]
