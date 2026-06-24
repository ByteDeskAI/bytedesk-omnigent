"""Tests for the pluggable request-principal seam (BDP-2388).

Covers the foundation laid in increment 1 with ZERO behavior change:

1. :class:`omnigent.server.principal.Principal` — value object shape /
   immutability / defaults.
2. :meth:`AuthProvider.get_principal` — concrete default adapts
   ``get_user_id`` (incl. ``None`` → ``None`` and the ``"local"`` fallback).
3. :class:`CompositeAuthProvider` — Chain of Responsibility: first non-None
   resolver wins, fall-through to the configured base, and (no extension
   resolver) behavior-identical to the base provider alone.
4. :func:`omnigent.extensions.extension_principal_resolvers` — aggregates the
   optional ``principal_resolvers()`` hook and defaults to ``[]``.
"""

from __future__ import annotations

import dataclasses

import pytest
from starlette.requests import HTTPConnection

from omnigent.server.auth import (
    RESERVED_USER_LOCAL,
    AuthProvider,
    CompositeAuthProvider,
    UnifiedAuthProvider,
    accounts_provider,
)
from omnigent.server.principal import Principal


def _conn(headers: dict[str, str] | None = None) -> HTTPConnection:
    """Build a minimal ASGI ``HTTPConnection`` with optional headers."""
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return HTTPConnection({"type": "http", "headers": raw})


class _StubProvider(AuthProvider):
    """A provider that returns a fixed user id (or ``None``)."""

    def __init__(self, user_id: str | None) -> None:
        self._user_id = user_id

    def get_user_id(self, request: HTTPConnection) -> str | None:
        return self._user_id


# ── Principal value object ────────────────────────────────────────────


def test_principal_defaults_are_empty() -> None:
    p = Principal(user_id="alice@example.com")
    assert p.user_id == "alice@example.com"
    assert p.tenant_id is None
    assert p.roles == ()
    assert dict(p.claims) == {}


def test_principal_is_frozen() -> None:
    p = Principal(user_id="alice@example.com")
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.user_id = "mallory@example.com"  # type: ignore[misc]


def test_principal_carries_optional_fields() -> None:
    p = Principal(
        user_id="alice@example.com",
        tenant_id="tenant-1",
        roles=("admin",),
        claims={"k": "v"},
    )
    assert p.tenant_id == "tenant-1"
    assert p.roles == ("admin",)
    assert p.claims["k"] == "v"


# ── AuthProvider.get_principal default (Adapter) ──────────────────────


def test_get_principal_wraps_user_id() -> None:
    provider = _StubProvider("alice@example.com")
    principal = provider.get_principal(_conn())
    assert principal == Principal(user_id="alice@example.com")


def test_get_principal_none_when_user_id_none() -> None:
    provider = _StubProvider(None)
    assert provider.get_principal(_conn()) is None


def test_get_principal_preserves_local_fallback() -> None:
    # Header mode, single-user local runtime: absent header → "local".
    provider = UnifiedAuthProvider(source="header", local_single_user=True)
    principal = provider.get_principal(_conn())
    assert principal == Principal(user_id=RESERVED_USER_LOCAL)
    # And the header value flows through unchanged.
    principal2 = provider.get_principal(_conn({"X-Forwarded-Email": "bob@example.com"}))
    assert principal2 == Principal(user_id="bob@example.com")


def test_get_principal_none_when_header_absent_fail_closed() -> None:
    # Header mode, NOT single-user: absent header → None (401), unchanged.
    provider = UnifiedAuthProvider(source="header", local_single_user=False)
    assert provider.get_principal(_conn()) is None
    assert provider.get_user_id(_conn()) is None


# ── CompositeAuthProvider (Chain of Responsibility) ───────────────────


def test_composite_requires_base() -> None:
    with pytest.raises(ValueError):
        CompositeAuthProvider(None)  # type: ignore[arg-type]


def test_composite_no_resolvers_is_identical_to_base() -> None:
    base = UnifiedAuthProvider(source="header", local_single_user=True)
    composite = CompositeAuthProvider(base, [])

    for req in (_conn(), _conn({"X-Forwarded-Email": "bob@example.com"})):
        assert composite.get_user_id(req) == base.get_user_id(req)
        assert composite.get_principal(req) == base.get_principal(req)


