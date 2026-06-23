"""The pluggable identity ports (Protocols).

Each port is a ``@runtime_checkable`` :class:`typing.Protocol` so a duck-typed
implementation (default or extension-contributed) satisfies it without a base
class. Method bodies live in :mod:`omnigent.identity.verifiers`,
:mod:`omnigent.identity.mint`, and :mod:`omnigent.identity.defaults`; the
registries that select an active impl live in :mod:`omnigent.identity.registry`.

``from __future__ import annotations`` keeps the value-object types as strings,
so importing this module pulls no runtime dependency.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from omnigent.identity.identity import ActingIdentity
from omnigent.identity.types import Credential, Decision


@runtime_checkable
class AssertionVerifier(Protocol):
    """Decide whether an inbound identity *assertion* is trusted (the trust subpart).

    Pulled out of the principal resolver so the trust mechanism (shared-HMAC vs
    asymmetric JWKS vs OIDC introspection) is swappable without rewriting how the
    header is parsed or how claims map to a principal.
    """

    name: str

    def verify(self, header: str) -> dict[str, Any] | None:
        """Return the verified payload dict, or ``None`` (fail-closed) on any failure."""
        ...


@runtime_checkable
class MintStrategy(Protocol):
    """*How* one outbound credential is minted (static / client-credentials / …).

    Selected by an :class:`OutboundCredentialProvider`; the strategy is the GoF
    *Strategy* under the provider's *Bridge*.
    """

    name: str

    def mint(
        self,
        *,
        identity: ActingIdentity | None,
        integration: str,
        config: Mapping[str, Any],
    ) -> Credential:
        """Mint a :class:`Credential` for *integration* using *config*."""
        ...


@runtime_checkable
class OutboundCredentialProvider(Protocol):
    """Resolve the credential a tool presents when it *acts as* an identity.

    Given the acting identity + the target integration (+ its config), select a
    :class:`MintStrategy` and return a :class:`Credential`. The default
    reproduces today's per-integration static-secret behaviour; a consumer swaps
    in a token-exchange/OBO provider later.
    """

    name: str

    def mint(
        self,
        *,
        identity: ActingIdentity | None,
        integration: str,
        config: Mapping[str, Any] | None = None,
    ) -> Credential | None:
        """Return a :class:`Credential` for *integration*, or ``None`` if unavailable."""
        ...


@runtime_checkable
class AuthorizationProvider(Protocol):
    """Decide whether *identity* may perform *action* on *resource*.

    The default allows (standalone single-user owner model); a consumer swaps in
    a capability-enforcing impl. Per the ADR this ships default-only as a typed
    seam the propagation can populate — not load-bearing pluggability until a
    second impl lands.
    """

    name: str

    def decide(self, *, identity: ActingIdentity | None, action: str, resource: str) -> Decision:
        """Return an allow/deny :class:`Decision` for the action."""
        ...


__all__ = [
    "AssertionVerifier",
    "AuthorizationProvider",
    "MintStrategy",
    "OutboundCredentialProvider",
]
