"""ByteDesk on-behalf-of (OBO) ``OutboundCredentialProvider`` (BDP-2434).

The bare-omnigent default (:class:`omnigent.identity.defaults.StaticSecretProvider`)
is identity-blind: it mints today's per-integration credential (e.g. the
``client_credentials`` MCP bearer) regardless of who is acting. ByteDesk contributes
this provider so that, when the acting identity carries the originating user's
``subject_token`` (their OpenIddict MCP access token) and the integration supplies
its ``oauth`` block, the egress presents an RFC 8693 token-exchange bearer that
acts *as* the user (``sub`` = user, ``act_sub`` = agent).

Degrade-to-default is the whole safety story: no ``subject_token`` on the identity,
no identity at all, or no ``oauth`` in the integration config ⇒ ``mint`` returns
``None``, so the outbound-credential registry falls back to the next provider
(ultimately the static-secret default) — i.e. today's ``client_credentials``
egress, unchanged.

This is the consumer layer the pluggable-identity ADR deferred: the seam
(:class:`omnigent.identity.ports.OutboundCredentialProvider`) and the strategy
(:class:`omnigent.identity.mint.TokenExchangeMintStrategy`) already exist; this
just selects the strategy from an :class:`~omnigent.identity.identity.ActingIdentity`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnigent.identity.identity import ActingIdentity
    from omnigent.identity.types import Credential

#: The mint strategy this provider selects (registered in
#: :data:`omnigent.identity.mint.MINT_REGISTRY`).
_TOKEN_EXCHANGE_STRATEGY = "token_exchange"


class OnBehalfOfCredentialProvider:
    """Select the ``token_exchange`` strategy when a subject_token is resolvable.

    Returns ``None`` (⇒ registry falls back to the default) when the on-behalf-of
    egress is not applicable — no acting identity, no ``subject_token`` on it, or
    no ``oauth`` block in the integration config.
    """

    name = "token_exchange_obo"

    def mint(
        self,
        *,
        identity: ActingIdentity | None,
        integration: str,
        config: Mapping[str, Any] | None = None,
    ) -> Credential | None:
        subject_token = getattr(identity, "subject_token", None) if identity is not None else None
        if not subject_token:
            return None
        if not config or config.get("oauth") is None:
            return None
        from omnigent.identity.mint import MINT_REGISTRY

        strategy = MINT_REGISTRY.get(_TOKEN_EXCHANGE_STRATEGY)
        return strategy.mint(
            identity=identity,
            integration=integration,
            config={"oauth": config["oauth"], "subject_token": subject_token},
        )


__all__ = ["OnBehalfOfCredentialProvider"]
