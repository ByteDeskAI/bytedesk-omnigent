"""In-box default port implementations + the actor-resolution helper.

These are what a bare omnigent (no extensions installed) uses, so the product
works standalone:

- :class:`StaticSecretProvider` — default ``OutboundCredentialProvider``: selects
  a :class:`~omnigent.identity.ports.MintStrategy` (``static`` unless config names
  another) and delegates. Identity-blind by default = today's per-integration
  secret behaviour (degrade-to-default when ``identity is None``).
- :class:`OwnerAllowAuthorizer` — default ``AuthorizationProvider``: allow
  (standalone single-user owner model). Ships default-only per the ADR.
- :func:`acting_identity_for` — the one home for actor resolution (a plain
  function, not a port: it has no second impl yet).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

# Annotation-only; avoids importing the server Principal at module import.
from typing import TYPE_CHECKING, Any

from omnigent.identity.identity import ActingIdentity
from omnigent.identity.mint import MINT_REGISTRY
from omnigent.identity.types import Credential, Decision

if TYPE_CHECKING:
    from omnigent.server.principal import Principal


class StaticSecretProvider:
    """Default ``OutboundCredentialProvider`` — per-integration static secret.

    Selects the :class:`~omnigent.identity.ports.MintStrategy` named in
    ``config['strategy']`` (default ``"static"``) and returns its credential. A
    caller that passes no ``config`` and no identity gets exactly today's
    behaviour once a ``secret_ref`` is supplied; ``None`` config yields ``None``
    (no credential resolvable) rather than raising.
    """

    name = "static_secret"

    def mint(
        self,
        *,
        identity: ActingIdentity | None,
        integration: str,
        config: Mapping[str, Any] | None = None,
    ) -> Credential | None:
        if config is None:
            return None
        strategy_name = config.get("strategy", "static")
        strategy = MINT_REGISTRY.get(strategy_name)
        return strategy.mint(identity=identity, integration=integration, config=config)


class OwnerAllowAuthorizer:
    """Default ``AuthorizationProvider`` — allow (standalone owner model).

    Standalone omnigent is single-user: the owner may do anything. A consumer
    swaps in a capability-enforcing authorizer; per the ADR this default-only
    seam is not load-bearing pluggability until that second impl exists.
    """

    name = "owner_allow"

    def decide(self, *, identity: ActingIdentity | None, action: str, resource: str) -> Decision:
        del identity, action, resource  # interface args; the owner-allow default ignores them
        return Decision(allowed=True, reason="standalone owner-allow default")


def acting_identity_for(
    principal: Principal | None = None,
    agent_id: str | None = None,
    delegation: Iterable[str] = (),
    subject_token: str | None = None,
) -> ActingIdentity:
    """Resolve the :class:`ActingIdentity` for a request (the actor-resolution seam).

    A plain function, not a port: standalone resolution is a pure pass-through of
    the inbound principal + the running agent id. A consumer that needs to map an
    agent to its own platform account replaces the call site, not a registry.

    *subject_token* (default ``None``) is the originating user's outbound access
    token, carried for an on-behalf-of egress; absent ⇒ today's behaviour.
    """
    return ActingIdentity(
        principal=principal,
        agent_id=agent_id,
        delegation=tuple(delegation),
        subject_token=subject_token,
    )


__all__ = ["OwnerAllowAuthorizer", "StaticSecretProvider", "acting_identity_for"]
