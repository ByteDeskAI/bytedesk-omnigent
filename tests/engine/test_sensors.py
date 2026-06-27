"""Tests for the built-in goal sensors + the sensor registry (BDP-2584)."""
from __future__ import annotations

import os

from bytedesk_omnigent.engine.sensors import (
    DeliverySensor,
    GoalOutcomeSensor,
    ManualSensor,
    SensorContext,
    TimeSensor,
    build_default_registry,
)
from bytedesk_omnigent.engine.sensors.registry import SENSOR_SEAM, SensorRegistry
from bytedesk_omnigent.goals import SqlAlchemyGoalStore


def _store(tmp_path) -> SqlAlchemyGoalStore:
    return SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'goals.db'}")


def _ctx(store, goal=None, now=1000) -> SensorContext:
    return SensorContext(goal=goal, goal_store=store, now=now)


# -- goal_outcome ------------------------------------------------------------
def test_goal_outcome_satisfied_when_other_goal_done(tmp_path) -> None:
    store = _store(tmp_path)
    other = store.create_goal(title="dependency goal", now=10)
    store.claim_goal(goal_id=other.id, owner_agent_id="maya", now=11)
    store.advance_goal(goal_id=other.id, status="in_progress", now=12)
    store.advance_goal(goal_id=other.id, status="done", now=13)

    sensor = GoalOutcomeSensor()
    r = sensor.evaluate({"goal_id": other.id}, _ctx(store))
    assert r["satisfied"] is True
    assert r["value"] == "done"


def test_goal_outcome_unsatisfied_when_open(tmp_path) -> None:
    store = _store(tmp_path)
    other = store.create_goal(title="dependency goal", now=10)
    r = GoalOutcomeSensor().evaluate({"goal_id": other.id}, _ctx(store))
    assert r["satisfied"] is False
    assert r["value"] == "open"


def test_goal_outcome_missing_goal_is_unsatisfied(tmp_path) -> None:
    store = _store(tmp_path)
    r = GoalOutcomeSensor().evaluate({"goal_id": "nope"}, _ctx(store))
    assert r["satisfied"] is False
    assert r["value"] is None


# -- time --------------------------------------------------------------------
def test_time_after(tmp_path) -> None:
    store = _store(tmp_path)
    sensor = TimeSensor()
    assert sensor.evaluate({"after": 500}, _ctx(store, now=1000))["satisfied"] is True
    assert sensor.evaluate({"after": 5000}, _ctx(store, now=1000))["satisfied"] is False


def test_time_within_window(tmp_path) -> None:
    store = _store(tmp_path)
    sensor = TimeSensor()
    assert sensor.evaluate({"within": [100, 2000]}, _ctx(store, now=1000))["satisfied"] is True
    assert sensor.evaluate({"within": [2000, 3000]}, _ctx(store, now=1000))["satisfied"] is False


# -- manual ------------------------------------------------------------------
def test_manual_reads_dependency_status(tmp_path) -> None:
    store = _store(tmp_path)
    goal = store.create_goal(
        title="needs sign-off",
        dependencies=[{"kind": "manual", "label": "exec approval"}],
        now=10,
    )
    dep = goal.dependencies[0]
    sensor = ManualSensor()
    assert sensor.evaluate({"dep_id": dep.id}, _ctx(store, goal=goal))["satisfied"] is False

    store.update_dependency(goal_id=goal.id, dependency_id=dep.id, status="satisfied", now=11)
    goal = store.get_goal(goal_id=goal.id)
    assert sensor.evaluate({"dep_id": dep.id}, _ctx(store, goal=goal))["satisfied"] is True


# -- delivery (milestone two-key, read from stored state) --------------------
def _delivery_payload(status="in_progress"):
    return {
        "hierarchy": {
            "milestones": [
                {"taskKey": "BDP-1", "title": "API", "status": status},
            ]
        }
    }


def test_delivery_reads_milestone_status_from_payload(tmp_path) -> None:
    store = _store(tmp_path)
    goal = store.create_goal(title="goal", payload=_delivery_payload("in_progress"), now=10)
    sensor = DeliverySensor()
    r = sensor.evaluate({"task_key": "BDP-1"}, _ctx(store, goal=goal))
    assert r["satisfied"] is False
    assert r["value"] == "in_progress"

    goal2 = store.create_goal(title="goal2", payload=_delivery_payload("done"), now=10)
    r2 = sensor.evaluate({"task_key": "BDP-1"}, _ctx(store, goal=goal2))
    assert r2["satisfied"] is True
    assert r2["value"] == "done"


def test_delivery_unknown_milestone(tmp_path) -> None:
    store = _store(tmp_path)
    goal = store.create_goal(title="goal", payload=_delivery_payload(), now=10)
    r = DeliverySensor().evaluate({"task_key": "NOPE"}, _ctx(store, goal=goal))
    assert r["satisfied"] is False
    assert r["value"] is None


# -- registry ----------------------------------------------------------------
def test_registry_register_get_default() -> None:
    reg = build_default_registry()
    assert "goal_outcome" in reg.names()
    assert "delivery" in reg.names()
    assert reg.seam == SENSOR_SEAM
    assert isinstance(reg.get("time"), TimeSensor)


def test_registry_override_env(monkeypatch) -> None:
    # A bare PluggableRegistry default-resolution override still works for the seam.
    reg: SensorRegistry = SensorRegistry(default=("time", TimeSensor))
    reg.register("manual", ManualSensor)
    monkeypatch.setenv(f"OMNIGENT_USE_{SENSOR_SEAM.upper()}", "manual")
    assert isinstance(reg.resolve_default(), ManualSensor)
    os.environ.pop(f"OMNIGENT_USE_{SENSOR_SEAM.upper()}", None)
    assert isinstance(reg.resolve_default(), TimeSensor)
