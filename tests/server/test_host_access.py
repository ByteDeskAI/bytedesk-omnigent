"""Host visibility-scope authorization (ADR-0151, BDP-2430)."""

from __future__ import annotations

import pytest

from omnigent.server import host_access
from omnigent.server.host_access import can_access_host, host_visibility_scope
from omnigent.stores.host_store import Host


def _host(owner: str, *, sandbox_provider: str | None = None) -> Host:
    return Host(
        host_id="host_x",
        name="box",
        owner=owner,
        status="online",
        created_at=1,
        updated_at=1,
        sandbox_provider=sandbox_provider,
    )


# ── can_access_host ───────────────────────────────────────────────────


def test_owner_always_allowed() -> None:
    assert can_access_host(_host("alice"), "alice", scope="private")
    assert can_access_host(_host("alice"), "alice", scope="org-shared")


def test_auth_disabled_allowed() -> None:
    # user_id None = single-user/local runtime → no isolation.
    assert can_access_host(_host("alice"), None, scope="private")


def test_external_org_shared_allows_non_owner() -> None:
    assert can_access_host(_host("alice"), "bob", scope="org-shared")


def test_external_private_denies_non_owner() -> None:
    assert not can_access_host(_host("alice"), "bob", scope="private")


def test_managed_host_never_shared_cross_owner() -> None:
    managed = _host("alice", sandbox_provider="modal")
    # Managed/sandbox hosts stay owner-only in EVERY scope.
    assert not can_access_host(managed, "bob", scope="org-shared")
    assert not can_access_host(managed, "bob", scope="private")
    # ...but their own owner still reaches them.
    assert can_access_host(managed, "alice", scope="org-shared")


# ── host_visibility_scope ─────────────────────────────────────────────


def test_visibility_scope_reads_config(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Val:
        value = "org-shared"

    class _Reg:
        def read(self, *_a: object, **_k: object) -> _Val:
            return _Val()

    monkeypatch.setattr("omnigent.config.build_registry", lambda: _Reg())
    assert host_visibility_scope() == "org-shared"


def test_visibility_scope_defaults_private_when_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> object:
        raise RuntimeError("no descriptor registered")

    # Fail to the more restrictive scope — a pool must never widen on a glitch.
    monkeypatch.setattr("omnigent.config.build_registry", _boom)
    assert host_visibility_scope() == "private"
    assert host_access._DEFAULT_SCOPE == "private"
