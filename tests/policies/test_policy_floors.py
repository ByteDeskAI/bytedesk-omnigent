"""Safety-floor enforcement for the built-in policy factories (BDP-2411, ADR-0150).

Each policy floor must fail CLOSED at construction time: a factory param that
would silently weaken or disable a safety policy raises ``PolicyFloorError``, so
the policy can never be built/attached in a bypassed state. Safe params (incl.
the seeded defaults) still construct cleanly.
"""

from __future__ import annotations

import pytest

from bytedesk_omnigent.policies._floors import PolicyFloorError
from bytedesk_omnigent.policies.budget import cost_hard_stop
from bytedesk_omnigent.policies.delegation import delegation_authority
from bytedesk_omnigent.policies.forever_gate import forever_denied
from bytedesk_omnigent.policies.outreach_compliance import outreach_compliance
from bytedesk_omnigent.policies.spawn_governor import spawn_breadth_governor
from bytedesk_omnigent.policies.two_key import two_key_required
from bytedesk_omnigent.policies.verify_gate import verify_as_gate


# ── two-key: min_approvers cannot collapse the two-person rule ────────────────
@pytest.mark.parametrize("bad", [1, 0, -1, True])
def test_two_key_rejects_below_floor(bad: object) -> None:
    with pytest.raises(PolicyFloorError):
        two_key_required(["billing\\.refund"], min_approvers=bad)  # type: ignore[arg-type]


def test_two_key_accepts_floor_and_above() -> None:
    assert two_key_required(["billing\\.refund"], min_approvers=2)
    assert two_key_required(["billing\\.refund"])  # default 2
    assert two_key_required(["billing\\.refund"], min_approvers=3)


# ── budget: the hard ceiling must be a finite, positive, non-absurd number ────
@pytest.mark.parametrize("bad", [0, -5, "100", None, float("inf"), float("nan"), 1e12])
def test_cost_hard_stop_rejects_unusable_ceiling(bad: object) -> None:
    with pytest.raises(PolicyFloorError):
        cost_hard_stop(bad)  # type: ignore[arg-type]


def test_cost_hard_stop_accepts_sane_ceiling() -> None:
    assert cost_hard_stop(5.0)
    assert cost_hard_stop(0.01)


# ── outreach: the unsubscribe legal floor cannot be turned off ────────────────
def test_outreach_rejects_disabled_unsubscribe() -> None:
    with pytest.raises(PolicyFloorError):
        outreach_compliance(["email\\.send"], require_unsubscribe=False)


def test_outreach_accepts_required_unsubscribe() -> None:
    assert outreach_compliance(["email\\.send"])  # default True
    assert outreach_compliance(["email\\.send"], require_unsubscribe=True)


# ── spawn governor: reject negatives + effectively-infinite caps; 0 = deny-all ─
@pytest.mark.parametrize("bad", [-1, 10_001, "16", True])
def test_spawn_governor_rejects_out_of_range(bad: object) -> None:
    with pytest.raises(PolicyFloorError):
        spawn_breadth_governor(bad)  # type: ignore[arg-type]


def test_spawn_governor_allows_zero_disable_and_sane_caps() -> None:
    assert spawn_breadth_governor(0)  # deny-all is a valid restriction
    assert spawn_breadth_governor(16)
    assert spawn_breadth_governor(16, per_task_max_spawns=4)


def test_spawn_governor_rejects_bad_per_task_cap() -> None:
    with pytest.raises(PolicyFloorError):
        spawn_breadth_governor(16, per_task_max_spawns=-1)


# ── verify-as-gate: an empty gated set is a silent fail-open ──────────────────
def test_verify_gate_rejects_empty_gated_tools() -> None:
    with pytest.raises(PolicyFloorError):
        verify_as_gate(gated_tools=[])


def test_verify_gate_accepts_non_empty() -> None:
    assert verify_as_gate(gated_tools=["bytedesk_release_trigger"])


# ── delegation: authority must be enumerated, never a wildcard ────────────────
@pytest.mark.parametrize("bad", ["*", "**", ".*"])
def test_delegation_rejects_wildcard(bad: str) -> None:
    with pytest.raises(PolicyFloorError):
        delegation_authority(["ag_dev", bad])


def test_delegation_accepts_named_targets() -> None:
    assert delegation_authority(["ag_dev", "ag_qa"])


# ── forever-denied: a non-compiling pattern fails closed at construction ──────
def test_forever_denied_rejects_uncompilable_pattern() -> None:
    with pytest.raises(PolicyFloorError):
        forever_denied(["deploy\\.run", "billing\\.(refund"])  # unbalanced paren


def test_forever_denied_accepts_valid_patterns() -> None:
    assert forever_denied(["deploy\\.run", "billing\\.(refund|charge)"])
