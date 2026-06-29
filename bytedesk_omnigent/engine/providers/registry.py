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

CONNECTED_APP_CONTRACT_VERSION = "connected-app.v1"
CONNECTED_APP_PROVIDER_SCHEMA_ID = (
    "https://omnigent.ai/contracts/connected-app/v1/provider-manifest.schema.json"
)


def _pick(data: dict[str, Any], camel: str, snake: str, default: Any = None) -> Any:
    """Read the v1 camelCase contract while accepting legacy snake_case bodies."""
    if camel in data:
        return data[camel]
    if snake in data:
        return data[snake]
    return default


ProviderRiskTier = int | str
_RISK_TIER_STRINGS = ("low", "medium", "high")


def normalize_provider_risk_tier(value: Any = "medium") -> ProviderRiskTier:
    """Normalize v1 provider actuator risk tiers.

    The connected-app contract prefers the semantic string enum used by Office
    (``low`` / ``medium`` / ``high``). Integers 0..5 remain accepted for legacy
    manifests that pre-date the v1 schema hardening.
    """
    if isinstance(value, bool):
        raise ValueError("riskTier boolean is not valid")
    if isinstance(value, int):
        if 0 <= value <= 5:
            return value
        raise ValueError("riskTier integer must be between 0 and 5")
    text = str(value or "medium").strip().lower()
    if text.isdigit():
        return normalize_provider_risk_tier(int(text))
    if text in _RISK_TIER_STRINGS:
        return text
    raise ValueError("riskTier must be low, medium, high, or an integer 0..5")


@dataclass(frozen=True)
class ActuatorSpec:
    """One actuator a provider offers, with its risk tier."""

    name: str
    risk_tier: ProviderRiskTier = "medium"


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
    contract_version: str = CONNECTED_APP_CONTRACT_VERSION
    schema_id: str = CONNECTED_APP_PROVIDER_SCHEMA_ID
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
            base_url=str(_pick(data, "baseUrl", "base_url")),
            contract_version=str(
                _pick(data, "contractVersion", "contract_version", CONNECTED_APP_CONTRACT_VERSION)
                or CONNECTED_APP_CONTRACT_VERSION
            ),
            schema_id=str(
                _pick(data, "schemaId", "schema_id", CONNECTED_APP_PROVIDER_SCHEMA_ID)
                or CONNECTED_APP_PROVIDER_SCHEMA_ID
            ),
            sensors=list(data.get("sensors") or []),
            actuators=[
                ActuatorSpec(
                    name=a["name"],
                    risk_tier=normalize_provider_risk_tier(
                        _pick(a, "riskTier", "risk_tier", "medium")
                    ),
                )
                for a in (data.get("actuators") or [])
            ],
            outcomes=list(data.get("outcomes") or []),
            webhook_sources=list(_pick(data, "webhookSources", "webhook_sources", []) or []),
            auth=auth,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the list endpoint. The auth SECRET is never emitted."""
        return {
            "name": self.name,
            "baseUrl": self.base_url,
            "contractVersion": self.contract_version,
            "schemaId": self.schema_id,
            "sensors": list(self.sensors),
            "actuators": [{"name": a.name, "riskTier": a.risk_tier} for a in self.actuators],
            "outcomes": list(self.outcomes),
            "webhookSources": list(self.webhook_sources),
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
    "ProviderRiskTier",
    "get_provider_registry",
    "normalize_provider_risk_tier",
]
