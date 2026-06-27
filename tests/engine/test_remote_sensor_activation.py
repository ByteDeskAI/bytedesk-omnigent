"""Remote (connected-app) sensors become live in a built registry (BDP-2595).

Wave 2: ``register_remote_providers`` fans a registered provider manifest's
declared sensors into a built default registry as ``RemoteSensor``s, so the
resolver can read them. Provider registry + httpx are faked — no network.
"""
from __future__ import annotations

from bytedesk_omnigent.engine.providers import (
    ProviderAuth,
    ProviderManifest,
    ProviderRegistry,
    RemoteSensor,
    register_remote_providers,
)
from bytedesk_omnigent.engine.providers.contract import ActuatorRegistry
from bytedesk_omnigent.engine.sensors import build_default_registry


def _manifest() -> ProviderManifest:
    return ProviderManifest(
        name="bytedesk",
        base_url="https://platform.bytedesk.ai/api/engine/",
        sensors=["jira", "github"],
        auth=ProviderAuth(header="X-Engine-Secret", secret="sssh"),
    )


def test_no_provider_registered_leaves_only_builtins() -> None:
    provider_reg = ProviderRegistry()  # empty
    sensor_reg = build_default_registry()
    builtins = set(sensor_reg.names())

    register_remote_providers(
        provider_reg,
        sensor_registry=sensor_reg,
        actuator_registry=ActuatorRegistry(),
    )
    assert set(sensor_reg.names()) == builtins  # no-op


def test_registered_manifest_makes_remote_sensors_resolvable() -> None:
    provider_reg = ProviderRegistry()
    provider_reg.register_provider(_manifest())
    sensor_reg = build_default_registry()

    register_remote_providers(
        provider_reg,
        sensor_registry=sensor_reg,
        actuator_registry=ActuatorRegistry(),
        sync_post=lambda url, body, headers: {"satisfied": True, "value": "done"},
    )

    # The manifest's declared sensors register under their declared names (stable,
    # provider-agnostic): a Leaf(sensor="jira") resolves to this RemoteSensor.
    assert "jira" in sensor_reg.names()
    assert "github" in sensor_reg.names()
    assert isinstance(sensor_reg.get("jira"), RemoteSensor)


def test_remote_sensor_drives_resolver_end_to_end() -> None:
    from bytedesk_omnigent.engine.resolver import resolve

    provider_reg = ProviderRegistry()
    provider_reg.register_provider(_manifest())
    sensor_reg = build_default_registry()
    register_remote_providers(
        provider_reg,
        sensor_registry=sensor_reg,
        actuator_registry=ActuatorRegistry(),
        sync_post=lambda url, body, headers: {"satisfied": True, "value": "done"},
    )

    class _Goal:
        id = "g1"
        readiness_kind = "immediate"
        dependencies: list = []
        payload = {
            "condition": {
                "type": "leaf",
                "sensor": "jira",
                "query": {"issue": "BDP-1"},
                "predicate": {"op": "exists"},
            }
        }

    out = resolve(_Goal(), registry=sensor_reg, goal_store=None, now=1000)
    assert out["actionable"] is True
