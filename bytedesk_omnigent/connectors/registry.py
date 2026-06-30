"""Connector manifest registry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from bytedesk_omnigent.connectors.manifests import ConnectorManifest
from bytedesk_omnigent.connectors.providers import ConnectorProvider


@dataclass
class ConnectorRegistry:
    """Read-side registry of connector manifests and provider strategies."""

    _manifests: dict[str, ConnectorManifest]
    _providers: dict[str, ConnectorProvider] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for provider in self._providers.values():
            self._manifests.setdefault(provider.manifest.provider, provider.manifest)

    def providers(self) -> list[ConnectorManifest]:
        return list(self._manifests.values())

    def get(self, provider: str) -> ConnectorManifest | None:
        return self._manifests.get(provider)

    def get_provider(self, provider: str) -> ConnectorProvider | None:
        return self._providers.get(provider)


def build_connector_registry() -> ConnectorRegistry:
    """Aggregate connector manifests and provider strategies from extensions."""
    from omnigent.kernel.extensions import (
        extension_connector_manifests,
        extension_connector_providers,
    )

    manifests = {m.provider: m for m in extension_connector_manifests()}
    provider_factories: dict[str, Callable[[], Any]] = dict(extension_connector_providers())
    providers: dict[str, ConnectorProvider] = {}
    for registered_name, factory in provider_factories.items():
        adapter = factory()
        provider_name = getattr(adapter, "provider", registered_name)
        providers[provider_name] = adapter
        manifest = getattr(adapter, "manifest", None)
        if manifest is not None:
            manifests[manifest.provider] = manifest
    return ConnectorRegistry(manifests, providers)
