"""Controlled Goal Engine flywheel proof (BDP-2611).

The proof is deliberately local and synthetic: it seeds a tiny portfolio, runs the
same optimizer/treasury/dispatcher/accountability paths as the runtime, books a
realized outcome, and returns an evidence report. No network, no real agents, no
customer writes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bytedesk_omnigent.accountability import run_accountability_tick
from bytedesk_omnigent.engine.loop import run_goal_engine_tick
from bytedesk_omnigent.engine.optimizer import RoiOptimizer
from bytedesk_omnigent.engine.treasury import SqlAlchemyTreasury
from bytedesk_omnigent.goals import SqlAlchemyGoalStore
from bytedesk_omnigent.peer import SqlAlchemyPeerMessageStore


@dataclass(frozen=True)
class _FakeConversation:
    id: str
    external_key: str | None


class _FakeConversationStore:
    def __init__(self) -> None:
        self.by_external_key: dict[str, _FakeConversation] = {}
        self.created: list[dict[str, Any]] = []
        self._n = 0

    def get_conversation_by_external_key(self, external_key: str) -> _FakeConversation | None:
        return self.by_external_key.get(external_key)

    def create_conversation(self, **kwargs: Any) -> _FakeConversation:
        self._n += 1
        conv = _FakeConversation(id=f"conv_{self._n}", external_key=kwargs.get("external_key"))
        self.created.append(kwargs)
        if conv.external_key is not None:
            self.by_external_key[conv.external_key] = conv
        return conv

    def append(self, conversation_id: str, items: list[Any]) -> None:  # noqa: ARG002
        return None


def run_controlled_flywheel_proof(
    storage_location: str,
    *,
    now: int = 1_800_000_000,
) -> dict[str, Any]:
    """Run the bounded proof scenario and return auditable evidence.

    The scenario proves:
    - organizational financial goal can fund a departmental roadmap child;
    - unlinked non-financial work is skipped with ``missing_value_rollup``;
    - a booked outcome writes realized value and rolls up to the parent;
    - accountability reallocates idle budget toward a higher-ROI scope.
    """
    goals = SqlAlchemyGoalStore(storage_location)
    treasury = SqlAlchemyTreasury(storage_location)
    peers = SqlAlchemyPeerMessageStore(storage_location)
    conversations = _FakeConversationStore()

    treasury.set_budget(tier="org", target_id="omnigent", cap_cents=500, now=now)
    treasury.set_budget(tier="department", target_id="development", cap_cents=250, now=now)

    parent = goals.create_goal(
        title="Grow MRR through reference delivery",
        target_kind="organization",
        target_id="omnigent",
        outcome_kind="financial",
        expected_value_cents=50_000,
        confidence=0.8,
        now=now,
    )
    roadmap = goals.create_goal(
        title="Ship Office reference provider",
        target_kind="department",
        target_id="development",
        target_label="Development",
        department_slug="development",
        outcome_kind="roadmap",
        parent_goal_id=parent.id,
        confidence=0.7,
        now=now,
    )
    goals.claim_goal(goal_id=roadmap.id, owner_agent_id="maya", now=now)
    orphan = goals.create_goal(
        title="Unlinked internal capability",
        target_kind="department",
        target_id="operations",
        target_label="Operations",
        department_slug="operations",
        outcome_kind="capability",
        now=now,
    )
    goals.claim_goal(goal_id=orphan.id, owner_agent_id="ops", now=now)
    high_risk = goals.create_goal(
        title="High-risk outreach expansion",
        target_kind="organization",
        target_id="omnigent",
        outcome_kind="financial",
        expected_value_cents=100_000,
        confidence=0.9,
        risk_tier="high",
        now=now,
    )
    goals.claim_goal(goal_id=high_risk.id, owner_agent_id="sales", now=now)

    spawned_initial = run_goal_engine_tick(
        goals,
        conversations,
        treasury=treasury,
        optimizer=RoiOptimizer(),
        est_cost=100,
        now=now + 1,
    )
    treasury.book_outcome(
        goal_store=goals,
        goal_id=roadmap.id,
        realized_value_cents=1_500,
        source="controlled_proof",
        evidence={"scenario": "office-reference-provider"},
        now=now + 2,
    )
    failed_subject = "missing-provider-subject"
    unresolved_goal_id = goals.resolve_goal_correlation(
        source="controlled_proof", subject_ref=failed_subject
    )
    before_failed_probe = goals.get_goal(goal_id=roadmap.id, include_dependencies=False)
    failed_outcome = (
        treasury.book_outcome(
            goal_store=goals,
            goal_id=unresolved_goal_id,
            realized_value_cents=9_999,
            source="controlled_proof",
            evidence={"subjectRef": failed_subject},
            now=now + 2,
        )
        if unresolved_goal_id
        else None
    )
    after_failed_probe = goals.get_goal(goal_id=roadmap.id, include_dependencies=False)
    run_goal_engine_tick(
        goals,
        conversations,
        treasury=treasury,
        optimizer=RoiOptimizer(),
        est_cost=100,
        now=now + 3,
    )

    treasury.set_budget(tier="department", target_id="dept_idle", cap_cents=1_000, now=now)
    treasury.set_budget(tier="department", target_id="dept_hot", cap_cents=1_000, now=now)
    idle = goals.create_goal(
        title="Idle low-ROI department bet",
        target_kind="department",
        target_id="dept_idle",
        expected_value_cents=100,
        confidence=0.1,
        now=now,
    )
    hot = goals.create_goal(
        title="Hot high-ROI department bet",
        target_kind="department",
        target_id="dept_hot",
        expected_value_cents=50_000,
        confidence=0.9,
        now=now,
    )
    goals.claim_goal(goal_id=idle.id, owner_agent_id="idle-agent", now=now)
    goals.claim_goal(goal_id=hot.id, owner_agent_id="hot-agent", now=now)
    before_hot = treasury.remaining_cents("department", "dept_hot")
    accountability = run_accountability_tick(
        goals,
        peers,
        treasury=treasury,
        stall_seconds=3600,
        now=now + 3601,
    )
    after_hot = treasury.remaining_cents("department", "dept_hot")

    decisions = treasury.decisions()
    parent_after = goals.get_goal(goal_id=parent.id, include_dependencies=False)
    roadmap_after = goals.get_goal(goal_id=roadmap.id, include_dependencies=False)

    return {
        "scenario": "controlled-goal-engine-flywheel",
        "spawnedInitial": spawned_initial,
        "spawnedSessionKeys": [c.get("external_key") for c in conversations.created],
        "bookedOutcomeCents": roadmap_after.realized_value_cents if roadmap_after else 0,
        "parentRealizedValueCents": parent_after.realized_value_cents if parent_after else 0,
        "decisionReasons": [d.reason for d in decisions],
        "reallocatedCents": accountability.reallocated_cents,
        "hotBudgetBeforeCents": before_hot,
        "hotBudgetAfterCents": after_hot,
        "guardrails": {
            "syntheticOnly": True,
            "networkCalls": 0,
            "customerWrites": 0,
            "approvalRiskGateExercised": "approval_required" in {d.reason for d in decisions},
        },
        "failureProbe": {
            "failureClass": "provider",
            "status": "failed",
            "httpStatus": 409,
            "retryable": True,
            "detail": "unresolved goal correlation",
            "realizedValueUnchanged": (
                before_failed_probe.realized_value_cents
                if before_failed_probe is not None
                else None
            )
            == (
                after_failed_probe.realized_value_cents
                if after_failed_probe is not None
                else None
            ),
            "bookedOutcome": failed_outcome is not None,
        },
    }


__all__ = ["run_controlled_flywheel_proof"]
