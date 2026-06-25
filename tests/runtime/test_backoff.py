"""Backoff-policy Strategy tests (BDP-2361, P9).

Proves the single registered exp-full-jitter curve matches the old hardcoded
numbers at both call sites (``llm_retry.compute_backoff_delay`` and
``RetryPolicy.compute_backoff_delay``), honors the cap, the ``retry_after``
floor, and the jitter flag, and is swappable with a fake.
"""

from __future__ import annotations

import pytest

from omnigent.kernel.pluggable import PluggableRegistry
from omnigent.runtime.backoff import (
    BackoffPolicy,
    ExpFullJitterBackoff,
    backoff_policy_registry,
    default_backoff_policy,
)


@pytest.fixture
def no_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``random.uniform`` to 1.0 so the full-jitter multiplier is a no-op."""
    monkeypatch.setattr("omnigent.runtime.backoff.random.uniform", lambda a, b: 1.0)


# ── Curve math (deterministic with no_jitter) ──


@pytest.mark.parametrize(
    ("exponent", "base", "cap", "expected"),
    [
        (0, 2.0, 30.0, 2.0),
        (1, 2.0, 30.0, 4.0),
        (2, 2.0, 30.0, 8.0),
        (3, 2.0, 30.0, 16.0),
        (4, 2.0, 30.0, 30.0),  # capped
        (10, 2.0, 5.0, 5.0),  # capped hard
    ],
)
def test_exp_full_jitter_curve(
    no_jitter: None, exponent: int, base: float, cap: float, expected: float
) -> None:
    assert ExpFullJitterBackoff().compute_delay(exponent, base, cap) == expected


def test_retry_after_floor_applied_before_cap(no_jitter: None) -> None:
    # base*2**0 = 2.0, retry_after raises it to 10.0, cap allows it.
    assert ExpFullJitterBackoff().compute_delay(0, 2.0, 30.0, retry_after_s=10.0) == 10.0
    # retry_after above cap is clamped by the cap.
    assert ExpFullJitterBackoff().compute_delay(0, 2.0, 5.0, retry_after_s=99.0) == 5.0


def test_jitter_off_returns_raw_curve(no_jitter: None) -> None:
    assert ExpFullJitterBackoff().compute_delay(2, 2.0, 30.0, jitter=False) == 8.0


def test_jitter_multiplier_bounds() -> None:
    policy = ExpFullJitterBackoff()
    for _ in range(200):
        d = policy.compute_delay(2, 2.0, 30.0)  # raw 8.0 → [4.0, 12.0]
        assert 4.0 <= d <= 12.0


# ── Call-site parity ──


def test_llm_retry_compute_backoff_matches_strategy(no_jitter: None) -> None:
    from omnigent.runtime.llm_retry import compute_backoff_delay

    # attempt_index is the 0-based exponent; jitter pinned to 1.0.
    assert compute_backoff_delay(2, 2.0, 30.0) == 8.0
    assert compute_backoff_delay(3, 10.0, 5.0) == 5.0  # capped


def test_retry_policy_compute_backoff_matches_strategy(no_jitter: None) -> None:
    from omnigent.spec.types import RetryPolicy

    policy = RetryPolicy(max_retries=5, backoff_base_s=2.0, backoff_max_s=30.0)
    # retry_index is 1-based → exponent retry_index-1.
    assert policy.compute_backoff_delay(1) == 2.0  # 2*2**0
    assert policy.compute_backoff_delay(3) == 8.0  # 2*2**2
    assert policy.compute_backoff_delay(2, retry_after_s=20.0) == 20.0  # floor


def test_retry_policy_jitter_flag_off(no_jitter: None) -> None:
    from omnigent.spec.types import RetryPolicy

    policy = RetryPolicy(
        max_retries=5, backoff_base_s=2.0, backoff_max_s=30.0, jitter=False
    )
    # With jitter False, value is the raw curve regardless of the patched uniform.
    assert policy.compute_backoff_delay(3) == 8.0


# ── Registry + swappability ──


def test_registry_default_is_exp_full_jitter() -> None:
    assert isinstance(default_backoff_policy(), ExpFullJitterBackoff)
    assert backoff_policy_registry().names() == ["exp_full_jitter"]


def test_policy_satisfies_protocol() -> None:
    assert isinstance(ExpFullJitterBackoff(), BackoffPolicy)


def test_seam_swappable_with_fake() -> None:
    class FakeBackoff:
        def compute_delay(
            self,
            exponent: int,
            base_s: float,
            max_s: float,
            *,
            retry_after_s: float | None = None,
            jitter: bool = True,
        ) -> float:
            return 99.0

    registry: PluggableRegistry[BackoffPolicy] = PluggableRegistry(
        "backoff_policy", default=("fake", FakeBackoff)
    )
    assert registry.resolve_default().compute_delay(0, 2.0, 30.0) == 99.0
