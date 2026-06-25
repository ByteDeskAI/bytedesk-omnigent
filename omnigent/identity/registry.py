"""Per-port :class:`~omnigent.kernel.pluggable.registry.PluggableRegistry` instances.

One registry per identity seam, each constructed with its in-box default so
``resolve_default()`` returns a working impl with zero extensions installed (the
"acts as a product standalone" guarantee, satisfied by construction). Each
registry also gets an ``OMNIGENT_USE_<SEAM>`` strangler env and an extension
discovery hook (wired into :data:`omnigent.kernel.pluggable.manifest.SEAMS`), so a
consumer can swap any subpart per-environment or via an installed extension.

Built per call (cheap; impls are stateless) rather than as module singletons, so
discovery re-runs and tests get a fresh registry. The mint-strategy registry
(:data:`omnigent.identity.mint.MINT_REGISTRY`) is the one nested singleton.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnigent.identity.defaults import OwnerAllowAuthorizer, StaticSecretProvider
from omnigent.identity.verifiers import HmacAssertionVerifier
from omnigent.kernel.pluggable.registry import PluggableRegistry

if TYPE_CHECKING:
    from omnigent.identity.ports import (
        AssertionVerifier,
        AuthorizationProvider,
        OutboundCredentialProvider,
    )

#: Seam ids (also the ``OMNIGENT_USE_<SEAM>`` suffix and the manifest key).
ASSERTION_VERIFIER_SEAM = "assertion_verifier"
OUTBOUND_CREDENTIAL_SEAM = "outbound_credential"
AUTHORIZER_SEAM = "authorizer"

#: Extension hook method names (the third column of the SEAMS table).
ASSERTION_VERIFIER_HOOK = "assertion_verifiers"
OUTBOUND_CREDENTIAL_HOOK = "outbound_credential_providers"
AUTHORIZER_HOOK = "authorization_providers"


def build_assertion_verifier_registry() -> PluggableRegistry[AssertionVerifier]:
    """Registry for the inbound-trust seam (default: unconfigured ``hmac``)."""
    return PluggableRegistry(
        ASSERTION_VERIFIER_SEAM, default=("hmac", HmacAssertionVerifier.from_env)
    )


def build_outbound_credential_registry() -> PluggableRegistry[OutboundCredentialProvider]:
    """Registry for the act-as egress seam (default: ``static_secret``)."""
    return PluggableRegistry(
        OUTBOUND_CREDENTIAL_SEAM, default=("static_secret", StaticSecretProvider)
    )


def build_authorizer_registry() -> PluggableRegistry[AuthorizationProvider]:
    """Registry for the authorization seam (default: ``owner_allow``)."""
    return PluggableRegistry(AUTHORIZER_SEAM, default=("owner_allow", OwnerAllowAuthorizer))


__all__ = [
    "ASSERTION_VERIFIER_HOOK",
    "ASSERTION_VERIFIER_SEAM",
    "AUTHORIZER_HOOK",
    "AUTHORIZER_SEAM",
    "OUTBOUND_CREDENTIAL_HOOK",
    "OUTBOUND_CREDENTIAL_SEAM",
    "build_assertion_verifier_registry",
    "build_authorizer_registry",
    "build_outbound_credential_registry",
]
