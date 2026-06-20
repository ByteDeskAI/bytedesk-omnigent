"""Black-box characterization skeletons (BDP-2326, ADR-0145 §parity).

One class per subsystem under the abstraction epic. Each test pins the
subsystem's *contract* — the observable behaviour the abstraction seam
must preserve — and marks, with an explicit ``TODO(golden)`` comment,
where a captured golden baseline still plugs in (see
``tests/parity/README.md``). Every subsystem also carries an
**error-path** characterization, because the abstraction must preserve
failure modes, not just happy paths.

These run under both flag states via :file:`scripts/test_parity.sh`
(OFF = legacy path, ON = abstraction path); identical outcomes are the
parity verdict. They are import-clean and fully collectible by pytest —
the live contract assertions pass today; the golden replays
:func:`pytest.skip` until a baseline is captured.

Subsystems:

- harness dispatch        (omnigent.harness_aliases)
- store CRUD round-trip   (SqlAlchemyConversationStore / SqlAlchemyAgentStore)
- policy apply            (SqlAlchemyPolicyStore + bytedesk spawn governor)
- spawn-tree shape        (conversation root/parent graph)
- durable-task lifecycle  (cron scheduler + idempotency store)
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy.exc import IntegrityError

from bytedesk_omnigent.idempotency import SqlAlchemyIdempotencyStore
from bytedesk_omnigent.policies.spawn_governor import spawn_breadth_governor
from bytedesk_omnigent.scheduler import (
    SqlAlchemyCronScheduler,
    compute_next_fire,
)
from omnigent.harness_aliases import canonicalize_harness, is_native_harness
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore
from tests.parity._harness import (
    assert_or_capture_golden,
    golden_capture_enabled,
    golden_exists,
)


def _replay_golden(name: str, observed: object) -> None:
    """Capture the golden baseline, or assert against it (BDP-2326).

    In capture mode (``OMNIGENT_PARITY_CAPTURE=1`` on the legacy path) the
    normalized contract is written to ``_golden/<name>.json`` even when no
    baseline exists yet — that first capture is the whole point of the
    capture run. Outside capture mode the golden is asserted; if it is
    somehow still absent the replay leg skips rather than hard-failing.
    """
    if golden_capture_enabled():
        assert_or_capture_golden(name, observed)
        return
    if not golden_exists(name):
        pytest.skip(
            f"TODO(golden): capture {name!r} baseline "
            "(OMNIGENT_PARITY_CAPTURE=1 on the legacy path)"
        )
    assert_or_capture_golden(name, observed)


# ── Harness dispatch ─────────────────────────────────────────────────


class TestHarnessDispatchCharacterization:
    """Contract: alias spellings canonicalize; native vs SDK is classified."""

    def test_alias_canonicalizes_to_runtime_id(self) -> None:
        # Contract: the documented user-facing spellings resolve to the
        # canonical runtime harness id the dispatcher routes on.
        assert canonicalize_harness("claude") == "claude-sdk"
        assert canonicalize_harness("openai-agents-sdk") == "openai-agents"
        assert canonicalize_harness("agy") == "antigravity"

        # TODO(golden): capture the full alias→canonical map as the
        # harness-dispatch baseline so a seam that rewrites the alias
        # table is parity-checked across every spelling, not just these.
        observed = {
            alias: canonicalize_harness(alias)
            for alias in ("claude", "openai-agents-sdk", "agy", "pi", "cursor")
        }
        _replay_golden("harness_dispatch", observed)

    def test_native_vs_sdk_classification(self) -> None:
        # Contract: only the native CLI harnesses are flagged native; SDK
        # harnesses replay the transcript and are NOT native.
        assert is_native_harness("claude-native") is True
        assert is_native_harness("native-codex") is True
        assert is_native_harness("claude-sdk") is False
        assert is_native_harness("openai-agents") is False

    def test_unknown_harness_passes_through_unchanged(self) -> None:
        # Error path: an unknown name is returned verbatim (the caller
        # keeps its own validation error) and never classified native.
        assert canonicalize_harness("bogus") == "bogus"
        assert canonicalize_harness(None) is None
        assert is_native_harness("some-unknown-harness") is False
        assert is_native_harness(None) is False


# ── Store CRUD round-trip ────────────────────────────────────────────


class TestStoreCrudCharacterization:
    """Contract: create → get round-trips the entity; missing id → None."""

    def test_conversation_create_get_round_trip(self, db_uri: str) -> None:
        store = SqlAlchemyConversationStore(db_uri)
        conv = store.create_conversation()
        assert conv.id.startswith("conv_")

        fetched = store.get_conversation(conv.id)
        assert fetched is not None
        assert fetched.id == conv.id
        # A top-level conversation roots itself.
        assert fetched.root_conversation_id == conv.id

        # TODO(golden): capture the normalized conversation dump (ids and
        # timestamps normalized by the harness) as the store-CRUD baseline.
        observed = {
            "kind": fetched.kind,
            "root_is_self": fetched.root_conversation_id == fetched.id,
            "parent_conversation_id": fetched.parent_conversation_id,
        }
        _replay_golden("store_crud", observed)

    def test_agent_create_get_round_trip(self, db_uri: str) -> None:
        store = SqlAlchemyAgentStore(db_uri)
        agent = store.create(
            agent_id="ag_parity_gpt4",
            name="gpt-4",
            bundle_location="ag_parity_gpt4/fakehash",
        )
        assert agent.id.startswith("ag_")

        fetched = store.get(agent.id)
        assert fetched is not None
        assert fetched.id == agent.id
        assert fetched.name == "gpt-4"

    def test_get_missing_returns_none(self, db_uri: str) -> None:
        # Error path: a lookup for an absent id returns None, never raises.
        conv_store = SqlAlchemyConversationStore(db_uri)
        agent_store = SqlAlchemyAgentStore(db_uri)
        assert conv_store.get_conversation("conv_nonexistent") is None
        assert agent_store.get("ag_nonexistent") is None


# ── Policy apply ─────────────────────────────────────────────────────


class TestPolicyApplyCharacterization:
    """Contract: policies persist their fields; the governor ALLOWs then DENYs."""

    def test_policy_persists_and_round_trips(self, db_uri: str) -> None:
        # A real conversation row is required: policies.session_id FKs it.
        session_id = SqlAlchemyConversationStore(db_uri).create_conversation().id
        store = SqlAlchemyPolicyStore(db_uri)

        policy = store.create(
            policy_id="pol_parity1",
            session_id=session_id,
            name="block_push",
            type="python",
            handler="github_mcp_policy.block_push",
        )
        assert policy.id == "pol_parity1"
        assert policy.type == "python"
        assert policy.enabled is True

        fetched = store.get("pol_parity1", session_id)
        assert fetched is not None
        assert fetched.handler == "github_mcp_policy.block_push"

        # TODO(golden): capture the normalized policy row as the
        # policy-apply baseline.
        observed = {
            "name": fetched.name,
            "type": fetched.type,
            "handler": fetched.handler,
            "enabled": fetched.enabled,
        }
        _replay_golden("policy_apply", observed)

    def test_spawn_governor_allows_then_denies_at_limit(self) -> None:
        # Contract: the applied policy ALLOWs under the cap (bumping the
        # per-session counter) and DENYs at the cap.
        gov = spawn_breadth_governor(max_spawns=2)

        def event(count: int) -> dict:
            return {
                "type": "tool_call",
                "data": {"name": "sys_session_create"},
                "session_state": {"_policy_spawn_count": count},
            }

        assert gov(event(0))["result"] == "ALLOW"
        assert gov(event(1))["result"] == "ALLOW"
        denied = gov(event(2))
        assert denied["result"] == "DENY"
        assert "spawn-breadth governor" in denied["reason"]

    def test_duplicate_policy_name_raises_in_session(self, db_uri: str) -> None:
        # Error path: composite uniqueness on (session_id, name) is
        # enforced at the DB layer; the abstraction must preserve it.
        session_id = SqlAlchemyConversationStore(db_uri).create_conversation().id
        store = SqlAlchemyPolicyStore(db_uri)
        store.create(
            policy_id="pol_dup_a",
            session_id=session_id,
            name="dup",
            type="python",
            handler="h.a",
        )
        with pytest.raises(IntegrityError):
            store.create(
                policy_id="pol_dup_b",
                session_id=session_id,
                name="dup",
                type="python",
                handler="h.b",
            )


# ── Spawn-tree shape ─────────────────────────────────────────────────


class TestSpawnTreeShapeCharacterization:
    """Contract: child conversations inherit the root; parent is recorded."""

    def test_child_inherits_root_and_records_parent(self, db_uri: str) -> None:
        store = SqlAlchemyConversationStore(db_uri)
        root = store.create_conversation()
        child = store.create_conversation(
            kind="sub_agent",
            parent_conversation_id=root.id,
            sub_agent_name="summarizer",
        )
        grandchild = store.create_conversation(
            kind="sub_agent",
            parent_conversation_id=child.id,
            sub_agent_name="researcher",
        )

        # Whole spawn tree shares the top-level root; parents are exact.
        assert root.root_conversation_id == root.id
        assert child.root_conversation_id == root.id
        assert child.parent_conversation_id == root.id
        assert grandchild.root_conversation_id == root.id
        assert grandchild.parent_conversation_id == child.id

        # TODO(golden): capture the normalized (id-stable) spawn-tree
        # adjacency as the baseline so a seam reorganizing conversation
        # creation can't silently reshape the tree.
        observed = {
            "root_self_rooted": root.root_conversation_id == root.id,
            "child_root_eq_root": child.root_conversation_id == root.id,
            "child_parent_eq_root": child.parent_conversation_id == root.id,
            "grandchild_root_eq_root": grandchild.root_conversation_id == root.id,
            "grandchild_parent_eq_child": (grandchild.parent_conversation_id == child.id),
        }
        _replay_golden("spawn_tree", observed)

    def test_top_level_conversation_has_no_parent(self, db_uri: str) -> None:
        # Error/edge path: a top-level conversation has a null parent and
        # roots itself — the abstraction must not invent a phantom parent.
        store = SqlAlchemyConversationStore(db_uri)
        conv = store.create_conversation()
        assert conv.parent_conversation_id is None
        assert conv.root_conversation_id == conv.id


# ── Durable-task lifecycle ───────────────────────────────────────────


class TestDurableTaskLifecycleCharacterization:
    """Contract: cron fires exactly-once per instant; idempotency at-most-once."""

    def test_cron_register_then_claim_fire_once(self, tmp_path) -> None:
        sched = SqlAlchemyCronScheduler(f"sqlite:///{tmp_path / 'cron.db'}")
        now = int(time.time())
        trig = sched.register_trigger(
            agent_id="maya",
            key="standup",
            schedule_kind="interval",
            schedule_expr="60",
            next_fire_at=now,
            payload={"message": "standup time"},
            now=now,
        )
        # Due now; next fire computed deterministically.
        assert [t.id for t in sched.due_triggers(now=now)] == [trig.id]
        nxt = compute_next_fire("interval", "60", now)
        assert nxt == now + 60

        # First claim of this instant wins; a second is an idempotent no-op.
        assert (
            sched.claim_fire(
                trigger_id=trig.id,
                expected_next_fire_at=now,
                new_next_fire_at=nxt,
                now=now,
            )
            is True
        )
        assert (
            sched.claim_fire(
                trigger_id=trig.id,
                expected_next_fire_at=now,
                new_next_fire_at=nxt,
                now=now,
            )
            is False
        )

        # TODO(golden): capture the normalized lifecycle transition
        # (due → claimed → advanced) as the durable-task baseline.
        observed = {
            "next_fire_after_claim": nxt - now,
            "no_longer_due_now": sched.due_triggers(now=now) == [],
            "due_again_next_window": [t.id for t in sched.due_triggers(now=now + 60)] == [trig.id],
        }
        _replay_golden("durable_task", observed)

    def test_idempotency_claim_is_at_most_once(self, tmp_path) -> None:
        store = SqlAlchemyIdempotencyStore(f"sqlite:///{tmp_path / 'idem.db'}")
        # First claim of (scope, key) wins; a duplicate delivery loses.
        assert store.claim(scope="event-trigger", key="msg-1") is True
        assert store.claim(scope="event-trigger", key="msg-1") is False
        assert store.is_claimed(scope="event-trigger", key="msg-1") is True

    def test_dead_lettered_key_is_not_reclaimable(self, tmp_path) -> None:
        # Error path: a claim marked dead-lettered (work failed past
        # redelivery) stays claimed — it must never re-run.
        store = SqlAlchemyIdempotencyStore(f"sqlite:///{tmp_path / 'idem.db'}")
        assert store.claim(scope="release", key="v1.2.3") is True
        store.mark_dead_lettered(scope="release", key="v1.2.3", result={"error": "timeout"})
        assert store.claim(scope="release", key="v1.2.3") is False
