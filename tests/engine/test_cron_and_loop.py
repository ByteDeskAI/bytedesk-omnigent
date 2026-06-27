"""Goal-aware cron dispatch + the immediate-goal engine tick (BDP-2583)."""
from __future__ import annotations

from dataclasses import dataclass

from bytedesk_omnigent.engine.cron import goal_cron_dispatch
from bytedesk_omnigent.engine.loop import run_goal_engine_tick
from bytedesk_omnigent.goals import SqlAlchemyGoalStore
from bytedesk_omnigent.lifecycle import ScheduleKind
from bytedesk_omnigent.scheduler.scheduler import CronTrigger, SqlAlchemyCronScheduler


@dataclass
class _FakeConversation:
    id: str
    external_key: str | None


class _FakeConversationStore:
    def __init__(self) -> None:
        self.by_external_key: dict[str, _FakeConversation] = {}
        self.created: list[dict] = []
        self._n = 0

    def get_conversation_by_external_key(self, external_key):
        return self.by_external_key.get(external_key)

    def create_conversation(self, **kwargs):
        self._n += 1
        conv = _FakeConversation(id=f"conv_{self._n}", external_key=kwargs.get("external_key"))
        self.created.append(kwargs)
        if conv.external_key is not None:
            self.by_external_key[conv.external_key] = conv
        return conv

    def append(self, conversation_id, items):
        pass


def _store(tmp_path) -> SqlAlchemyGoalStore:
    return SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'goals.db'}")


def _trigger(goal_id: str, *, kind: str = "goal", next_fire_at: int = 1000) -> CronTrigger:
    return CronTrigger(
        id="cron_x",
        agent_id="maya",
        key=f"goal:{goal_id}",
        schedule_kind=ScheduleKind.CRON,
        schedule_expr="0 * * * *",
        next_fire_at=next_fire_at,
        enabled=True,
        payload={"goal_id": goal_id, "agent_id": "maya", "kind": kind},
    )


def test_goal_cron_dispatch_routes_goal_payload(tmp_path) -> None:
    store = _store(tmp_path)
    convs = _FakeConversationStore()
    goal = store.create_goal(title="hourly sweep")
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya")

    fired: list = []
    dispatch = goal_cron_dispatch(
        conversation_store=convs, goal_store=store, fallback=lambda t: fired.append(t)
    )
    dispatch(_trigger(goal.id, next_fire_at=3600))

    assert len(convs.created) == 1  # spawned a session for this fire
    assert convs.created[0]["external_key"] == f"goal:{goal.id}:3600"
    assert fired == []  # goal payload did NOT fall through to the fabric outbox


def test_goal_cron_dispatch_falls_back_for_non_goal_payload(tmp_path) -> None:
    store = _store(tmp_path)
    convs = _FakeConversationStore()
    fired: list = []
    dispatch = goal_cron_dispatch(
        conversation_store=convs, goal_store=store, fallback=lambda t: fired.append(t)
    )
    trig = _trigger("ignored", kind="heartbeat")
    dispatch(trig)

    assert convs.created == []
    assert fired == [trig]  # non-goal trigger goes to the original dispatch


def test_engine_tick_dispatches_ready_immediate_goal_once(tmp_path) -> None:
    store = _store(tmp_path)
    convs = _FakeConversationStore()
    goal = store.create_goal(title="needs work")
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya")

    first = run_goal_engine_tick(store, convs, now=100)
    second = run_goal_engine_tick(store, convs, now=200)

    assert first == 1
    assert second == 0  # idempotent: a live session already exists
    assert len(convs.created) == 1


def test_engine_tick_skips_recurring_goals(tmp_path) -> None:
    store = _store(tmp_path)
    convs = _FakeConversationStore()
    goal = store.create_goal(
        title="recurring one",
        cadence_kind="recurring",
        cadence_expr="0 * * * *",
        scheduler=SqlAlchemyCronScheduler(f"sqlite:///{tmp_path / 'goals.db'}"),
    )
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya")

    # the tick only dispatches immediate goals — recurring ones run via cron
    assert run_goal_engine_tick(store, convs, now=100) == 0
    assert convs.created == []


def test_engine_tick_skips_unowned_goals(tmp_path) -> None:
    store = _store(tmp_path)
    convs = _FakeConversationStore()
    store.create_goal(title="no owner yet")  # open + ready but unowned

    assert run_goal_engine_tick(store, convs, now=100) == 0
    assert convs.created == []
