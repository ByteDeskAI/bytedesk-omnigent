"""Tests for the accountability tick: rebalance + escalate (BDP-2272 C4, ADR-0142)."""
from __future__ import annotations

from bytedesk_omnigent.accountability import run_accountability_tick
from bytedesk_omnigent.goals import SqlAlchemyGoalStore
from bytedesk_omnigent.peer import SqlAlchemyPeerMessageStore


def _stores(tmp_path) -> tuple[SqlAlchemyGoalStore, SqlAlchemyPeerMessageStore]:
    db = f"sqlite:///{tmp_path / 'org.db'}"
    return SqlAlchemyGoalStore(db), SqlAlchemyPeerMessageStore(db)


def test_rebalance_reopens_stalled_owned_goal_and_notifies_owner(tmp_path) -> None:
    goals, peers = _stores(tmp_path)
    goal = goals.create_goal(title="ship feature", now=100)
    goals.claim_goal(goal_id=goal.id, owner_agent_id="ag_alice", now=100)  # updated_at=100

    report = run_accountability_tick(
        goals, peers, manager_agent_id="ag_mgr", stall_seconds=3600, now=100 + 3601
    )

    assert report.rebalanced == 1
    reopened = goals.list_goals(status="open")
    assert any(g.id == goal.id and g.owner_agent_id is None for g in reopened)
    # The dropped owner is notified.
    feed = peers.topic_feed(topic="accountability:rebalance")
    assert len(feed) == 1
    assert feed[0].to_agent == "ag_alice"


def test_fresh_owned_goal_is_not_rebalanced(tmp_path) -> None:
    goals, peers = _stores(tmp_path)
    goal = goals.create_goal(title="x", now=100)
    goals.claim_goal(goal_id=goal.id, owner_agent_id="ag_a", now=100)

    report = run_accountability_tick(
        goals, peers, stall_seconds=3600, now=100 + 100  # only 100s old
    )

    assert report.rebalanced == 0
    assert goals.list_goals(status="assigned")[0].owner_agent_id == "ag_a"


def test_escalates_blocked_goal_to_manager(tmp_path) -> None:
    goals, peers = _stores(tmp_path)
    goal = goals.create_goal(title="db migration", now=100)
    goals.advance_goal(goal_id=goal.id, status="blocked", now=100)

    report = run_accountability_tick(
        goals, peers, manager_agent_id="ag_mgr", stall_seconds=3600, now=200
    )

    assert report.escalated == 1
    feed = peers.topic_feed(topic="accountability:escalation")
    assert len(feed) == 1
    assert feed[0].to_agent == "ag_mgr"
    assert feed[0].kind == "escalation"


def test_no_manager_skips_escalation_but_still_rebalances(tmp_path) -> None:
    goals, peers = _stores(tmp_path)
    stalled = goals.create_goal(title="stalled", now=100)
    goals.claim_goal(goal_id=stalled.id, owner_agent_id="ag_a", now=100)
    blocked = goals.create_goal(title="blocked", now=100)
    goals.advance_goal(goal_id=blocked.id, status="blocked", now=100)

    report = run_accountability_tick(
        goals, peers, manager_agent_id=None, stall_seconds=3600, now=100 + 3601
    )

    assert report.escalated == 0
    assert report.rebalanced == 1
    assert peers.topic_feed(topic="accountability:escalation") == []


def test_escalation_fires_once_per_blocked_episode_not_every_tick(tmp_path) -> None:
    """A blocked goal escalates ONCE, not on every tick (no escalation spam);
    re-blocking re-arms it for one more escalation (BDP-2283 #7)."""
    goals, peers = _stores(tmp_path)
    goal = goals.create_goal(title="db migration", now=100)
    goals.advance_goal(goal_id=goal.id, status="blocked", now=100)

    first = run_accountability_tick(goals, peers, manager_agent_id="ag_mgr", now=200)
    second = run_accountability_tick(goals, peers, manager_agent_id="ag_mgr", now=300)

    assert first.escalated == 1
    assert second.escalated == 0  # not re-escalated
    assert len(peers.topic_feed(topic="accountability:escalation")) == 1

    # Unblock, then re-block → escalates exactly once more.
    goals.advance_goal(goal_id=goal.id, status="in_progress", now=400)
    goals.advance_goal(goal_id=goal.id, status="blocked", now=500)
    third = run_accountability_tick(goals, peers, manager_agent_id="ag_mgr", now=600)

    assert third.escalated == 1
    assert len(peers.topic_feed(topic="accountability:escalation")) == 2
