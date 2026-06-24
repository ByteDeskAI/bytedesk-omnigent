"""Tests for the accountability tick: rebalance + escalate (BDP-2272 C4, ADR-0142)."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Any

import pytest

import bytedesk_omnigent.maintenance as maintenance
from bytedesk_omnigent.accountability import accountability_loop, run_accountability_tick
from bytedesk_omnigent.goals import SqlAlchemyGoalStore
from bytedesk_omnigent.peer import SqlAlchemyPeerMessageStore

pytestmark_loop = pytest.mark.asyncio


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


@contextmanager
def _fake_lock(acquired: bool):
    yield acquired


async def _run_accountability_one_tick(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    acquired: bool = True,
    manager_agent_id: str | None = "ag_mgr",
) -> None:
    """Drive exactly one ``accountability_loop`` iteration then cancel."""
    goals, peers = _stores(tmp_path)
    monkeypatch.setattr("bytedesk_omnigent.goals.get_goal_store", lambda: goals)
    monkeypatch.setattr("bytedesk_omnigent.peer.get_peer_message_store", lambda: peers)

    calls = {"n": 0}

    async def _sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(maintenance.asyncio, "sleep", _sleep)
    monkeypatch.setattr(
        maintenance,
        "advisory_lock",
        lambda engine, key: _fake_lock(acquired),
    )

    with pytest.raises(asyncio.CancelledError):
        await accountability_loop(
            manager_agent_id=manager_agent_id,
            stall_seconds=3600,
            interval_seconds=0,
        )


@pytest.mark.asyncio
async def test_accountability_loop_runs_tick_under_lock(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The background loop invokes ``run_accountability_tick`` via ``to_thread``."""
    goals, peers = _stores(tmp_path)
    goal = goals.create_goal(title="loop stalled", now=100)
    goals.claim_goal(goal_id=goal.id, owner_agent_id="ag_loop", now=100)

    tick_calls: list[int] = []
    real_tick = run_accountability_tick

    def _recording_tick(*args, **kwargs):
        tick_calls.append(1)
        return real_tick(*args, **kwargs)

    monkeypatch.setattr(
        "bytedesk_omnigent.accountability.loop.run_accountability_tick",
        _recording_tick,
    )
    await _run_accountability_one_tick(monkeypatch, tmp_path)
    assert tick_calls == [1]
    assert goals.list_goals(status="open")


@pytest.mark.asyncio
async def test_accountability_loop_logs_nonzero_report(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tick that rebalances or escalates emits an info log line."""
    import bytedesk_omnigent.accountability.loop as loop_mod

    goals, peers = _stores(tmp_path)
    goal = goals.create_goal(title="blocked loop", now=100)
    goals.advance_goal(goal_id=goal.id, status="blocked", now=100)
    logged: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _capture_info(msg: str, *args: Any, **kwargs: Any) -> None:
        logged.append(((msg, *args), kwargs))

    monkeypatch.setattr(loop_mod._logger, "info", _capture_info)
    await _run_accountability_one_tick(monkeypatch, tmp_path, manager_agent_id="ag_mgr")

    assert logged
    msg, rebalanced, escalated = logged[0][0]
    assert msg == "accountability: rebalanced=%d escalated=%d"
    assert escalated == 1
