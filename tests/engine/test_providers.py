"""Tests for the connected-app provider contract (Phase 4, BDP-2586).

Fakes only — no network, no LLM. httpx is mocked via injected ``post`` callables.
"""
from __future__ import annotations

import asyncio

from bytedesk_omnigent.engine.providers import (
    ActuatorSpec,
    FakeActuator,
    FakeProvider,
    FakeSensor,
    ProviderAuth,
    ProviderManifest,
    ProviderRegistry,
    RemoteActuator,
    RemoteSensor,
    register_remote_providers,
)
from bytedesk_omnigent.engine.providers.contract import ActuatorRegistry
from bytedesk_omnigent.engine.sensors import SensorContext, build_default_registry


def _manifest(**kw) -> ProviderManifest:
    base = dict(
        name="bytedesk",
        base_url="https://platform.bytedesk.ai/api/engine/",
        sensors=["jira_issue"],
        actuators=[ActuatorSpec(name="send_email", risk_tier=3)],
        outcomes=["outcome.booked"],
        webhook_sources=["stripe"],
        auth=ProviderAuth(header="X-Engine-Secret", secret="sssh"),
    )
    base.update(kw)
    return ProviderManifest(**base)


# -- manifest + registry ------------------------------------------------------
def test_registry_register_list_and_upsert() -> None:
    reg = ProviderRegistry()
    reg.register_provider(_manifest())
    assert [m.name for m in reg.providers()] == ["bytedesk"]
    # re-register = upsert (no duplicate)
    reg.register_provider(_manifest(base_url="https://new/"))
    assert len(reg.providers()) == 1
    assert reg.get("bytedesk").base_url == "https://new"  # rstrip("/")


def test_manifest_from_dict_and_to_dict_hides_secret() -> None:
    m = ProviderManifest.from_dict(
        {
            "name": "p",
            "base_url": "https://x.test/api/",
            "sensors": ["s1"],
            "actuators": [{"name": "a1", "risk_tier": 4}],
            "outcomes": ["outcome.booked"],
            "webhook_sources": ["gh"],
            "auth": {"header": "X-Secret", "secret": "TOPSECRET"},
        }
    )
    assert m.base_url == "https://x.test/api"
    assert m.actuators[0].risk_tier == 4
    out = m.to_dict()
    assert out["auth"] == {"header": "X-Secret"}  # secret never emitted
    assert "TOPSECRET" not in str(out)


# -- remote adapters (mock httpx via injected post) ---------------------------
def test_remote_sensor_maps_response_and_sends_auth() -> None:
    seen: dict = {}

    def fake_post(url, body, headers):
        seen["url"] = url
        seen["body"] = body
        seen["headers"] = headers
        return {"satisfied": True, "value": "done", "stale_after_s": 30}

    sensor = RemoteSensor("jira_issue", _manifest(), post=fake_post)
    reading = sensor.evaluate({"issue": "BDP-1"}, SensorContext(goal=None, goal_store=None, now=99))

    assert reading["satisfied"] is True
    assert reading["value"] == "done"
    assert reading["stale_after_s"] == 30
    assert seen["url"] == "https://platform.bytedesk.ai/api/engine/goal-sensors/jira_issue/evaluate"
    assert seen["body"] == {"query": {"issue": "BDP-1"}, "now": 99}
    assert seen["headers"] == {"X-Engine-Secret": "sssh"}


def test_remote_sensor_fails_closed_on_malformed_response() -> None:
    sensor = RemoteSensor("jira_issue", _manifest(), post=lambda *_: {})
    r = sensor.evaluate({}, SensorContext(goal=None, goal_store=None, now=5))
    assert r["satisfied"] is False  # missing field never over-fires
    assert r["observed_at"] == 5


def test_remote_actuator_maps_response_and_sends_auth() -> None:
    seen: dict = {}

    async def fake_post(url, body, headers):
        seen.update(url=url, body=body, headers=headers)
        return {"ok": True, "output": {"id": "msg_1"}}

    actuator = RemoteActuator("send_email", _manifest(), risk_tier=3, post=fake_post)
    result = asyncio.run(actuator.execute({"to": "a@b.c"}))

    assert result.ok is True
    assert result.output == {"id": "msg_1"}
    assert seen["url"] == "https://platform.bytedesk.ai/api/engine/goal-actuators/send_email/execute"
    assert seen["body"] == {"action": {"to": "a@b.c"}}
    assert seen["headers"] == {"X-Engine-Secret": "sssh"}


def test_register_remote_providers_populates_both_registries() -> None:
    provider_reg = ProviderRegistry()
    provider_reg.register_provider(_manifest())
    sensor_reg = build_default_registry()
    actuator_reg = ActuatorRegistry()

    register_remote_providers(
        provider_reg,
        sensor_registry=sensor_reg,
        actuator_registry=actuator_reg,
        sync_post=lambda *_: {"satisfied": False},
    )

    assert "jira_issue" in sensor_reg.names()
    assert isinstance(sensor_reg.get("jira_issue"), RemoteSensor)
    assert "send_email" in actuator_reg.names()
    assert isinstance(actuator_reg.get("send_email"), RemoteActuator)


# -- fake provider drives the resolver end-to-end -----------------------------
def test_fake_provider_gates_a_goal_actionable_via_resolver() -> None:
    """A FakeProvider's sensor feeds the resolver — proving the full Phase 1-4
    seam runs standalone with no connected app + no network."""
    from bytedesk_omnigent.engine.resolver import resolve

    sensor_reg = build_default_registry()
    actuator_reg = ActuatorRegistry()
    provider = FakeProvider(
        sensors={"revenue": FakeSensor("revenue", satisfied=True, value=42)},
        actuators={"refund": FakeActuator("refund")},
    )
    provider.register_into(sensor_registry=sensor_reg, actuator_registry=actuator_reg)

    class _Goal:
        id = "g1"
        readiness_kind = "immediate"
        dependencies: list = []
        payload = {
            "condition": {
                "type": "leaf",
                "sensor": "revenue",
                "query": {},
                "predicate": {"op": "exists"},
            }
        }

    out = resolve(_Goal(), registry=sensor_reg, goal_store=None, now=1000)
    assert out["actionable"] is True


def test_fake_actuator_records_calls() -> None:
    a = FakeActuator("refund")
    result = asyncio.run(a.execute({"amount": 5}))
    assert result.ok is True
    assert a.calls == [{"amount": 5}]
