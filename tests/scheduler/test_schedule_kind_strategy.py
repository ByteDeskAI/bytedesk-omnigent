"""Seam tests for the schedule-kind Strategy registry (BDP-2349 #13).

Proves: built-in interval/once behavior is byte-identical, cron stays the same
NotImplementedError seam, an unknown kind raises ValueError, and a new kind can
be registered as a strategy without editing compute_next_fire.
"""
from __future__ import annotations

import pytest

from bytedesk_omnigent.scheduler import compute_next_fire, register_schedule_kind


def test_interval_strategy_unchanged() -> None:
    assert compute_next_fire("interval", "60", 1000) == 1060
    assert compute_next_fire("interval", "5", 0) == 5


def test_once_strategy_returns_none() -> None:
    assert compute_next_fire("once", "ignored", 1000) is None


def test_cron_seam_still_not_implemented() -> None:
    # The cron seam preserves the historical NotImplementedError until a real
    # croniter-backed strategy is registered.
    with pytest.raises(NotImplementedError):
        compute_next_fire("cron", "* * * * *", 1000)


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
