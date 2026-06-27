"""Close-the-loop wiring (BDP-2594, Phase 2 Wave 1).

The engine actually *completes* goals (success_condition → done), *assigns*
owners (assignment seam), *accounts* for cost (per-model pricing + settle), and
*acts* (actuator seam) — the pieces Phase 1 stored but never consumed.

Fakes only, no network/LLM.
"""
from __future__ import annotations

from dataclasses import dataclass

from bytedesk_omnigent.engine.conditions import All, Leaf, Predicate
from bytedesk_omnigent.engine.loop import run_goal_engine_tick
from bytedesk_omnigent.engine.optimizer import RoiOptimizer
from bytedesk_omnigent.engine.providers.contract import ActuatorRegistry, ActuatorResult
from bytedesk_omnigent.engine.resolver import evaluate_success_condition
from bytedesk_omnigent.engine.sensors import build_default_registry
from bytedesk_omnigent.engine.treasury import SqlAlchemyTreasury
from bytedesk_omnigent.goals import SqlAlchemyGoalStore


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


def _setup(tmp_path):
    loc = f"sqlite:///{tmp_path / 'goals.db'}"
    store = SqlAlchemyGoalStore(loc)
    treasury = SqlAlchemyTreasury(loc)
    convs = _FakeConversationStore()
    return store, treasury, convs


def _ready(store, **kw):
    goal = store.create_goal(**kw)
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya")
    return goal


def _done_upstream(store):
    """A goal that is already `done`, usable as a success_condition target."""
    dep = store.create_goal(title="upstream")
    store.claim_goal(goal_id=dep.id, owner_agent_id="m")
    store.advance_goal(goal_id=dep.id, status="done")
    return dep


# -- (1) success_condition auto-completion -----------------------------------
def test_success_condition_marks_goal_done(tmp_path) -> None:
    store, _treasury, convs = _setup(tmp_path)
    dep = store.create_goal(title="upstream")  # still open
    sc = All([Leaf("goal_outcome", {"goal_id": dep.id}, Predicate("equals", "done"))]).to_dict()
    goal = _ready(store, title="g", success_condition=sc)
    store.advance_goal(goal_id=goal.id, status="in_progress")

    # Not satisfied yet: tick must NOT complete it.
    run_goal_engine_tick(
        store, convs, now=100, sensor_registry=build_default_registry()
    )
    assert str(store.get_goal(goal_id=goal.id).status) == "in_progress"

    # Satisfy the condition, re-tick → goal goes done.
    store.claim_goal(goal_id=dep.id, owner_agent_id="m")
    store.advance_goal(goal_id=dep.id, status="done")
    run_goal_engine_tick(
        store, convs, now=200, sensor_registry=build_default_registry()
    )
    assert str(store.get_goal(goal_id=goal.id).status) == "done"


def test_success_condition_completion_is_idempotent(tmp_path) -> None:
    store, _treasury, convs = _setup(tmp_path)
    dep = _done_upstream(store)
    sc = All([Leaf("goal_outcome", {"goal_id": dep.id}, Predicate("equals", "done"))]).to_dict()
    goal = _ready(store, title="g", success_condition=sc)
    store.advance_goal(goal_id=goal.id, status="in_progress")
    # Two ticks: the second must not raise IllegalTransition (done -> done).
    run_goal_engine_tick(store, convs, now=100, sensor_registry=build_default_registry())
    run_goal_engine_tick(store, convs, now=200, sensor_registry=build_default_registry())
    assert str(store.get_goal(goal_id=goal.id).status) == "done"


def test_success_condition_does_not_fabricate_value(tmp_path) -> None:
    store, _treasury, convs = _setup(tmp_path)
    dep = _done_upstream(store)
    sc = All([Leaf("goal_outcome", {"goal_id": dep.id}, Predicate("equals", "done"))]).to_dict()
    goal = _ready(store, title="g", success_condition=sc, expected_value_cents=5000)
    store.advance_goal(goal_id=goal.id, status="in_progress")
    run_goal_engine_tick(store, convs, now=100, sensor_registry=build_default_registry())
    assert store.get_goal(goal_id=goal.id).realized_value_cents == 0


def test_legacy_goal_without_success_condition_unchanged(tmp_path) -> None:
    store, _treasury, convs = _setup(tmp_path)
    goal = _ready(store, title="g")  # assigned, no success_condition
    run_goal_engine_tick(store, convs, now=100, sensor_registry=build_default_registry())
    # It dispatched (spawned a session) and is NOT auto-completed.
    assert str(store.get_goal(goal_id=goal.id).status) == "assigned"
    assert len(convs.created) == 1


