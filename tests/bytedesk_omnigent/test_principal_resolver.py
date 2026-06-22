"""Tests for the ByteDesk principal resolver (BDP-2389 increment 2a).

The resolver reads a gateway-minted, HMAC-signed ``X-Bytedesk-Principal``
header, verifies it fail-CLOSED, and adapts the verified payload into a core
:class:`~omnigent.server.principal.Principal` (Strategy + Adapter). It is
registered on :class:`BytedeskExtension` ONLY when the signing secret is
configured, so a default deploy is zero behavior change.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping

import pytest
from starlette.requests import HTTPConnection

from bytedesk_omnigent.auth.principal_resolver import (
    HEADER_NAME,
    SECRET_ENV,
    ByteDeskPrincipalResolver,
    map_capabilities_to_roles,
)
from bytedesk_omnigent.extension import BytedeskExtension

_SECRET = "test-principal-secret"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _mint(payload: Mapping[str, object], *, secret: str = _SECRET) -> str:
    """Mint a header value matching the resolver's verification scheme."""
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).digest()
    return f"{_b64url(payload_bytes)}.{_b64url(sig)}"


def _conn(headers: dict[str, str] | None = None) -> HTTPConnection:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return HTTPConnection({"type": "http", "headers": raw})


def _valid_payload(**overrides: object) -> dict[str, object]:
    now = int(time.time())
    payload: dict[str, object] = {
        "user_id": "alice@example.com",
        "tenant_id": "tenant-1",
        "roles": ["member"],
        "capabilities": ["office.workflows.administer"],
        "iat": now,
        "exp": now + 300,
    }
    payload.update(overrides)
    return payload


# ── happy path ────────────────────────────────────────────────────────


def test_valid_header_resolves_principal() -> None:
    resolver = ByteDeskPrincipalResolver(_SECRET)
    header = _mint(_valid_payload())
    principal = resolver.get_principal(_conn({HEADER_NAME: header}))
    assert principal is not None
    assert principal.user_id == "alice@example.com"
    assert principal.tenant_id == "tenant-1"


def test_get_user_id_derives_from_principal() -> None:
    resolver = ByteDeskPrincipalResolver(_SECRET)
    header = _mint(_valid_payload())
    assert resolver.get_user_id(_conn({HEADER_NAME: header})) == "alice@example.com"


def test_capabilities_mapped_to_roles_and_raw_in_claims() -> None:
    resolver = ByteDeskPrincipalResolver(_SECRET)
    payload = _valid_payload(
        roles=[],
        capabilities=["office.workflows.administer", "office.agents.view"],
    )
    principal = resolver.get_principal(_conn({HEADER_NAME: _mint(payload)}))
    assert principal is not None
    # Mapped omnigent roles derived from platform capabilities.
    assert "workflow-admin" in principal.roles
    assert "agent-viewer" in principal.roles
    # Raw capabilities preserved in claims for debugging.
    assert principal.claims["capabilities"] == [
        "office.workflows.administer",
        "office.agents.view",
    ]


def test_explicit_roles_and_mapped_roles_merge() -> None:
    resolver = ByteDeskPrincipalResolver(_SECRET)
    payload = _valid_payload(roles=["custom"], capabilities=["office.agents.view"])
    principal = resolver.get_principal(_conn({HEADER_NAME: _mint(payload)}))
    assert principal is not None
    assert "custom" in principal.roles
    assert "agent-viewer" in principal.roles


# ── fail-closed verification ──────────────────────────────────────────


def test_missing_header_returns_none() -> None:
    resolver = ByteDeskPrincipalResolver(_SECRET)
    assert resolver.get_principal(_conn()) is None
    assert resolver.get_user_id(_conn()) is None


def test_tampered_signature_returns_none() -> None:
    resolver = ByteDeskPrincipalResolver(_SECRET)
    header = _mint(_valid_payload())
    payload_b64, _, _sig = header.partition(".")
    tampered = f"{payload_b64}.{_b64url(b'not-the-signature')}"
    assert resolver.get_principal(_conn({HEADER_NAME: tampered})) is None


