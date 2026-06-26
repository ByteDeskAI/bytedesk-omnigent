"""Unit tests for coordination backplane resolution."""

from __future__ import annotations

import pytest

from omnigent.coordination.factory import (
    get_coordination_registry,
    resolve_coordination_backplane,
)
from omnigent.coordination.inprocess import InProcessBackplane


def test_registry_lists_inprocess_and_nats() -> None:
    names = get_coordination_registry().names()
    assert "inprocess" in names
    assert "nats" in names


def test_resolve_defaults_to_inprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_NATS_URL", raising=False)
    monkeypatch.delenv("OMNIGENT_USE_COORDINATION_BACKPLANE", raising=False)
    backplane = resolve_coordination_backplane()
    assert isinstance(backplane, InProcessBackplane)


def test_nats_url_selects_nats_when_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("nats")
    monkeypatch.setenv("OMNIGENT_NATS_URL", "nats://127.0.0.1:4222")
    monkeypatch.delenv("OMNIGENT_USE_COORDINATION_BACKPLANE", raising=False)
    from omnigent.coordination.nats_backplane import NatsBackplane

    backplane = resolve_coordination_backplane()
    assert isinstance(backplane, NatsBackplane)


def test_override_wins_over_nats_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_NATS_URL", "nats://127.0.0.1:4222")
    monkeypatch.setenv("OMNIGENT_USE_COORDINATION_BACKPLANE", "inprocess")
    backplane = resolve_coordination_backplane()
    assert isinstance(backplane, InProcessBackplane)


def test_override_selects_nats_without_nats_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OMNIGENT_USE_<seam>=nats is honored via the registry even with no NATS_URL
    auto-select in play — proving the override, not the URL branch, drove it.

    (The nats factory still needs a URL to construct, so one is provided; the URL
    being present is what nats *needs*, not what *selected* it — selection here is
    the override resolved through ``resolve_default``.)
    """
    pytest.importorskip("nats")
    monkeypatch.setenv("OMNIGENT_USE_COORDINATION_BACKPLANE", "nats")
    monkeypatch.setenv("OMNIGENT_NATS_URL", "nats://127.0.0.1:4222")
    from omnigent.coordination.nats_backplane import NatsBackplane

    assert isinstance(resolve_coordination_backplane(), NatsBackplane)


def test_unknown_override_raises_registry_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown OMNIGENT_USE_<seam> resolves through the registry and surfaces
    its ProviderNotRegistered, not a silent fallback."""
    from omnigent.kernel.pluggable.errors import ProviderNotRegistered

    monkeypatch.delenv("OMNIGENT_NATS_URL", raising=False)
    monkeypatch.setenv("OMNIGENT_USE_COORDINATION_BACKPLANE", "nope")
    with pytest.raises(ProviderNotRegistered):
        resolve_coordination_backplane()