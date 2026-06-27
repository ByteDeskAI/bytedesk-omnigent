"""Boot-state observability for the goal engine (BDP-2599, Wave 6).

``goal_engine_boot_summary`` is a pure dict reflecting the engine's operating
state at loop startup: autonomy posture, coordination backplane mode, registered
providers, arming-enabled, tick interval. An operator reads one log line and
knows whether the org is armed + multi-replica-safe + which providers are wired.
"""
from __future__ import annotations

from bytedesk_omnigent.engine.config import GoalEngineConfig
from bytedesk_omnigent.engine.observability import goal_engine_boot_summary


class _FakeBackplane:
    def __init__(self, name: str) -> None:
        self.__class__.__name__ = name  # type: ignore[misc]


class _FakeRegistry:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    def providers(self):
        return [type("P", (), {"name": n})() for n in self._names]


def test_summary_reflects_gated_inprocess_no_providers() -> None:
    summary = goal_engine_boot_summary(
        config=GoalEngineConfig(),  # default = gated
        backplane=None,
        provider_registry=_FakeRegistry([]),
        arming_enabled=False,
        interval_seconds=30,
    )
    assert summary == {
        "autonomy_posture": "gated",
        "armed": False,
        "arming_enabled": False,
        "backplane": "inprocess",
        "providers": [],
        "tick_interval_seconds": 30,
    }


def test_summary_reflects_full_auto_nats_with_providers_armed() -> None:
    summary = goal_engine_boot_summary(
        config=GoalEngineConfig(autonomy_posture="full_auto"),
        backplane=_FakeBackplane("NatsBackplane"),
        provider_registry=_FakeRegistry(["stripe", "jira"]),
        arming_enabled=True,
        interval_seconds=15,
    )
    assert summary["autonomy_posture"] == "full_auto"
    assert summary["backplane"] == "nats"
    assert summary["providers"] == ["stripe", "jira"]
    assert summary["arming_enabled"] is True
    assert summary["armed"] is True
    assert summary["tick_interval_seconds"] == 15


def test_armed_requires_both_full_auto_and_arming_enabled() -> None:
    # full_auto posture but arming flag off → NOT armed (Wave-5 gate semantics).
    summary = goal_engine_boot_summary(
        config=GoalEngineConfig(autonomy_posture="full_auto"),
        backplane=None,
        provider_registry=_FakeRegistry([]),
        arming_enabled=False,
        interval_seconds=30,
    )
    assert summary["armed"] is False


def test_inprocess_backplane_classified() -> None:
    summary = goal_engine_boot_summary(
        config=GoalEngineConfig(),
        backplane=_FakeBackplane("InProcessBackplane"),
        provider_registry=_FakeRegistry([]),
        arming_enabled=False,
        interval_seconds=30,
    )
    assert summary["backplane"] == "inprocess"
