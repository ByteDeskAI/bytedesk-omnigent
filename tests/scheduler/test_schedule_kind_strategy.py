"""Seam tests for the schedule-kind Strategy registry (BDP-2349 #13)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bytedesk_omnigent.scheduler import compute_next_fire, register_schedule_kind


def test_interval_strategy_unchanged() -> None:
    assert compute_next_fire("interval", "60", 1000) == 1060
    assert compute_next_fire("interval", "5", 0) == 5


def test_once_strategy_returns_none() -> None:
    assert compute_next_fire("once", "ignored", 1000) is None


def test_cron_strategy_computes_next_fire() -> None:
    after = int(datetime(2026, 6, 25, 13, 5, tzinfo=timezone.utc).timestamp())
    expected = int(datetime(2026, 6, 25, 14, 0, tzinfo=timezone.utc).timestamp())
    assert compute_next_fire("cron", "0 * * * *", after) == expected


def test_unknown_kind_raises_value_error() -> None:
    with pytest.raises(ValueError):
        compute_next_fire("rrule", "FREQ=DAILY", 1000)


def test_register_new_schedule_kind_strategy() -> None:
    register_schedule_kind("test_double", lambda expr, after: after + 2 * int(expr))
    try:
        assert compute_next_fire("test_double", "10", 100) == 120
    finally:
        from bytedesk_omnigent.scheduler import scheduler as _sched

        _sched._SCHEDULE_KIND_STRATEGIES.pop("test_double", None)
