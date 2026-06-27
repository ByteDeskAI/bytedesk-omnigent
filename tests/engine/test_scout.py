"""Opportunity discovery / scout (BDP-2596 Wave 3, feature 6).

A standing recurring "scout" goal's dispatched agent scans sensors and PROPOSES
new goals as DRAFTS that enter the governance gate (not auto-activated). This
covers the deterministic proposal path + the seed template; the live agent scan
is exercised only via fakes.
"""
from __future__ import annotations

from bytedesk_omnigent.engine.loop import run_goal_engine_tick
from bytedesk_omnigent.engine.scout import SCOUT_GOAL_SLUG, propose_goal, scout_seed
from bytedesk_omnigent.engine.sensors import build_default_registry
from bytedesk_omnigent.goals import SqlAlchemyGoalStore


def _store(tmp_path):
    return SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'g.db'}")


class _Convs:
    created: list = []

    def get_conversation_by_external_key(self, k):
        return None

    def create_conversation(self, **kw):
        raise AssertionError("a proposed goal must NOT dispatch")

    def append(self, *a):
        pass


def test_propose_goal_creates_a_deferred_draft(tmp_path) -> None:
    store = _store(tmp_path)
    proposed = propose_goal(
        store, title="Win back churned account ACME",
        source="scout", expected_value_cents=20_000,
        rationale="churn sensor flagged ACME idle 30d",
    )
    refreshed = store.get_goal(goal_id=proposed.id)
    # A draft is NOT actionable: deferred/paused, flagged proposed for governance.
    assert refreshed.readiness_kind == "deferred"
    assert refreshed.activation_state == "paused"
    assert refreshed.attributes["approval_state"] == "proposed"
    assert refreshed.attributes["rationale"] == "churn sensor flagged ACME idle 30d"


def test_proposed_goal_is_not_auto_dispatched(tmp_path) -> None:
    store = _store(tmp_path)
    propose_goal(store, title="opportunity", source="scout")
    # Even with a roster provider (assignment pre-pass), a deferred draft is not
    # ready, so it is never claimed nor dispatched.
    from bytedesk_omnigent.assignment import CandidateAgent

    spawned = run_goal_engine_tick(
        store, _Convs(), now=100,
        sensor_registry=build_default_registry(),
        roster_provider=lambda: [CandidateAgent(agent_id="alice")],
    )
    assert spawned == 0


def test_approving_a_proposed_goal_makes_it_actionable(tmp_path) -> None:
    store = _store(tmp_path)
    proposed = propose_goal(store, title="approve me", source="scout")
    # Governance approval = activate it (immediate + ready) and clear the flag.
    store.activate_goal(goal_id=proposed.id)
    store.mutate_payload(
        goal_id=proposed.id,
        mutator=lambda p: p.setdefault("attributes", {}).update(
            {"approval_state": "approved"}
        ),
    )
    refreshed = store.get_goal(goal_id=proposed.id)
    assert refreshed.activation_state == "ready"
    assert refreshed.attributes["approval_state"] == "approved"


def test_scout_seed_is_a_recurring_standing_goal() -> None:
    seed = scout_seed(cadence_expr="0 8 * * *")
    assert seed["cadence_kind"] == "recurring"
    assert seed["cadence_expr"] == "0 8 * * *"
    assert seed["title"]
    # Tagged so it is discoverable / idempotently seeded.
    assert seed["payload"]["slug"] == SCOUT_GOAL_SLUG


def test_scout_seed_idempotent_create(tmp_path) -> None:
    from bytedesk_omnigent.engine.scout import ensure_scout_goal

    store = _store(tmp_path)

    class _Sched:
        def register_trigger(self, **kw):
            pass

    first = ensure_scout_goal(store, scheduler=_Sched())
    second = ensure_scout_goal(store, scheduler=_Sched())
    assert first.id == second.id  # a second ensure does not create a duplicate
    standing = [
        g for g in store.list_goals()
        if (g.payload or {}).get("slug") == SCOUT_GOAL_SLUG
    ]
    assert len(standing) == 1
