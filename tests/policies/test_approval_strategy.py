"""Tests for the ``ApprovalStrategy`` seam (BDP-2341, ADR-0008/0142).

The contract under test: :class:`DefaultApprovalStrategy` reproduces the hardcoded
core ASK behavior (:func:`omnigent.runtime.policies.approval._await_elicitation`)
**verbatim** — same registered/emitted wire shape, same accept→writes-applied,
same decline / cancel / timeout / malformed→no-side-effects (POLICIES.md §7.2),
same strict ``action == "accept"`` verdict, same 1024-char preview truncation. The
parity tests drive the strategy through :func:`drive_elicitation` (the
strategy-aware counterpart of ``_await_elicitation``) and assert byte-for-byte
identical outcomes against the same recorder + engine the core helper produces.

Also covers the registry round-trip (set/get + default fallback).
"""
from __future__ import annotations

from typing import Any

import pytest

from bytedesk_omnigent.approval_strategy import (
    ApprovalStrategy,
    DefaultApprovalStrategy,
    drive_elicitation,
    get_approval_strategy,
    set_approval_strategy,
)
from omnigent.policies.types import ElicitationRequest, PolicyResult
from omnigent.runtime.policies.approval import _await_elicitation
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.types import Phase, PolicyAction, StateUpdate, StateUpdateAction
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

# ── Fixtures / helpers ────────────────────────────────


@pytest.fixture()
def conversation_store(db_uri: str) -> SqlAlchemyConversationStore:
    """Conversation store backed by the per-test SQLite DB."""
    return SqlAlchemyConversationStore(db_uri)


def _engine(
    store: SqlAlchemyConversationStore,
    *,
    ask_timeout: int = 30,
) -> PolicyEngine:
    """Build a real engine (no policies needed — these tests drive the
    elicitation directly with a fabricated composed ASK result)."""
    conv = store.create_conversation()
    return PolicyEngine(
        policies=[],
        label_defs={},
        ask_timeout=ask_timeout,
        conversation_id=conv.id,
        initial_labels={},
        conversation_store=store,
    )


def _composed_ask(
    *,
    deciding_policy: str = "gate",
    reason: str = "please approve",
    set_labels: dict[str, str] | None = None,
    state_updates: list[StateUpdate] | None = None,
) -> PolicyResult:
    """Fabricate an engine-composed ASK result."""
    return PolicyResult(
        action=PolicyAction.ASK,
        reason=reason,
        set_labels=set_labels,
        deciding_policy=deciding_policy,
        state_updates=state_updates,
    )


class _Recorder:
    """Records the register / emit seam invocations (mirrors the core
    approval-test recorder)."""

    def __init__(self) -> None:
        self.registered: list[tuple[str, str, str]] = []
        self.emitted: list[dict[str, Any]] = []

    def register(self, elicitation_id: str, task_id: str, params_json: str) -> None:
        self.registered.append((elicitation_id, task_id, params_json))

    def emit(self, event: dict[str, Any]) -> None:
        self.emitted.append(event)


def _park_returning(verdict: str | None) -> Any:
    """Park callback that instantly returns ``verdict``."""

    async def _park(elicitation_id: str, timeout_s: int) -> str | None:
        return verdict

    return _park


def _timing_out_park() -> Any:
    """Park callback that always raises TimeoutError."""

    async def _park(elicitation_id: str, timeout_s: int) -> str | None:
        raise TimeoutError(f"no verdict within {timeout_s}s")

    return _park


# ── registry ──────────────────────────────────────────


def test_registry_round_trip_and_default_fallback() -> None:
    """set/get round-trips a live strategy; clearing falls back to a
    fresh :class:`DefaultApprovalStrategy` (the seam never strands the
    default)."""
    sentinel = DefaultApprovalStrategy()
    try:
        # No registration → default fallback (a DefaultApprovalStrategy
        # instance, satisfying the protocol).
        fallback = get_approval_strategy()
        assert isinstance(fallback, DefaultApprovalStrategy)
        assert isinstance(fallback, ApprovalStrategy)

        set_approval_strategy(sentinel)
        assert get_approval_strategy() is sentinel
    finally:
        set_approval_strategy(None)
    assert isinstance(get_approval_strategy(), DefaultApprovalStrategy)


def test_default_strategy_satisfies_protocol() -> None:
    """:class:`DefaultApprovalStrategy` is a structural
    :class:`ApprovalStrategy`."""
    assert isinstance(DefaultApprovalStrategy(), ApprovalStrategy)


