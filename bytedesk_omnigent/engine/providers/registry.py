"""Provider manifest + registry (Phase 4, BDP-2586).

A connected app declares what it offers the engine via a :class:`ProviderManifest`
(which sensors / actuators / outcomes / webhook sources, plus its base URL and the
reverse-auth header). :class:`ProviderRegistry` holds the registered manifests.

**Persistence: in-memory module singleton** (a connected app re-registers its
manifest on boot — the registration call is the source of truth, the registry is a
runtime index). This is deliberately NOT the config control plane: a manifest is
discovered + re-asserted at connect time, not hand-edited config, so a durable
table or settings key would be ceremony with no payoff. ``ponytail:`` in-memory;
move to the config control plane (ADR-0150) if a manifest must survive a restart
without the app reconnecting.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ActuatorSpec:
    """One actuator a provider offers, with its risk tier."""

    name: str
    risk_tier: int = 2


@dataclass(frozen=True)
class ProviderAuth:
    """Reverse-auth for the engine→app direction: a shared-secret header."""

    header: str
    secret: str | None = None  # resolved at call time; never logged


@dataclass(frozen=True)
class ProviderManifest:
    """What a connected app offers the engine (the extension-seam contract)."""

    name: str
    base_url: str
    sensors: list[str] = field(default_factory=list)
    actuators: list[ActuatorSpec] = field(default_factory=list)
    outcomes: list[str] = field(default_factory=list)
    webhook_sources: list[str] = field(default_factory=list)
    auth: ProviderAuth | None = None

    def __post_init__(self) -> None:
        # Normalize the base URL in one place so remote URLs never double-slash,
        # regardless of how the manifest was built (frozen → object.__setattr__).
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProviderManifest:
        """Build a manifest from a posted JSON body (the register endpoint)."""
        auth_raw = data.get("auth")
        auth = (
            ProviderAuth(header=auth_raw["header"], secret=auth_raw.get("secret"))
            if isinstance(auth_raw, dict) and auth_raw.get("header")
            else None
        )
        return cls(
            name=data["name"],
            base_url=str(data["base_url"]),
            sensors=list(data.get("sensors") or []),
            actuators=[
                ActuatorSpec(name=a["name"], risk_tier=int(a.get("risk_tier", 2)))
                for a in (data.get("actuators") or [])
            ],
            outcomes=list(data.get("outcomes") or []),
            webhook_sources=list(data.get("webhook_sources") or []),
            auth=auth,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the list endpoint. The auth SECRET is never emitted."""
        return {
            "name": self.name,
            "base_url": self.base_url,
            "sensors": list(self.sensors),
            "actuators": [{"name": a.name, "risk_tier": a.risk_tier} for a in self.actuators],
            "outcomes": list(self.outcomes),
            "webhook_sources": list(self.webhook_sources),
            "auth": {"header": self.auth.header} if self.auth else None,
        }


class ProviderRegistry:
    """In-memory registry of connected-app manifests (re-register is upsert)."""

    def __init__(self) -> None:
        self._manifests: dict[str, ProviderManifest] = {}

    def register_provider(self, manifest: ProviderManifest) -> None:
        """Register/replace a provider by name (idempotent upsert)."""
        self._manifests[manifest.name] = manifest

    def get(self, name: str) -> ProviderManifest | None:
        return self._manifests.get(name)

    def providers(self) -> list[ProviderManifest]:
        return list(self._manifests.values())

    def remove(self, name: str) -> None:
        self._manifests.pop(name, None)


_registry: ProviderRegistry | None = None


def get_provider_registry() -> ProviderRegistry:
    """The process-wide provider registry singleton."""
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry


__all__ = [
    "ActuatorSpec",
    "ProviderAuth",
    "ProviderManifest",
    "ProviderRegistry",
    "get_provider_registry",
]