def test_evaluate_success_condition_helper(tmp_path) -> None:
    store = SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'g.db'}")
    dep = _done_upstream(store)
    sc = All([Leaf("goal_outcome", {"goal_id": dep.id}, Predicate("equals", "done"))]).to_dict()
    goal = store.create_goal(title="g", success_condition=sc)
    assert evaluate_success_condition(
        goal, registry=build_default_registry(), goal_store=store, now=0
    ) is True
    # A goal with no success_condition is never auto-complete (returns False).
    plain = store.create_goal(title="plain")
    assert evaluate_success_condition(
        plain, registry=build_default_registry(), goal_store=store, now=0
    ) is False


# -- (2) assignment seam consumed --------------------------------------------
def _roster(*agents):
    from bytedesk_omnigent.assignment import CandidateAgent

    return lambda: [CandidateAgent(agent_id=a) for a in agents]


def test_unowned_goal_is_assigned_and_dispatched(tmp_path) -> None:
    store, _treasury, convs = _setup(tmp_path)
    goal = store.create_goal(title="g")  # open, unowned
    spawned = run_goal_engine_tick(
        store, convs, now=100,
        sensor_registry=build_default_registry(),
        roster_provider=_roster("alice"),
    )
    assert spawned == 1
    refreshed = store.get_goal(goal_id=goal.id)
    assert refreshed.owner_agent_id == "alice"
    assert str(refreshed.status) == "assigned"


def test_unowned_goal_with_no_candidate_waits_without_crash(tmp_path) -> None:
    store, _treasury, convs = _setup(tmp_path)
    goal = store.create_goal(title="g")  # open, unowned
    spawned = run_goal_engine_tick(
        store, convs, now=100,
        sensor_registry=build_default_registry(),
        roster_provider=lambda: [],  # empty roster
    )
    assert spawned == 0
    assert store.get_goal(goal_id=goal.id).owner_agent_id is None


def test_no_roster_provider_leaves_unowned_goals_alone(tmp_path) -> None:
    store, _treasury, convs = _setup(tmp_path)
    store.create_goal(title="g")  # open, unowned
    # Behaviour-preserving: without a roster_provider, unowned goals are ignored.
    assert run_goal_engine_tick(
        store, convs, now=100, sensor_registry=build_default_registry()
    ) == 0


# -- (3) config knobs wired --------------------------------------------------
def test_default_cap_cents_gates_uncapped_scope(tmp_path) -> None:
    from bytedesk_omnigent.engine.config import GoalEngineConfig

    store, treasury, convs = _setup(tmp_path)
    # No explicit budget for org:omnigent. A default cap of 50 < est_cost 100 denies.
    _ready(store, title="g", expected_value_cents=1000)
    cfg = GoalEngineConfig(budget_default_cap_cents=50)
    spawned = run_goal_engine_tick(
        store, convs, now=100, treasury=treasury, optimizer=RoiOptimizer(),
        est_cost=100, config=cfg,
    )
    assert spawned == 0  # the auto-seeded default cap denied funding


def test_default_cap_cents_zero_is_uncapped(tmp_path) -> None:
    from bytedesk_omnigent.engine.config import GoalEngineConfig

    store, treasury, convs = _setup(tmp_path)
    _ready(store, title="g", expected_value_cents=1000)
    cfg = GoalEngineConfig(budget_default_cap_cents=0)
    spawned = run_goal_engine_tick(
        store, convs, now=100, treasury=treasury, optimizer=RoiOptimizer(),
        est_cost=100, config=cfg,
    )
    assert spawned == 1  # 0 = uncapped, behaviour unchanged


def test_anomaly_threshold_from_config_trips_circuit(tmp_path) -> None:
    from bytedesk_omnigent.engine.config import GoalEngineConfig

    store, treasury, convs = _setup(tmp_path)
    treasury.set_budget(tier="org", target_id="omnigent", cap_cents=10_000)
    _ready(store, title="g", expected_value_cents=1000, confidence=0.9)
    # threshold below est_cost → after the reserve charge, spend >= threshold with
    # zero realized value, so the circuit auto-trips.
    cfg = GoalEngineConfig(anomaly_threshold_cents=50)
    run_goal_engine_tick(
        store, convs, now=100, treasury=treasury, optimizer=RoiOptimizer(),
        est_cost=100, config=cfg,
    )
    assert treasury.circuit_open("org:omnigent") is True