# ── compose_ask parity ────────────────────────────────


def test_compose_ask_builds_truncates_registers_and_emits(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """``compose_ask`` builds the :class:`ElicitationRequest` with the
    reason / phase / deciding-policy / 1024-truncated preview, registers
    the row, and emits the event — verbatim core behavior."""
    rec = _Recorder()
    result = _composed_ask(deciding_policy="shell_gate", reason="approve shell?")
    long_preview = "x" * 2000

    elicitation = DefaultApprovalStrategy().compose_ask(
        elicitation_id="elicit_fixed",
        task_id="task_1",
        result=result,
        phase=Phase.TOOL_CALL,
        content_preview=long_preview,
        register=rec.register,
        emit=rec.emit,
    )

    assert isinstance(elicitation, ElicitationRequest)
    assert elicitation.message == "approve shell?"
    assert elicitation.phase == "tool_call"
    assert elicitation.policy_name == "shell_gate"
    # 1024-char truncation with the core marker.
    assert elicitation.content_preview == "x" * 1024 + " [truncated]"

    assert len(rec.registered) == 1
    eid, task_id, _params = rec.registered[0]
    assert (eid, task_id) == ("elicit_fixed", "task_1")

    assert len(rec.emitted) == 1
    event = rec.emitted[0]
    assert event["type"] == "response.elicitation_request"
    assert event["method"] == "elicitation/create"
    assert event["elicitation_id"] == "elicit_fixed"
    assert event["params"]["message"] == "approve shell?"
    assert event["params"]["phase"] == "tool_call"
    assert event["params"]["policy_name"] == "shell_gate"


def test_compose_ask_matches_core_helper_wire_shape(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """The strategy's registered params + emitted event are byte-for-byte
    identical to what the core ``_await_elicitation`` produces for the same
    inputs (proving the default is a pure refactor of the wire shape)."""
    result = _composed_ask(deciding_policy="gate", reason="needs review")

    # Strategy path.
    strat_rec = _Recorder()
    DefaultApprovalStrategy().compose_ask(
        elicitation_id="elicit_same",
        task_id="task_1",
        result=result,
        phase=Phase.REQUEST,
        content_preview="ls -la",
        register=strat_rec.register,
        emit=strat_rec.emit,
    )

    # Core path — same id (we mint it; core mints its own, so compare the
    # serialized params + event params block, which are id-independent).
    from omnigent.runtime.policies.approval import (
        build_elicitation_params_json,
        build_elicitation_request_event,
    )

    core_elicitation = ElicitationRequest(
        message="needs review",
        phase=Phase.REQUEST.value,
        policy_name="gate",
        content_preview="ls -la",
    )
    core_params_json = build_elicitation_params_json(core_elicitation)
    core_event = build_elicitation_request_event("elicit_same", core_elicitation)

    assert strat_rec.registered[0][2] == core_params_json
    assert strat_rec.emitted[0] == core_event


# ── apply_verdict parity ──────────────────────────────


@pytest.mark.parametrize(
    ("raw", "approved"),
    [
        ('{"action": "accept"}', True),
        ('{"action": "decline"}', False),
        ('{"action": "cancel"}', False),
        ('{"action": "ACCEPT"}', False),  # strict — case matters
        ('{"approved": true}', False),  # legacy shape rejected
        ("not json", False),
        ("", False),
        (None, False),
    ],
)
def test_apply_verdict_strict_matches_core(
    conversation_store: SqlAlchemyConversationStore,
    raw: str | None,
    approved: bool,
) -> None:
    """``apply_verdict`` returns True only on exact ``action == "accept"``
    — identical to the core ``_parse_verdict`` fail-closed contract."""
    engine = _engine(conversation_store)
    result = _composed_ask()
    got = DefaultApprovalStrategy().apply_verdict(
        raw_verdict=raw,
        result=result,
        policy_engine=engine,
    )
    assert got is approved


def test_apply_verdict_applies_labels_and_state_on_accept(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """On accept, the ASK-accumulated ``set_labels`` + ``state_updates``
    land in the engine (and persist) — same as the core helper."""
    engine = _engine(conversation_store)
    result = _composed_ask(
        set_labels={"integrity": "0"},
        state_updates=[
            StateUpdate(key="approved_once", action=StateUpdateAction.SET, value=True)
        ],
    )

    accepted = DefaultApprovalStrategy().apply_verdict(
        raw_verdict='{"action": "accept"}',
        result=result,
        policy_engine=engine,
    )

    assert accepted is True
    assert engine.labels == {"integrity": "0"}
    assert engine.session_state.get("approved_once") is True
    conv = conversation_store.get_conversation(engine.conversation_id)
    assert conv is not None
    assert conv.labels == {"integrity": "0"}


def test_apply_verdict_no_side_effects_on_decline(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """On decline, nothing is applied — the load-bearing §7.2 invariant
    (a denied ASK leaves no trace), identical to the core helper."""
    engine = _engine(conversation_store)
    result = _composed_ask(
        set_labels={"integrity": "0"},
        state_updates=[
            StateUpdate(key="approved_once", action=StateUpdateAction.SET, value=True)
        ],
    )

    accepted = DefaultApprovalStrategy().apply_verdict(
        raw_verdict='{"action": "decline"}',
        result=result,
        policy_engine=engine,
    )

    assert accepted is False
    assert engine.labels == {}
    assert "approved_once" not in engine.session_state
    conv = conversation_store.get_conversation(engine.conversation_id)
    assert conv is not None
    assert conv.labels == {}


# ── end-to-end parity with _await_elicitation ─────────


@pytest.mark.asyncio
async def test_drive_elicitation_accept_matches_await_elicitation(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Driving :class:`DefaultApprovalStrategy` through
    :func:`drive_elicitation` produces the same accept outcome (return
    value + applied labels) as the hardcoded ``_await_elicitation`` for the
    same inputs."""
    # Strategy-driven engine.
    s_engine = _engine(conversation_store)
    s_rec = _Recorder()
    s_result = _composed_ask(set_labels={"integrity": "0"})
    strat_accepted = await drive_elicitation(
        strategy=DefaultApprovalStrategy(),
        elicitation_id="elicit_strat",
        task_id="task_1",
        result=s_result,
        phase=Phase.REQUEST,
        content_preview="hello",
        policy_engine=s_engine,
        register=s_rec.register,
        emit=s_rec.emit,
        park=_park_returning('{"action": "accept"}'),
    )

    # Core helper engine (independent conversation).
    c_engine = _engine(conversation_store)
    c_rec = _Recorder()
    c_result = _composed_ask(set_labels={"integrity": "0"})
    core_accepted = await _await_elicitation(
        task_id="task_1",
        root_task_id="task_1",
        result=c_result,
        phase=Phase.REQUEST,
        content_preview="hello",
        policy_engine=c_engine,
        register=c_rec.register,
        emit=c_rec.emit,
        park=_park_returning('{"action": "accept"}'),
    )

    assert strat_accepted == core_accepted is True
    assert s_engine.labels == c_engine.labels == {"integrity": "0"}
    # Same number of register/emit invocations.
    assert len(s_rec.registered) == len(c_rec.registered) == 1
    assert len(s_rec.emitted) == len(c_rec.emitted) == 1


@pytest.mark.asyncio
async def test_drive_elicitation_timeout_returns_false_no_writes(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A park TimeoutError → refusal with no side effects, identical to
    ``_await_elicitation``."""
    engine = _engine(conversation_store)
    rec = _Recorder()
    result = _composed_ask(set_labels={"integrity": "0"})

    accepted = await drive_elicitation(
        strategy=DefaultApprovalStrategy(),
        elicitation_id="elicit_to",
        task_id="task_1",
        result=result,
        phase=Phase.REQUEST,
        content_preview="hello",
        policy_engine=engine,
        register=rec.register,
        emit=rec.emit,
        park=_timing_out_park(),
    )

    assert accepted is False
    assert engine.labels == {}
    # The ask was still composed (registered + emitted) before the timeout.
    assert len(rec.registered) == 1
    assert len(rec.emitted) == 1


@pytest.mark.asyncio
async def test_drive_elicitation_uses_registered_strategy(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """The default returned by :func:`get_approval_strategy` (when none is
    registered) drives a correct accept round-trip — proving the seam is
    transparently usable via the registry."""
    engine = _engine(conversation_store)
    rec = _Recorder()
    result = _composed_ask()

    accepted = await drive_elicitation(
        strategy=get_approval_strategy(),
        elicitation_id="elicit_reg",
        task_id="task_1",
        result=result,
        phase=Phase.REQUEST,
        content_preview="hi",
        policy_engine=engine,
        register=rec.register,
        emit=rec.emit,
        park=_park_returning('{"action": "accept"}'),
    )

    assert accepted is True
