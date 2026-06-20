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