def test_wrong_secret_returns_none() -> None:
    resolver = ByteDeskPrincipalResolver(_SECRET)
    header = _mint(_valid_payload(), secret="other-secret")
    assert resolver.get_principal(_conn({HEADER_NAME: header})) is None


def test_expired_token_returns_none() -> None:
    resolver = ByteDeskPrincipalResolver(_SECRET)
    now = int(time.time())
    header = _mint(_valid_payload(iat=now - 600, exp=now - 120))
    assert resolver.get_principal(_conn({HEADER_NAME: header})) is None


def test_exp_within_skew_tolerance_accepted() -> None:
    resolver = ByteDeskPrincipalResolver(_SECRET)
    now = int(time.time())
    # Expired 30s ago — within the ~60s clock-skew tolerance.
    header = _mint(_valid_payload(exp=now - 30))
    assert resolver.get_principal(_conn({HEADER_NAME: header})) is not None


def test_missing_user_id_returns_none() -> None:
    resolver = ByteDeskPrincipalResolver(_SECRET)
    payload = _valid_payload()
    del payload["user_id"]
    assert resolver.get_principal(_conn({HEADER_NAME: _mint(payload)})) is None


def test_malformed_header_returns_none() -> None:
    resolver = ByteDeskPrincipalResolver(_SECRET)
    for bad in ("", "no-dot", "not-base64!.also-bad!", "a.b.c"):
        assert resolver.get_principal(_conn({HEADER_NAME: bad})) is None


def test_non_json_payload_returns_none() -> None:
    resolver = ByteDeskPrincipalResolver(_SECRET)
    raw = b"this is not json"
    sig = hmac.new(_SECRET.encode("utf-8"), raw, hashlib.sha256).digest()
    header = f"{_b64url(raw)}.{_b64url(sig)}"
    assert resolver.get_principal(_conn({HEADER_NAME: header})) is None


# ── capability → role mapping ─────────────────────────────────────────


def test_map_capabilities_known() -> None:
    roles = map_capabilities_to_roles(
        ["office.workflows.administer", "office.agents.administer"]
    )
    assert "workflow-admin" in roles
    assert "agent-admin" in roles


def test_map_capabilities_unknown_ignored() -> None:
    roles = map_capabilities_to_roles(["totally.unknown.capability"])
    assert roles == ()


def test_map_capabilities_empty() -> None:
    assert map_capabilities_to_roles([]) == ()


def test_map_capabilities_deduplicates() -> None:
    # Two capabilities mapping to overlapping roles must not duplicate.
    roles = map_capabilities_to_roles(
        ["office.workflows.administer", "office.workflows.administer"]
    )
    assert roles.count("workflow-admin") == 1


# ── constant-time compare ─────────────────────────────────────────────


def test_uses_constant_time_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    import bytedesk_omnigent.auth.principal_resolver as mod

    calls: list[bool] = []
    real = hmac.compare_digest

    def _spy(a: object, b: object) -> bool:
        calls.append(True)
        return real(a, b)  # type: ignore[arg-type]

    monkeypatch.setattr(mod.hmac, "compare_digest", _spy)
    resolver = ByteDeskPrincipalResolver(_SECRET)
    resolver.get_principal(_conn({HEADER_NAME: _mint(_valid_payload())}))
    assert calls, "verification must use hmac.compare_digest"


# ── flag-gated registration (zero behavior change when unset) ─────────


def test_principal_resolvers_empty_when_secret_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SECRET_ENV, raising=False)
    assert BytedeskExtension().principal_resolvers() == []


def test_principal_resolvers_registers_when_secret_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SECRET_ENV, _SECRET)
    resolvers = BytedeskExtension().principal_resolvers()
    assert len(resolvers) == 1
    assert isinstance(resolvers[0], ByteDeskPrincipalResolver)
    # And it actually verifies with the configured secret.
    principal = resolvers[0].get_principal(_conn({HEADER_NAME: _mint(_valid_payload())}))
    assert principal is not None
    assert principal.user_id == "alice@example.com"
