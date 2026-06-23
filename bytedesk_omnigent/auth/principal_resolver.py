"""ByteDesk gateway-header principal resolver (BDP-2389 increment 2a).

The platform gateway authenticates the end user (tenant, roles, capabilities)
and forwards that identity to omnigent as a compact, HMAC-signed
``X-Bytedesk-Principal`` header. This resolver is the omnigent-side trust
boundary: it VERIFIES that header fail-CLOSED and adapts the verified payload
into a core :class:`~omnigent.server.principal.Principal`.

Design (per ``CLAUDE.md`` §5 / ADR-0008, ``adr-omnigent-pluggable-identity``):

- **Strategy** — it is an :class:`~omnigent.server.auth.AuthProvider`, so the
  core :class:`~omnigent.server.auth.CompositeAuthProvider` can chain it ahead
  of the configured base provider with no core edit.
- **Adapter** — it translates the external platform identity wire format (a
  signed token carrying platform *capabilities*) into omnigent's internal
  identity vocabulary (a :class:`Principal` with *roles*).
- **The trust mechanism is a pluggable subpart** — verification is delegated to a
  core :class:`~omnigent.identity.verifiers.HmacAssertionVerifier`, so the
  shared-HMAC scheme can be swapped for JWKS/OIDC without rewriting this
  resolver's header parsing or capability→role adaptation.

Header format (a tiny JWS-like form, mirroring
:func:`bytedesk_omnigent.ingress.verify_hmac_signature`'s HMAC-SHA256 scheme)::

    base64url(payload_json) "." base64url(hmac_sha256(secret, payload_bytes))

where ``payload_json`` is ``{user_id, tenant_id, roles, capabilities, iat, exp}``.
Verification requires a constant-time HMAC match, a **present numeric** ``exp``
that is not expired (with a ~60s clock-skew tolerance), and a present
``user_id``. ANY failure returns ``None`` so the request falls through the chain
to the configured base provider — this resolver never raises into the request
path and never grants identity it could not verify.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from starlette.requests import HTTPConnection

from omnigent.identity.verifiers import (
    DEFAULT_RSA_PUBLIC_KEY_ENV,
    HmacAssertionVerifier,
)
from omnigent.server.auth import AuthProvider
from omnigent.server.principal import Principal

if TYPE_CHECKING:
    from omnigent.identity.ports import AssertionVerifier

logger = logging.getLogger(__name__)

#: Request header carrying the gateway-minted signed principal token.
HEADER_NAME = "X-Bytedesk-Principal"

#: Env var holding the shared HMAC signing secret (mirrors the platform gateway).
SECRET_ENV = "OMNIGENT_BYTEDESK_PRINCIPAL_SECRET"

#: Env var holding the PEM public key for the asymmetric (RSA) principal scheme
#: (BDP-2424). When set it takes precedence over the HMAC secret: Office signs
#: with the private key omnigent never holds, so omnigent can verify but not
#: forge — closing the symmetric-HMAC forgeability for the inbound principal.
RSA_PUBLIC_KEY_ENV = DEFAULT_RSA_PUBLIC_KEY_ENV

#: Clock-skew tolerance (seconds) applied to the ``exp`` check.
_CLOCK_SKEW_S = 60

#: Canonical Data Model translation (Q3): platform capabilities → omnigent roles.
#: Small + explicit on purpose. Unknown capabilities are IGNORED (never passed
#: through verbatim); the raw capability list is preserved in ``Principal.claims``
#: for debugging. Keep this table small and documented — extend deliberately.
_CAPABILITY_ROLE_MAP: Mapping[str, str] = {
    "office.workflows.administer": "workflow-admin",
    "office.workflows.edit": "workflow-editor",
    "office.agents.administer": "agent-admin",
    "office.agents.edit": "agent-editor",
    "office.agents.view": "agent-viewer",
}


def map_capabilities_to_roles(capabilities: Iterable[str]) -> tuple[str, ...]:
    """Map platform *capabilities* to the omnigent role vocabulary (Adapter).

    Unknown capabilities are silently dropped — only mapped roles are returned,
    order-preserving and de-duplicated. See :data:`_CAPABILITY_ROLE_MAP`.
    """
    roles: list[str] = []
    for cap in capabilities:
        role = _CAPABILITY_ROLE_MAP.get(cap)
        if role is not None and role not in roles:
            roles.append(role)
    return tuple(roles)


def _str_list(value: object) -> list[str]:
    """Coerce an untrusted JSON value into a list of strings (non-strings dropped)."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


class ByteDeskPrincipalResolver(AuthProvider):
    """Resolve a verified gateway header into a :class:`Principal` (fail-closed).

    :param verifier_or_secret: Either an
        :class:`~omnigent.identity.ports.AssertionVerifier` (e.g. the asymmetric
        :class:`~omnigent.identity.verifiers.RsaAssertionVerifier`, used as-is) or
        a ``str``/``bytes`` HMAC secret (back-compat, the BDP-2389 path: wrapped
        in an :class:`~omnigent.identity.verifiers.HmacAssertionVerifier`). The
        trust mechanism is thus a swappable subpart.
    """

    def __init__(self, verifier_or_secret: AssertionVerifier | str | bytes) -> None:
        if isinstance(verifier_or_secret, (str, bytes)):
            self._verifier: AssertionVerifier = HmacAssertionVerifier(
                verifier_or_secret, clock_skew_s=float(_CLOCK_SKEW_S), require_exp=True
            )
        else:
            self._verifier = verifier_or_secret

    def get_user_id(self, request: HTTPConnection) -> str | None:
        """Derive the user id from :meth:`get_principal` (``None`` falls through)."""
        principal = self.get_principal(request)
        return principal.user_id if principal is not None else None

    def get_principal(self, request: HTTPConnection) -> Principal | None:
        """Verify ``X-Bytedesk-Principal`` and adapt it into a :class:`Principal`.

        Returns ``None`` (fall through the chain) when the header is absent OR
        fails verification. Verification failures are logged at WARNING with no
        secret/header values.
        """
        header = request.headers.get(HEADER_NAME)
        if not header:
            return None  # absent → fall through (not an error)

        payload = self._verifier.verify(header)
        if payload is None:
            return None  # any verification failure → fail closed

        user_id = payload.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            logger.warning("rejected %s: missing user_id", HEADER_NAME)
            return None

        tenant_id = payload.get("tenant_id")
        tenant = tenant_id if isinstance(tenant_id, str) and tenant_id else None

        explicit_roles = tuple(_str_list(payload.get("roles")))
        raw_caps = _str_list(payload.get("capabilities"))
        mapped_roles = map_capabilities_to_roles(raw_caps)
        roles = (*explicit_roles, *(r for r in mapped_roles if r not in explicit_roles))

        return Principal(
            user_id=user_id,
            tenant_id=tenant,
            roles=roles,
            claims={"capabilities": raw_caps},
        )
