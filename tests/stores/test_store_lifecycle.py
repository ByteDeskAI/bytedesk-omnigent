"""Tests for omnigent.stores.lifecycle (BDP-2327, Phase 2).

Covers the optional async store lifecycle hooks and the gated driver:
no-op defaults change nothing, the driver is an immediate no-op while
``OMNIGENT_STORE_LIFECYCLE_HOOKS`` is off, and when on it invokes a hook
only for stores that define one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omnigent.stores.factory import StoreBootstrapper
from omnigent.stores.lifecycle import (
    LIFECYCLE_HOOKS_ENV_VAR,
    StoreLifecycleMixin,
    run_store_lifecycle,
)


class _OptedInStore(StoreLifecycleMixin):
    """A store that opts into the mixin and records hook calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.healthy = True

    async def startup(self) -> None:
        self.calls.append("startup")

    async def shutdown(self) -> None:
        self.calls.append("shutdown")

    async def health_check(self) -> bool:
        self.calls.append("health_check")
        return self.healthy


class _PlainStore:
    """A store that has not opted in — no lifecycle hooks at all."""


def test_mixin_defaults_are_noops() -> None:
    """The mixin's default hooks run, return inert values, and don't raise."""

    class _Bare(StoreLifecycleMixin):
        pass

    store = _Bare()
    assert asyncio.run(store.startup()) is None
    assert asyncio.run(store.shutdown()) is None
    assert asyncio.run(store.health_check()) is True


def test_driver_is_noop_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the flag unset the driver invokes nothing and returns empty."""
    monkeypatch.delenv(LIFECYCLE_HOOKS_ENV_VAR, raising=False)
    store = _OptedInStore()

    result = asyncio.run(run_store_lifecycle([store], "startup"))

    assert result == {}
    assert store.calls == []


def test_driver_invokes_opted_in_store_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the flag on, the opted-in store's hook runs and is reported."""
    monkeypatch.setenv(LIFECYCLE_HOOKS_ENV_VAR, "1")
    store = _OptedInStore()

    result = asyncio.run(run_store_lifecycle([store], "startup"))

    assert store.calls == ["startup"]
    assert result == {id(store): None}


def test_driver_skips_store_without_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """A store that didn't opt in is transparently skipped, not errored."""
    monkeypatch.setenv(LIFECYCLE_HOOKS_ENV_VAR, "1")
    opted_in = _OptedInStore()
    plain = _PlainStore()

    result = asyncio.run(run_store_lifecycle([plain, opted_in], "shutdown"))

    assert opted_in.calls == ["shutdown"]
    # Only the opted-in store appears; the plain store is omitted entirely.
    assert result == {id(opted_in): None}


def test_health_check_reports_bool(monkeypatch: pytest.MonkeyPatch) -> None:
    """health_check threads the store's bool through the driver result."""
    monkeypatch.setenv(LIFECYCLE_HOOKS_ENV_VAR, "1")
    store = _OptedInStore()
    store.healthy = False

    result = asyncio.run(run_store_lifecycle([store], "health_check"))

    assert result == {id(store): False}


def test_bootstrapped_stores_run_lifecycle_noop_default(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BootstrappedStores.run_lifecycle is a no-op over real stores by default.

    None of the existing concrete stores have opted into the mixin, and the
    flag defaults off, so driving the real bundle changes nothing and
    returns an empty mapping — proving the seam is inert against today's
    store set.
    """
    monkeypatch.delenv(LIFECYCLE_HOOKS_ENV_VAR, raising=False)
    stores = StoreBootstrapper.create(db_uri, str(tmp_path / "artifacts"))

    assert asyncio.run(stores.run_lifecycle("startup")) == {}
    assert asyncio.run(stores.run_lifecycle("health_check")) == {}


def test_all_stores_returns_full_bundle(db_uri: str, tmp_path: Path) -> None:
    """all_stores() exposes every wired store for uniform iteration."""
    stores = StoreBootstrapper.create(db_uri, str(tmp_path / "artifacts"))
    bundle = stores.all_stores()

    assert len(bundle) == 8
    assert stores.agent_store in bundle
    assert stores.host_store in bundle
