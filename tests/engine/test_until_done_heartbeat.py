"""until_done heartbeat (BDP-2596 Wave 3, feature 4).

An until_done goal re-spawns each cadence fire (goal cron dispatch) until its
success_condition trips, then completes and stops re-spawning. Fakes only.
"""
from __future__ import annotations

from dataclasses import dataclass

from bytedesk_omnigent.engine.conditions import All, Leaf, Predicate
from bytedesk_omnigent.engine.cron import goal_cron_dispatch
from bytedesk_omnigent.engine.sensors import build_default_registry
from bytedesk_omnigent.goals import SqlAlchemyGoalStore
from bytedesk_omnigent.scheduler.scheduler import CronTrigger


@dataclass
class _FakeConversation:
    id: str
    external_key: str | None


class _Convs:
    def __init__(self):
        self.by_key = {}
        self.created = []
        self._n = 0

    def get_conversation_by_external_key(self, k):
        return self.by_key.get(k)

    def create_conversation(self, **kw):
        self._n += 1
        c = _FakeConversation(id=f"c{self._n}", external_key=kw.get("external_key"))
        self.created.append(kw)
        if c.external_key:
            self.by_key[c.external_key] = c
        return c

    def append(self, *a):
        pass


class _FakeScheduler:
    def register_trigger(self, **kw):
        pass


def _trigger(goal_id, at):
    return CronTrigger(
        id=f"trig_{at}", agent_id="maya", key=f"goal:{goal_id}",
        schedule_kind="cron", schedule_expr="* * * * *",
        next_fire_at=at, enabled=True,
        payload={"goal_id": goal_id, "kind": "goal"},
    )


def test_until_done_respawns_each_fire_until_condition_trips(tmp_path) -> None:
    store = SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'g.db'}")
    dep = store.create_goal(title="dep")  # open → condition NOT satisfied
    sc = All([Leaf("goal_outcome", {"goal_id": dep.id}, Predicate("equals", "done"))]).to_dict()
    goal = store.create_goal(
        title="patrol", cadence_kind="until_done", cadence_expr="* * * * *",
        success_condition=sc, scheduler=_FakeScheduler(),
    )
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya")

    convs = _Convs()
    dispatch = goal_cron_dispatch(
        conversation_store=convs, goal_store=store, fallback=lambda t: None,
        sensor_registry=build_default_registry(),
    )

    # Fire 1: condition not met → re-spawns a session for this occurrence.
    dispatch(_trigger(goal.id, at=100))
    assert len(convs.created) == 1
    assert str(store.get_goal(goal_id=goal.id).status) != "done"

    # Fire 2 (new occurrence): still not met → another session.
    dispatch(_trigger(goal.id, at=200))
    assert len(convs.created) == 2

    # The condition trips.
    store.claim_goal(goal_id=dep.id, owner_agent_id="m")
    store.advance_goal(goal_id=dep.id, status="done")

    # Fire 3: condition met → goal completes, NO new session this fire.
    dispatch(_trigger(goal.id, at=300))
    assert str(store.get_goal(goal_id=goal.id).status) == "done"
    assert len(convs.created) == 2  # no spawn on the completing fire

    # Fire 4: a done goal never re-spawns.
    dispatch(_trigger(goal.id, at=400))
    assert len(convs.created) == 2


def test_done_goal_cron_fire_is_a_noop(tmp_path) -> None:
    store = SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'g.db'}")
    goal = store.create_goal(
        title="x", cadence_kind="until_done", cadence_expr="* * * * *",
        scheduler=_FakeScheduler(),
    )
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya")
    store.advance_goal(goal_id=goal.id, status="done")
    convs = _Convs()
    dispatch = goal_cron_dispatch(
        conversation_store=convs, goal_store=store, fallback=lambda t: None,
    )
    dispatch(_trigger(goal.id, at=100))
    assert convs.created == []  # a done goal does not re-spawn


def test_recurring_goal_without_success_condition_still_respawns(tmp_path) -> None:
    # Behaviour-preserving: a recurring goal (no success_condition) re-spawns
    # every fire exactly as before — the heartbeat only short-circuits when a
    # success_condition is both present AND satisfied.
    store = SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'g.db'}")
    goal = store.create_goal(
        title="daily", cadence_kind="recurring", cadence_expr="0 9 * * *",
        scheduler=_FakeScheduler(),
    )
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya")
    convs = _Convs()
    dispatch = goal_cron_dispatch(
        conversation_store=convs, goal_store=store, fallback=lambda t: None,
        sensor_registry=build_default_registry(),
    )
    dispatch(_trigger(goal.id, at=100))
    dispatch(_trigger(goal.id, at=200))
    assert len(convs.created) == 2
