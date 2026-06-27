"""Connected-app provider contract (Phase 4, BDP-2586).

The extension seam that lets a connected app (the ByteDesk platform, Phase 5) FEED
and ACT FOR the goal engine without owning goals — the engine stays domain-blind.

- :mod:`registry` — :class:`ProviderManifest` + :class:`ProviderRegistry`.
- :mod:`contract` — the role Protocols (:class:`Actuator`, :class:`WebhookTranslator`,
  re-exported :class:`Sensor`) + :class:`ActuatorRegistry`.
- :mod:`remote` — Remote adapters (:class:`RemoteSensor` / :class:`RemoteActuator`).
- :mod:`fake` — in-memory fallback :class:`FakeProvider` (standalone + tests).
- :mod:`outcome` — :class:`OutcomeProcessor`: the OutcomeSource sink → treasury.
- :mod:`ingress` — canonical passthrough translator (lights up ADR-0155 P8).
"""
from __future__ import annotations

from bytedesk_omnigent.engine.providers.contract import (
    Actuator,
    ActuatorRegistry,
    ActuatorResult,
    Sensor,
    WebhookTranslator,
)
from bytedesk_omnigent.engine.providers.fake import FakeActuator, FakeProvider, FakeSensor
from bytedesk_omnigent.engine.providers.registry import (
    ActuatorSpec,
    ProviderAuth,
    ProviderManifest,
    ProviderRegistry,
    get_provider_registry,
)
from bytedesk_omnigent.engine.providers.remote import (
    RemoteActuator,
    RemoteSensor,
    register_remote_providers,
)

__all__ = [
    "Actuator",
    "ActuatorRegistry",
    "ActuatorResult",
    "ActuatorSpec",
    "FakeActuator",
    "FakeProvider",
    "FakeSensor",
    "ProviderAuth",
    "ProviderManifest",
    "ProviderRegistry",
    "RemoteActuator",
    "RemoteSensor",
    "Sensor",
    "WebhookTranslator",
    "get_provider_registry",
    "register_remote_providers",
]
