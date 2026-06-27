"""Autonomy posture wired into the portfolio tick (BDP-2589, Phase 7).

``gated`` (default): a medium-risk goal is funded+spawned UNLESS the tenant sets
``require_approval_all`` — then it routes to approval instead of spawning.
``full_auto``: a medium-risk goal funds+spawns without per-action approval.
High-risk is ALWAYS gated regardless of posture (blast-radius never auto-arms).
"""
from __future__ import annotations

from dataclasses import dataclass

from bytedesk_omnigent.engine.config import GoalEngineConfig
from bytedesk_omnigent.engine.loop import run_goal_engine_tick
from bytedesk_omnigent.engine.optimizer import RoiOptimizer
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
    return store, treasury, _FakeConversationStore()


def _ready(store, **kw):
    goal = store.create_goal(**kw)
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya")
    return goal


def _gated(**over):
    return GoalEngineConfig(autonomy_posture="gated", **over)


def _full_auto(**over):
    return GoalEngineConfig(autonomy_posture="full_auto", **over)


def test_gated_medium_risk_spawns_by_default(tmp_path) -> None:
    store, treasury, convs = _setup(tmp_path)
    g = _ready(store, title="m", risk_tier="medium", expected_value_cents=1000)
    spawned = run_goal_engine_tick(
        store, convs, now=100, treasury=treasury, optimizer=RoiOptimizer(), config=_gated()
    )
    assert spawned == 1
    assert convs.created[0]["external_key"] == f"goal:{g.id}"


def test_gated_with_require_approval_all_routes_to_approval(tmp_path) -> None:
    store, treasury, convs = _setup(tmp_path)
    g = _ready(store, title="m", risk_tier="medium", expected_value_cents=1000)
    spawned = run_goal_engine_tick(
        store, convs, now=100, treasury=treasury, optimizer=RoiOptimizer(),
        config=_gated(require_approval_all=True),
    )
    assert spawned == 0
    assert convs.created == []
    reasons = [d.reason for d in treasury.decisions(goal_id=g.id)]
    assert "approval_required" in reasons


def test_full_auto_medium_risk_spawns(tmp_path) -> None:
    store, treasury, convs = _setup(tmp_path)
    # full_auto must spawn even with require_approval_all set by a careless admin —
    # full_auto is the explicit arm; only blast-radius (high) stays gated.
    g = _ready(store, title="m", risk_tier="medium", expected_value_cents=1000)
    spawned = run_goal_engine_tick(
        store, convs, now=100, treasury=treasury, optimizer=RoiOptimizer(),
        config=_full_auto(require_approval_all=True),
    )
    assert spawned == 1
    assert convs.created[0]["external_key"] == f"goal:{g.id}"


def test_high_risk_always_gated_even_in_full_auto(tmp_path) -> None:
    store, treasury, convs = _setup(tmp_path)
    g = _ready(store, title="risky", risk_tier="high", expected_value_cents=5000)
    spawned = run_goal_engine_tick(
        store, convs, now=100, treasury=treasury, optimizer=RoiOptimizer(), config=_full_auto()
    )
    assert spawned == 0
    assert convs.created == []
    reasons = [d.reason for d in treasury.decisions(goal_id=g.id)]
    assert "approval_required" in reasons


def test_no_config_is_legacy_gated_behaviour(tmp_path) -> None:
    # config=None preserves today's behaviour (gated, high-risk gated, others fund).
    store, treasury, convs = _setup(tmp_path)
    _ready(store, title="m", risk_tier="medium", expected_value_cents=1000)
    spawned = run_goal_engine_tick(
        store, convs, now=100, treasury=treasury, optimizer=RoiOptimizer()
    )
    assert spawned == 1


def test_per_tenant_posture_map(tmp_path) -> None:
    # The tick honours a per-tenant config map: acme armed, default gated+approval.
    store, treasury, convs = _setup(tmp_path)
    acme = _ready(
        store, title="acme-m", target_kind="organization", target_id="acme",
        risk_tier="medium", expected_value_cents=1000,
    )
    other = _ready(
        store, title="other-m", target_kind="organization", target_id="omnigent",
        risk_tier="medium", expected_value_cents=1000,
    )
    configs = {"acme": _full_auto()}
    spawned = run_goal_engine_tick(
        store, convs, now=100, treasury=treasury, optimizer=RoiOptimizer(),
        config=_gated(require_approval_all=True), configs=configs,
    )
    assert spawned == 1
    assert convs.created[0]["external_key"] == f"goal:{acme.id}"
    assert "approval_required" in [d.reason for d in treasury.decisions(goal_id=other.id)]
