"""Resolved-principal value object (BDP-2388).

:class:`Principal` is the canonical, immutable result of resolving the identity
of an incoming request. Today the only populated field is ``user_id`` — the
default :meth:`AuthProvider.get_principal` adapts the existing
``get_user_id`` and so produces a ``Principal`` whose other fields are empty.
The richer fields (``tenant_id``, ``roles``, ``claims``) exist so a later
increment's external resolver (e.g. the platform supplying tenant + roles via a
gateway header) can populate them WITHOUT changing this surface — the seam is
laid here, the behavior change is not.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

_EMPTY_CLAIMS: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class Principal:
    """The resolved identity of a request.

    :param user_id: The authenticated user id (e.g. ``"alice@example.com"`` or
        the reserved ``"local"`` sentinel). Always populated for a non-``None``
        principal.
    :param tenant_id: Owning tenant, when an external resolver supplies one.
        ``None`` for every in-core auth mode today.
    :param roles: Coarse roles carried by the principal. Empty by default.
    :param claims: Free-form additional claims an external resolver may attach.
        Empty (read-only) mapping by default.
    """

    user_id: str
    tenant_id: str | None = None
    roles: tuple[str, ...] = ()
    claims: Mapping[str, Any] = field(default_factory=lambda: _EMPTY_CLAIMS)
