"""AgentStore provider factory cutover tests."""

from __future__ import annotations

import pytest

from omnigent.kernel.pluggable.errors import ProviderNotRegistered
from omnigent.stores.agent_store.nats_store import NatsAgentStore
from omnigent.stores.factory import _build_agent_store_registry, _create_agent_store


def test_agent_store_registry_defaults_to_nats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_USE_AGENT_STORE", raising=False)
    registry = _build_agent_store_registry("nats://omnigent-nats:4222/omnigent-artifacts")

    assert registry.describe()["default"] == "nats"
    assert registry.describe()["names"] == ["nats"]
    assert isinstance(registry.resolve_default(), NatsAgentStore)


def test_agent_store_registry_has_no_sql_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _build_agent_store_registry("nats://omnigent-nats:4222/omnigent-artifacts")
    monkeypatch.setenv("OMNIGENT_USE_AGENT_STORE", "sqlalchemy")

    with pytest.raises(ProviderNotRegistered):
        registry.resolve_default()


def test_agent_store_uses_consolidated_nats_url_from_artifact_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIGENT_AGENT_STORE_NATS_URL", raising=False)
    monkeypatch.delenv("OMNIGENT_NATS_URL", raising=False)

    store = _create_agent_store("nats://omnigent-nats:4222/omnigent-artifacts")

    assert isinstance(store, NatsAgentStore)
    assert store.nats_url == "nats://omnigent-nats:4222"


def test_agent_store_requires_nats_location_or_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_AGENT_STORE_NATS_URL", raising=False)
    monkeypatch.delenv("OMNIGENT_NATS_URL", raising=False)

    with pytest.raises(RuntimeError, match="NATS AgentStore requires"):
        _create_agent_store("/tmp/artifacts")
