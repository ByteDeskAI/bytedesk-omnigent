"""Tests for the native ``kpi`` sensor (BDP-2595, Wave 2).

The kpi sensor reads a generic metric from an in-process source and returns it as
the reading ``value`` (so a condition-tree predicate can threshold it) plus a
convenience ``satisfied`` when the query carries an inline ``threshold``. No
network, no LLM, no domain knowledge — the source is injected.
"""
from __future__ import annotations

from bytedesk_omnigent.engine.sensors import (
    KpiSensor,
    SensorContext,
    build_default_registry,
)


def _ctx(now=1000) -> SensorContext:
    return SensorContext(goal=None, goal_store=None, now=now)


def test_kpi_reads_metric_value_from_injected_source() -> None:
    sensor = KpiSensor(metric_source=lambda metric, scope: 42.0)
    r = sensor.evaluate({"metric": "revenue"}, _ctx())
    assert r["value"] == 42.0
    # No inline threshold → satisfied iff the metric exists (is not None).
    assert r["satisfied"] is True


def test_kpi_missing_metric_is_unsatisfied() -> None:
    sensor = KpiSensor(metric_source=lambda metric, scope: None)
    r = sensor.evaluate({"metric": "revenue"}, _ctx())
    assert r["satisfied"] is False
    assert r["value"] is None


def test_kpi_inline_threshold_satisfied() -> None:
    sensor = KpiSensor(metric_source=lambda metric, scope: 100.0)
    assert sensor.evaluate({"metric": "mrr", "threshold": 50}, _ctx())["satisfied"] is True
    assert sensor.evaluate({"metric": "mrr", "threshold": 200}, _ctx())["satisfied"] is False


def test_kpi_inline_threshold_with_op() -> None:
    sensor = KpiSensor(metric_source=lambda metric, scope: 10.0)
    # default op is >= ; an explicit lt op flips the comparison.
    assert sensor.evaluate({"metric": "errors", "threshold": 5, "op": "lt"}, _ctx())["satisfied"] is False
    assert sensor.evaluate({"metric": "errors", "threshold": 20, "op": "lt"}, _ctx())["satisfied"] is True


def test_kpi_passes_scope_to_source() -> None:
    seen: dict = {}

    def source(metric, scope):
        seen["metric"] = metric
        seen["scope"] = scope
        return 1.0

    KpiSensor(metric_source=source).evaluate({"metric": "m", "scope": "tenant-x"}, _ctx())
    assert seen == {"metric": "m", "scope": "tenant-x"}


def test_kpi_registered_as_builtin() -> None:
    reg = build_default_registry()
    assert "kpi" in reg.names()
    assert isinstance(reg.get("kpi"), KpiSensor)


def test_kpi_default_source_reads_goal_store_scoreboard(tmp_path) -> None:
    from bytedesk_omnigent.goals import SqlAlchemyGoalStore

    store = SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'goals.db'}")
    store.record_score(agent_id="maya", metric="deals", value=7.0, now=10)
    # Zero-arg construction (the registry factory shape) → default source reads the
    # goal store on ctx.
    sensor = KpiSensor()
    ctx = SensorContext(goal=None, goal_store=store, now=100)
    r = sensor.evaluate({"metric": "deals"}, ctx)
    assert r["value"] == 7.0
    assert r["satisfied"] is True
