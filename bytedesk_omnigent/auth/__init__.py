"""ByteDesk request-authentication contributions (BDP-2389).

The :class:`~bytedesk_omnigent.auth.principal_resolver.ByteDeskPrincipalResolver`
adapts a gateway-minted, HMAC-signed ``X-Bytedesk-Principal`` header into a core
:class:`omnigent.server.principal.Principal`, contributed to the request
principal chain via the ``omnigent.extensions`` seam (ADR-0143).
"""

from __future__ import annotations

from bytedesk_omnigent.auth.principal_resolver import (
    HEADER_NAME,
    SECRET_ENV,
    ByteDeskPrincipalResolver,
    map_capabilities_to_roles,
)

__all__ = [
    "HEADER_NAME",
    "SECRET_ENV",
    "ByteDeskPrincipalResolver",
    "map_capabilities_to_roles",
]
