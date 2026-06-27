"""Contention arbitration (BDP-2597, Phase 2 Wave 4).

When multiple ready goals contend for the same actor, an arbiter orders them by
tier × priority × ROI; the tick funds in that order and losers wait (with a
``waiting_reason``) rather than double-spawning. Off → today's straight ROI order.

Fakes only, no network/LLM.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bytedesk_omnigent.engine.arbitration import arbitrate


@dataclass
class _G:
    id: str
    owner_agent_id: str | None = None
    tier: str = "org"
    priority: int = 3
    expected_value_cents: int = 0
    confidence: float = 0.5
    risk_tier: str = "low"
    created_at: int = 0
    _payload: dict[str, Any] = field(default_factory=dict)

    @property
    def payload(self) -> dict[str, Any]:
        return self._payload

    @property
    def attributes(self) -> dict[str, Any]:
        attrs = self._payload.get("attributes")
        return attrs if isinstance(attrs, dict) else {}


def test_no_contention_passes_all_through() -> None:
    # Distinct actors → no contention → every goal is a winner, none waits.
    goals = [_G(id="a", owner_agent_id="alice"), _G(id="b", owner_agent_id="bob")]
    winners, losers = arbitrate(goals)
    assert [g.id for g in winners] == ["a", "b"]
    assert losers == []


def test_contending_goals_one_winner_others_wait() -> None:
    # Two goals for the SAME actor → only one is funded this tick; the other waits.
    hi = _G(id="hi", owner_agent_id="alice", expected_value_cents=1000, confidence=1.0)
    lo = _G(id="lo", owner_agent_id="alice", expected_value_cents=10, confidence=1.0)
    winners, losers = arbitrate([lo, hi])
    assert [g.id for g in winners] == ["hi"]
    assert [g.id for g, _reason in losers] == ["lo"]
    assert all(reason for _g, reason in losers)  # every loser carries a reason


def test_arbitration_orders_by_tier_priority_roi() -> None:
    # Same actor, same EV: a lower priority NUMBER (more urgent) wins the slot.
    urgent = _G(id="urgent", owner_agent_id="x", priority=1, expected_value_cents=100, confidence=1.0)
    normal = _G(id="normal", owner_agent_id="x", priority=5, expected_value_cents=100, confidence=1.0)
    winners, _losers = arbitrate([normal, urgent])
    assert winners[0].id == "urgent"


def test_roi_breaks_within_same_priority() -> None:
    a = _G(id="a", owner_agent_id="x", priority=3, expected_value_cents=100, confidence=1.0)
    b = _G(id="b", owner_agent_id="x", priority=3, expected_value_cents=900, confidence=1.0)
    winners, _losers = arbitrate([a, b])
    assert winners[0].id == "b"  # higher ROI wins the slot


def test_unowned_goals_do_not_contend() -> None:
    # Goals with no actor yet can't contend for one — both pass through.
    g1 = _G(id="g1", owner_agent_id=None)
    g2 = _G(id="g2", owner_agent_id=None)
    winners, losers = arbitrate([g1, g2])
    assert {g.id for g in winners} == {"g1", "g2"}
    assert losers == []
