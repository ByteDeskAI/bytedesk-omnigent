"""Tests for inbound processors + fan-out routing (ADR-0155, BDP-2561)."""
from __future__ import annotations

from bytedesk_omnigent.goals import SqlAlchemyGoalStore
from bytedesk_omnigent.inbound.event import InboundEvent
from bytedesk_omnigent.inbound.pipeline import ingest
from bytedesk_omnigent.inbound.processors import (
    AgenticInboxProcessor,
    GoalDeliveryProcessor,
    SignalBusProcessor,
    all_processors,
    interested_processors,
)
from bytedesk_omnigent.inbound.store import SqlAlchemyInboundEventStore
from bytedesk_omnigent.inbound.translators import CHANNEL_GOAL_DELIVERY

_GH_MERGED = {
    "action": "closed",
    "pull_request": {"number": 987, "merged": True, "head": {"ref": "feature/x"},
                     "base": {"ref": "develop"}, "merge_commit_sha": "deadbeef"},
    "repository": {"full_name": "ByteDeskAI/bytedesk-platform"},
}


def _event(type_):
    return InboundEvent(idempotency_key="k", source="s", type=type_, occurred_at=1,
                        received_at=1, raw_payload={})


# -- Message Filter predicates + Content-Based Router ------------------------
def test_interest_predicates() -> None:
    assert GoalDeliveryProcessor().interested(_event("pull_request.merged")) is True
    assert GoalDeliveryProcessor().interested(_event("email.received")) is False
    assert SignalBusProcessor().interested(_event("signal.deliver")) is True
    assert AgenticInboxProcessor().interested(_event("email.received")) is True


def test_content_based_router_routes_one_processor_per_type() -> None:
    assert [p.name for p in interested_processors(_event("pull_request.merged"))] == ["goal-delivery"]
    assert [p.name for p in interested_processors(_event("email.received"))] == ["agentic-inbox"]
    assert [p.name for p in interested_processors(_event("signal.deliver"))] == ["signal-bus"]
    assert interested_processors(_event("unknown.thing")) == []


def test_registry_exposes_all_three() -> None:
    assert {p.name for p in all_processors()} == {"goal-delivery", "signal-bus", "agentic-inbox"}


# -- GoalDeliveryProcessor end-to-end through ingest -------------------------
def test_goal_delivery_processor_flips_milestone_via_ingest(tmp_path, monkeypatch) -> None:
    goal_store = SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'goals.db'}")
    goal = goal_store.create_goal(
        title="demo", source="goal-planner", now=100,
        payload={"jiraEpicKey": "BDP-E", "hierarchy": {"milestones": [{
            "taskKey": "BDP-T", "status": "in_progress", "jiraDone": False, "prMerged": False,
            "delivery": {"jira": {"taskKey": "BDP-T"}, "github": {
                "repo": "ByteDeskAI/bytedesk-platform", "branch": "feature/x",
                "baseBranch": "develop", "prNumber": None}}}]}})
    monkeypatch.setattr("bytedesk_omnigent.goals.get_goal_store", lambda: goal_store)

    inbound_store = SqlAlchemyInboundEventStore(f"sqlite:///{tmp_path / 'inbound.db'}")
    r = ingest(channel=CHANNEL_GOAL_DELIVERY, source="github", raw_payload=_GH_MERGED,
               headers={"x-github-delivery": "g1"}, store=inbound_store,
               processors=all_processors(), now=110)
    assert r.status == "projected" and r.http_status == 202
    milestone = goal_store.get_goal(goal_id=goal.id).payload["hierarchy"]["milestones"][0]
    assert milestone["status"] == "awaiting_jira" and milestone["prMerged"] is True


def test_goal_delivery_no_match_is_404(tmp_path, monkeypatch) -> None:
    goal_store = SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'goals.db'}")  # empty backlog
    monkeypatch.setattr("bytedesk_omnigent.goals.get_goal_store", lambda: goal_store)
    inbound_store = SqlAlchemyInboundEventStore(f"sqlite:///{tmp_path / 'inbound.db'}")
    r = ingest(channel=CHANNEL_GOAL_DELIVERY, source="github", raw_payload=_GH_MERGED,
               headers={"x-github-delivery": "g1"}, store=inbound_store,
               processors=all_processors(), now=110)
    assert r.status == "no_match" and r.http_status == 404
    # still observable in the wire-tap log
    assert inbound_store.get(r.idempotency_key) is not None