def test_composite_falls_through_to_base() -> None:
    silent = _StubProvider(None)
    base = _StubProvider("base-user")
    composite = CompositeAuthProvider(base, [silent])
    assert composite.get_user_id(_conn()) == "base-user"
    assert composite.get_principal(_conn()) == Principal(user_id="base-user")


def test_composite_first_non_none_resolver_wins() -> None:
    winner = _StubProvider("ext-user")
    base = _StubProvider("base-user")
    composite = CompositeAuthProvider(base, [winner])
    assert composite.get_user_id(_conn()) == "ext-user"
    assert composite.get_principal(_conn()) == Principal(user_id="ext-user")


def test_composite_resolver_order_first_match_wins() -> None:
    first = _StubProvider("first")
    second = _StubProvider("second")
    base = _StubProvider("base")
    composite = CompositeAuthProvider(base, [first, second])
    assert composite.get_principal(_conn()) == Principal(user_id="first")


class _RichResolver(AuthProvider):
    """A resolver that overrides get_principal to supply tenant + roles."""

    def get_user_id(self, request: HTTPConnection) -> str | None:
        return "ext-user"

    def get_principal(self, request: HTTPConnection) -> Principal | None:
        return Principal(user_id="ext-user", tenant_id="tenant-9", roles=("agent",))

    def get_principal_marker(self) -> bool:  # pragma: no cover - clarity only
        return True


def test_composite_rich_resolver_supplies_tenant() -> None:
    base = _StubProvider("base-user")
    composite = CompositeAuthProvider(base, [_RichResolver()])
    principal = composite.get_principal(_conn())
    assert principal == Principal(
        user_id="ext-user", tenant_id="tenant-9", roles=("agent",)
    )
    # get_user_id resolves through the same chain.
    assert composite.get_user_id(_conn()) == "ext-user"


# ── accounts_provider unwrap (BDP-2426) ───────────────────────────────


def test_accounts_provider_recognizes_bare_accounts() -> None:
    base = UnifiedAuthProvider(source="accounts", local_single_user=False)
    assert accounts_provider(base) is base


def test_accounts_provider_unwraps_composite_wrapped_accounts() -> None:
    # The bug (BDP-2426): a principal-resolver wrap hid the accounts provider,
    # so bootstrap/router/`/v1/info` all stopped recognizing accounts mode.
    base = UnifiedAuthProvider(source="accounts", local_single_user=False)
    composite = CompositeAuthProvider(base, [_StubProvider("ext-user")])
    assert accounts_provider(composite) is base


def test_accounts_provider_none_for_header() -> None:
    base = UnifiedAuthProvider(source="header", local_single_user=True)
    assert accounts_provider(base) is None
    composite = CompositeAuthProvider(base, [_StubProvider("ext-user")])
    assert accounts_provider(composite) is None


def test_accounts_provider_none_for_oidc() -> None:
    base = UnifiedAuthProvider(source="oidc")
    assert accounts_provider(base) is None
    assert accounts_provider(CompositeAuthProvider(base, [])) is None


def test_accounts_provider_none_for_none() -> None:
    assert accounts_provider(None) is None


# ── extension_principal_resolvers aggregator ──────────────────────────


def test_extension_principal_resolvers_default_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.extensions as ext_mod

    class _ExtNoHook:
        name = "no-hook"

        def routers(self, auth_provider: object | None = None) -> list:
            return []

    monkeypatch.setattr(ext_mod, "discover_extensions", lambda: [_ExtNoHook()])
    assert ext_mod.extension_principal_resolvers() == []


def test_extension_principal_resolvers_collected(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.extensions as ext_mod

    resolver = _StubProvider("ext-user")

    class _ExtWithHook:
        name = "with-hook"

        def routers(self, auth_provider: object | None = None) -> list:
            return []

        def principal_resolvers(self) -> list:
            return [resolver]

    monkeypatch.setattr(ext_mod, "discover_extensions", lambda: [_ExtWithHook()])
    assert ext_mod.extension_principal_resolvers() == [resolver]
