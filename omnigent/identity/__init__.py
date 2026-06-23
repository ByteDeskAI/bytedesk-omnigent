"""Pluggable identity & auth seams for omnigent (product-first).

See ``docs/architecture/adr-omnigent-pluggable-identity.md``. Every identity
subpart is a replaceable port with an in-box default, so a bare omnigent works
standalone and a consumer (Office/platform, later) can swap any piece:

- :class:`~omnigent.identity.ports.AssertionVerifier` — how an inbound identity
  assertion is *trusted* (default: :class:`~omnigent.identity.verifiers.HmacAssertionVerifier`).
- :class:`~omnigent.identity.ports.OutboundCredentialProvider` +
  :class:`~omnigent.identity.ports.MintStrategy` — how a tool *acts as* an identity
  (default: :class:`~omnigent.identity.defaults.StaticSecretProvider` over the three
  existing egress strategies).
- :class:`~omnigent.identity.ports.AuthorizationProvider` — whether an action is
  allowed (default: :class:`~omnigent.identity.defaults.OwnerAllowAuthorizer`).

The boundary value object :class:`~omnigent.identity.identity.ActingIdentity`
carries the verified principal + acting agent to the point of action.

This package stays light (no FastAPI / server graph) so it is safe on the runner
hot path; heavy egress helpers are imported lazily at mint time.
"""

from __future__ import annotations

from omnigent.identity.identity import ActingIdentity
from omnigent.identity.types import Credential, Decision

__all__ = ["ActingIdentity", "Credential", "Decision"]