# -- (4) cost model + settle -------------------------------------------------
def test_cost_model_prices_by_model() -> None:
    from bytedesk_omnigent.engine.cost import estimate_goal_cost_cents

    cheap = estimate_goal_cost_cents(model="gpt-5-mini")
    pricey = estimate_goal_cost_cents(model="gpt-5.5")
    assert cheap > 0 and pricey > 0
    assert pricey > cheap  # the premium model is priced higher
    # Unknown model falls back to the default price (does not crash).
    assert estimate_goal_cost_cents(model="who-knows") > 0


# -- (5) actuator seam (deterministic path) ----------------------------------
class _RecordingActuator:
    name = "echo"
    risk_tier = 0

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def execute(self, action):
        self.calls.append(action)
        return ActuatorResult(ok=True, output={"echoed": action})


def test_actuator_goal_executes_instead_of_spawning(tmp_path) -> None:
    store, treasury, convs = _setup(tmp_path)
    actuator = _RecordingActuator()
    registry = ActuatorRegistry(default=("echo", lambda: actuator))
    goal = _ready(
        store, title="act",
        payload={"actuator": {"name": "echo", "input": {"k": "v"}}},
    )
    spawned = run_goal_engine_tick(
        store, convs, now=100,
        sensor_registry=build_default_registry(),
        actuator_registry=registry,
    )
    # An actuator goal does NOT spawn an agent session.
    assert spawned == 0
    assert convs.created == []
    assert actuator.calls == [{"k": "v"}]
    # Deterministic success advances the goal.
    assert str(store.get_goal(goal_id=goal.id).status) == "done"


def test_actuator_goal_failure_does_not_advance(tmp_path) -> None:
    store, treasury, convs = _setup(tmp_path)

    class _FailingActuator:
        name = "boom"
        risk_tier = 0

        async def execute(self, action):
            return ActuatorResult(ok=False, detail="nope")

    registry = ActuatorRegistry(default=("boom", lambda: _FailingActuator()))
    goal = _ready(
        store, title="act", payload={"actuator": {"name": "boom", "input": {}}},
    )
    run_goal_engine_tick(
        store, convs, now=100,
        sensor_registry=build_default_registry(),
        actuator_registry=registry,
    )
    assert str(store.get_goal(goal_id=goal.id).status) != "done"


# -- (6) END-TO-END PROOF ----------------------------------------------------
def test_end_to_end_goal_lifecycle(tmp_path) -> None:
    """Seed → dispatch → success_condition satisfied → done → decision + flywheel."""
    store, treasury, convs = _setup(tmp_path)
    treasury.set_budget(tier="org", target_id="omnigent", cap_cents=1000)

    # A success_condition that is satisfied once `dep` reaches done.
    dep = store.create_goal(title="dep")  # open
    sc = All([Leaf("goal_outcome", {"goal_id": dep.id}, Predicate("equals", "done"))]).to_dict()
    goal = _ready(
        store, title="ship it", success_condition=sc,
        expected_value_cents=10_000, confidence=0.9,
    )

    sensors = build_default_registry()
    # Tick 1: condition not met → the goal is dispatched (a session spawns).
    spawned = run_goal_engine_tick(
        store, convs, now=100, sensor_registry=sensors,
        treasury=treasury, optimizer=RoiOptimizer(), est_cost=100,
    )
    assert spawned == 1
    assert convs.created[0]["external_key"] == f"goal:{goal.id}"
    assert str(store.get_goal(goal_id=goal.id).status) == "assigned"
    funded = treasury.decisions(goal_id=goal.id)
    assert any(d.reason == "funded" for d in funded)

    spent_after_dispatch = treasury.spent_cents(tier="org", target_id="omnigent")
    assert spent_after_dispatch == 100  # reserved est_cost

    # The work happens (simulated): satisfy the success condition.
    store.claim_goal(goal_id=dep.id, owner_agent_id="m")
    store.advance_goal(goal_id=dep.id, status="done")

    # Tick 2: condition now met → goal goes done.
    run_goal_engine_tick(
        store, convs, now=200, sensor_registry=sensors,
        treasury=treasury, optimizer=RoiOptimizer(), est_cost=100,
    )
    assert str(store.get_goal(goal_id=goal.id).status) == "done"

    # The rail books realized value → the flywheel refills the budget.
    before = treasury.spent_cents(tier="org", target_id="omnigent")
    treasury.book_outcome(
        goal_store=store, goal_id=goal.id, realized_value_cents=300,
        source="rail", evidence=None,
    )
    after = treasury.spent_cents(tier="org", target_id="omnigent")
    assert after < before  # replenish reduced spend → more remaining budget
